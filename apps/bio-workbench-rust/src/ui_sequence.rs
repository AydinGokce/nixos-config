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
            self.center = view["length"].as_u64().unwrap_or(literal.len() as u64) as f32 / 2.;
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
        ui.horizontal_wrapped(|ui| {
            ui.selectable_value(&mut self.mode,0,"Auto");ui.selectable_value(&mut self.mode,1,"Circular");ui.selectable_value(&mut self.mode,2,"Linear");
            if ui.button("Fit").clicked(){self.zoom=1.;self.center=length as f32/2.;}
            ui.label("Zoom");ui.add(egui::Slider::new(&mut self.zoom,1.0..=max_zoom).logarithmic(true).show_value(false).custom_formatter(|v,_|format!("{v:.1}×")));
            ui.checkbox(&mut self.show_features,"Annotations");ui.checkbox(&mut self.show_orfs,"ORFs");
            ui.label("Min ORF");ui.add(egui::DragValue::new(&mut self.min_orf).range(1..=10000));ui.weak("aa");
            egui::ComboBox::from_id_salt("map-code").selected_text(format!("Code {}",self.genetic_code)).show_ui(ui,|ui|{ui.selectable_value(&mut self.genetic_code,1,"1 · Standard");ui.selectable_value(&mut self.genetic_code,11,"11 · Bacterial");});
            egui::ComboBox::from_id_salt("map-reading-frame").selected_text(format!("Preview frame {:+}", self.translation_frame)).show_ui(ui, |ui| {
                for frame in [1, 2, 3, -1, -2, -3] {
                    ui.selectable_value(&mut self.translation_frame, frame, format!("{frame:+}"));
                }
            });
            if ui.button("Find ORFs").clicked(){self.show_orfs=true;let params=json!({"ref":self.reference,"min_orf_aa":self.min_orf,"genetic_code":self.genetic_code});self.requested_options=params.clone();actions.push(Action::Options(params));}
        });
        let circular =
            self.mode == 1 || self.mode == 0 && self.view["circular"] == true && self.zoom < 1.6;
        ui.weak(if circular{"Scroll to zoom into the linear sequence; click a feature or drag a range. Coordinates are 1-based."}else{"Scroll to zoom; right-drag to pan; left-drag to select bases. One preview translation follows the selected reading frame."});
        let size = Vec2::new(
            ui.available_width().max(300.),
            if circular { 420. } else { 400. },
        );
        let (rect, response) = ui.allocate_exact_size(size, Sense::click_and_drag());
        let painter = ui.painter_at(rect);
        painter.rect_filled(rect, 3., Color32::from_rgb(25, 29, 32));
        let visible = (length as f32 / self.zoom).max(12.).min(length as f32);
        self.center = self
            .center
            .clamp(visible / 2., length as f32 - visible / 2.);
        let left = (self.center - visible / 2.).max(0.);
        let plot = Rect::from_min_max(
            rect.min + Vec2::new(50., 35.),
            rect.max - Vec2::new(24., 20.),
        );
        let items = track_items(&self.view, self.show_features, self.show_orfs);
        let pointer = response
            .hover_pos()
            .or_else(|| response.interact_pointer_pos());
        let base_at = |p: Pos2| {
            if circular {
                circle_base(p, circle_center(rect), length)
            } else {
                (left + (p.x - plot.left()) / plot.width() * visible)
                    .floor()
                    .clamp(0., (length - 1) as f32) as usize
            }
        };
        if response.hovered() {
            let scroll = ui.input(|i| i.smooth_scroll_delta.y);
            if scroll.abs() > 0.1 {
                if circular && let Some(p) = pointer {
                    self.center = base_at(p) as f32;
                }
                self.zoom = (self.zoom * (scroll * 0.006).exp()).clamp(1., max_zoom);
                ui.input_mut(|i| {
                    i.smooth_scroll_delta = Vec2::ZERO;
                });
            }
        }
        if response.dragged_by(egui::PointerButton::Secondary) && !circular {
            self.center -= response.drag_delta().x / plot.width() * visible;
        }
        if response.drag_started_by(egui::PointerButton::Primary)
            && let Some(p) = pointer
        {
            self.anchor = Some(base_at(p));
        }
        if response.dragged_by(egui::PointerButton::Primary)
            && let (Some(start), Some(p)) = (self.anchor, pointer)
        {
            self.selection = Some(Selection {
                segments: selection_ranges(start, base_at(p), length, circular),
                strand: 1,
                label: "Selected range".into(),
                ..Default::default()
            });
        }
        if response.drag_stopped() {
            self.anchor = None;
        }
        let hit = if circular {
            draw_circle(
                &painter,
                rect,
                &items,
                self.selection.as_ref(),
                length,
                pointer,
            )
        } else {
            draw_linear(
                &painter,
                plot,
                &items,
                self.selection.as_ref(),
                &sequence,
                (left, visible, molecule == "rna", self.translation_frame),
                pointer,
            )
        };
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
                if ui.button("Zoom to selection").clicked(){let start=selection.segments.first().map(|p|p.0).unwrap_or(0);let end=selection.segments.last().map(|p|p.1).unwrap_or(length);if end>start{self.center=(start+end)as f32/2.;self.zoom=(length as f32/(end-start)as f32/1.2).clamp(1.,max_zoom);self.mode=2;}}
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
fn circle_base(p: Pos2, c: Pos2, length: usize) -> usize {
    let a = ((p.y - c.y).atan2(p.x - c.x) + std::f32::consts::FRAC_PI_2)
        .rem_euclid(std::f32::consts::TAU);
    ((a / std::f32::consts::TAU * length as f32).floor() as usize).min(length.saturating_sub(1))
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
fn arrow(painter: &egui::Painter, end: Pos2, direction: f32, color: Color32) {
    painter.add(egui::Shape::convex_polygon(
        vec![
            end + Vec2::new(5. * direction, 0.),
            end + Vec2::new(-4. * direction, -5.),
            end + Vec2::new(-4. * direction, 5.),
        ],
        color,
        Stroke::NONE,
    ));
}
fn circle_center(rect: Rect) -> Pos2 {
    if rect.width() > 760. {
        Pos2::new(rect.left() + rect.width() * 0.39, rect.center().y)
    } else {
        rect.center()
    }
}
fn draw_circle(
    p: &egui::Painter,
    rect: Rect,
    items: &[Selection],
    selected: Option<&Selection>,
    length: usize,
    pointer: Option<Pos2>,
) -> Option<Selection> {
    let c = circle_center(rect);
    let radius = (rect.height() / 2. - 55.).min(rect.width() / 2. - 95.);
    let tau = std::f32::consts::TAU;
    p.circle_stroke(c, radius, Stroke::new(2., Color32::from_rgb(109, 121, 129)));
    p.text(
        c - Vec2::new(0., 10.),
        Align2::CENTER_CENTER,
        format!("{length} bp"),
        FontId::proportional(22.),
        Color32::LIGHT_GRAY,
    );
    p.text(
        c + Vec2::new(0., 14.),
        Align2::CENTER_CENTER,
        "SEQUENCE MAP",
        FontId::monospace(11.),
        Color32::GRAY,
    );
    for i in 0..12 {
        let a = i as f32 / 12. * tau - std::f32::consts::FRAC_PI_2;
        let v = Vec2::angled(a);
        p.line_segment(
            [c + v * (radius - 4.), c + v * (radius + 5.)],
            Stroke::new(1., Color32::GRAY),
        );
        p.text(
            c + v * (radius + 35.),
            Align2::CENTER_CENTER,
            format!("{}", i * length / 12 + 1),
            FontId::monospace(10.),
            Color32::LIGHT_GRAY,
        );
    }
    let mut hit = None;
    let mut annotation_index = 0usize;
    for (i, item) in items.iter().enumerate() {
        if item.kind == "source" {
            continue;
        }
        let ring = if item.is_orf {
            radius - 66. - (i % 6) as f32 * 7.
        } else {
            let ring = radius - 14. - (annotation_index % 4) as f32 * 11.;
            annotation_index += 1;
            ring
        };
        let color = item_color(item, i);
        let active = selected.is_some_and(|s| s.segments == item.segments && s.label == item.label);
        for &(start, end) in &item.segments {
            let a = start as f32 / length as f32 * tau - std::f32::consts::FRAC_PI_2;
            let b = end as f32 / length as f32 * tau - std::f32::consts::FRAC_PI_2;
            let steps = (((b - a) * ring / 5.).ceil() as usize).clamp(2, 300);
            let points: Vec<_> = (0..=steps)
                .map(|j| c + Vec2::angled(a + (b - a) * j as f32 / steps as f32) * ring)
                .collect();
            p.add(egui::Shape::line(
                points,
                Stroke::new(
                    if active {
                        9.
                    } else if item.is_orf {
                        2.
                    } else {
                        6.
                    },
                    color.gamma_multiply(if item.is_orf { 0.6 } else { 1. }),
                ),
            ));
            let tip = if item.strand < 0 { a } else { b };
            let tang = tip
                + if item.strand < 0 {
                    -std::f32::consts::FRAC_PI_2
                } else {
                    std::f32::consts::FRAC_PI_2
                };
            let center = c + Vec2::angled(tip) * ring;
            let along = Vec2::angled(tang);
            let normal = Vec2::angled(tip);
            p.add(egui::Shape::convex_polygon(
                vec![
                    center + along * 5.,
                    center - along * 4. + normal * 5.,
                    center - along * 4. - normal * 5.,
                ],
                color,
                Stroke::NONE,
            ));
            if let Some(pos) = pointer {
                let base = circle_base(pos, c, length);
                if (pos.distance(c) - ring).abs() < 7. && base >= start && base < end {
                    hit = Some(item.clone());
                }
            }
        }
    }
    if rect.width() > 760. {
        let legend_x = rect.right() - 275.;
        let legend_y = rect.top() + 24.;
        p.text(
            Pos2::new(legend_x, legend_y),
            Align2::LEFT_TOP,
            "FEATURES",
            FontId::monospace(11.),
            Color32::GRAY,
        );
        for (i, item) in items
            .iter()
            .filter(|v| v.kind != "source" && !v.is_orf)
            .take(16)
            .enumerate()
        {
            let y = legend_y + 24. + i as f32 * 21.;
            let region = Rect::from_min_size(Pos2::new(legend_x, y), Vec2::new(260., 20.));
            p.rect_filled(
                Rect::from_min_size(Pos2::new(legend_x, y + 3.), Vec2::new(7., 7.)),
                1.,
                item_color(item, i),
            );
            p.text(
                Pos2::new(legend_x + 14., y),
                Align2::LEFT_TOP,
                item.label.chars().take(34).collect::<String>(),
                FontId::proportional(11.),
                Color32::LIGHT_GRAY,
            );
            if pointer.is_some_and(|pos| region.contains(pos)) {
                hit = Some(item.clone());
            }
        }
        p.text(
            Pos2::new(legend_x, rect.bottom() - 30.),
            Align2::LEFT_TOP,
            "Full annotation / ORF list below",
            FontId::proportional(10.),
            Color32::GRAY,
        );
    }
    if let Some(s) = selected {
        for &(start, end) in &s.segments {
            let a = start as f32 / length as f32 * tau - std::f32::consts::FRAC_PI_2;
            let b = end as f32 / length as f32 * tau - std::f32::consts::FRAC_PI_2;
            let points = (0..=80)
                .map(|i| c + Vec2::angled(a + (b - a) * i as f32 / 80.) * (radius + 12.))
                .collect();
            p.add(egui::Shape::line(points, Stroke::new(5., AMBER)));
        }
    }
    hit
}
fn draw_linear(
    p: &egui::Painter,
    plot: Rect,
    items: &[Selection],
    selected: Option<&Selection>,
    sequence: &str,
    window: (f32, f32, bool, i64),
    pointer: Option<Pos2>,
) -> Option<Selection> {
    let (left, visible, rna, reading_frame) = window;
    let px = plot.width() / visible;
    let x = |base: f32| plot.left() + (base - left) * px;
    let axis = plot.top() + 16.;
    let right = left + visible;
    let mut hit = None;
    p.line_segment(
        [Pos2::new(plot.left(), axis), Pos2::new(plot.right(), axis)],
        Stroke::new(1., Color32::GRAY),
    );
    let step = if visible > 2000. {
        1000
    } else if visible > 400. {
        100
    } else if visible > 90. {
        20
    } else {
        5
    };
    let begin = (left as usize / step) * step;
    for base in (begin..=(right.ceil() as usize).min(sequence.len())).step_by(step) {
        let xx = x(base as f32);
        if plot.left() <= xx && xx <= plot.right() {
            p.line_segment(
                [Pos2::new(xx, axis - 4.), Pos2::new(xx, axis + 4.)],
                Stroke::new(1., Color32::GRAY),
            );
            p.text(
                Pos2::new(xx, axis - 8.),
                Align2::CENTER_BOTTOM,
                format!("{}", base + 1),
                FontId::monospace(10.),
                Color32::LIGHT_GRAY,
            );
        }
    }
    let mut lanes = [f32::NEG_INFINITY; 10];
    for (index, item) in items.iter().enumerate() {
        let start = item.segments.iter().map(|s| s.0).min().unwrap_or(0) as f32;
        let end = item.segments.iter().map(|s| s.1).max().unwrap_or(0) as f32;
        if end < left || start > right {
            continue;
        }
        let base_lane = if item.is_orf {
            if item.strand < 0 { 7 } else { 4 }
        } else {
            0
        };
        let lane = (base_lane..(base_lane + 3).min(10))
            .find(|i| lanes[*i] < start)
            .unwrap_or(base_lane);
        lanes[lane] = end;
        let yy = axis + 22. + lane as f32 * 18.;
        let color = item_color(item, index);
        for &(s, e) in &item.segments {
            let a = x((s as f32).max(left));
            let b = x((e as f32).min(right));
            if a >= b {
                continue;
            }
            let r = Rect::from_min_max(Pos2::new(a, yy - 5.), Pos2::new(b, yy + 5.));
            p.rect_filled(
                r,
                2.,
                color.gamma_multiply(if item.is_orf { 0.65 } else { 1. }),
            );
            arrow(
                p,
                Pos2::new(if item.strand < 0 { a } else { b }, yy),
                if item.strand < 0 { -1. } else { 1. },
                color,
            );
            if b - a > 65. {
                p.text(
                    r.center(),
                    Align2::CENTER_CENTER,
                    item.label
                        .chars()
                        .take(((b - a) / 7.) as usize)
                        .collect::<String>(),
                    FontId::monospace(10.),
                    Color32::from_rgb(21, 25, 28),
                );
            }
            if pointer.is_some_and(|pos| r.expand(4.).contains(pos)) {
                hit = Some(item.clone());
            }
        }
    }
    if let Some(s) = selected {
        for &(start, end) in &s.segments {
            let a = x((start as f32).max(left));
            let b = x((end as f32).min(right));
            if a < b {
                p.rect_filled(
                    Rect::from_min_max(Pos2::new(a, axis + 8.), Pos2::new(b, plot.bottom())),
                    0.,
                    Color32::from_rgba_unmultiplied(220, 174, 75, 35),
                );
            }
        }
    }
    if px >= 3. {
        let y = axis + 214.;
        let bytes = sequence.as_bytes();
        let start = left.floor() as usize;
        let end = (right.ceil() as usize).min(bytes.len());
        p.text(
            Pos2::new(plot.left() - 8., y + 12.),
            Align2::RIGHT_CENTER,
            "5′",
            FontId::monospace(11.),
            Color32::GRAY,
        );
        p.text(
            Pos2::new(plot.left() - 8., y + 38.),
            Align2::RIGHT_CENTER,
            "3′",
            FontId::monospace(11.),
            Color32::GRAY,
        );
        let tracks = p.with_clip_rect(plot);
        for (i, &b) in bytes.iter().enumerate().take(end).skip(start) {
            letter_block(
                &tracks,
                Rect::from_min_size(Pos2::new(x(i as f32), y), Vec2::new(px, 25.)),
                b,
                base_color(b),
                false,
            );
            let complementary = if rna && b == b'A' {
                b'U'
            } else {
                complement(b)
            };
            letter_block(
                &tracks,
                Rect::from_min_size(Pos2::new(x(i as f32), y + 26.), Vec2::new(px, 25.)),
                complementary,
                base_color(complementary),
                false,
            );
        }
        let frame = reading_frame.unsigned_abs().clamp(1, 3) as usize - 1;
        let reverse = reading_frame < 0;
        p.text(
            Pos2::new(plot.left() - 8., y + 67.),
            Align2::RIGHT_CENTER,
            format!("{reading_frame:+}"),
            FontId::monospace(11.),
            Color32::LIGHT_GRAY,
        );
        let mut i = start.saturating_sub(2);
        while i < end {
            if i + 3 <= bytes.len()
                && if reverse {
                    (bytes.len() - i - 3) % 3 == frame
                } else {
                    i % 3 == frame
                }
            {
                let amino = if reverse {
                    codon(&[
                        complement(bytes[i + 2]),
                        complement(bytes[i + 1]),
                        complement(bytes[i]),
                    ])
                } else {
                    codon(&bytes[i..i + 3])
                };
                letter_block(
                    &tracks,
                    Rect::from_min_size(Pos2::new(x(i as f32), y + 54.), Vec2::new(px * 3., 27.)),
                    amino,
                    amino_color(amino),
                    false,
                );
            }
            i += 1;
        }
    } else {
        p.text(
            Pos2::new(plot.left(), plot.bottom() - 8.),
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
