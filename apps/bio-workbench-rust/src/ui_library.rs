//! Read-only library navigation; adding an input preserves the selected revision.
use super::*;

#[derive(Default)]
pub(super) struct Explorer {
    pub records: Vec<Value>,
    pub projects: Vec<Value>,
    pub selected: String,
    pub detail: Value,
    pub error: String,
    pub detail_error: String,
    query: String,
    kind: String,
    molecule: String,
    project: String,
    review_only: bool,
    loaded: bool,
    scope: String,
    loading_scope: String,
    next_offset: Option<u64>,
    filtered_count: u64,
    total_count: u64,
    tab: usize,
    source_document: bool,
    history: Vec<String>,
    added: String,
}

impl Explorer {
    pub fn restore(extra: &BTreeMap<String, Value>) -> Self {
        let value = extra.get("library_explorer").unwrap_or(&Value::Null);
        Self {
            selected: text(value, "selected").into(),
            query: text(value, "query").into(),
            kind: text(value, "kind").into(),
            molecule: text(value, "molecule").into(),
            project: text(value, "project").into(),
            review_only: value["review_only"].as_bool().unwrap_or(false),
            ..Default::default()
        }
    }

    pub fn preferences(&self) -> Value {
        json!({"selected":self.selected,"query":self.query,"kind":self.kind,
            "molecule":self.molecule,"project":self.project,"review_only":self.review_only})
    }

    fn visible(&self, record: &Value) -> bool {
        if (!self.kind.is_empty() && text(record, "kind") != self.kind)
            || (!self.molecule.is_empty() && text(record, "molecule_type") != self.molecule)
            || (self.review_only && text(record, "review_status") != "review_required")
        {
            return false;
        }
        let haystack = record.to_string().to_lowercase();
        self.query
            .to_lowercase()
            .split_whitespace()
            .all(|word| haystack.contains(word))
    }

    fn accept_detail(&mut self, reference: &str, value: Value) -> bool {
        if self.selected != reference {
            return false;
        }
        if text(&value, "ref") != reference || !value["record"].is_object() {
            self.detail_error =
                "The head returned a different or incomplete library record.".into();
            return false;
        }
        self.detail = value;
        self.detail_error.clear();
        true
    }
}

fn pinned_input(detail: &Value, chain: String) -> Result<Input, String> {
    let reference = text(detail, "ref");
    let record = &detail["record"];
    let kind = text(record, "kind");
    let expected = format!(
        "{}:{}@{}",
        kind,
        text(record, "id"),
        record["revision"].as_u64().unwrap_or(0)
    );
    if reference != expected || record["revision"].as_u64().unwrap_or(0) == 0 {
        return Err("The selected record has no verified immutable revision.".into());
    }
    if detail
        .pointer("/submission/allowed")
        .and_then(Value::as_bool)
        != Some(true)
    {
        let reason = detail
            .pointer("/submission/reason")
            .and_then(Value::as_str)
            .unwrap_or("");
        return Err(if reason.is_empty() {
            "This record is not available as a prediction input.".into()
        } else {
            reason.into()
        });
    }
    let molecule = if kind == "assembly" {
        "assembly"
    } else if kind != "construct" {
        return Err("Select a construct or assembly to add an input.".into());
    } else {
        match text(&record["identity"], "molecule_type") {
            "small_molecule" => "ligand",
            "protein" => "protein",
            "dna" => "dna",
            "rna" => "rna",
            _ => return Err(
                "This molecular identity needs a supported model adapter before it can be added."
                    .into(),
            ),
        }
    };
    Ok(Input {
        id: uid(),
        name: text(record, "name").into(),
        molecule_type: molecule.into(),
        chain_id: chain,
        source: json!({"kind":"library","ref":reference}),
        ..Default::default()
    })
}

fn kind_label(record: &Value) -> &str {
    match text(record, "kind") {
        "project" => "PROJECT",
        "assembly" => "ASSEMBLY",
        "monomer" => "MONOMER",
        _ => match text(record, "molecule_type") {
            "protein" => "PROTEIN",
            "dna" => "DNA",
            "rna" => "RNA",
            "small_molecule" => "LIGAND",
            "mixed_polymer" => "MIXED",
            _ => "CONSTRUCT",
        },
    }
}

fn review_label(record: &Value) -> (&str, Color32) {
    match text(record, "review_status") {
        "review_required" => ("REVIEW REQUIRED", AMBER),
        "reference_matched" => ("REFERENCE MATCHED", GREEN),
        other if !other.is_empty() => (other, Color32::LIGHT_GRAY),
        _ => (text(record, "status"), Color32::LIGHT_GRAY),
    }
}

