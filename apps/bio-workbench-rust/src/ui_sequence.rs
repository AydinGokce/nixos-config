//! Native sequence maps and revision-bound protein definitions.
use super::ui_library::wrapped_sequence;
use super::*;
use egui::{Align2, FontId, Pos2, Rect, Sense, Stroke};

#[derive(Clone, Debug, PartialEq)]
pub(super) enum Action {
    Options(Value),
    Preview(Value),
    Write(&'static str, Value),
    Parent(String),
    EditSequence,
}

#[derive(Clone, Default)]
struct Selection {
    segments: Vec<(usize, usize)>,
    strand: i64,
    label: String,
    kind: String,
    is_orf: bool,
    translation: Option<Value>,
    warning: String,
}

#[derive(Default)]
pub(super) struct Viewer {
    reference: String,
    pub view: Value,
    zoom: f32,
    center: f32,
    mode: usize,
    selection: Option<Selection>,
    anchor: Option<usize>,
    protein_anchor: Option<usize>,
    focus_residue: Option<usize>,
    translation_frame: i64,
    frame_drag: Option<FrameDrag>,
    variant_open: bool,
    rebase_variant: Option<String>,
    pending_preview: Option<Value>,
    show_features: bool,
    show_orfs: bool,
    min_orf: usize,
    genetic_code: u64,
    editor: Option<ProteinEditor>,
    standalone: Option<Standalone>,
    requested_options: Value,
    options_due: Option<f64>,
    pub error: String,
}

struct ProteinEditor {
    target: Option<(String, String)>,
    parent: String,
    alt_name: String,
    segments: Vec<(usize, usize)>, // Displayed 1-based inclusive intervals.
    strand: i64,
    code: u64,
    codon_start: u64,
    initiation: String,
    schema: u64,
    stop_policy: Option<String>,
    first_residue: usize,
    last_residue: String,
    preview: Value,
    preview_for: Value,
    requested: Value,
    error: String,
}

struct Standalone {
    project: String,
    sha256: String,
    alt_name: String,
    sequence: String,
}

impl Viewer {
    pub fn accept(&mut self, reference: &str, mut view: Value, literal: &str) {
        if text(&view, "ref") != reference {
            return;
        }
        if !view["sequence"].is_string() && text(&view, "derivation_kind") != "derived" {
            view["sequence"] = json!(literal);
        }
        if self.reference != reference {
            let same_family = self.reference.split('@').next() == reference.split('@').next();
            self.reference = reference.into();
            self.zoom = 1.;
            self.center = if view["circular"] == true {
                0.
            } else {
                view["length"].as_u64().unwrap_or(literal.len() as u64) as f32 / 2.
            };
            self.mode = 0;
            self.selection = None;
            self.anchor = None;
            self.protein_anchor = None;
            self.focus_residue = None;
            self.translation_frame = 1;
            self.frame_drag = None;
            if !same_family {
                self.editor = None;
                self.variant_open = false;
                self.rebase_variant = None;
                self.pending_preview = None;
            }
            self.requested_options = Value::Null;
            self.options_due = None;
            self.show_features = true;
            self.show_orfs = rows(&view, "features").is_empty();
            self.min_orf = 30;
            self.genetic_code = 1;
        }
        if self.rebase_variant.as_deref() == Some(reference) {
            self.rebase_variant = None;
            if let Some(old) = self.editor.take() {
                if old.target.is_none() {
                    let mut editor =
                        ProteinEditor::new(text(&view, "parent_ref"), &view["translation"]);
                    editor.alt_name = old.alt_name;
                    if let Ok(params) = editor.params() {
                        editor.requested = params.clone();
                        self.pending_preview = Some(params);
                    }
                    self.editor = Some(editor);
                } else {
                    self.variant_open = false;
                }
            }
        }
        self.view = view;
        self.error.clear();
    }

    pub fn received_options(&mut self, sent: &Value, view: Value, literal: &str) {
        if self.requested_options == *sent {
            self.accept(text(sent, "ref"), view, literal);
        }
    }

    fn schedule_options(&mut self, now: f64) {
        self.show_orfs = true;
        self.requested_options = Value::Null;
        self.options_due = Some(now + 0.3);
    }

    fn poll_options(&mut self, now: f64) -> Option<Action> {
        if self.options_due.is_some_and(|due| now >= due) {
            self.options_due = None;
            let params = json!({"ref":self.reference,"min_orf_aa":self.min_orf,"genetic_code":self.genetic_code});
            self.requested_options = params.clone();
            Some(Action::Options(params))
        } else {
            None
        }
    }
    pub fn refresh_receipt(&mut self, reference: &str, sha: &str) {
        if let Some(editor) = &mut self.editor
            && let Some((target, digest)) = &mut editor.target
            && target == reference
            && digest.is_empty()
        {
            *digest = sha.into();
        }
    }

    pub fn open_standalone(&mut self, project: &str, sha256: &str) {
        self.standalone = Some(Standalone {
            project: project.into(),
            sha256: sha256.into(),
            alt_name: String::new(),
            sequence: String::new(),
        });
    }

    pub fn received_preview(&mut self, sent: &Value, value: Value) {
        if let Some(editor) = &mut self.editor
            && editor.requested == *sent
        {
            editor.preview = value;
            editor.preview_for = sent.clone();
            editor.error.clear();
        }
    }

    pub fn preview_failed(&mut self, sent: &Value, message: &str) {
        if let Some(editor) = &mut self.editor
            && editor.requested == *sent
        {
            editor.error = message.into();
            editor.requested = Value::Null;
        }
    }

    pub fn write_received(&mut self, result: &Value, sent: &Value) {
        if sent["patch"]["frame_offset"].is_u64()
            && text(sent, "ref") == self.reference
            && let Some(change) = rows(result, "changed_refs")
                .iter()
                .find(|change| text(change, "before_ref") == self.reference)
        {
            self.rebase_variant = Some(text(change, "after_ref").into());
            self.selection = None;
            self.focus_residue = None;
            if let Some(editor) = &mut self.editor {
                editor.preview = Value::Null;
                editor.preview_for = Value::Null;
                editor.requested = Value::Null;
            }
        }
        let saved = self.editor.as_ref().is_some_and(|editor| {
            let correct_target = if let Some((reference, _)) = &editor.target {
                text(sent, "ref") == reference
            } else {
                text(sent, "parent_ref") == editor.parent
                    && text(sent, "alt_name") == editor.alt_name
            };
            correct_target
                && editor
                    .definition()
                    .is_ok_and(|d| d == sent["translation"] || d == sent["patch"]["translation"])
        });
        if saved {
            self.editor = None;
            self.variant_open = false;
        }
        if let Some(editor) = &mut self.editor
            && let Some((target, sha)) = &mut editor.target
            && let Some(change) = rows(result, "changed_refs")
                .iter()
                .find(|c| !text(c, "before_ref").is_empty() && text(c, "before_ref") == target)
        {
            *target = text(change, "after_ref").into();
            sha.clear();
        }
        if let Some(standalone) = &mut self.standalone {
            if text(sent, "project_ref") == standalone.project
                && text(sent, "sequence") == standalone.sequence
                && text(sent, "alt_name") == standalone.alt_name
            {
                self.standalone = None;
            } else if let Some(change) = rows(result, "changed_refs")
                .iter()
                .find(|c| text(c, "before_ref") == standalone.project)
            {
                standalone.project = text(change, "after_ref").into();
                standalone.sha256.clear();
            }
        }
    }
    pub fn refresh_projects(&mut self, projects: &[Value]) {
        if let Some(form) = &mut self.standalone
            && form.sha256.is_empty()
            && let Some(project) = projects.iter().find(|p| text(p, "ref") == form.project)
        {
            form.sha256 = text(project, "sha256").into();
        }
    }

    fn begin_definition(
        &mut self,
        detail: &Value,
        translation: Value,
        editing: bool,
    ) -> Option<Action> {
        let parent = if editing || text(&self.view, "derivation_kind") == "derived" {
            text(&self.view, "parent_ref")
        } else {
            text(detail, "ref")
        };
        let mut editor = ProteinEditor::new(parent, &translation);
        if editing {
            editor.target = Some((text(detail, "ref").into(), text(detail, "sha256").into()));
        }
        let params = editor.params().ok()?;
        editor.requested = params.clone();
        self.editor = Some(editor);
        self.variant_open = true;
        Some(Action::Preview(params))
    }

    pub fn definition_editor(&mut self, detail: &Value) -> Option<Action> {
        let definition = self.view["translation"].clone();
        definition
            .is_object()
            .then(|| self.begin_definition(detail, definition, true))
            .flatten()
    }

    fn select_variant_span(&mut self, detail: &Value, selection: Selection) -> Option<Action> {
        self.focus_residue = selection.segments.first().map(|span| span.0);
        self.selection = Some(selection.clone());
        let editor = self.editor.as_ref()?;
        if !self.variant_open
            || editor.target.is_some()
            || !selection.warning.is_empty()
            || selection.segments.len() != 1
            || self.rebase_variant.is_some()
        {
            return None;
        }
        let definition = cropped_variant(&self.view["translation"], selection.segments[0]);
        if editor
            .definition()
            .is_ok_and(|current| current == definition)
        {
            return None;
        }
        let alt_name = editor.alt_name.clone();
        let action = self.begin_definition(detail, definition, false);
        if let Some(editor) = &mut self.editor {
            editor.alt_name = alt_name;
        }
        action
    }