impl Workbench {
    pub(super) fn open_library(&mut self) {
        self.sidebar_tab = 2;
        if !self.library.loaded {
            self.library_refresh();
        }
        if !self.library.selected.is_empty() && self.library.detail.is_null() {
            self.library_select(&self.library.selected.clone());
        }
    }

    pub(super) fn library_refresh(&mut self) {
        if self.busy(&Purpose::Library)
            || self
                .pending
                .values()
                .any(|pending| matches!(pending.purpose, Purpose::LibraryPage(_)))
        {
            return;
        }
        self.library.error.clear();
        self.library.loading_scope = self.library.project.clone();
        let mut params = json!({"limit":500});
        if !self.library.project.is_empty() {
            params["project_ref"] = json!(self.library.project);
        }
        if self
            .request("library.list", params, Purpose::Library)
            .is_none()
        {
            self.library.error =
                "Library connection unavailable. Check the connection and refresh.".into();
        }
    }

    pub(super) fn library_load_page(&mut self, offset: u64) {
        if self.busy(&Purpose::Library) || self.library.scope != self.library.project {
            return;
        }
        self.library.loading_scope = self.library.project.clone();
        let mut params = json!({"limit":500,"offset":offset});
        if !self.library.project.is_empty() {
            params["project_ref"] = json!(self.library.project);
        }
        self.request("library.list", params, Purpose::LibraryPage(offset));
    }

    pub(super) fn library_received_list(&mut self, value: Value, append: bool) {
        if self.library.loading_scope != self.library.project {
            self.library_refresh();
            return;
        }
        if !value["records"].is_array() {
            self.library.error = "The head returned an incomplete library listing.".into();
            return;
        }
        if !append {
            self.library.records.clear();
        }
        for record in rows(&value, "records") {
            let reference = text(record, "ref");
            if !reference.is_empty()
                && !self
                    .library
                    .records
                    .iter()
                    .any(|r| text(r, "ref") == reference)
            {
                self.library.records.push(record.clone());
            }
        }
        self.library.projects = rows(&value, "projects").to_vec();
        self.library.next_offset = value["next_offset"].as_u64();
        self.library.filtered_count = value["filtered_count"]
            .as_u64()
            .unwrap_or(self.library.records.len() as u64);
        self.library.total_count = value["total_count"]
            .as_u64()
            .unwrap_or(self.library.filtered_count);
        self.library.scope = self.library.loading_scope.clone();
        self.library.loaded = true;
        self.library.error.clear();
        if self.library.selected.is_empty() {
            let reference = self
                .library
                .projects
                .first()
                .or_else(|| self.library.records.first())
                .map(|record| text(record, "ref").to_owned());
            if let Some(reference) = reference {
                self.library_select(&reference);
            }
        }
    }

    pub(super) fn library_select(&mut self, reference: &str) {
        if reference.is_empty() {
            return;
        }
        if self.library.selected != reference {
            if !self.library.selected.is_empty() {
                self.library.history.push(self.library.selected.clone());
                if self.library.history.len() > 100 {
                    self.library.history.remove(0);
                }
            }
            self.library.selected = reference.into();
            self.library.detail = Value::Null;
            self.library.detail_error.clear();
            self.library.added.clear();
        }
        if self
            .request(
                "library.get",
                json!({"ref":reference}),
                Purpose::LibraryRecord(reference.into()),
            )
            .is_none()
            && !self.busy(&Purpose::LibraryRecord(reference.into()))
        {
            self.library.detail_error =
                "Record unavailable. Check the connection and retry.".into();
        }
    }

    pub(super) fn library_received_record(&mut self, reference: &str, value: Value) {
        self.library.accept_detail(reference, value);
    }

    pub(super) fn library_failed(&mut self, purpose: &Purpose, message: &str) {
        match purpose {
            Purpose::Library | Purpose::LibraryPage(_) => self.library.error = message.into(),
            Purpose::LibraryRecord(reference) if reference == &self.library.selected => {
                self.library.detail_error = message.into()
            }
            _ => {}
        }
    }