    fn show_definition_form(
        &mut self,
        ui: &mut egui::Ui,
        writing: bool,
        parents: &[Value],
    ) -> Vec<Action> {
        let mut actions = Vec::new();
        if let Some(editor) = &mut self.editor {
            let mut close = false;
            egui::Frame::group(ui.style()).show(ui, |ui| {
                ui.label(RichText::new(if editor.target.is_some() { "EDIT PROTEIN DEFINITION" } else { "NEW DERIVED PROTEIN" }).strong().color(AMBER));
                ui.weak("The amino-acid sequence is computed from this parent and these coordinates. Positions below are 1-based and inclusive.");
                ui.add_enabled_ui(!writing, |ui| {
                    ui.horizontal(|ui| {
                        ui.label("Parent");
                        ui.add(egui::TextEdit::singleline(&mut editor.parent).desired_width(310.));
                        egui::ComboBox::from_id_salt("product-parent").selected_text("Choose DNA / RNA…").show_ui(ui, |ui| {
                            for record in parents.iter().filter(|r| text(r,"kind") == "construct" && matches!(text(r,"molecule_type"), "dna" | "rna")) {
                                let id = if text(record,"inventory_id").is_empty() { text(record,"ref") } else { text(record,"inventory_id") };
                                let name = if text(record,"alt_name").is_empty() { text(record,"name") } else { text(record,"alt_name") };
                                let label = format!("{id} {name}");
                                ui.selectable_value(&mut editor.parent, text(record,"ref").into(), label);
                            }
                        });
                    });
                    if editor.target.is_none() { ui.horizontal(|ui| { ui.label("Alt name"); ui.add(egui::TextEdit::singleline(&mut editor.alt_name).hint_text("Optional protein or variant name").desired_width(400.)); }); }
                    ui.horizontal(|ui| {
                        ui.label("Strand");
                        ui.selectable_value(&mut editor.strand, 1, "Forward +");
                        ui.selectable_value(&mut editor.strand, -1, "Reverse −");
                        egui::ComboBox::from_id_salt("product-code").selected_text(format!("Genetic code {}",editor.code)).show_ui(ui,|ui| { ui.selectable_value(&mut editor.code,1,"1 · Standard");ui.selectable_value(&mut editor.code,11,"11 · Bacterial"); });
                        ui.label("First codon starts at");ui.add(egui::DragValue::new(&mut editor.codon_start).range(1..=3));
                        egui::ComboBox::from_id_salt("product-initiation").selected_text(&editor.initiation).show_ui(ui,|ui| { ui.selectable_value(&mut editor.initiation,"cds".into(),"CDS initiation");ui.selectable_value(&mut editor.initiation,"literal".into(),"Literal translation"); });
                        if let Some(policy) = &mut editor.stop_policy {
                            egui::ComboBox::from_id_salt("product-stop-policy").selected_text(if policy == "first_stop" { "First stop" } else { "Strict stops" }).show_ui(ui, |ui| {
                                ui.selectable_value(policy, "first_stop".into(), "First stop");
                                ui.selectable_value(policy, "strict".into(), "Strict stops");
                            });
                        }
                    });
                    let mut remove=None;
                    for (i,(start,end)) in editor.segments.iter_mut().enumerate() {
                        ui.horizontal(|ui| { ui.label(format!("Range {}",i+1));ui.add(egui::DragValue::new(start).range(1..=1_000_000));ui.label("through");ui.add(egui::DragValue::new(end).range(1..=1_000_000));if ui.small_button("Remove").clicked(){remove=Some(i);} });
                    }
                    if let Some(i)=remove {editor.segments.remove(i);}
                    if ui.small_button("+ Add nucleotide range").clicked() {editor.segments.push((1,3));}
                    ui.weak("Ranges are joined in the listed biological order; reverse-strand ranges are reverse-complemented individually.");
                    ui.horizontal(|ui| {
                        ui.label("Keep amino acids");ui.add(egui::DragValue::new(&mut editor.first_residue).range(1..=1_000_000));ui.label("through");
                        ui.add(egui::TextEdit::singleline(&mut editor.last_residue).hint_text("last residue").desired_width(105.));
                        ui.weak("Positions refer to the full translated ORF; a blank end keeps the rest.");
                    });
                    ui.weak("Crop amino acids to create variants such as a mature or tag-removed protein. The parent sequence stays unchanged.");
                    let params=editor.params();
                    if let Err(error)=&params {ui.colored_label(RED,error);}
                    let current=params.as_ref().is_ok_and(|p| *p==editor.preview_for);
                    ui.horizontal(|ui| {
                        if ui.add_enabled(params.is_ok(),egui::Button::new("Preview translation")).clicked() && let Ok(params)=params.clone() {editor.requested=params.clone();editor.error.clear();actions.push(Action::Preview(params));}
                        let valid=current && editor.preview["available"]==true && !text(&editor.preview,"parent_sha256").is_empty() && editor.target.as_ref().is_none_or(|(_,sha)|!sha.is_empty());
                        if ui.add_enabled(valid,egui::Button::new(if editor.target.is_some(){"Save definition"}else{"Create protein"})).clicked() && let Ok(definition)=editor.definition() {
                            if let Some((reference,sha))=&editor.target {actions.push(Action::Write("library.edit",json!({"ref":reference,"expected_sha256":sha,"patch":{"translation":definition,"parent_ref":editor.parent}})));}
                            else {actions.push(Action::Write("library.product_create",json!({"parent_ref":editor.parent,"expected_sha256":editor.preview["parent_sha256"],"translation":definition,"alt_name":editor.alt_name})));}
                        }
                        close=ui.button("Cancel").clicked();
                        if !current && !editor.preview.is_null(){ui.weak("Preview again after changing the definition.");}
                    });
                    if current {
                        if editor.preview["available"]==true {
                            ui.colored_label(GREEN,format!("{} amino acids · translation available",editor.preview["length"]));
                            egui::ScrollArea::vertical().id_salt("protein-preview").max_height(90.).show(ui,|ui|{ui.monospace(wrapped_sequence(text(&editor.preview,"sequence")));});
                        }
                        show_issues(ui,&editor.preview);
                    }
                    if !editor.error.is_empty(){ui.colored_label(RED,&editor.error);}
                });
            });
            if close {
                self.editor = None;
            }
        }
        actions
    }

    pub fn show_dialogs(
        &mut self,
        ui: &mut egui::Ui,
        writing: bool,
        _parents: &[Value],
    ) -> Vec<Action> {
        let mut actions = Vec::new();
        if let Some(form) = &mut self.standalone {
            let mut close = false;
            egui::Frame::group(ui.style()).show(ui,|ui| {
                ui.label(RichText::new("NEW STANDALONE PROTEIN").strong().color(AMBER));
                ui.weak("A standalone protein stores its own editable amino-acid sequence in the current project.");
                ui.add_enabled_ui(!writing,|ui|{
                    ui.horizontal(|ui|{ui.label("Alt name");ui.text_edit_singleline(&mut form.alt_name);});
                    ui.add(egui::TextEdit::multiline(&mut form.sequence).font(egui::TextStyle::Monospace).desired_width(f32::INFINITY).desired_rows(5).hint_text("Exact uppercase amino-acid sequence"));
                    let valid=!form.sha256.is_empty() && !form.sequence.is_empty() && form.sequence.bytes().all(|b|b"ACDEFGHIKLMNPQRSTVWYBXZJUO".contains(&b));
                    ui.horizontal(|ui|{
                        if ui.add_enabled(valid,egui::Button::new("Create protein")).clicked(){actions.push(Action::Write("library.create",json!({"project_ref":form.project,"expected_sha256":form.sha256,"sequence":form.sequence,"alt_name":form.alt_name})));}
                        close=ui.button("Cancel").clicked();ui.weak(format!("{} amino acids",form.sequence.len()));
                    });
                });
            });
            if close {
                self.standalone = None;
            }
        }
        actions
    }

    pub fn show(
        &mut self,
        ui: &mut egui::Ui,
        detail: &Value,
        writing: bool,
        parents: &[Value],
    ) -> Vec<Action> {
        let mut actions = Vec::new();
        let sequence = text(&self.view, "sequence").to_owned();
        let molecule = text(&self.view, "molecule_type").to_owned();
        let derived = text(&self.view, "derivation_kind") == "derived";
        let editable = detail["is_latest"] != false && !writing;
        if let Some(params) = self.pending_preview.take() {
            actions.push(Action::Preview(params));
        }
        ui.horizontal_wrapped(|ui| {
            ui.strong(format!(
                "{} · {} {}",
                molecule,
                sequence.len(),
                if molecule == "protein" { "aa" } else { "nt" }
            ));
            if derived {
                ui.colored_label(AMBER, "DERIVED");
                if ui.button("Open parent").clicked() {
                    actions.push(Action::Parent(text(&self.view, "parent_ref").into()));
                }
                if ui
                    .add_enabled(editable, egui::Button::new("Edit definition"))
                    .clicked()
                    && let Some(action) = self.definition_editor(detail)
                {
                    actions.push(action);
                }
            } else if ui
                .add_enabled(editable, egui::Button::new("Edit sequence"))
                .clicked()
            {
                actions.push(Action::EditSequence);
            }
            if ui
                .add_enabled(!sequence.is_empty(), egui::Button::new("Copy sequence"))
                .clicked()
            {
                ui.ctx().copy_text(sequence.clone());
            }
            if ui
                .add_enabled(!sequence.is_empty(), egui::Button::new("Copy FASTA"))
                .clicked()
            {
                ui.ctx().copy_text(format!(
                    ">{} {}\n{}\n",
                    text(detail, "ref"),
                    text(detail, "alt_name"),
                    wrapped_sequence(&sequence)
                ));
            }
        });
        show_issues(ui, &self.view);
        if !self.error.is_empty() {
            ui.colored_label(RED, &self.error);
        }
        if sequence.is_empty() && molecule != "protein" {
            ui.weak("No available sequence for this revision.");
            return actions;
        }
        if molecule == "protein" {
            egui::CollapsingHeader::new("Protein sequence")
                .id_salt(("protein-sequence", self.reference.split('@').next()))
                .default_open(true)
                .show(ui, |ui| {
                    egui::ScrollArea::vertical()
                        .id_salt(("protein-plaintext-scroll", &self.reference))
                        .max_height(240.)
                        .auto_shrink([false, true])
                        .show(ui, |ui| {
                            protein_plaintext(ui, &sequence, &self.reference);
                        });
                });
        }
        if derived {
            if ui
                .button(if self.variant_open {
                    "[-] New variant"
                } else {
                    "[+] New variant"
                })
                .clicked()
            {
                if self.variant_open {
                    self.variant_open = false;
                } else if self.editor.is_some() {
                    self.variant_open = true;
                } else if let Some(action) =
                    self.begin_definition(detail, self.view["translation"].clone(), false)
                {
                    actions.push(action);
                }
            }
            if !self.variant_open {
                return actions;
            }
        }
        if self.editor.is_some() {
            actions.extend(self.show_definition_form(
                ui,
                !editable || self.rebase_variant.is_some(),
                parents,
            ));
            if self.editor.is_none() && derived {
                self.variant_open = false;
                return actions;
            }
        }
        if molecule == "protein" {
            if !derived {
                return actions;
            }
            if derived {
                ui.weak("This peptide is computed from the pinned parent and definition. Editing the parent or definition creates coordinated new revisions; previous runs keep their original inputs.");
                let original = frame_offset(&self.view["translation"]);
                let mut offset = original;
                let strand = if self.view["translation"]["strand"].as_i64() == Some(-1) {
                    "−"
                } else {
                    "+"
                };
                ui.horizontal_wrapped(|ui| {
                    ui.label("Current protein frame");
                    ui.add_enabled_ui(editable && self.frame_drag.is_none(), |ui| {
                        egui::ComboBox::from_id_salt("protein-reading-frame")
                            .selected_text(format!("{strand}{}", offset + 1))
                            .show_ui(ui, |ui| {
                                for choice in 0..3 {
                                    ui.selectable_value(
                                        &mut offset,
                                        choice,
                                        format!("{strand}{}", choice + 1),
                                    );
                                }
                            });
                    });
                    ui.weak("Offset within the saved coding footprint; strand is preserved.");
                });
                if offset != original {
                    actions.push(frame_action(
                        text(detail, "ref"),
                        text(detail, "sha256"),
                        offset,
                    ));
                }
            }
            let features = track_items(&self.view, true, false);
            if !features.is_empty() {
                ui.label(RichText::new("PROTEIN ANNOTATIONS").strong().color(AMBER));
                let (rect, response) = ui.allocate_exact_size(
                    Vec2::new(
                        ui.available_width(),
                        (features.len().min(8) * 22 + 24) as f32,
                    ),
                    Sense::click(),
                );
                let painter = ui.painter_at(rect);
                for (index, item) in features.iter().take(8).enumerate() {
                    for &(start, end) in &item.segments {
                        let span = Rect::from_min_max(
                            Pos2::new(
                                rect.left() + rect.width() * start as f32 / sequence.len() as f32,
                                rect.top() + index as f32 * 22.,
                            ),
                            Pos2::new(
                                rect.left() + rect.width() * end as f32 / sequence.len() as f32,
                                rect.top() + index as f32 * 22. + 17.,
                            ),
                        );
                        painter.rect_filled(span, 2., item_color(item, index).gamma_multiply(0.7));
                        painter.text(
                            span.left_center() + Vec2::new(4., 0.),
                            Align2::LEFT_CENTER,
                            item.label
                                .chars()
                                .take((span.width() / 7.).max(0.) as usize)
                                .collect::<String>(),
                            FontId::monospace(10.),
                            Color32::WHITE,
                        );
                        if response.clicked()
                            && response
                                .interact_pointer_pos()
                                .is_some_and(|pos| span.contains(pos))
                            && let Some(action) = self.select_variant_span(detail, item.clone())
                        {
                            actions.push(action);
                        }
                    }
                }
                for item in &features {
                    ui.horizontal_wrapped(|ui| {
                        if ui
                            .selectable_label(
                                self.selection
                                    .as_ref()
                                    .is_some_and(|s| s.label == item.label),
                                &item.label,
                            )
                            .clicked()
                            && let Some(action) = self.select_variant_span(detail, item.clone())
                        {
                            actions.push(action);
                        }
                        ui.monospace(range_label(&item.segments));
                        if !item.warning.is_empty() {
                            ui.colored_label(AMBER, &item.warning);
                        }
                    });
                }
            }
            if let Some(selection) = self.selection.clone() {
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new(&selection.label).color(AMBER));
                    ui.monospace(format!("residues {}", range_label(&selection.segments)));
                    if ui.small_button("Clear selection").clicked() {
                        self.selection = None;
                    }
                });
            }
            let outcome = protein_lines(
                ui,
                ProteinBlockInput {
                    sequence: &sequence,
                    view: &self.view,
                    selected: self.selection.as_ref(),
                    focus: self.focus_residue.take(),
                    editable,
                    reference: text(detail, "ref"),
                    sha256: text(detail, "sha256"),
                },
                &mut self.protein_anchor,
                &mut self.frame_drag,
            );
            if let Some(selection) = outcome.selection {
                if outcome.apply_selection {
                    if let Some(action) = self.select_variant_span(detail, selection) {
                        actions.push(action);
                    }
                } else {
                    self.selection = Some(selection);
                }
            }
            if let Some(action) = outcome.action {
                actions.push(action);
            }
            return actions;
        }
        if !matches!(molecule.as_str(), "dna" | "rna") {
            ui.monospace(wrapped_sequence(&sequence));
            return actions;
        }
        let length = sequence.len();
        let max_zoom = (length as f32 / 12.).max(1.);
        self.zoom = self.zoom.clamp(1., max_zoom);
        let original_options = (self.min_orf, self.genetic_code);
        ui.horizontal_wrapped(|ui| {
            ui.selectable_value(&mut self.mode, 0, "Auto");
            ui.selectable_value(&mut self.mode, 1, "Circular");
            ui.selectable_value(&mut self.mode, 2, "Linear");
            if ui.button("Fit").clicked() {
                self.zoom = 1.;
                self.center = if self.view["circular"] == true || self.mode == 1 {
                    0.
                } else {
                    length as f32 / 2.
                };
            }
            ui.label("Zoom");
            ui.add(
                egui::Slider::new(&mut self.zoom, 1.0..=max_zoom)
                    .logarithmic(true)
                    .show_value(false)
                    .custom_formatter(|v, _| format!("{v:.1}×")),
            );
            ui.checkbox(&mut self.show_features, "Annotations");
            ui.checkbox(&mut self.show_orfs, "ORFs");
            ui.label("Min ORF");
            ui.add(egui::DragValue::new(&mut self.min_orf).range(1..=10000));
            ui.weak("aa");
            egui::ComboBox::from_id_salt("map-code")
                .selected_text(format!("Code {}", self.genetic_code))
                .show_ui(ui, |ui| {
                    ui.selectable_value(&mut self.genetic_code, 1, "1 · Standard");
                    ui.selectable_value(&mut self.genetic_code, 11, "11 · Bacterial");
                });
            egui::ComboBox::from_id_salt("map-reading-frame")
                .selected_text(format!("Preview frame {:+}", self.translation_frame))
                .show_ui(ui, |ui| {
                    for frame in [1, 2, 3, -1, -2, -3] {
                        ui.selectable_value(
                            &mut self.translation_frame,
                            frame,
                            format!("{frame:+}"),
                        );
                    }
                });
        });
        let now = ui.input(|input| input.time);
        if original_options != (self.min_orf, self.genetic_code) {
            self.schedule_options(now);
        }
        if let Some(action) = self.poll_options(now) {
            actions.push(action);
        }
        if let Some(due) = self.options_due {
            ui.ctx()
                .request_repaint_after(std::time::Duration::from_secs_f64((due - now).max(0.)));
        }
        let cyclic = self.view["circular"] == true || self.mode == 1;
        ui.weak("Scroll to pan / rotate · Ctrl/Cmd + scroll to zoom · Drag to select · Right-drag to pan");
        let (rect, response) = ui.allocate_exact_size(
            Vec2::new(ui.available_width().max(300.), 420.),
            Sense::click_and_drag(),
        );
        let painter = ui.painter_at(rect);
        painter.rect_filled(rect, 3., Color32::from_rgb(25, 29, 32));
        let items = track_items(&self.view, self.show_features, self.show_orfs);
        let pointer = response
            .hover_pos()
            .or_else(|| response.interact_pointer_pos());
        let previous = MapGeometry::new(rect, length, self.center, self.zoom, self.mode, cyclic);
        if response.hovered() {
            let (scroll, zoom_delta, modified) = ui.input(|input| {
                (
                    input.smooth_scroll_delta.x + input.smooth_scroll_delta.y,
                    input.zoom_delta(),
                    input.modifiers.ctrl || input.modifiers.command,
                )
            });
            if (zoom_delta - 1.).abs() > f32::EPSILON || modified {
                let factor = if (zoom_delta - 1.).abs() > f32::EPSILON {
                    zoom_delta
                } else {
                    (scroll * 0.006).exp()
                };
                self.zoom = (self.zoom * factor).clamp(1., max_zoom);
            } else {
                self.center -= scroll / previous.scale;
            }
            // Consume map navigation only while the pointer is inside this map.
            ui.input_mut(|input| input.smooth_scroll_delta = Vec2::ZERO);
        }
        if response.dragged_by(egui::PointerButton::Secondary) {
            self.center -= response.drag_delta().x / previous.scale;
        }
        let geometry = MapGeometry::new(rect, length, self.center, self.zoom, self.mode, cyclic);
        self.center = geometry.center;
        let base_at = |pos| geometry.base_at(pos);
        if response.drag_started_by(egui::PointerButton::Primary)
            && let Some(p) = pointer
        {
            self.anchor = Some(base_at(p));
        }
        if response.dragged_by(egui::PointerButton::Primary)
            && let (Some(start), Some(p)) = (self.anchor, pointer)
        {
            self.selection = Some(Selection {
                segments: selection_ranges(start, base_at(p), length, cyclic),
                strand: 1,
                label: "Selected range".into(),
                ..Default::default()
            });
        }
        if response.drag_stopped() {
            self.anchor = None;
        }
        let hit = draw_map(
            &painter,
            geometry,
            &items,
            self.selection.as_ref(),
            &sequence,
            (molecule == "rna", self.translation_frame),
            pointer,
        );
        if response.clicked() {
            if let Some(item) = hit {
                self.selection = Some(item);
            } else if let Some(p) = pointer {
                let base = base_at(p);
                self.selection = Some(Selection {
                    segments: vec![(base, base + 1)],
                    strand: 1,
                    label: "Selected base".into(),
                    ..Default::default()
                });
            }
        }
        if let Some(selection) = self.selection.clone() {
            ui.horizontal_wrapped(|ui| {
                ui.strong(&selection.label);ui.monospace(range_label(&selection.segments));ui.label(if selection.strand<0{"reverse strand"}else{"forward strand"});
                if ui.button("Zoom to selection").clicked(){let start=selection.segments.first().map(|p|p.0).unwrap_or(0);let span=selection.segments.iter().map(|(start,end)|end-start).sum::<usize>();if span>0{self.center=start as f32+span as f32/2.;self.zoom=(length as f32/span as f32/1.2).clamp(1.,max_zoom);self.mode=2;}}
                if ui.add_enabled(editable && selection.warning.is_empty(),egui::Button::new("Create protein…")).clicked(){
                    let definition=selection.translation.clone().unwrap_or_else(||json!({"schema":1,"segments":selection.segments.iter().map(|(start,end)|json!({"start":start,"end":end})).collect::<Vec<_>>(),"strand":selection.strand,"genetic_code":self.genetic_code,"codon_start":1,"initiation":"literal","residue_start":0,"residue_end":null}));
                    if let Some(action)=self.begin_definition(detail,definition,false){actions.push(action);}
                }
                if ui.button("Clear selection").clicked(){self.selection=None;}
            });
            if !selection.warning.is_empty() {
                ui.colored_label(AMBER, &selection.warning);
            }
        }
        if self.view["features_truncated"] == true || self.view["orfs_truncated"] == true {
            ui.colored_label(AMBER,"Some tracks were omitted to keep this view responsive. Increase the minimum ORF length or inspect the retained annotations.");
        }
        egui::CollapsingHeader::new(format!("Annotations and ORFs · {} tracks", items.len())).show(
            ui,
            |ui| {
                egui::ScrollArea::vertical()
                    .id_salt("sequence-tracks")
                    .max_height(180.)
                    .show(ui, |ui| {
                        for item in &items {
                            ui.horizontal(|ui| {
                                if ui
                                    .selectable_label(
                                        self.selection.as_ref().is_some_and(|s| {
                                            s.label == item.label && s.segments == item.segments
                                        }),
                                        &item.label,
                                    )
                                    .clicked()
                                {
                                    self.selection = Some(item.clone());
                                }
                                ui.monospace(range_label(&item.segments));
                                ui.weak(if item.strand < 0 { "−" } else { "+" });
                                if !item.warning.is_empty() {
                                    ui.colored_label(AMBER, &item.warning);
                                }
                            });
                        }
                    });
            },
        );
        actions
    }
}

impl ProteinEditor {
    fn new(parent: &str, t: &Value) -> Self {
        Self {
            target: None,
            parent: parent.into(),
            alt_name: String::new(),
            segments: rows(t, "segments")
                .iter()
                .map(|s| {
                    (
                        s["start"].as_u64().unwrap_or(0) as usize + 1,
                        s["end"].as_u64().unwrap_or(3) as usize,
                    )
                })
                .collect(),
            strand: t["strand"].as_i64().unwrap_or(1),
            code: t["genetic_code"].as_u64().unwrap_or(1),
            codon_start: t["codon_start"].as_u64().unwrap_or(1),
            initiation: text(t, "initiation").to_owned(),
            schema: t["schema"].as_u64().unwrap_or(1),
            stop_policy: t["stop_policy"].as_str().map(str::to_owned),
            first_residue: t["residue_start"].as_u64().unwrap_or(0) as usize + 1,
            last_residue: t["residue_end"]
                .as_u64()
                .map(|v| v.to_string())
                .unwrap_or_default(),
            preview: Value::Null,
            preview_for: Value::Null,
            requested: Value::Null,
            error: String::new(),
        }
    }
    fn definition(&self) -> Result<Value, String> {
        if self.segments.is_empty() || self.segments.iter().any(|(s, e)| *s == 0 || e < s) {
            return Err(
                "Each nucleotide range needs a first and last base, in ascending coordinate order."
                    .into(),
            );
        }
        let end =
            if self.last_residue.trim().is_empty() {
                None
            } else {
                Some(self.last_residue.trim().parse::<usize>().map_err(|_| {
                    "Enter a last amino-acid position or leave it blank.".to_string()
                })?)
            };
        if self.first_residue == 0 || end.is_some_and(|e| e < self.first_residue) {
            return Err("The amino-acid range must contain at least one residue.".into());
        }
        let mut definition = json!({"schema":self.schema,"segments":self.segments.iter().map(|(s,e)|json!({"start":s-1,"end":e})).collect::<Vec<_>>(),"strand":self.strand,"genetic_code":self.code,"codon_start":self.codon_start,"initiation":self.initiation,"residue_start":self.first_residue-1,"residue_end":end});
        if let Some(policy) = &self.stop_policy {
            definition["stop_policy"] = json!(policy);
        }
        Ok(definition)
    }
    fn params(&self) -> Result<Value, String> {
        if !self.parent.starts_with("construct:")
            || !self
                .parent
                .rsplit_once('@')
                .is_some_and(|(_, r)| r.parse::<u64>().is_ok_and(|r| r > 0))
        {
            return Err("Choose a parent construct with a pinned revision.".into());
        }
        Ok(json!({"parent_ref":self.parent,"translation":self.definition()?}))
    }
}