    pub(super) fn library_toolbar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if ui
                .add_enabled(
                    !self.library.history.is_empty(),
                    egui::Button::new("< Back"),
                )
                .clicked()
                && let Some(reference) = self.library.history.pop()
            {
                self.library.selected.clear();
                self.library_select(&reference);
            }
            if ui.button("Refresh library").clicked() {
                self.library_refresh();
            }
            if ui.button("Molecular viewer").clicked() {
                self.sidebar_tab = 1;
            }
            ui.separator();
            ui.label(
                RichText::new("HEAD / MOLECULAR LIBRARY")
                    .strong()
                    .color(AMBER),
            );
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Connection…").clicked() {
                    self.connection_open = true;
                }
                ui.small(&self.connection.host);
            });
        });
    }

    pub(super) fn library_sidebar(&mut self, ui: &mut egui::Ui) {
        Self::section(ui, "PROJECTS");
        let mut project = None;
        if ui
            .selectable_label(self.library.project.is_empty(), "All library records")
            .clicked()
        {
            project = Some(String::new());
        }
        for item in &self.library.projects {
            let reference = text(item, "ref");
            if ui
                .selectable_label(self.library.project == reference, text(item, "name"))
                .on_hover_text(reference)
                .clicked()
            {
                project = Some(reference.to_owned());
            }
        }
        if let Some(reference) = project {
            self.library.project = reference.clone();
            if !reference.is_empty() {
                self.library_select(&reference);
            }
            self.library_refresh();
        }
        Self::section(ui, "RECORDS");
        ui.add(
            egui::TextEdit::singleline(&mut self.library.query)
                .hint_text("Search names, aliases, references…")
                .desired_width(f32::INFINITY),
        );
        ui.horizontal(|ui| {
            egui::ComboBox::from_id_salt("library-kind")
                .width(94.)
                .selected_text(if self.library.kind.is_empty() {
                    "All kinds"
                } else {
                    &self.library.kind
                })
                .show_ui(ui, |ui| {
                    for kind in ["", "construct", "assembly", "monomer", "project"] {
                        ui.selectable_value(
                            &mut self.library.kind,
                            kind.into(),
                            if kind.is_empty() { "All kinds" } else { kind },
                        );
                    }
                });
            egui::ComboBox::from_id_salt("library-molecule")
                .width(94.)
                .selected_text(if self.library.molecule.is_empty() {
                    "All molecules"
                } else {
                    &self.library.molecule
                })
                .show_ui(ui, |ui| {
                    for molecule in [
                        "",
                        "protein",
                        "dna",
                        "rna",
                        "small_molecule",
                        "mixed_polymer",
                    ] {
                        ui.selectable_value(
                            &mut self.library.molecule,
                            molecule.into(),
                            if molecule.is_empty() {
                                "All molecules"
                            } else {
                                molecule
                            },
                        );
                    }
                });
        });
        ui.horizontal(|ui| {
            ui.checkbox(&mut self.library.review_only, "Needs review");
            if ui.small_button("Clear filters").clicked() {
                self.library.query.clear();
                self.library.kind.clear();
                self.library.molecule.clear();
                self.library.review_only = false;
            }
        });
        if !self.library.error.is_empty() {
            ui.colored_label(RED, &self.library.error);
            if ui.button("Retry library").clicked() {
                self.library_refresh();
            }
        }
        if self.busy(&Purpose::Library) {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.small("Loading library…");
            });
        }
        if self.library.scope != self.library.project {
            return;
        }
        let visible: Vec<_> = self
            .library
            .records
            .iter()
            .filter(|record| self.library.visible(record))
            .cloned()
            .collect();
        ui.weak(format!(
            "{} shown · {} loaded / {} in scope",
            visible.len(),
            self.library.records.len(),
            self.library.filtered_count
        ));
        let mut selected = None;
        for record in &visible {
            let reference = text(record, "ref");
            let is_selected = self.library.selected == reference;
            let (status, color) = review_label(record);
            egui::Frame::NONE
                .fill(if is_selected {
                    Color32::from_rgb(53, 73, 87)
                } else {
                    Color32::from_rgb(40, 42, 45)
                })
                .inner_margin(egui::Margin::symmetric(6, 4))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    if ui
                        .add(
                            egui::Button::selectable(
                                is_selected,
                                RichText::new(text(record, "name")).strong(),
                            )
                            .min_size(Vec2::new(ui.available_width(), 20.)),
                        )
                        .on_hover_text(reference)
                        .clicked()
                    {
                        selected = Some(reference.to_owned());
                    }
                    ui.horizontal(|ui| {
                        ui.small(
                            RichText::new(kind_label(record))
                                .color(Color32::from_rgb(145, 178, 198)),
                        );
                        if let Some(length) = record["sequence_length"].as_u64() {
                            ui.weak(format!(
                                "{length} {}",
                                if text(record, "molecule_type") == "protein" {
                                    "aa"
                                } else {
                                    "nt"
                                }
                            ));
                        }
                        ui.weak(format!("r{}", record["revision"].as_u64().unwrap_or(0)));
                    });
                    if !status.is_empty() {
                        ui.label(RichText::new(status.replace('_', " ")).small().color(color))
                            .on_hover_text(text(record, "review_reason"));
                    }
                });
        }
        if let Some(reference) = selected {
            self.library_select(&reference);
        }
        if let Some(offset) = self.library.next_offset {
            if ui
                .add_enabled(
                    !self.busy(&Purpose::LibraryPage(offset)),
                    egui::Button::new("Load more records"),
                )
                .clicked()
            {
                self.library_load_page(offset);
            }
            ui.small("Filters apply to loaded records. Load more to search the remaining records.");
        } else if visible.is_empty() && self.library.loaded {
            ui.weak(if self.library.records.is_empty() {
                "No records in this scope."
            } else {
                "No records match these filters."
            });
        }
    }

    pub(super) fn library_details(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        egui::Frame::NONE.inner_margin(12).show(ui, |ui| {
            ui.set_min_size(ui.available_size());
            if self.library.selected.is_empty() {
                ui.heading("Molecular library");
                ui.label("Choose a project or construct on the left to inspect its identity, purpose, and retained source files.");
                ui.weak("The head stores immutable revisions. Adding a record to Inputs preserves the selected revision.");
                return;
            }
            if !self.library.detail_error.is_empty() {
                ui.colored_label(RED, &self.library.detail_error);
                if ui.button("Retry record").clicked() { self.library_select(&self.library.selected.clone()); }
                return;
            }
            if self.library.detail.is_null() {
                ui.horizontal(|ui| { ui.spinner(); ui.label(format!("Loading {}…", self.library.selected)); });
                return;
            }
            let detail = self.library.detail.clone();
            let record = &detail["record"];
            ui.horizontal_wrapped(|ui| {
                ui.heading(RichText::new(text(record, "name")).size(18.));
                let mut revision = text(&detail, "ref").to_owned();
                egui::ComboBox::from_id_salt("library-revision").selected_text(format!("Revision {}", record["revision"]))
                    .show_ui(ui, |ui| {
                        for item in rows(&detail, "revisions") {
                            let reference = item.as_str().unwrap_or_else(|| text(item, "ref"));
                            ui.selectable_value(&mut revision, reference.to_owned(), reference);
                        }
                    });
                if revision != text(&detail, "ref") { self.library_select(&revision); }
            });
            ui.horizontal_wrapped(|ui| {
                ui.monospace(text(&detail, "ref"));
                if ui.small_button("Copy ref").clicked() { ctx.copy_text(text(&detail, "ref").into()); }
                ui.separator();
                ui.label(format!("{} · {}", text(record, "kind"), text(&record["identity"], "molecule_type")));
                ui.weak(text(record, "status"));
            });
            let review = &record["identity"]["product_review"];
            if !text(review, "status").is_empty() {
                let color = if text(review, "status") == "review_required" { AMBER } else { GREEN };
                ui.colored_label(color, text(review, "status").replace('_', " ").to_uppercase());
            }
            if matches!(text(record, "kind"), "construct" | "assembly") {
                let input = pinned_input(&detail, self.state.next_chain());
                ui.horizontal_wrapped(|ui| {
                    if ui.add_enabled(input.is_ok(), egui::Button::new("+ Add this revision to Inputs")).clicked()
                        && let Ok(input) = input.clone()
                    {
                        self.state.inputs.push(input);
                        self.library.added = format!("Added {} to the run composer.", text(&detail, "ref"));
                        self.log(self.library.added.clone());
                        self.persist();
                    }
                    if ui.button(format!("Open Inputs ({})", self.state.inputs.len())).clicked() { self.sidebar_tab = 0; }
                    if let Err(reason) = input { ui.colored_label(AMBER, reason); }
                });
                if !self.library.added.is_empty() { ui.colored_label(GREEN, &self.library.added); }
                ui.weak("Adding an input does not launch a job. Model compatibility is checked in Preview.");
            }
            ui.add_space(5.);
            ui.horizontal_wrapped(|ui| {
                for (index, label) in ["Purpose", "Sequence / identity", "Relationships", "Attachments", "Record JSON"].iter().enumerate() {
                    ui.selectable_value(&mut self.library.tab, index, *label);
                }
            });
            ui.separator();
            egui::ScrollArea::both().id_salt(("library-detail", self.library.selected.clone(), self.library.tab))
                .auto_shrink([false, false]).show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    match self.library.tab {
                        0 => self.library_purpose(ui, &detail, ctx),
                        1 => self.library_identity(ui, &detail, ctx),
                        2 => self.library_relations(ui, &detail),
                        3 => self.library_attachments(ui, &detail),
                        _ => {
                            if ui.button("Copy record JSON").clicked() { ctx.copy_text(serde_json::to_string_pretty(record).unwrap_or_default()); }
                            readonly(ui, &serde_json::to_string_pretty(record).unwrap_or_default());
                        }
                    }
                });
        });
    }

    fn library_purpose(&mut self, ui: &mut egui::Ui, detail: &Value, ctx: &egui::Context) {
        let description = &detail["description"];
        let document = description
            .as_str()
            .unwrap_or_else(|| text(description, "text"));
        ui.horizontal(|ui| {
            ui.label(
                RichText::new(if text(&detail["record"], "kind") == "project" {
                    "PROJECT BRIEF"
                } else {
                    "CONSTRUCT PURPOSE"
                })
                .strong()
                .color(AMBER),
            );
            ui.checkbox(&mut self.library.source_document, "Markdown source");
            if ui.small_button("Copy document").clicked() {
                ctx.copy_text(document.into());
            }
        });
        if description["incomplete"] == true
            || document.contains("bio-library:purpose-scaffold:v1 incomplete")
        {
            ui.colored_label(
                AMBER,
                "Purpose document is incomplete. This record still needs manual context.",
            );
        }
        if document.is_empty() {
            ui.weak("No purpose document is attached to this revision.");
        } else if self.library.source_document {
            readonly(ui, document);
        } else {
            markdown(ui, document);
        }
        if !rows(detail, "members").is_empty() {
            ui.add_space(10.);
            Self::section(ui, "PROJECT MEMBERS — PINNED REVISIONS");
            let mut selected = None;
            for member in rows(detail, "members") {
                ui.horizontal_wrapped(|ui| {
                    let reference = text(member, "source_ref");
                    if ui.link(reference).clicked() {
                        selected = Some(reference.to_owned());
                    }
                    ui.label(text(member, "name"));
                    let (label, color) = review_label(member);
                    if !label.is_empty() {
                        ui.colored_label(color, label.replace('_', " "));
                    }
                });
                if !text(member, "role").is_empty() {
                    ui.weak(text(member, "role"));
                }
                ui.separator();
            }
            if let Some(reference) = selected {
                self.library_select(&reference);
            }
        }
    }

    fn library_identity(&mut self, ui: &mut egui::Ui, detail: &Value, ctx: &egui::Context) {
        let record = &detail["record"];
        let identity = &record["identity"];
        let sequence = text(identity, "sequence");
        if !sequence.is_empty() {
            ui.horizontal_wrapped(|ui| {
                ui.label(
                    RichText::new(format!(
                        "{} · {} {}",
                        text(identity, "molecule_type"),
                        sequence.len(),
                        if text(identity, "molecule_type") == "protein" {
                            "amino acids"
                        } else {
                            "nucleotides"
                        }
                    ))
                    .strong(),
                );
                if identity["circular"] == true {
                    ui.colored_label(AMBER, "CIRCULAR");
                }
                if !text(identity, "molecular_form").is_empty() {
                    ui.label(text(identity, "molecular_form"));
                }
                if ui.button("Copy sequence").clicked() {
                    ctx.copy_text(sequence.into());
                }
                if ui.button("Copy FASTA").clicked() {
                    ctx.copy_text(format!(
                        ">{} {}\n{}\n",
                        text(detail, "ref"),
                        text(record, "name"),
                        wrapped_sequence(sequence)
                    ));
                }
            });
            ui.weak(
                "Coordinates are 1-based. Copied sequences contain letters only; library inputs retain the complete identity metadata.",
            );
            readonly(ui, &numbered_sequence(sequence));
            ui.add_space(8.);
        }
        Self::section(ui, "IDENTITY METADATA");
        let mut metadata = identity.clone();
        if let Some(object) = metadata.as_object_mut() {
            object.remove("sequence");
        }
        readonly(
            ui,
            &serde_json::to_string_pretty(&metadata).unwrap_or_default(),
        );
        Self::section(ui, "ALIASES & TAGS");
        for field in ["aliases", "tags"] {
            ui.label(format!(
                "{}: {}",
                field,
                rows(record, field)
                    .iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        if !text(record, "notes").is_empty() {
            ui.label(text(record, "notes"));
        }
        Self::section(ui, "PROVENANCE");
        readonly(
            ui,
            &serde_json::to_string_pretty(&record["provenance"]).unwrap_or_default(),
        );
    }

    fn library_relations(&mut self, ui: &mut egui::Ui, detail: &Value) {
        let mut selected = None;
        Self::section(ui, "MOLECULAR & SOURCE RELATIONSHIPS");
        for relation in rows(detail, "relations") {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(text(relation, "relation").replace('_', " ")).color(AMBER));
                if ui.link(text(relation, "ref")).clicked() {
                    selected = Some(text(relation, "ref").to_owned());
                }
                ui.weak(text(relation, "label"));
            });
        }
        if rows(detail, "relations").is_empty() {
            ui.weak("No source or component relationships are recorded.");
        }
        Self::section(ui, "PROJECT MEMBERSHIP");
        for project in rows(detail, "projects") {
            ui.horizontal_wrapped(|ui| {
                if ui.link(text(project, "ref")).clicked() {
                    selected = Some(text(project, "ref").to_owned());
                }
                ui.label(text(project, "name"));
            });
        }
        if rows(detail, "projects").is_empty() {
            ui.weak("No project revision lists this exact record.");
        }
        Self::section(ui, "REVISION HISTORY");
        for revision in rows(detail, "revisions") {
            let reference = revision.as_str().unwrap_or_else(|| text(revision, "ref"));
            ui.horizontal_wrapped(|ui| {
                if ui
                    .selectable_label(reference == self.library.selected, reference)
                    .clicked()
                {
                    selected = Some(reference.into());
                }
                ui.weak(text(revision, "created_at"));
            });
        }
        if let Some(reference) = selected {
            self.library_select(&reference);
        }
    }

    fn library_attachments(&mut self, ui: &mut egui::Ui, detail: &Value) {
        ui.weak("Download the original retained bytes. Size and SHA-256 are verified against this revision before export.");
        let Some(attachments) = detail["record"]["attachments"].as_array() else {
            ui.label("No attachments.");
            return;
        };
        let mut requested = None;
        for receipt in attachments {
            let name = text(receipt, "path")
                .strip_prefix("attachments/")
                .unwrap_or("");
            if name.is_empty() {
                continue;
            }
            ui.push_id(name, |ui| {
                ui.separator();
                ui.horizontal_wrapped(|ui| {
                    let purpose =
                        Purpose::LibraryAttachment(text(detail, "ref").into(), name.to_owned());
                    if ui
                        .add_enabled(!self.busy(&purpose), egui::Button::new("Save…"))
                        .clicked()
                    {
                        requested = Some((name.to_owned(), receipt.clone()));
                    }
                    ui.label(RichText::new(name).strong());
                    ui.weak(format!("{} bytes", receipt["bytes"].as_u64().unwrap_or(0)));
                    if let Some(pending) = self
                        .pending
                        .values()
                        .find(|pending| pending.purpose == purpose)
                    {
                        ui.spinner();
                        if pending.total > 0 {
                            ui.small(format!("{} / {} bytes", pending.done, pending.total));
                        }
                    }
                });
                ui.monospace(text(receipt, "sha256"));
            });
        }
        if let Some((name, receipt)) = requested {
            let reference = text(detail, "ref");
            if let Some(session) = self.session.as_mut() {
                match session.library_attachment(reference, &name, &receipt) {
                    Ok(id) => {
                        self.pending.insert(
                            id,
                            Pending {
                                purpose: Purpose::LibraryAttachment(reference.into(), name.clone()),
                                label: format!("Library attachment {name}"),
                                done: 0,
                                total: receipt["bytes"].as_u64().unwrap_or(0),
                            },
                        );
                    }
                    Err(error) => self.log(error.to_string()),
                }
            }
        }
    }

    pub(super) fn library_received_attachment(
        &mut self,
        reference: &str,
        name: &str,
        value: Value,
        ctx: &egui::Context,
    ) {
        let metadata = &value["metadata"];
        if text(metadata, "ref") != reference || text(metadata, "name") != name {
            self.log("Library export rejected: the retained attachment identity changed.");
            return;
        }
        let source = PathBuf::from(text(&value, "local_path"));
        let name = name.to_owned();
        let metadata = metadata.clone();
        let sender = self.ui_tx.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            if let Some(destination) = rfd::FileDialog::new().set_file_name(&name).save_file() {
                let result = (|| {
                    let (size, sha) = rpc::file_hash(&source).map_err(|e| e.to_string())?;
                    if Some(size) != metadata["size"].as_u64() || sha != text(&metadata, "sha256") {
                        return Err("Verified library cache changed before export.".into());
                    }
                    std::fs::copy(source, &destination).map_err(|e| e.to_string())?;
                    Ok(destination)
                })();
                let _ = sender.send(UiEvent::Exported(result));
                ctx.request_repaint();
            }
        });
    }
}