/// Keep the buffer continuous: visual wrapping and the separate gutter never enter the clipboard.
fn protein_plaintext(
    ui: &mut egui::Ui,
    sequence: &str,
    reference: &str,
) -> egui::text_edit::TextEditOutput {
    let font = FontId::monospace(13.);
    let color = ui.visuals().text_color();
    let gutter_width = sequence.len().max(1).to_string().len() as f32 * 8. + 12.;
    ui.horizontal_top(|ui| {
        let (gutter, _) = ui.allocate_exact_size(Vec2::new(gutter_width, 0.), Sense::hover());
        let mut buffer = sequence;
        let mut layouter = |ui: &egui::Ui, buffer: &dyn egui::TextBuffer, width: f32| {
            let mut job = egui::text::LayoutJob::simple(
                buffer.as_str().to_owned(),
                font.clone(),
                color,
                width,
            );
            job.wrap.break_anywhere = true;
            ui.fonts_mut(|fonts| fonts.layout_job(job))
        };
        let output = egui::TextEdit::multiline(&mut buffer)
            .id_salt(("protein-plaintext", reference))
            .font(font.clone())
            .frame(false)
            .desired_rows(1)
            .desired_width(ui.available_width())
            .layouter(&mut layouter)
            .show(ui);
        let mut position = 1;
        for row in &output.galley.rows {
            let point = Pos2::new(gutter.right(), output.galley_pos.y + row.pos.y);
            if point.y + row.size.y >= ui.clip_rect().top() && point.y <= ui.clip_rect().bottom() {
                ui.painter().text(
                    point,
                    Align2::RIGHT_TOP,
                    position.to_string(),
                    font.clone(),
                    ui.visuals().weak_text_color(),
                );
            }
            position += row.char_count_excluding_newline();
        }
        output
    })
    .inner
}

fn show_issues(ui: &mut egui::Ui, value: &Value) {
    for issue in rows(value, "issues") {
        let message = issue.as_str().unwrap_or_else(|| text(issue, "message"));
        if !message.is_empty() {
            ui.colored_label(AMBER, message);
        }
    }
}
fn cropped_variant(definition: &Value, range: (usize, usize)) -> Value {
    let mut result = definition.clone();
    let start = definition["residue_start"].as_u64().unwrap_or(0) as usize;
    result["residue_start"] = json!(start + range.0);
    result["residue_end"] = json!(start + range.1);
    result
}
fn range_label(segments: &[(usize, usize)]) -> String {
    segments
        .iter()
        .map(|(s, e)| format!("{}–{}", s + 1, e))
        .collect::<Vec<_>>()
        .join(" / ")
}
fn selection_ranges(
    start: usize,
    end: usize,
    length: usize,
    circular: bool,
) -> Vec<(usize, usize)> {
    if circular && end < start {
        vec![(start, length), (0, end + 1)]
    } else {
        vec![(start.min(end), start.max(end) + 1)]
    }
}
fn track_items(view: &Value, features: bool, orfs: bool) -> Vec<Selection> {
    let length = view["length"].as_u64().unwrap_or(0) as usize;
    [features.then_some("features"), orfs.then_some("orfs")]
        .into_iter()
        .flatten()
        .flat_map(|key| rows(view, key).iter())
        .filter_map(|v| {
            let segments: Vec<_> = rows(v, "segments")
                .iter()
                .filter_map(|s| Some((s["start"].as_u64()? as usize, s["end"].as_u64()? as usize)))
                .collect();
            if segments.is_empty() || segments.iter().any(|(s, e)| s >= e || *e > length) {
                return None;
            }
            let warning = [
                (
                    "stale",
                    "Historical annotation; coordinates may not match this revision",
                ),
                ("partial", "Partial or fuzzy source coordinates"),
                ("unsupported", "Unsupported source coordinate expression"),
            ]
            .into_iter()
            .filter_map(|(k, label)| (v[k] == true).then_some(label))
            .collect::<Vec<_>>()
            .join(" · ");
            Some(Selection {
                segments,
                strand: v["strand"].as_i64().unwrap_or(1),
                label: text(v, "label").into(),
                kind: text(v, "kind").into(),
                is_orf: text(v, "id").starts_with("orf:"),
                translation: v["translation"]
                    .is_object()
                    .then(|| v["translation"].clone()),
                warning,
            })
        })
        .collect()
}
fn item_color(item: &Selection, _index: usize) -> Color32 {
    if !item.warning.is_empty() {
        Color32::from_rgb(131, 120, 86)
    } else if item.is_orf {
        Color32::from_rgb(92, 173, 209)
    } else {
        match item.kind.as_str() {
            "CDS" => Color32::from_rgb(103, 194, 153),
            "promoter" => Color32::from_rgb(115, 165, 221),
            "rep_origin" => Color32::from_rgb(220, 177, 92),
            "terminator" => Color32::from_rgb(209, 124, 142),
            _ => [
                Color32::from_rgb(154, 173, 195),
                Color32::from_rgb(173, 134, 201),
                Color32::from_rgb(203, 167, 90),
            ][item
                .label
                .bytes()
                .fold(0usize, |h, b| h.wrapping_mul(31).wrapping_add(b as usize))
                % 3],
        }
    }
}
#[derive(Clone, Copy)]
struct MapGeometry {
    rect: Rect,
    plot: Rect,
    anchor: Pos2,
    length: f32,
    center: f32,
    scale: f32,
    curve: f32,
    angular: f32,
    cyclic: bool,
}

impl MapGeometry {
    fn new(rect: Rect, length: usize, center: f32, zoom: f32, mode: usize, cyclic: bool) -> Self {
        let plot = Rect::from_min_max(
            rect.min + Vec2::new(50., 35.),
            rect.max - Vec2::new(24., 20.),
        );
        let progress = (zoom.max(1.).log2() / 2.).clamp(0., 1.);
        let straight = progress * progress * (3. - 2. * progress);
        let curve = match mode {
            1 => 1.,
            0 if cyclic => 1. - straight,
            _ => 0.,
        };
        let radius = (rect.height() / 2. - 65.)
            .min(plot.width() / 2. - 25.)
            .max(20.);
        let length = length.max(1) as f32;
        let circumference = std::f32::consts::TAU * radius;
        // Keep scale monotonic even when a narrow circle straightens into a shorter axis.
        let span_pixels = match mode {
            1 => circumference * zoom,
            0 if cyclic && zoom < 4. => {
                circumference * (4. * plot.width() / circumference).powf(progress)
            }
            _ => plot.width() * zoom,
        };
        let scale = span_pixels / length;
        let visible = (plot.width() / scale).min(length);
        let center = if cyclic {
            center.rem_euclid(length)
        } else {
            center.clamp(visible / 2., length - visible / 2.)
        };
        Self {
            rect,
            plot,
            anchor: Pos2::new(plot.center().x, plot.top() + 16.),
            length,
            center,
            scale,
            curve,
            angular: std::f32::consts::TAU * curve / length,
            cyclic,
        }
    }

    fn position(self, base: f32, offset: f32) -> Pos2 {
        let delta = base - self.center;
        if self.curve < 0.0001 {
            return self.anchor + Vec2::new(delta * self.scale, offset);
        }
        let angle = delta * self.angular;
        let radius = self.scale / self.angular;
        self.anchor
            + Vec2::new(
                angle.sin() * (radius - offset),
                2. * (angle / 2.).sin().powi(2) * radius + angle.cos() * offset,
            )
    }

    fn tangent(self, base: f32) -> Vec2 {
        Vec2::angled((base - self.center) * self.angular)
    }

    fn base_at(self, pos: Pos2) -> usize {
        let delta = if self.curve < 0.0001 {
            (pos.x - self.anchor.x) / self.scale
        } else {
            let radius = self.scale / self.angular;
            (pos.x - self.anchor.x).atan2(radius - (pos.y - self.anchor.y)) / self.angular
        };
        let base = self.center + delta.clamp(-self.length / 2., self.length / 2.);
        let base = if self.cyclic {
            base.rem_euclid(self.length)
        } else {
            base.clamp(0., self.length - 1.)
        };
        (base.floor() as usize).min(self.length as usize - 1)
    }

    fn window(self) -> (f32, f32) {
        let half = if self.curve < 0.0001 {
            (self.plot.width() / 2. + 35.) / self.scale
        } else {
            let radius = self.scale / self.angular;
            if radius > self.rect.height() + 100. {
                (self.plot.width() / 2. + 35.).atan2(radius - self.rect.height()) / self.angular
            } else {
                self.length / 2.
            }
        }
        .min(self.length / 2.);
        let (left, right) = (self.center - half, self.center + half);
        if self.cyclic {
            (left, right)
        } else {
            (left.max(0.), right.min(self.length))
        }
    }

    fn spans(self, start: usize, end: usize) -> Vec<(f32, f32)> {
        let (left, right) = self.window();
        let cycles = if self.cyclic {
            (left / self.length).floor() as i32..=(right / self.length).floor() as i32
        } else {
            0..=0
        };
        cycles
            .filter_map(|cycle| {
                let shift = cycle as f32 * self.length;
                let a = (start as f32 + shift).max(left);
                let b = (end as f32 + shift).min(right);
                (a < b).then_some((a, b))
            })
            .collect()
    }

    fn path(self, start: f32, end: f32, offset: f32) -> Vec<Pos2> {
        let steps = (((end - start) * self.angular).abs() * 60.)
            .ceil()
            .clamp(1., 500.) as usize;
        (0..=steps)
            .map(|index| {
                self.position(egui::lerp(start..=end, index as f32 / steps as f32), offset)
            })
            .collect()
    }
}

fn tick_step(span: f32) -> usize {
    let raw = (span / 8.).max(1.);
    let magnitude = 10f32.powf(raw.log10().floor());
    let multiplier = [1., 2., 5., 10.]
        .into_iter()
        .find(|factor| magnitude * factor >= raw)
        .unwrap_or(10.);
    (magnitude * multiplier).max(1.) as usize
}

fn segment_distance(point: Pos2, start: Pos2, end: Pos2) -> f32 {
    let along = end - start;
    let fraction = ((point - start).dot(along) / along.length_sq().max(f32::EPSILON)).clamp(0., 1.);
    point.distance(start + along * fraction)
}

fn draw_map(
    p: &egui::Painter,
    geometry: MapGeometry,
    items: &[Selection],
    selected: Option<&Selection>,
    sequence: &str,
    preview: (bool, i64),
    pointer: Option<Pos2>,
) -> Option<Selection> {
    let (rna, reading_frame) = preview;
    let (left, right) = geometry.window();
    p.add(egui::Shape::line(
        geometry.path(left, right, 0.),
        Stroke::new(2., Color32::from_rgb(109, 121, 129)),
    ));
    let step = tick_step((geometry.plot.width() / geometry.scale).min(geometry.length)) as i64;
    let begin = (left.floor() as i64).div_euclid(step) * step;
    for base in (begin..=right.ceil() as i64).step_by(step as usize) {
        let base = base as f32;
        let point = geometry.position(base, -18.);
        if !geometry.rect.contains(point) {
            continue;
        }
        p.line_segment(
            [geometry.position(base, -5.), geometry.position(base, 4.)],
            Stroke::new(1., Color32::GRAY),
        );
        p.text(
            point,
            Align2::CENTER_CENTER,
            format!("{}", base.rem_euclid(geometry.length) as usize + 1),
            FontId::monospace(10.),
            Color32::LIGHT_GRAY,
        );
    }
    if geometry.curve > 0.85 && geometry.scale * geometry.length < 1300. {
        let center = geometry.anchor + Vec2::new(0., geometry.scale / geometry.angular);
        p.text(
            center - Vec2::new(0., 10.),
            Align2::CENTER_CENTER,
            format!("{} bp", sequence.len()),
            FontId::proportional(22.),
            Color32::LIGHT_GRAY,
        );
        p.text(
            center + Vec2::new(0., 14.),
            Align2::CENTER_CENTER,
            "SEQUENCE MAP",
            FontId::monospace(11.),
            Color32::GRAY,
        );
    }
    let mut hit = None;
    if geometry.curve > 0.8 {
        let radius = geometry.scale / geometry.angular;
        let legend_x = geometry.rect.right() - 225.;
        let opacity = ((legend_x - geometry.anchor.x - radius - 20.) / 55.).clamp(0., 1.)
            * ((geometry.rect.bottom() - geometry.anchor.y - radius * 2.) / 30.).clamp(0., 1.);
        if opacity > 0.01 {
            let top = geometry.rect.top() + 24.;
            p.text(
                Pos2::new(legend_x, top),
                Align2::LEFT_TOP,
                "FEATURES",
                FontId::monospace(11.),
                Color32::GRAY.gamma_multiply(opacity),
            );
            for (index, item) in items
                .iter()
                .filter(|item| item.kind != "source" && !item.is_orf)
                .take(16)
                .enumerate()
            {
                let pos = Pos2::new(legend_x, top + 24. + index as f32 * 20.);
                let region = Rect::from_min_size(pos, Vec2::new(215., 19.));
                p.rect_filled(
                    Rect::from_min_size(pos + Vec2::new(0., 3.), Vec2::splat(7.)),
                    1.,
                    item_color(item, index).gamma_multiply(opacity),
                );
                p.text(
                    pos + Vec2::new(14., 0.),
                    Align2::LEFT_TOP,
                    item.label.chars().take(28).collect::<String>(),
                    FontId::proportional(11.),
                    Color32::LIGHT_GRAY.gamma_multiply(opacity),
                );
                if opacity > 0.25 && pointer.is_some_and(|point| region.contains(point)) {
                    hit = Some(item.clone());
                }
            }
        }
    }
    let mut annotation_index = 0;
    for (index, item) in items.iter().enumerate() {
        if item.kind == "source" {
            continue;
        }
        let (circular_offset, lane) = if item.is_orf {
            (
                66. + (index % 6) as f32 * 7.,
                (if item.strand < 0 { 7 } else { 4 }) + index % 3,
            )
        } else {
            let lane = annotation_index % 4;
            annotation_index += 1;
            (14. + lane as f32 * 11., lane)
        };
        let offset = egui::lerp(
            circular_offset.min(geometry.scale / geometry.angular * 0.85)..=22. + lane as f32 * 18.,
            1. - geometry.curve,
        );
        let color = item_color(item, index);
        let active = selected.is_some_and(|selection| {
            selection.segments == item.segments && selection.label == item.label
        });
        for &(start, end) in &item.segments {
            for (a, b) in geometry.spans(start, end) {
                let points = geometry.path(a, b, offset);
                if pointer.is_some_and(|pos| {
                    points
                        .windows(2)
                        .any(|pair| segment_distance(pos, pair[0], pair[1]) <= 7.)
                }) {
                    hit = Some(item.clone());
                }
                p.add(egui::Shape::line(
                    points,
                    Stroke::new(
                        if active {
                            10.
                        } else if item.is_orf {
                            3.
                        } else {
                            7.
                        },
                        color.gamma_multiply(if item.is_orf { 0.65 } else { 1. }),
                    ),
                ));
                let tip_base = if item.strand < 0 { a } else { b };
                let tip = geometry.position(tip_base, offset);
                let along = geometry.tangent(tip_base) * if item.strand < 0 { -1. } else { 1. };
                let normal = Vec2::new(-along.y, along.x);
                p.add(egui::Shape::convex_polygon(
                    vec![
                        tip + along * 5.,
                        tip - along * 4. + normal * 5.,
                        tip - along * 4. - normal * 5.,
                    ],
                    color,
                    Stroke::NONE,
                ));
                if geometry.curve < 0.8 && (b - a) * geometry.scale > 65. {
                    p.text(
                        geometry.position((a + b) / 2., offset),
                        Align2::CENTER_CENTER,
                        item.label
                            .chars()
                            .take((((b - a) * geometry.scale) / 7.).min(60.) as usize)
                            .collect::<String>(),
                        FontId::monospace(10.),
                        Color32::from_rgb(21, 25, 28),
                    );
                }
            }
        }
    }
    if let Some(selection) = selected {
        for &(start, end) in &selection.segments {
            for (a, b) in geometry.spans(start, end) {
                p.add(egui::Shape::line(
                    geometry.path(a, b, -9.),
                    Stroke::new(5., AMBER),
                ));
            }
        }
    }
    if geometry.scale >= 3.
        && (geometry.curve < 0.0001
            || geometry.scale / geometry.angular > geometry.rect.height() + 100.)
    {
        let bytes = sequence.as_bytes();
        let start = left.floor() as i64;
        let end = right.ceil() as i64;
        for base in start..end {
            let index = base.rem_euclid(bytes.len() as i64) as usize;
            let b = bytes[index];
            for (offset, letter) in [
                (214., b),
                (
                    240.,
                    if rna && b == b'A' {
                        b'U'
                    } else {
                        complement(b)
                    },
                ),
            ] {
                let pos = geometry.position(base as f32 + 0.5, offset);
                let rect = Rect::from_center_size(
                    pos + Vec2::new(0., 12.),
                    Vec2::new(geometry.scale, 25.),
                );
                letter_block(p, rect, letter, base_color(letter), false);
            }
        }
        let frame = reading_frame.unsigned_abs().clamp(1, 3) as usize - 1;
        let reverse = reading_frame < 0;
        for base in start.saturating_sub(2)..end {
            if !geometry.cyclic && (base < 0 || base >= bytes.len() as i64) {
                continue;
            }
            let index = base.rem_euclid(bytes.len() as i64) as usize;
            if index + 3 <= bytes.len()
                && if reverse {
                    (bytes.len() - index - 3) % 3 == frame
                } else {
                    index % 3 == frame
                }
            {
                let amino = if reverse {
                    codon(&[
                        complement(bytes[index + 2]),
                        complement(bytes[index + 1]),
                        complement(bytes[index]),
                    ])
                } else {
                    codon(&bytes[index..index + 3])
                };
                let pos = geometry.position(base as f32 + 1.5, 268.);
                letter_block(
                    p,
                    Rect::from_center_size(
                        pos + Vec2::new(0., 13.),
                        Vec2::new(geometry.scale * 3., 27.),
                    ),
                    amino,
                    amino_color(amino),
                    false,
                );
            }
        }
        p.text(
            geometry.plot.left_bottom(),
            Align2::LEFT_BOTTOM,
            format!("5' / 3' · preview frame {reading_frame:+}"),
            FontId::monospace(10.),
            Color32::GRAY,
        );
    } else {
        p.text(
            geometry.plot.left_bottom(),
            Align2::LEFT_BOTTOM,
            "Zoom closer to reveal bases and codon translations",
            FontId::proportional(11.),
            Color32::GRAY,
        );
    }
    hit
}
fn base_color(b: u8) -> Color32 {
    match b {
        b'A' => Color32::from_rgb(132, 203, 151),
        b'C' => Color32::from_rgb(118, 170, 225),
        b'G' => Color32::from_rgb(222, 186, 96),
        b'T' | b'U' => Color32::from_rgb(218, 130, 140),
        _ => Color32::GRAY,
    }
}
fn amino_color(b: u8) -> Color32 {
    match b {
        b'D' | b'E' => Color32::from_rgb(222, 136, 121),
        b'K' | b'R' | b'H' => Color32::from_rgb(127, 170, 226),
        b'S' | b'T' | b'N' | b'Q' => Color32::from_rgb(130, 204, 181),
        b'A' | b'V' | b'L' | b'I' | b'M' | b'F' | b'W' | b'Y' => Color32::from_rgb(213, 185, 128),
        b'G' | b'P' => Color32::from_rgb(182, 156, 211),
        b'C' => Color32::from_rgb(219, 205, 112),
        b'*' => Color32::from_rgb(236, 118, 118),
        _ => Color32::from_rgb(153, 166, 174),
    }
}

fn letter_block(p: &egui::Painter, rect: Rect, letter: u8, color: Color32, selected: bool) {
    if rect.width() < 1. || rect.height() < 1. {
        return;
    }
    let color = if selected { AMBER } else { color };
    let cell = rect.shrink2(Vec2::new(0.5, 1.));
    p.rect_filled(
        cell,
        1.,
        color.gamma_multiply(if selected { 0.52 } else { 0.24 }),
    );
    p.rect_filled(
        Rect::from_min_max(cell.min, Pos2::new(cell.right(), cell.top() + 2.)),
        0.,
        color,
    );
    if selected {
        p.rect_stroke(cell, 1., Stroke::new(1., AMBER), egui::StrokeKind::Inside);
    }
    if cell.width() >= 8. {
        p.text(
            cell.center() + Vec2::new(0., 1.),
            Align2::CENTER_CENTER,
            (letter as char).to_string(),
            FontId::monospace((cell.height() * 0.6).clamp(10., 15.)),
            Color32::from_rgb(230, 236, 237),
        );
    }
}

fn complement(b: u8) -> u8 {
    match b {
        b'A' => b'T',
        b'T' | b'U' => b'A',
        b'C' => b'G',
        b'G' => b'C',
        b'R' => b'Y',
        b'Y' => b'R',
        b'S' => b'S',
        b'W' => b'W',
        b'K' => b'M',
        b'M' => b'K',
        b'B' => b'V',
        b'V' => b'B',
        b'D' => b'H',
        b'H' => b'D',
        _ => b'N',
    }
}
fn codon(bytes: &[u8]) -> u8 {
    const TABLE: &[u8; 64] = b"FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG";
    let mut index = 0;
    for &b in bytes.iter().take(3) {
        let n = match b {
            b'T' | b'U' => 0,
            b'C' => 1,
            b'A' => 2,
            b'G' => 3,
            _ => return b'X',
        };
        index = index * 4 + n;
    }
    if bytes.len() == 3 { TABLE[index] } else { b'X' }
}
struct CodonAlignment<'a> {
    source: &'a str,
    positions: Vec<[usize; 3]>,
    reverse: bool,
    rna: bool,
}

#[derive(Clone)]
struct FrameDrag {
    reference: String,
    sha256: String,
    original: usize,
    candidate: usize,
    dragged_bases: i64,
    origin_x: f32,
}

fn frame_offset(definition: &Value) -> usize {
    definition["codon_start"].as_u64().unwrap_or(1).clamp(1, 3) as usize - 1
}

fn dragged_frame(original: usize, horizontal_pixels: f32) -> usize {
    (original as i64 + (horizontal_pixels / 18.).round() as i64).rem_euclid(3) as usize
}

fn frame_action(reference: &str, sha256: &str, offset: usize) -> Action {
    Action::Write(
        "library.edit",
        json!({"ref":reference,"expected_sha256":sha256,"patch":{"frame_offset":offset}}),
    )
}

fn finish_frame(drag: FrameDrag) -> Option<Action> {
    (drag.candidate != drag.original)
        .then(|| frame_action(&drag.reference, &drag.sha256, drag.candidate))
}

struct ProteinBlockInput<'a> {
    sequence: &'a str,
    view: &'a Value,
    selected: Option<&'a Selection>,
    focus: Option<usize>,
    editable: bool,
    reference: &'a str,
    sha256: &'a str,
}

#[derive(Default)]
struct ProteinBlockOutcome {
    selection: Option<Selection>,
    apply_selection: bool,
    action: Option<Action>,
}

fn codon_alignment<'a>(view: &'a Value, sequence: &str) -> Option<CodonAlignment<'a>> {
    let source = &view["source"];
    let bases = source["sequence"].as_str()?;
    if source["complete"] != true
        || view["codon_positions_complete"] != true
        || text(source, "ref") != text(view, "parent_ref")
        || bases.is_empty()
        || !bases.is_ascii()
    {
        return None;
    }
    let positions: Option<Vec<_>> = view["codon_positions"]
        .as_array()?
        .iter()
        .map(|row| {
            let row = row.as_array()?;
            if row.len() != 3 {
                return None;
            }
            let positions = [
                row[0].as_u64()? as usize,
                row[1].as_u64()? as usize,
                row[2].as_u64()? as usize,
            ];
            positions
                .iter()
                .all(|&p| p < bases.len())
                .then_some(positions)
        })
        .collect();
    let positions = positions?;
    if positions.len() != sequence.len() {
        return None;
    }
    Some(CodonAlignment {
        source: bases,
        positions,
        reverse: view["translation"]["strand"].as_i64() == Some(-1),
        rna: text(source, "molecule_type") == "rna",
    })
}