fn readonly(ui: &mut egui::Ui, value: &str) {
    let mut view = value;
    ui.add(
        egui::TextEdit::multiline(&mut view)
            .font(egui::TextStyle::Monospace)
            .desired_width(f32::INFINITY)
            .desired_rows(value.lines().count().clamp(2, 25)),
    );
}

fn wrapped_sequence(sequence: &str) -> String {
    sequence
        .as_bytes()
        .chunks(80)
        .map(|line| String::from_utf8_lossy(line))
        .collect::<Vec<_>>()
        .join("\n")
}

fn numbered_sequence(sequence: &str) -> String {
    sequence
        .as_bytes()
        .chunks(80)
        .enumerate()
        .map(|(index, line)| format!("{:>7}  {}", index * 80 + 1, String::from_utf8_lossy(line)))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Small native Markdown presentation. The source view always exposes exact bytes;
/// HTML is never evaluated, and document text never executes commands or requests.
fn markdown(ui: &mut egui::Ui, source: &str) {
    let mut code = false;
    let mut comment = false;
    let lines: Vec<_> = source.lines().collect();
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index];
        let start = index;
        index += 1;
        let trimmed = line.trim();
        if trimmed.starts_with("<!--") {
            comment = true;
        }
        if comment {
            if trimmed.contains("-->") {
                comment = false;
            }
            continue;
        }
        if trimmed.starts_with("```") {
            code = !code;
            continue;
        }
        if code {
            ui.monospace(line);
            continue;
        }
        if trimmed.is_empty() {
            ui.add_space(5.);
            continue;
        }
        if let Some((end, table)) = markdown_table(&lines, start) {
            let columns = table[0].len();
            let width =
                ((ui.available_width() - (columns - 1) as f32 * 12.) / columns as f32).max(70.);
            egui::Grid::new(("purpose-table", start))
                .num_columns(columns)
                .striped(true)
                .min_col_width(width)
                .max_col_width(width)
                .spacing([12., 8.])
                .show(ui, |ui| {
                    for (row, cells) in table.iter().enumerate() {
                        for cell in cells {
                            if row == 0 {
                                ui.label(RichText::new(*cell).strong().color(AMBER));
                            } else {
                                ui.add(egui::Label::new(inline_markdown(cell)).wrap());
                            }
                        }
                        ui.end_row();
                    }
                });
            ui.add_space(6.);
            index = end;
            continue;
        }
        let level = trimmed.bytes().take_while(|byte| *byte == b'#').count();
        if (1..=6).contains(&level) && trimmed.as_bytes().get(level) == Some(&b' ') {
            ui.add_space(5.);
            ui.label(
                RichText::new(&trimmed[level + 1..])
                    .strong()
                    .size(if level < 3 { 15. } else { 12.5 })
                    .color(AMBER),
            );
        } else if matches!(trimmed, "---" | "***" | "___") {
            ui.separator();
        } else {
            let display = if let Some(tail) = trimmed
                .strip_prefix("- ")
                .or_else(|| trimmed.strip_prefix("* "))
            {
                format!("• {tail}")
            } else {
                trimmed.to_owned()
            };
            ui.label(inline_markdown(&display));
        }
    }
}