fn source_footprint(view: &Value) -> Option<CodonAlignment<'_>> {
    let source = &view["source"];
    let bases = source["sequence"].as_str()?;
    if source["complete"] != true
        || bases.is_empty()
        || !bases.is_ascii()
        || text(source, "ref") != text(view, "parent_ref")
    {
        return None;
    }
    let definition = &view["translation"];
    let reverse = definition["strand"].as_i64() == Some(-1);
    let mut positions = Vec::new();
    for segment in rows(definition, "segments") {
        let start = segment["start"].as_u64()? as usize;
        let end = segment["end"].as_u64()? as usize;
        if start >= end || end > bases.len() {
            return None;
        }
        if reverse {
            positions.extend((start..end).rev());
        } else {
            positions.extend(start..end);
        }
        if positions.len() > 1_000_000 {
            return None;
        }
    }
    let skip = frame_offset(definition);
    let positions = positions
        .get(skip..)?
        .chunks_exact(3)
        .take(16_384)
        .map(|p| [p[0], p[1], p[2]])
        .collect::<Vec<_>>();
    (!positions.is_empty()).then_some(CodonAlignment {
        source: bases,
        positions,
        reverse,
        rna: text(source, "molecule_type") == "rna",
    })
}

fn aligned_bases(alignment: &CodonAlignment<'_>, index: usize) -> [u8; 3] {
    alignment.positions[index].map(|position| {
        let base = alignment.source.as_bytes()[position];
        if alignment.reverse {
            let base = complement(base);
            if alignment.rna && base == b'T' {
                b'U'
            } else {
                base
            }
        } else {
            base
        }
    })
}

fn selected_residue(selection: Option<&Selection>, index: usize) -> bool {
    selection.is_some_and(|s| {
        s.segments
            .iter()
            .any(|&(start, end)| start <= index && index < end)
    })
}