fn markdown_table<'a>(lines: &[&'a str], start: usize) -> Option<(usize, Vec<Vec<&'a str>>)> {
    fn cells(line: &str) -> Vec<&str> {
        line.trim()
            .trim_matches('|')
            .split('|')
            .map(str::trim)
            .collect()
    }
    let header = cells(lines.get(start)?);
    if !(2..=8).contains(&header.len()) {
        return None;
    }
    let separators = cells(lines.get(start + 1)?);
    if separators.len() != header.len()
        || !separators.iter().all(|cell| {
            let dashes = cell.trim_matches(':');
            dashes.len() >= 3 && dashes.bytes().all(|byte| byte == b'-')
        })
    {
        return None;
    }
    let mut table = vec![header];
    let mut end = start + 2;
    while let Some(line) = lines.get(end) {
        let row = cells(line);
        if row.len() != table[0].len() {
            break;
        }
        table.push(row);
        end += 1;
    }
    Some((end, table))
}

fn inline_markdown(value: &str) -> egui::text::LayoutJob {
    let mut job = egui::text::LayoutJob::default();
    let mut rest = value;
    let mut strong = false;
    let mut code = false;
    while !rest.is_empty() {
        if rest.starts_with("**") && !code {
            strong = !strong;
            rest = &rest[2..];
            continue;
        }
        if rest.starts_with('`') {
            code = !code;
            rest = &rest[1..];
            continue;
        }
        let end = rest
            .char_indices()
            .skip(1)
            .find_map(|(index, _)| {
                let tail = &rest[index..];
                (tail.starts_with('`') || (tail.starts_with("**") && !code)).then_some(index)
            })
            .unwrap_or(rest.len());
        let font = if code {
            egui::FontId::monospace(11.5)
        } else {
            egui::FontId::proportional(12.)
        };
        job.append(
            &rest[..end],
            0.,
            egui::TextFormat {
                font_id: font,
                color: if strong {
                    Color32::WHITE
                } else {
                    Color32::from_gray(219)
                },
                background: if code {
                    Color32::from_rgb(26, 28, 30)
                } else {
                    Color32::TRANSPARENT
                },
                ..Default::default()
            },
        );
        rest = &rest[end..];
    }
    job
}