fn protein_lines(
    ui: &mut egui::Ui,
    input: ProteinBlockInput<'_>,
    anchor: &mut Option<usize>,
    frame_drag: &mut Option<FrameDrag>,
) -> ProteinBlockOutcome {
    let ProteinBlockInput {
        sequence,
        view,
        selected,
        focus,
        editable,
        reference,
        sha256,
    } = input;
    let mut alignment = codon_alignment(view, sequence);
    let mut placeholder = String::new();
    let unavailable = sequence.is_empty();
    if unavailable {
        alignment = source_footprint(view);
        if let Some(alignment) = &alignment {
            placeholder = "?".repeat(alignment.positions.len());
            ui.colored_label(
                AMBER,
                "Translation unavailable · showing the uncropped source footprint.",
            );
        } else {
            ui.weak("No available sequence or source footprint for this revision.");
            return ProteinBlockOutcome::default();
        }
    }
    let sequence = if unavailable {
        placeholder.as_str()
    } else {
        sequence
    };
    let cell_width = if alignment.is_some() { 54. } else { 18. };
    let row_height = if alignment.is_some() { 76. } else { 33. };
    let width = ((ui.available_width() - 65.) / cell_width).floor().max(1.) as usize;
    let count = sequence.len().div_ceil(width);
    let mut outcome = ProteinBlockOutcome::default();
    if let Some(alignment) = &alignment {
        ui.horizontal_wrapped(|ui| {
            ui.weak(if alignment.reverse {
                "Source 5' -> 3' · reverse complement"
            } else {
                "Source 5' -> 3' · forward strand"
            });
            ui.weak("Each amino-acid block spans its three source bases.");
        });
    } else if text(view, "derivation_kind") == "derived" {
        ui.weak("Source codon alignment is unavailable for this revision.");
    }
    let can_drag_frame = text(view, "derivation_kind") == "derived"
        && alignment.is_some()
        && editable
        && !sha256.is_empty();
    if can_drag_frame {
        ui.weak("Drag the amino-acid row left or right to change the protein frame. Shift-drag selects a variant span.");
    } else {
        ui.weak("Click or drag residue blocks to select a variant span.");
    }
    if let Some(drag) = frame_drag.as_ref() {
        ui.colored_label(
            AMBER,
            format!(
                "Frame {}{} · release to translate and save",
                if view["translation"]["strand"].as_i64() == Some(-1) {
                    "−"
                } else {
                    "+"
                },
                drag.candidate + 1
            ),
        );
    }
    let mut scroll = egui::ScrollArea::vertical()
        .id_salt("protein-sequence")
        .max_height(410.);
    if let Some(focus) = focus {
        scroll = scroll.vertical_scroll_offset(
            (focus.min(sequence.len().saturating_sub(1)) / width).saturating_sub(1) as f32
                * row_height,
        );
    }
    scroll.show_rows(ui, row_height, count, |ui, range| {
        for row in range {
            let start = row * width;
            let end = (start + width).min(sequence.len());
            let (rect, response) = ui.allocate_exact_size(
                Vec2::new(ui.available_width(), row_height),
                Sense::click_and_drag(),
            );
            let painter = ui.painter_at(rect);
            let left = rect.left() + 55.;
            let aa_y = rect.top() + if alignment.is_some() { 39. } else { 3. };
            painter.text(
                Pos2::new(left - 9., aa_y + 13.),
                Align2::RIGHT_CENTER,
                (start + 1).to_string(),
                FontId::monospace(11.),
                Color32::GRAY,
            );
            for index in start..end {
                let x = left + (index - start) as f32 * cell_width;
                let active = !unavailable && selected_residue(selected, index);
                if let Some(alignment) = &alignment {
                    let positions = alignment.positions[index];
                    painter.text(
                        Pos2::new(x + 2., rect.top() + 8.),
                        Align2::LEFT_CENTER,
                        (positions[0] + 1).to_string(),
                        FontId::monospace(9.),
                        Color32::GRAY,
                    );
                    for (offset, base) in aligned_bases(alignment, index).into_iter().enumerate() {
                        letter_block(
                            &painter,
                            Rect::from_min_size(
                                Pos2::new(x + offset as f32 * 18., rect.top() + 14.),
                                Vec2::new(18., 23.),
                            ),
                            base,
                            base_color(base),
                            active,
                        );
                    }
                    if index > 0 {
                        let previous = alignment.positions[index - 1][2] as i64;
                        let step = if alignment.reverse { -1 } else { 1 };
                        if positions[0] as i64 != previous + step {
                            painter.line_segment(
                                [
                                    Pos2::new(x, rect.top() + 10.),
                                    Pos2::new(x, rect.bottom() - 4.),
                                ],
                                Stroke::new(2., AMBER),
                            );
                        }
                    }
                }
                let pending = frame_drag
                    .as_ref()
                    .filter(|drag| drag.candidate != drag.original);
                let shift = pending.map_or(0., |drag| drag.dragged_bases as f32 * 18.);
                let amino = if pending.is_some() {
                    b'?'
                } else {
                    sequence.as_bytes()[index]
                };
                letter_block(
                    &painter,
                    Rect::from_min_size(Pos2::new(x + shift, aa_y), Vec2::new(cell_width, 27.)),
                    amino,
                    amino_color(amino),
                    active,
                );
            }
            let residue_at = |position: Pos2| {
                let column = ((position.x - left) / cell_width)
                    .floor()
                    .clamp(0., width.saturating_sub(1) as f32) as i64;
                let row_offset = ((position.y - rect.top()) / row_height).floor() as i64;
                (start as i64 + row_offset * width as i64 + column)
                    .clamp(0, sequence.len().saturating_sub(1) as i64) as usize
            };
            if response.drag_started_by(egui::PointerButton::Primary)
                && let Some(position) = response.interact_pointer_pos()
            {
                let start_position = position - response.total_drag_delta().unwrap_or_default();
                if can_drag_frame && start_position.y >= aa_y && !ui.input(|i| i.modifiers.shift) {
                    let original = frame_offset(&view["translation"]);
                    *frame_drag = Some(FrameDrag {
                        reference: reference.into(),
                        sha256: sha256.into(),
                        original,
                        candidate: original,
                        dragged_bases: 0,
                        origin_x: start_position.x,
                    });
                    *anchor = None;
                } else if !unavailable {
                    *anchor = Some(residue_at(start_position));
                }
            }
            if response.dragged_by(egui::PointerButton::Primary)
                && let Some(drag) = frame_drag.as_mut()
            {
                let delta = response.total_drag_delta().unwrap_or_default().x;
                drag.candidate = dragged_frame(drag.original, delta);
                drag.dragged_bases = (delta / 18.).round() as i64;
            }
            if !unavailable
                && response.clicked()
                && let Some(position) = response.interact_pointer_pos()
            {
                let index = residue_at(position);
                outcome.selection = Some(Selection {
                    segments: vec![(index, index + 1)],
                    strand: 1,
                    label: "Selected residue".into(),
                    ..Default::default()
                });
                outcome.apply_selection = true;
            } else if frame_drag.is_none()
                && response.dragged_by(egui::PointerButton::Primary)
                && let (Some(first), Some(position)) = (*anchor, response.interact_pointer_pos())
            {
                let last = residue_at(position);
                outcome.selection = Some(Selection {
                    segments: vec![(first.min(last), first.max(last) + 1)],
                    strand: 1,
                    label: "Selected residues".into(),
                    ..Default::default()
                });
            }
            if let Some(position) = response.hover_pos() {
                let index = residue_at(position);
                let mut tooltip = format!(
                    "Residue {} · {}",
                    index + 1,
                    sequence.as_bytes()[index] as char
                );
                if let Some(alignment) = &alignment {
                    let p = alignment.positions[index];
                    tooltip.push_str(&format!(
                        "\nSource bases {}, {}, {}",
                        p[0] + 1,
                        p[1] + 1,
                        p[2] + 1
                    ));
                }
                response.on_hover_text(tooltip);
            }
        }
    });
    if !ui.input(|input| input.pointer.primary_down()) {
        if let Some(drag) = frame_drag.as_mut()
            && let Some(position) = ui.input(|input| input.pointer.interact_pos())
        {
            let delta = position.x - drag.origin_x;
            drag.candidate = dragged_frame(drag.original, delta);
            drag.dragged_bases = (delta / 18.).round() as i64;
        }
        if anchor.is_some()
            && frame_drag.is_none()
            && ui.input(|input| input.pointer.button_released(egui::PointerButton::Primary))
        {
            outcome.selection = selected.cloned();
            outcome.apply_selection = true;
        }
        *anchor = None;
        if let Some(drag) = frame_drag.take()
            && editable
            && reference == drag.reference
            && sha256 == drag.sha256
            && ui.input(|input| input.pointer.button_released(egui::PointerButton::Primary))
        {
            outcome.action = finish_frame(drag);
        }
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(zoom: f32, mode: usize, center: f32) -> MapGeometry {
        MapGeometry::new(
            Rect::from_min_size(Pos2::ZERO, Vec2::new(900., 420.)),
            1000,
            center,
            zoom,
            mode,
            true,
        )
    }

    #[test]
    fn circular_zoom_scales_the_arc_without_moving_its_focal_base() {
        let fitted = map(1., 1, 250.);
        let zoomed = map(1.25, 1, 250.);
        assert_eq!(fitted.position(250., 0.), zoomed.position(250., 0.));
        assert!((zoomed.scale / fitted.scale - 1.25).abs() < 0.0001);
        assert!(
            fitted
                .position(400., 0.)
                .distance(zoomed.position(400., 0.))
                > 20.
        );
        assert_eq!(
            map(5., 1, 250.).curve,
            1.,
            "explicit Circular keeps its curvature while enlarging"
        );
    }

    #[test]
    fn auto_unroll_preserves_focal_base_scale_and_position_continuity() {
        let mut previous = map(1., 0, 990.);
        for index in 1..=300 {
            let zoom = 1. + index as f32 / 100.;
            let current = map(zoom, 0, 990.);
            assert_eq!(current.position(990., 0.), previous.position(990., 0.));
            assert!(current.curve <= previous.curve);
            assert!(current.scale >= previous.scale);
            assert!(
                current
                    .position(1020., 22.)
                    .distance(previous.position(1020., 22.))
                    < 3.
            );
            previous = current;
        }
        let before = map(3.9999, 0, 990.);
        let after = map(4., 0, 990.);
        assert!(
            before
                .position(1020., 22.)
                .distance(after.position(1020., 22.))
                < 0.01
        );
        assert_eq!(after.curve, 0.);
        assert_eq!(
            after.position(1020., 22.),
            map(4., 2, 990.).position(1020., 22.)
        );
    }

    #[test]
    fn auto_zoom_scale_is_monotonic_on_narrow_and_wide_viewports() {
        for width in [300., 450., 700., 900., 1800.] {
            let rect = Rect::from_min_size(Pos2::ZERO, Vec2::new(width, 420.));
            let mut previous = MapGeometry::new(rect, 1000, 0., 1., 0, true);
            let (left, right) = previous.window();
            assert!(
                previous
                    .position(left, 0.)
                    .distance(previous.position(right, 0.))
                    < 0.001,
                "fitted ring is closed"
            );
            for index in 1..=500 {
                let zoom = 1. + index as f32 / 100.;
                let current = MapGeometry::new(rect, 1000, 0., zoom, 0, true);
                assert!(
                    current.scale > previous.scale,
                    "zoom must enlarge bases at width {width}, zoom {zoom}"
                );
                previous = current;
            }
        }
    }

    #[test]
    fn map_hit_testing_and_feature_spans_follow_rotation_across_origin() {
        for mode in 0..=2 {
            for zoom in [1., 1.6, 2.5, 4., 10.] {
                let geometry = map(zoom, mode, 990.);
                for base in [970usize, 990, 1010, 1040] {
                    let point = geometry.position(base as f32 + 0.25, 14.);
                    assert_eq!(
                        geometry.base_at(point),
                        base % 1000,
                        "mode {mode}, zoom {zoom}, base {base}"
                    );
                }
            }
        }
        let geometry = map(4., 0, 0.);
        assert_eq!(geometry.spans(980, 1000), vec![(-20., 0.)]);
        assert_eq!(geometry.spans(0, 20), vec![(0., 20.)]);
        assert_eq!(map(4., 0, -10.).center, 990.);
        let linear = MapGeometry::new(geometry.rect, 1000, -10., 4., 2, false);
        assert!(linear.center >= 125.);
        assert!(linear.spans(980, 1000).is_empty());
    }

    fn screen() -> egui::RawInput {
        egui::RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, Vec2::new(700., 600.))),
            ..Default::default()
        }
    }

    #[test]
    fn map_wheel_routes_pan_and_modified_zoom_only_inside_its_viewport() {
        let context = egui::Context::default();
        let reference = "construct:wheel-proof@1";
        let mut viewer = Viewer::default();
        viewer.accept(
            reference,
            json!({"ref":reference,"molecule_type":"dna","length":1000,"circular":true}),
            &"ACGT".repeat(250),
        );
        let detail = json!({"ref":reference,"is_latest":true});
        let draw = |viewer: &mut Viewer, input| {
            context.run(input, |context| {
                egui::CentralPanel::default().show(context, |ui| {
                    viewer.show(ui, &detail, false, &[]);
                });
            })
        };
        let first = draw(&mut viewer, screen());
        let rect = first
            .shapes
            .iter()
            .find_map(|shape| match &shape.shape {
                egui::Shape::Rect(rect) if rect.fill == Color32::from_rgb(25, 29, 32) => {
                    Some(rect.rect)
                }
                _ => None,
            })
            .expect("map background is rendered");
        let wheel = |pos, modifiers| {
            let mut input = screen();
            input.modifiers = modifiers;
            input.events = vec![
                egui::Event::PointerMoved(pos),
                egui::Event::MouseWheel {
                    unit: egui::MouseWheelUnit::Point,
                    delta: Vec2::new(0., 4.),
                    modifiers,
                },
            ];
            input
        };
        let before = (viewer.center, viewer.zoom, context.zoom_factor());
        draw(&mut viewer, wheel(rect.center(), egui::Modifiers::NONE));
        assert_ne!(viewer.center, before.0);
        assert_eq!(
            viewer.zoom, before.1,
            "ordinary wheel rotates without zooming"
        );
        assert_eq!(context.input(|input| input.smooth_scroll_delta), Vec2::ZERO);
        let center = viewer.center;
        draw(
            &mut viewer,
            wheel(
                rect.center(),
                egui::Modifiers {
                    ctrl: true,
                    command: true,
                    ..Default::default()
                },
            ),
        );
        assert_eq!(viewer.center, center);
        assert!(viewer.zoom > before.1);
        assert_eq!(
            context.zoom_factor(),
            before.2,
            "map zoom must not resize the GUI"
        );
        let before = (viewer.center, viewer.zoom);
        draw(&mut viewer, wheel(Pos2::new(2., 2.), egui::Modifiers::NONE));
        assert_eq!((viewer.center, viewer.zoom), before);
        assert!(
            context.input(|input| input.smooth_scroll_delta.y) > 0.,
            "outside scrolling remains available to the page"
        );
        viewer.mode = 2;
        viewer.zoom = 4.;
        let center = viewer.center;
        draw(&mut viewer, wheel(rect.center(), egui::Modifiers::NONE));
        assert_ne!(viewer.center, center);
        assert_eq!(viewer.zoom, 4., "ordinary wheel pans the linear view");
    }

    fn painted_text(shapes: &[egui::epaint::ClippedShape]) -> Vec<String> {
        fn collect(shape: &egui::Shape, result: &mut Vec<String>) {
            match shape {
                egui::Shape::Text(text) => result.push(text.galley.job.text.clone()),
                egui::Shape::Vec(shapes) => {
                    for shape in shapes {
                        collect(shape, result);
                    }
                }
                _ => {}
            }
        }
        let mut result = Vec::new();
        for shape in shapes {
            collect(&shape.shape, &mut result);
        }
        result
    }

    #[test]
    fn plaintext_cross_row_copy_contains_only_selected_residues() {
        let context = egui::Context::default();
        let sequence = "ACDEFGHIKLMNPQRSTVWY".repeat(20);
        let mut selected = 0..0;
        let first = context.run(screen(), |context| {
            egui::CentralPanel::default().show(context, |ui| {
                ui.set_max_width(280.);
                let mut output = protein_plaintext(ui, &sequence, "construct:copy-proof@1");
                assert_eq!(output.galley.job.text, sequence);
                assert!(output.galley.rows.len() > 3);
                assert!(output.galley.rows.iter().all(|row| !row.ends_with_newline));
                let first_row = output.galley.rows[0].char_count_excluding_newline();
                selected = first_row - 3..first_row * 2 + 4;
                output
                    .state
                    .cursor
                    .set_char_range(Some(egui::text::CCursorRange::two(
                        egui::text::CCursor::new(selected.start),
                        egui::text::CCursor::new(selected.end),
                    )));
                output.response.request_focus();
                output.state.store(context, output.response.id);
            });
        });
        assert!(
            painted_text(&first.shapes).contains(&"1".into()),
            "numbered gutter is painted separately"
        );
        let mut input = screen();
        input.events.push(egui::Event::Copy);
        let copied = context.run(input, |context| {
            egui::CentralPanel::default().show(context, |ui| {
                ui.set_max_width(280.);
                protein_plaintext(ui, &sequence, "construct:copy-proof@1");
            });
        });
        let text = copied
            .platform_output
            .commands
            .iter()
            .find_map(|command| match command {
                egui::OutputCommand::CopyText(text) => Some(text.as_str()),
                _ => None,
            })
            .expect("the actual immutable text widget handles Copy");
        assert_eq!(text, &sequence[selected]);
        assert!(text.bytes().all(|byte| byte.is_ascii_uppercase()));
        assert!(!text.contains(['\n', '\r']));
    }

    #[test]
    fn protein_sequence_and_revision_controls_show_with_variant_closed() {
        for derived in [false, true] {
            let mut viewer = Viewer::default();
            let reference = "construct:plain-proof@1";
            viewer.accept(
                reference,
                json!({
                    "ref":reference,"molecule_type":"protein","length":3,"sequence":"MAG",
                    "derivation_kind":if derived {"derived"} else {"literal"},
                    "parent_ref":"construct:parent@1"
                }),
                "MAG",
            );
            let context = egui::Context::default();
            let output = context.run(screen(), |context| {
                egui::CentralPanel::default().show(context, |ui| {
                    assert!(
                        viewer
                            .show(ui, &json!({"ref":reference,"is_latest":true}), false, &[])
                            .is_empty()
                    );
                });
            });
            let labels = painted_text(&output.shapes);
            for label in ["Protein sequence", "MAG", "Copy sequence", "Copy FASTA"] {
                assert!(
                    labels.iter().any(|painted| painted == label),
                    "missing {label}: {labels:?}"
                );
            }
            assert!(labels.iter().any(|label| label
                == if derived {
                    "Edit definition"
                } else {
                    "Edit sequence"
                }));
            assert_eq!(labels.iter().any(|label| label == "Open parent"), derived);
            assert!(!labels.iter().any(|label| label == "PROTEIN ANNOTATIONS"));
            assert!(!viewer.variant_open);
        }
    }

    #[test]
    fn automatic_orf_rescan_debounces_and_rejects_previous_reply() {
        let reference = "construct:scan-proof@1";
        let mut viewer = Viewer::default();
        viewer.accept(
            reference,
            json!({"ref":reference,"molecule_type":"dna","length":12}),
            "ATGGCTGGGTAA",
        );
        viewer.schedule_options(1.);
        assert!(viewer.poll_options(1.2).is_none());
        viewer.min_orf = 5;
        viewer.schedule_options(1.2);
        assert!(viewer.poll_options(1.4).is_none());
        let Some(Action::Options(first)) = viewer.poll_options(1.51) else {
            panic!("settled options must refresh");
        };
        assert_eq!(first["min_orf_aa"], 5);
        assert!(
            viewer.poll_options(2.).is_none(),
            "one request per settled change"
        );
        viewer.genetic_code = 11;
        viewer.schedule_options(2.);
        viewer.received_options(&first, json!({"ref":reference,"sequence":"STALE"}), "");
        assert_eq!(text(&viewer.view, "sequence"), "ATGGCTGGGTAA");
        let Some(Action::Options(second)) = viewer.poll_options(2.31) else {
            panic!("updated code must refresh");
        };
        assert_eq!(second["genetic_code"], 11);
        viewer.accept(
            "construct:other@1",
            json!({"ref":"construct:other@1","length":3}),
            "ATG",
        );
        viewer.received_options(&second, json!({"ref":reference,"sequence":"STALE"}), "");
        assert_eq!(viewer.reference, "construct:other@1");
        assert!(viewer.options_due.is_none());
    }
    #[test]
    fn wrap_selection_retains_origin_order() {
        assert_eq!(selection_ranges(95, 4, 100, true), vec![(95, 100), (0, 5)]);
        assert_eq!(selection_ranges(95, 4, 100, false), vec![(4, 96)]);
    }
    #[test]
    fn biological_parts_and_residue_crop_roundtrip() {
        let t = json!({"schema":1,"segments":[{"start":80,"end":100},{"start":10,"end":40}],"strand":-1,"genetic_code":11,"codon_start":2,"initiation":"cds","residue_start":3,"residue_end":9});
        let e = ProteinEditor::new("construct:parent@2", &t);
        assert_eq!(e.segments, vec![(81, 100), (11, 40)]);
        assert_eq!(e.definition().unwrap(), t);
    }
    #[test]
    fn stale_preview_cannot_replace_new_definition() {
        let t = json!({"segments":[{"start":0,"end":9}],"strand":1,"initiation":"cds"});
        let mut v = Viewer {
            editor: Some(ProteinEditor::new("construct:p@1", &t)),
            ..Default::default()
        };
        let old = v.editor.as_ref().unwrap().params().unwrap();
        v.editor.as_mut().unwrap().parent = "construct:p@2".into();
        v.editor.as_mut().unwrap().requested = v.editor.as_ref().unwrap().params().unwrap();
        v.received_preview(&old, json!({"available":true,"sequence":"MK"}));
        assert!(v.editor.as_ref().unwrap().preview.is_null());
    }
    #[test]
    fn malformed_features_do_not_draw_outside_sequence() {
        let v = json!({"length":10,"features":[{"label":"valid","segments":[{"start":0,"end":3}]},{"label":"bad","segments":[{"start":9,"end":11}]}]});
        assert_eq!(track_items(&v, true, false).len(), 1);
    }
    #[test]
    fn standard_codon_text_and_unknowns() {
        assert_eq!(codon(b"ATG"), b'M');
        assert_eq!(codon(b"UGA"), b'*');
        assert_eq!(codon(b"GCT"), b'A');
        assert_eq!(codon(b"NNN"), b'X');
    }
    #[test]
    fn range_variant_offsets_from_existing_crop() {
        let t = json!({"residue_start":10,"residue_end":50});
        assert_eq!(
            cropped_variant(&t, (2, 8)),
            json!({"residue_start":12,"residue_end":18})
        );
    }
    #[test]
    fn unrelated_write_preserves_definition_draft_and_rebases_receipt() {
        let t = json!({"schema":1,"segments":[{"start":0,"end":300}],"strand":1,"genetic_code":1,"codon_start":1,"initiation":"cds","residue_start":0,"residue_end":null});
        let mut editor = ProteinEditor::new("construct:parent@1", &t);
        editor.target = Some(("construct:child@1".into(), "oldsha".into()));
        editor.first_residue = 4;
        let mut viewer = Viewer {
            reference: "construct:child@1".into(),
            editor: Some(editor),
            ..Default::default()
        };
        viewer.write_received(&json!({"changed_refs":[{"before_ref":"construct:child@1","after_ref":"construct:child@2"}]}),&json!({"ref":"construct:other@1","patch":{"archived":true}}));
        viewer.accept("construct:child@2",json!({"ref":"construct:child@2","derivation_kind":"derived","sequence":"AAAA","length":4}),"");
        viewer.refresh_receipt("construct:child@2", "newsha");
        let editor = viewer.editor.unwrap();
        assert_eq!(editor.first_residue, 4);
        assert_eq!(
            editor.target,
            Some(("construct:child@2".into(), "newsha".into()))
        );
    }

    fn aligned_view(sequence: &str, peptide: &str, positions: Value, strand: i64) -> Value {
        json!({"parent_ref":"construct:p@1","derivation_kind":"derived","sequence":peptide,
            "translation":{"schema":1,"segments":[{"start":0,"end":sequence.len()}],"strand":strand,"genetic_code":1,"codon_start":1,"initiation":"cds","residue_start":0,"residue_end":null},
            "source":{"ref":"construct:p@1","sequence":sequence,"molecule_type":"dna","complete":true},
            "codon_positions":positions,"codon_positions_complete":true})
    }

    #[test]
    fn initial_codon_alignment_preserves_authoritative_cds_initiation() {
        let view = aligned_view("TTGGCCTAA", "MA", json!([[0, 1, 2], [3, 4, 5]]), 1);
        let alignment = codon_alignment(&view, text(&view, "sequence")).unwrap();
        assert_eq!(aligned_bases(&alignment, 0), *b"TTG");
        assert_eq!(codon(&aligned_bases(&alignment, 0)), b'L');
        assert_eq!(text(&view, "sequence"), "MA");
        assert_eq!(aligned_bases(&alignment, 1), *b"GCC");
    }

    #[test]
    fn reverse_join_origin_and_cropped_codon_positions_stay_exact() {
        let reverse = aligned_view("CATGGCTAA", "ML", json!([[2, 1, 0], [8, 7, 6]]), -1);
        let alignment = codon_alignment(&reverse, "ML").unwrap();
        assert_eq!(aligned_bases(&alignment, 0), *b"ATG");
        assert_eq!(aligned_bases(&alignment, 1), *b"TTA");
        let origin = aligned_view("CATGGCTAA", "T", json!([[8, 0, 1]]), 1);
        assert_eq!(
            aligned_bases(&codon_alignment(&origin, "T").unwrap(), 0),
            *b"ACA"
        );
        let mut crop = aligned_view("ATGGCCTAA", "A", json!([[3, 4, 5]]), 1);
        crop["translation"]["residue_start"] = json!(1);
        assert_eq!(
            aligned_bases(&codon_alignment(&crop, "A").unwrap(), 0),
            *b"GCC"
        );
    }

    #[test]
    fn missing_mismatched_or_partial_source_never_invents_codon_alignment() {
        let view = aligned_view("ATGTAA", "M", json!([[0, 1, 2]]), 1);
        for change in [
            json!({"field":"source", "value":{"ref":"construct:p@2","sequence":"ATGTAA","complete":true}}),
            json!({"field":"source", "value":{"ref":"construct:p@1","sequence":"ATGTAA","complete":false}}),
            json!({"field":"codon_positions", "value":[[0,1,6]]}),
            json!({"field":"codon_positions", "value":[]}),
            json!({"field":"codon_positions_complete", "value":false}),
        ] {
            let mut changed = view.clone();
            changed[text(&change, "field")] = change["value"].clone();
            assert!(codon_alignment(&changed, "M").is_none());
        }
        assert!(
            codon_alignment(&json!({"sequence":"M","derivation_kind":"explicit"}), "M").is_none()
        );
    }

    #[test]
    fn unavailable_product_can_still_show_exact_uncropped_source_footprint() {
        let mut view = aligned_view("ATGAATAAATAA", "", json!([]), 1);
        view["translation"]["codon_start"] = json!(3);
        view["translation"]["residue_start"] = json!(900);
        let footprint = source_footprint(&view).unwrap();
        assert_eq!(footprint.positions[0], [2, 3, 4]);
        assert_eq!(aligned_bases(&footprint, 0), *b"GAA");
        assert_eq!(text(&view, "sequence"), "");
    }

    #[test]
    fn first_stop_definition_and_variant_keep_the_saved_policy() {
        let definition = json!({"schema":2,"segments":[{"start":10,"end":40}],"strand":-1,"genetic_code":11,"codon_start":2,"initiation":"literal","stop_policy":"first_stop","residue_start":1,"residue_end":7});
        assert_eq!(
            ProteinEditor::new("construct:p@1", &definition)
                .definition()
                .unwrap(),
            definition
        );
        let variant = cropped_variant(&definition, (1, 4));
        assert_eq!(variant["stop_policy"], "first_stop");
        assert_eq!(variant["schema"], 2);
        assert_eq!(variant["residue_start"], 2);
        assert_eq!(variant["residue_end"], 5);
    }

    #[test]
    fn frame_drag_wraps_in_biological_direction_and_same_frame_is_a_noop() {
        assert_eq!(dragged_frame(0, 18.), 1);
        assert_eq!(dragged_frame(0, -18.), 2);
        assert_eq!(dragged_frame(2, 18.), 0);
        assert_eq!(dragged_frame(1, 54.), 1);
        let drag = FrameDrag {
            reference: "construct:protein@3".into(),
            sha256: "pinned-digest".into(),
            original: 1,
            candidate: 1,
            dragged_bases: 0,
            origin_x: 0.,
        };
        assert!(finish_frame(drag.clone()).is_none());
        assert_eq!(
            finish_frame(FrameDrag {
                candidate: 2,
                ..drag
            }),
            Some(Action::Write(
                "library.edit",
                json!({"ref":"construct:protein@3","expected_sha256":"pinned-digest","patch":{"frame_offset":2}})
            ))
        );
    }

    #[test]
    fn new_revision_cancels_a_drag_that_began_on_old_source() {
        let mut viewer = Viewer {
            reference: "construct:protein@1".into(),
            frame_drag: Some(FrameDrag {
                reference: "construct:protein@1".into(),
                sha256: "old".into(),
                original: 0,
                candidate: 1,
                dragged_bases: 1,
                origin_x: 0.,
            }),
            ..Default::default()
        };
        viewer.accept("construct:protein@2", json!({"ref":"construct:protein@2","sequence":"M","length":1,"derivation_kind":"derived"}), "");
        assert!(viewer.frame_drag.is_none());
    }

    #[test]
    fn changing_selected_cds_updates_only_the_open_variant_form_and_preview() {
        let mut view = aligned_view(
            "ATGATGATGATGATGATGTAA",
            "MMMMMM",
            json!([
                [0, 1, 2],
                [3, 4, 5],
                [6, 7, 8],
                [9, 10, 11],
                [12, 13, 14],
                [15, 16, 17]
            ]),
            1,
        );
        view["ref"] = json!("construct:protein@1");
        let detail = json!({"ref":"construct:protein@1","sha256":"source-digest"});
        let mut viewer = Viewer::default();
        viewer.accept("construct:protein@1", view.clone(), "");
        viewer.begin_definition(&detail, view["translation"].clone(), false);
        viewer.editor.as_mut().unwrap().alt_name = "Chosen variant name".into();
        let first = Selection {
            label: "CDS A".into(),
            segments: vec![(0, 2)],
            ..Default::default()
        };
        let second = Selection {
            label: "CDS B".into(),
            segments: vec![(3, 6)],
            ..Default::default()
        };
        assert!(matches!(
            viewer.select_variant_span(&detail, first),
            Some(Action::Preview(_))
        ));
        let first_request = viewer.editor.as_ref().unwrap().requested.clone();
        assert!(matches!(
            viewer.select_variant_span(&detail, second),
            Some(Action::Preview(_))
        ));
        let editor = viewer.editor.as_ref().unwrap();
        assert_eq!(editor.first_residue, 4);
        assert_eq!(editor.last_residue, "6");
        assert_eq!(editor.alt_name, "Chosen variant name");
        assert_ne!(editor.requested, first_request);
        assert_eq!(viewer.focus_residue, Some(3));
        assert!(selected_residue(viewer.selection.as_ref(), 4));
        assert!(!selected_residue(viewer.selection.as_ref(), 0));
        assert_eq!(viewer.view, view);
    }

    #[test]
    fn saved_frame_refreshes_variant_definition_and_rejects_old_preview() {
        let mut old_view = aligned_view(
            "ATGAATAAATAA",
            "MNK",
            json!([[0, 1, 2], [3, 4, 5], [6, 7, 8]]),
            1,
        );
        old_view["ref"] = json!("construct:protein@1");
        let mut viewer = Viewer::default();
        viewer.accept("construct:protein@1", old_view.clone(), "");
        let detail = json!({"ref":"construct:protein@1","sha256":"old-digest"});
        viewer.begin_definition(&detail, old_view["translation"].clone(), false);
        let editor = viewer.editor.as_mut().unwrap();
        editor.alt_name = "Keep this variant name".into();
        let old_params = editor.requested.clone();
        editor.preview = json!({"available":true,"sequence":"MNK"});
        editor.preview_for = old_params.clone();
        viewer.write_received(&json!({"changed_refs":[{"before_ref":"construct:protein@1","after_ref":"construct:protein@2"}]}), &json!({"ref":"construct:protein@1","patch":{"frame_offset":2}}));
        assert!(viewer.editor.as_ref().unwrap().preview.is_null());
        let mut next = aligned_view("ATGAATAAATAA", "E", json!([[2, 3, 4]]), 1);
        next["ref"] = json!("construct:protein@2");
        next["translation"]["schema"] = json!(2);
        next["translation"]["codon_start"] = json!(3);
        next["translation"]["initiation"] = json!("literal");
        next["translation"]["stop_policy"] = json!("first_stop");
        viewer.accept("construct:protein@2", next.clone(), "");
        viewer.received_preview(&old_params, json!({"available":true,"sequence":"MNK"}));
        let editor = viewer.editor.as_ref().unwrap();
        assert_eq!(editor.definition().unwrap(), next["translation"]);
        assert_eq!(editor.alt_name, "Keep this variant name");
        assert!(editor.preview.is_null());
        assert!(viewer.pending_preview.is_some());
        assert!(viewer.variant_open);
    }
}