#[cfg(test)]
mod tests {
    use super::*;

    fn detail() -> Value {
        json!({"ref":"construct:protein@2","record":{"kind":"construct","id":"protein","revision":2,"name":"Protein", "identity":{"molecule_type":"protein","sequence":"ACDE"}},"submission":{"allowed":true}})
    }

    #[test]
    fn composer_uses_exact_record_and_explicit_permission() {
        let mut value = detail();
        let input = pinned_input(&value, "B".into()).unwrap();
        assert_eq!(
            input.source,
            json!({"kind":"library","ref":"construct:protein@2"})
        );
        assert_eq!(input.chain_id, "B");
        value["submission"] = json!({"allowed":false,"reason":"Protein product requires review"});
        assert_eq!(
            pinned_input(&value, "B".into()).err().unwrap(),
            "Protein product requires review"
        );
        value["submission"] = Value::Null;
        assert!(pinned_input(&value, "B".into()).is_err());
    }

    #[test]
    fn composer_rejects_floating_mismatched_and_nonmolecular_refs() {
        for reference in [
            "construct:protein",
            "construct:protein@1",
            "construct:other@2",
        ] {
            let mut value = detail();
            value["ref"] = json!(reference);
            assert!(pinned_input(&value, "A".into()).is_err());
        }
        let mut value = detail();
        value["record"]["kind"] = json!("project");
        value["ref"] = json!("project:protein@2");
        assert!(pinned_input(&value, "A".into()).is_err());
    }

    #[test]
    fn late_detail_cannot_replace_current_selection() {
        let mut explorer = Explorer {
            selected: "construct:other@1".into(),
            ..Default::default()
        };
        assert!(!explorer.accept_detail("construct:protein@2", detail()));
        assert!(explorer.detail.is_null());
        explorer.selected = "construct:protein@2".into();
        assert!(explorer.accept_detail("construct:protein@2", detail()));
        assert!(!explorer.accept_detail(
            "construct:protein@2",
            json!({"ref":"construct:other@1","record":{}})
        ));
    }

    #[test]
    fn search_includes_aliases_and_preserves_review_candidates() {
        let record = json!({"ref":"construct:editor@1","kind":"construct","molecule_type":"protein","aliases":["pGC009"],"review_status":"review_required"});
        let mut explorer = Explorer {
            query: "pgc009 protein".into(),
            review_only: true,
            ..Default::default()
        };
        assert!(explorer.visible(&record));
        explorer.molecule = "dna".into();
        assert!(!explorer.visible(&record));
        explorer.molecule.clear();
        explorer.kind = "project".into();
        assert!(!explorer.visible(&record));
    }

    #[test]
    fn sequence_copy_and_numbered_display_preserve_order() {
        let sequence = "ACGT".repeat(23);
        assert_eq!(wrapped_sequence(&sequence).replace('\n', ""), sequence);
        let numbered = numbered_sequence(&sequence);
        assert!(numbered.starts_with("      1  "));
        assert!(numbered.contains("\n     81  "));
    }

    #[test]
    fn markdown_tables_keep_cell_content_and_stop_before_the_next_paragraph() {
        let lines = [
            "| Construct | Status |",
            "| :--- | ---: |",
            "| `example@1` | **Needs review** |",
            "",
            "Following paragraph",
        ];
        let (end, table) = markdown_table(&lines, 0).unwrap();
        assert_eq!(end, 3);
        assert_eq!(
            table,
            vec![
                vec!["Construct", "Status"],
                vec!["`example@1`", "**Needs review**"]
            ]
        );
        assert!(markdown_table(&["Not | a table", "Ordinary prose"], 0).is_none());
        assert!(markdown_table(&["A | B", "--- | invalid"], 0).is_none());
    }
}
