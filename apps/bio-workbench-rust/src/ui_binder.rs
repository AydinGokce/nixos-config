//! Native target selection, hotspot editing and durable binder submission.
use super::*;
use binder_state::{Draft, Intent, Patch, Residue};
use sha2::{Digest, Sha256};

#[derive(Clone, PartialEq)]
pub(super) enum Request {
    Catalog,
    Inspect(String),
    Run(String),
    Batch(String),
    Candidates(String, Option<String>),
    Projects,
    LibraryTargets(String),
    Context(String, String, usize),
    Save(String),
}

#[derive(Default)]
pub(super) struct Panel {
    pub draft: Draft,
    pub catalog: Value,
    pub error: String,
    pub status: String,
    pub pending_view: Option<usize>,
    pub pending_source_ref: Option<String>,
    pub target_runs: Vec<Value>,
    pub target_library_ref: String,
    pub target_picker_open: bool,
    pub upload_token: String,
    pub progress_open: bool,
    pub batch: Value,
    pub results_open: bool,
    pub candidates: Value,
    pub candidates_at: Option<Instant>,
    pub candidate_cursor: Option<String>,
    pub candidate_pages: Vec<Option<String>>,
    pub context_pending: Option<Value>,
    pub context_request: Option<(String, String, usize)>,
    pub result_sort: String,
    pub descending: bool,
    pub selected_candidate: String,
    pub save_candidate: Option<Value>,
    pub projects: Vec<Value>,
    pub save_project: String,
    pub save_name: String,
    pub save_error: String,
    pub save_receipt: Value,
    pub patch_name: String,
    pub manual: String,
    undo: Vec<BTreeSet<Residue>>,
    redo: Vec<BTreeSet<Residue>>,
    last_hotspots: BTreeSet<Residue>,
}
impl Panel {
    pub fn restore(state: &UiState) -> Self {
        let mut draft: Draft = state
            .extra
            .get("binder_design")
            .cloned()
            .and_then(|v| serde_json::from_value(v).ok())
            .unwrap_or_default();
        for intent in [&mut draft.intent, &mut draft.save_intent]
            .into_iter()
            .flatten()
        {
            if intent.unresolved() {
                intent.error="A saved operation needs reconciliation after reopening. Recover its exact request.".into();
                intent.uncertain = true;
            }
        }
        let progress_open = draft.intent.as_ref().is_some_and(Intent::unresolved);
        Self {
            last_hotspots: draft.hotspots.clone(),
            draft,
            progress_open,
            result_sort: "status".into(),
            ..Default::default()
        }
    }
    fn record_selection(&mut self, selection: BTreeSet<Residue>) {
        if self.last_hotspots != selection {
            self.undo.push(std::mem::replace(
                &mut self.last_hotspots,
                selection.clone(),
            ));
            if self.undo.len() > 50 {
                self.undo.remove(0);
            }
            self.redo.clear();
        }
        self.draft.hotspots = selection;
    }
}

fn residue_from_key(key: &scene::ResidueKey) -> Option<Residue> {
    Some(Residue {
        chain: key.chain.clone(),
        number: key.sequence.parse().ok()?,
        insertion_code: key.insertion.clone(),
    })
}

impl Workbench {
    pub(super) fn binder_use_active(&mut self) {
        self.binder.pending_view = None;
        self.binder.pending_source_ref = None;
        self.binder_activate_target(None);
    }

    pub(super) fn binder_activate_target(&mut self, selected_source: Option<String>) {
        // Every explicit target choice supersedes earlier asynchronous loads,
        // including a library result that is already open in a viewer tab.
        self.binder.pending_view = None;
        self.binder.pending_source_ref = None;
        let slot = self.state.selected_view;
        let Some(view) = self
            .views
            .get(&slot)
            .filter(|_| !self.view_loading.contains_key(&slot))
        else {
            self.binder.error =
                "Open a target structure first, then choose Use active structure.".into();
            return;
        };
        let sha = format!("{:x}", Sha256::digest(view.bytes.as_slice()));
        let name = ui_views::display_name(&view.metadata);
        let format = view.molecule.format.clone();
        let bytes = view.bytes.clone();
        let artifact = text(&view.metadata, "artifact_id").to_owned();
        let current_head_artifact =
            !artifact.is_empty() && self.artifact_metadata.contains_key(&artifact);
        let source_ref = selected_source.unwrap_or_else(|| {
            if current_head_artifact {
                view.metadata["job_provenance"]["source_refs"]
                    .as_array()
                    .filter(|refs| refs.len() == 1)
                    .and_then(|refs| refs[0].as_str())
                    .unwrap_or("")
                    .to_owned()
            } else {
                String::new()
            }
        });
        self.binder.draft.enabled = true;
        self.sidebar_tab = 0;
        self.binder.draft.endpoint = self.run_endpoint();
        self.binder.draft.target_slot = Some(slot);
        self.binder.draft.target_name = name.clone();
        self.binder.draft.name = format!("{name} binders");
        self.binder.draft.inspection = Value::Null;
        self.binder.draft.crop.clear();
        self.binder.draft.crop_enabled = false;
        self.binder.draft.source_ref = source_ref;
        self.binder.draft.hotspots.clear();
        self.binder.last_hotspots.clear();
        self.binder.undo.clear();
        self.binder.redo.clear();
        self.binder.error.clear();
        self.binder.upload_token.clear();
        self.binder.draft.chains = view
            .molecule
            .chains
            .iter()
            .filter(|chain| chain.kind == scene::MoleculeKind::Protein)
            .map(|chain| chain.id.clone())
            .collect();
        if let Some(view) = self.views.get_mut(&slot) {
            view.hotspots.enabled = true;
            view.hotspots.residues.clear();
        }
        if current_head_artifact {
            self.binder.draft.target = json!({"kind":"artifact","id":artifact,"sha256":sha});
            self.binder_inspect();
        } else {
            // Capture the already parsed bytes, not a mutable file path selected earlier.
            let snapshot = (|| -> Result<PathBuf, String> {
                let dir = session::state_directory()
                    .map_err(|e| e.to_string())?
                    .join("binder-targets");
                rpc::private_dir(&dir).map_err(|e| e.to_string())?;
                let ext = if matches!(format.as_str(), "cif" | "mmcif") {
                    "cif"
                } else {
                    "pdb"
                };
                let path = dir.join(format!("{sha}.{ext}"));
                if !path.exists() {
                    use std::io::Write;
                    let mut file = std::fs::OpenOptions::new()
                        .create_new(true)
                        .write(true)
                        .open(&path)
                        .map_err(|e| e.to_string())?;
                    file.write_all(&bytes)
                        .and_then(|_| file.sync_all())
                        .map_err(|e| e.to_string())?;
                }
                let saved = std::fs::read(&path).map_err(|e| e.to_string())?;
                if format!("{:x}", Sha256::digest(saved)) != sha {
                    return Err("Saved target snapshot failed its content hash check.".into());
                }
                Ok(path)
            })();
            match snapshot {
                Ok(path) => {
                    self.binder.draft.target = json!({"kind":"upload","id":"","sha256":sha});
                    self.binder.upload_token = uid();
                    self.binder.status = "Uploading target structure…".into();
                    self.start_upload(path, UploadTarget::Binder(self.binder.upload_token.clone()));
                }
                Err(error) => self.binder.error = error,
            }
        }
        self.persist();
    }

    pub(super) fn binder_uploaded(&mut self, token: &str, receipt: Value) {
        if self.binder.upload_token != token {
            return;
        }
        if text(&receipt, "upload_id").is_empty()
            || receipt["sha256"] != self.binder.draft.target["sha256"]
        {
            self.binder.error =
                "Target upload receipt did not match the selected structure.".into();
            return;
        }
        self.binder.draft.target["id"] = receipt["upload_id"].clone();
        self.binder.upload_token.clear();
        self.binder_inspect();
    }

    pub(super) fn binder_inspect(&mut self) {
        let sha = text(&self.binder.draft.target, "sha256").to_owned();
        self.binder.status = "Inspecting target chains and residue identities…".into();
        if self
            .request(
                "binder.inspect",
                json!({"target":self.binder.draft.target}),
                Purpose::Binder(Request::Inspect(sha)),
            )
            .is_none()
        {
            self.binder.error =
                "Could not inspect the target. Connect to the head and retry.".into();
        }
    }

    pub(super) fn binder_sync_selection(&mut self) {
        let Some(slot) = self.binder.draft.target_slot else {
            return;
        };
        let Some(view) = self.views.get_mut(&slot) else {
            return;
        };
        if !self.binder.draft.enabled {
            return;
        }
        if view.metadata["sha256"] != self.binder.draft.target["sha256"] {
            return;
        }
        view.renderer
            .consume_pick(&view.molecule, &mut view.selected, &mut view.hotspots);
        let selection = view
            .hotspots
            .residues
            .iter()
            .filter_map(residue_from_key)
            .collect();
        self.binder.record_selection(selection);
    }

    fn binder_apply_selection(&mut self, selection: BTreeSet<Residue>) {
        self.binder.record_selection(selection.clone());
        if let Some(view) = self
            .binder
            .draft
            .target_slot
            .and_then(|slot| self.views.get_mut(&slot))
        {
            view.hotspots.residues = view
                .molecule
                .residues
                .iter()
                .filter(|residue| {
                    residue_from_key(&residue.key).is_some_and(|key| selection.contains(&key))
                })
                .map(|r| r.key.clone())
                .collect();
        }
    }

    pub(super) fn binder_left(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        Self::section(ui, "BINDER DESIGN · BindCraft");
        if self.binder.catalog["runtime_ready"] == true {
            ui.colored_label(GREEN, "BindCraft ready");
        } else if !self.binder.catalog.is_null() {
            ui.colored_label(AMBER, "BindCraft runtime unavailable");
        }
        if self.binder.draft.target.is_null() {
            let endpoint = self.run_endpoint();
            let saved = self
                .state
                .extra
                .get("detached_binder_drafts")
                .and_then(Value::as_array)
                .and_then(|drafts| {
                    drafts.iter().rposition(|item| {
                        text(item, "endpoint") == endpoint && !item["draft"]["target"].is_null()
                    })
                });
            if let Some(index) = saved
                && ui.button("Restore previous draft for this head").clicked()
            {
                let archive = self
                    .state
                    .extra
                    .get_mut("detached_binder_drafts")
                    .unwrap()
                    .as_array_mut()
                    .unwrap();
                let saved = archive.remove(index);
                self.state
                    .extra
                    .insert("binder_design".into(), saved["draft"].clone());
                let catalog = self.binder.catalog.clone();
                self.binder = Panel::restore(&self.state);
                self.binder.catalog = catalog;
                self.persist();
            }
        }
        ui.horizontal_wrapped(|ui| {
            if ui.button("Use active structure").clicked() {
                self.binder_use_active();
            }
            if ui.button("Load target…").clicked() {
                self.choose_files(Pick::BinderTarget, ctx);
            }
            if ui.button("Library…").clicked() {
                self.open_library();
            }
        });
        if self.binder.draft.target.is_null() {
            ui.label("Choose a target structure from a viewer tab or file.");
            ui.weak("For a sequence-only construct, open a folding result from its library run history first.");
        } else {
            ui.strong(&self.binder.draft.target_name);
            if let Some(slot) = self.binder.draft.target_slot
                && self.has_view(slot)
                && ui.small_button("Show target tab").clicked()
            {
                self.focus_view(slot);
            }
            ui.add(
                egui::TextEdit::singleline(&mut self.binder.draft.name)
                    .hint_text("Design run name")
                    .desired_width(f32::INFINITY),
            );
            if self.binder.draft.inspection.is_null() {
                ui.horizontal(|ui| {
                    ui.spinner();
                    ui.label(&self.binder.status);
                });
                if !text(&self.binder.draft.target, "id").is_empty()
                    && ui.button("Retry inspection").clicked()
                {
                    self.binder_inspect();
                }
            } else {
                Self::section(ui, "SUBMITTED TARGET");
                let chains: Vec<_> = rows(&self.binder.draft.inspection, "chains")
                    .iter()
                    .map(|chain| {
                        (
                            text(chain, "chain").to_owned(),
                            rows(chain, "residues").len(),
                            rows(chain, "residues")
                                .iter()
                                .any(|r| r["supported"] != false),
                            chain["supported"] != false,
                        )
                    })
                    .collect();
                for (id, count, supported, complete) in chains {
                    let mut enabled = self.binder.draft.chains.contains(&id);
                    let label = format!(
                        "{} · {count} residues",
                        if id.is_empty() { "(blank)" } else { &id }
                    );
                    if ui
                        .add_enabled(supported, egui::Checkbox::new(&mut enabled, label))
                        .changed()
                    {
                        if enabled {
                            self.binder.draft.chains.insert(id);
                        } else {
                            self.binder.draft.chains.remove(&id);
                        }
                    }
                    if !complete {
                        ui.weak(if supported {
                            "Contains unsupported residues; select a supported crop."
                        } else {
                            "No supported protein residues."
                        });
                    }
                }
                egui::CollapsingHeader::new("Structure inspection notes").show(ui, |ui| {
                    for warning in rows(&self.binder.draft.inspection, "warnings")
                        .iter()
                        .filter_map(Value::as_str)
                    {
                        ui.weak(warning);
                    }
                });
                ui.weak("Only the selected protein chains/crop enter design. Other displayed molecules remain visual context.");
                ui.checkbox(&mut self.binder.draft.crop_enabled, "Crop target residues");
                if self.binder.draft.crop_enabled {
                    ui.add(
                        egui::TextEdit::multiline(&mut self.binder.draft.crop)
                            .desired_rows(2)
                            .desired_width(f32::INFINITY)
                            .hint_text("A:100-250, B:12-80"),
                    );
                    ui.horizontal_wrapped(|ui| {
                        if ui.small_button("Use selected residues").clicked() {
                            self.binder.draft.crop = self
                                .binder
                                .draft
                                .hotspots
                                .iter()
                                .map(Residue::label)
                                .collect::<Vec<_>>()
                                .join(", ");
                        }
                        if ui.small_button("Clear crop").clicked() {
                            self.binder.draft.crop.clear();
                        }
                    });
                    match binder_state::parse_selection(
                        &self.binder.draft.crop,
                        &binder_state::inspected_residues(&self.binder.draft.inspection),
                    ) {
                        Ok(residues) => {
                            ui.small(format!(
                                "{} submitted residues · original numbering retained in provenance",
                                residues.len()
                            ));
                        }
                        Err(error) => {
                            ui.colored_label(AMBER, error);
                        }
                    }
                }
            }
        }
        Self::section(ui, "DESIGN LIMITS");
        egui::Grid::new("binder-limits")
            .num_columns(2)
            .min_col_width(105.)
            .spacing([8., 6.])
            .show(ui, |ui| {
                ui.label("Binder length");
                ui.horizontal(|ui| {
                    ui.add(egui::DragValue::new(&mut self.binder.draft.lengths[0]).range(5..=1000));
                    ui.label("–");
                    ui.add(egui::DragValue::new(&mut self.binder.draft.lengths[1]).range(5..=1000));
                });
                ui.end_row();
                ui.label("Accepted designs");
                ui.add(egui::DragValue::new(&mut self.binder.draft.designs).range(1..=10_000));
                ui.end_row();
                ui.label("Runtime limit");
                ui.add(
                    egui::DragValue::new(&mut self.binder.draft.timeout_minutes)
                        .range(1..=1425)
                        .suffix(" min"),
                );
                ui.end_row();
                ui.label("Run cost ceiling");
                ui.add(
                    egui::DragValue::new(&mut self.binder.draft.max_cost_usd)
                        .range(0.1..=750.)
                        .speed(0.5)
                        .prefix("$")
                        .fixed_decimals(2),
                );
                ui.end_row();
            });
        ui.weak("Accepted designs are a goal; the run may reach its limit without any passing candidates.");
        egui::CollapsingHeader::new("Seed and provenance").show(ui, |ui| {
            ui.label("Random seed (empty = random)");
            ui.text_edit_singleline(&mut self.binder.draft.seed);
            ui.label("Target construct revision (optional)");
            ui.add(
                egui::TextEdit::singleline(&mut self.binder.draft.source_ref)
                    .hint_text("construct:name@revision"),
            );
            ui.label("Project revision (optional)");
            ui.add(
                egui::TextEdit::singleline(&mut self.binder.draft.project_ref)
                    .hint_text("project:name@revision"),
            );
            ui.small(format!(
                "Target SHA256: {}",
                text(&self.binder.draft.target, "sha256")
            ));
            ui.weak("Standard BindCraft four-stage design and acceptance filters.");
        });
        self.binder_sync_selection();
        let pending = self
            .binder
            .draft
            .intent
            .as_ref()
            .is_some_and(Intent::unresolved);
        let run = ui.add_enabled(
            !pending && self.connected,
            egui::Button::new(
                RichText::new("▶ Design binders")
                    .strong()
                    .color(Color32::WHITE),
            )
            .fill(Color32::from_rgb(28, 125, 67))
            .min_size(Vec2::new(ui.available_width(), 34.)),
        );
        if run.clicked() {
            match self.binder.draft.request(&self.run_endpoint()) {
                Ok(params) => self.binder_start(params),
                Err(error) => self.binder.error = error,
            }
        }
        if let Some(intent) = &self.binder.draft.intent
            && !intent.batch_id.is_empty()
            && ui.button("Show design progress").clicked()
        {
            self.binder.progress_open = true;
        }
        if !self.binder.error.is_empty() {
            ui.colored_label(RED, &self.binder.error);
        }
        if self
            .binder
            .draft
            .intent
            .as_ref()
            .is_some_and(|intent| !intent.error.is_empty() && intent.batch_id.is_empty())
        {
            if ui.button("Recover exact submission").clicked() {
                self.binder_retry_run();
            }
            ui.weak("Recovery preserves the original target, settings, and request key.");
        }
    }

    pub(super) fn binder_inspector(&mut self, ui: &mut egui::Ui) {
        self.binder_sync_selection();
        Self::section(ui, "BINDING PATCH");
        let slot = self.binder.draft.target_slot;
        if let Some(view) = slot.and_then(|slot| self.views.get_mut(&slot)) {
            ui.checkbox(&mut view.hotspots.enabled, "Pick hotspot residues");
            if ui.button("Surface").clicked() {
                view.style = scene::Representation::Surface;
            }
        }
        if slot.is_some_and(|slot| slot != self.state.selected_view) {
            ui.colored_label(AMBER, "Selection belongs to the target tab.");
        }
        ui.weak("Click residues to toggle; Shift-click a sequence range. Drag to rotate.");
        ui.small(format!(
            "{} hotspot residues",
            self.binder.draft.hotspots.len()
        ));
        let mut remove = None;
        ui.horizontal_wrapped(|ui| {
            for residue in &self.binder.draft.hotspots {
                if ui
                    .button(RichText::new(format!("{} ×", residue.label())).color(AMBER))
                    .clicked()
                {
                    remove = Some(residue.clone());
                }
            }
        });
        if let Some(residue) = remove {
            let mut selected = self.binder.draft.hotspots.clone();
            selected.remove(&residue);
            self.binder_apply_selection(selected);
        }
        ui.horizontal(|ui| {
            if ui
                .add_enabled(!self.binder.undo.is_empty(), egui::Button::new("Undo"))
                .clicked()
                && let Some(previous) = self.binder.undo.pop()
            {
                let current = self.binder.draft.hotspots.clone();
                self.binder.last_hotspots = previous.clone();
                self.binder_apply_selection(previous);
                self.binder.redo.push(current);
            }
            if ui
                .add_enabled(!self.binder.redo.is_empty(), egui::Button::new("Redo"))
                .clicked()
                && let Some(next) = self.binder.redo.pop()
            {
                let current = self.binder.draft.hotspots.clone();
                self.binder.last_hotspots = next.clone();
                self.binder_apply_selection(next);
                self.binder.undo.push(current);
            }
            if ui.button("Clear").clicked() {
                self.binder_apply_selection(BTreeSet::new());
            }
        });
        ui.horizontal(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.binder.manual)
                    .desired_width((ui.available_width() - 60.).max(60.))
                    .hint_text("A:56, A:58-61"),
            );
            if ui.add_sized([44., 22.], egui::Button::new("Add")).clicked() {
                match binder_state::parse_selection(
                    &self.binder.manual,
                    &binder_state::inspected_residues(&self.binder.draft.inspection),
                ) {
                    Ok(extra) => {
                        let mut selected = self.binder.draft.hotspots.clone();
                        selected.extend(extra);
                        self.binder_apply_selection(selected);
                        self.binder.manual.clear();
                        self.binder.error.clear();
                    }
                    Err(error) => self.binder.error = error,
                }
            }
        });
        if self.binder.draft.hotspots.is_empty() {
            ui.weak("No hotspots: allow BindCraft to explore binding sites.");
        }
        ui.weak("Hotspots guide a region; inspect the resulting contacts.");
        Self::section(ui, "SAVED PATCHES");
        ui.horizontal(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.binder.patch_name)
                    .desired_width((ui.available_width() - 70.).max(60.))
                    .hint_text("Patch name"),
            );
            if ui
                .add_enabled(
                    !self.binder.patch_name.trim().is_empty()
                        && !self.binder.draft.target.is_null(),
                    egui::Button::new("Save"),
                )
                .clicked()
            {
                let patch = Patch {
                    name: self.binder.patch_name.trim().into(),
                    sha256: text(&self.binder.draft.target, "sha256").into(),
                    chains: self.binder.draft.chains.clone(),
                    crop: if self.binder.draft.crop_enabled {
                        self.binder.draft.crop.clone()
                    } else {
                        String::new()
                    },
                    hotspots: self.binder.draft.hotspots.clone(),
                };
                if let Some(existing) = self
                    .binder
                    .draft
                    .patches
                    .iter_mut()
                    .find(|p| p.name == patch.name && p.sha256 == patch.sha256)
                {
                    *existing = patch;
                } else if self.binder.draft.patches.len() < 128 {
                    self.binder.draft.patches.push(patch);
                } else {
                    self.binder.error =
                        "Saved patch limit reached (128). Remove an old patch first.".into();
                }
                self.binder.patch_name.clear();
                self.persist();
            }
        });
        let sha = text(&self.binder.draft.target, "sha256").to_owned();
        let mut load = None;
        let mut delete = None;
        for (i, patch) in self
            .binder
            .draft
            .patches
            .iter()
            .enumerate()
            .filter(|(_, p)| p.sha256 == sha)
        {
            ui.horizontal(|ui| {
                if ui.button(&patch.name).clicked() {
                    load = Some(patch.clone());
                }
                ui.weak(format!("{} residues", patch.hotspots.len()));
                if ui
                    .small_button("×")
                    .on_hover_text("Remove saved patch")
                    .clicked()
                {
                    delete = Some(i);
                }
            });
        }
        if let Some(patch) = load {
            self.binder.draft.chains = patch.chains;
            self.binder.draft.crop_enabled = !patch.crop.is_empty();
            self.binder.draft.crop = patch.crop;
            self.binder_apply_selection(patch.hotspots);
        }
        if let Some(index) = delete {
            self.binder.draft.patches.remove(index);
        }
        ui.weak("Saved locally for this exact structure; submitted selections are retained on the head.");
        ui.separator();
    }

    fn binder_start(&mut self, params: Value) {
        let id = uid();
        self.binder.draft.intent = Some(Intent {
            id: id.clone(),
            endpoint: self.run_endpoint(),
            request: params.clone(),
            ..Default::default()
        });
        self.binder.progress_open = true;
        self.binder.batch = Value::Null;
        self.binder.error.clear();
        self.persist();
        if !self.save_error.is_empty() {
            let error = format!(
                "Could not save the submission intent locally: {}",
                self.save_error
            );
            let intent = self.binder.draft.intent.as_mut().unwrap();
            intent.error = error.clone();
            intent.uncertain = true;
            self.binder.error = error;
            return;
        }
        if let Some(operation) =
            self.request("binder.run", params, Purpose::Binder(Request::Run(id)))
        {
            self.binder.draft.intent.as_mut().unwrap().operation = operation;
        } else {
            self.binder.error =
                "Could not queue the design request. Check the connection and retry.".into();
            let intent = self.binder.draft.intent.as_mut().unwrap();
            intent.error = self.binder.error.clone();
            intent.uncertain = true;
        }
        self.persist();
    }

    fn binder_retry_run(&mut self) {
        let Some(intent) = self.binder.draft.intent.clone() else {
            return;
        };
        if intent.endpoint != self.run_endpoint() {
            self.binder.error = "Reconnect to the original head to recover this submission.".into();
            return;
        }
        self.persist();
        if !self.save_error.is_empty() {
            self.binder.error = self.save_error.clone();
            return;
        }
        if intent.operation.is_empty() {
            if let Some(operation) = self.request(
                "binder.run",
                intent.request,
                Purpose::Binder(Request::Run(intent.id)),
            ) {
                let intent = self.binder.draft.intent.as_mut().unwrap();
                intent.operation = operation;
                intent.error.clear();
            }
        } else {
            self.retry(
                &intent.operation,
                Purpose::Binder(Request::Run(intent.id)),
                "binder.run".into(),
            );
        }
        self.persist();
    }

    pub(super) fn binder_received(&mut self, request: Request, value: Value, ctx: &egui::Context) {
        match request {
            Request::Catalog => self.binder.catalog = value,
            Request::Inspect(sha) => {
                if text(&self.binder.draft.target, "sha256") != sha {
                    return;
                }
                if text(&value["target"], "sha256") != sha {
                    self.binder.error = "Head inspection returned a different target hash.".into();
                    return;
                }
                self.binder.draft.inspection = value;
                self.binder.status = "Target ready".into();
                self.binder.error.clear();
                let supported: BTreeSet<_> = rows(&self.binder.draft.inspection, "chains")
                    .iter()
                    .filter(|c| rows(c, "residues").iter().any(|r| r["supported"] != false))
                    .map(|c| text(c, "chain").to_owned())
                    .collect();
                self.binder.draft.chains.retain(|c| supported.contains(c));
            }
            Request::Run(id) => {
                if let Some(intent) = self.binder.draft.intent.as_mut().filter(|i| i.id == id) {
                    let batch = text(&value, "batch_id");
                    if batch.is_empty() {
                        intent.error =
                            "Submission response omitted its batch. Recover the exact request."
                                .into();
                        intent.uncertain = true;
                        return;
                    }
                    intent.batch_id = batch.into();
                    intent.error.clear();
                    intent.uncertain = false;
                    self.binder.error.clear();
                    self.binder.batch = value.clone();
                    self.ingest_batch(value);
                    self.log(
                        "Binder design queued on the head. Closing the Console leaves it running.",
                    );
                }
            }
            Request::Batch(id) => {
                if text(&value, "batch_id") == id
                    && self
                        .binder
                        .draft
                        .intent
                        .as_ref()
                        .is_some_and(|intent| intent.matches_batch(&self.run_endpoint(), &id))
                {
                    self.binder.batch = value.clone();
                    if self.state.active_batch == id {
                        self.ingest_batch(value);
                    }
                }
            }
            Request::Candidates(id, cursor) => {
                if self.binder.draft.results_job == id
                    && self.binder.candidate_cursor == cursor
                    && text(&value, "job_id") == id
                {
                    self.binder.candidates = value;
                    self.binder.candidates_at = Some(Instant::now());
                }
            }
            Request::Projects => {
                self.binder.projects = rows(&value, "projects").to_vec();
                if self.binder.projects.is_empty() {
                    self.binder.projects = rows(&value, "records").to_vec();
                }
                if !self
                    .binder
                    .projects
                    .iter()
                    .any(|p| text(p, "ref") == self.binder.save_project)
                {
                    self.binder.save_project = self
                        .binder
                        .projects
                        .first()
                        .map(|p| text(p, "ref").to_owned())
                        .unwrap_or_default();
                }
            }
            Request::LibraryTargets(reference) => {
                if self.binder.target_library_ref == reference {
                    self.binder.target_runs = rows(&value, "records").to_vec();
                }
            }
            Request::Context(job, artifact, slot) => {
                self.binder_received_context(&job, &artifact, slot, value, ctx)
            }
            Request::Save(id) => {
                if let Some(intent) = self
                    .binder
                    .draft
                    .save_intent
                    .as_mut()
                    .filter(|i| i.id == id)
                {
                    intent.batch_id = "complete".into();
                    intent.error.clear();
                    intent.uncertain = false;
                }
                self.binder.save_receipt = value.clone();
                self.binder.save_candidate = None;
                self.binder.save_error.clear();
                self.library_received_write(value, &json!({}));
                self.log("Binder sequence saved to the project with target, patch, settings and run provenance.");
            }
        }
        self.persist();
    }

    pub(super) fn binder_failed(&mut self, purpose: &Purpose, message: &str, uncertain: bool) {
        match purpose {
            Purpose::Binder(Request::Run(id)) => {
                if let Some(intent) = self.binder.draft.intent.as_mut().filter(|i| &i.id == id) {
                    intent.error = message.into();
                    intent.uncertain = uncertain;
                }
                self.binder.error = message.into();
            }
            Purpose::Binder(Request::Save(id)) => {
                if let Some(intent) = self
                    .binder
                    .draft
                    .save_intent
                    .as_mut()
                    .filter(|i| &i.id == id)
                {
                    intent.error = message.into();
                    intent.uncertain = uncertain;
                }
                self.binder.save_error = message.into();
            }
            Purpose::Binder(Request::Projects) => self.binder.save_error = message.into(),
            Purpose::Binder(_) => self.binder.error = message.into(),
            Purpose::Upload(UploadTarget::Binder(token)) if self.binder.upload_token == *token => {
                self.binder.error = message.into()
            }
            _ => {}
        }
    }

    pub(super) fn binder_poll(&mut self) {
        if let Some(intent) = self
            .binder
            .draft
            .intent
            .as_ref()
            .filter(|i| !i.batch_id.is_empty() && i.endpoint == self.run_endpoint())
        {
            let id = intent.batch_id.clone();
            if self.binder.progress_open || !ui_state::terminal(text(&self.binder.batch, "state")) {
                self.request(
                    "batch.get",
                    json!({"batch_id":id}),
                    Purpose::Binder(Request::Batch(id)),
                );
            }
        }
        if self.binder.results_open && !self.binder.draft.results_job.is_empty() {
            self.binder_fetch_candidates();
        }
    }

    pub(super) fn binder_progress_dialog(&mut self, ctx: &egui::Context) {
        if !self.binder.progress_open {
            return;
        }
        let mut open = true;
        egui::Window::new("Binder design · progress").open(&mut open).default_width(560.).resizable(true).show(ctx,|ui|{
            let Some(intent)=self.binder.draft.intent.clone() else{return};
            if intent.endpoint != self.run_endpoint() {
                ui.label("Reconnect to the original head to view this design run.");
                return;
            }
            ui.strong(text(&intent.request,"name"));
            if intent.batch_id.is_empty() {ui.horizontal(|ui|{ui.spinner();ui.label("Submitting the captured target and settings…");});}
            if !intent.error.is_empty() {ui.colored_label(RED,&intent.error);if ui.button("Recover exact submission").clicked(){self.binder_retry_run();}}
            let batch=if intent.matches_batch(&self.run_endpoint(),text(&self.binder.batch,"batch_id")) {
                self.binder.batch.clone()
            } else { Value::Null };
            for job in rows(&batch,"jobs") {
                let id=text(job,"job_id").to_owned();
                ui.separator();ui.label(format!("{} · {}",text(job,"state"),text(job,"phase")));
                if !text(&job["progress"],"message").is_empty(){ui.label(text(&job["progress"],"message"));}
                let observed=&job["progress"]["binder"];
                if observed["attempts_started"].is_number(){
                    ui.label(format!("{} attempts started · {} trajectories completed · {} accepted · {} rejected",
                        observed["attempts_started"],observed["trajectories_completed"],observed["candidates_accepted"],observed["candidates_rejected"]));
                    ui.weak("Observed from the live log; final candidate tables are authoritative.");
                }
                if let Some(error)=job.get("error").filter(|v|!v.is_null()){ui.colored_label(RED,error.to_string());}
                ui.horizontal(|ui|{
                    if ui.button("Candidates").clicked(){self.binder_open_candidates(id.clone());}
                    if ui.button("Log").clicked(){self.focused_job=id.clone();self.log_offset=0;self.job_log.clear();self.console_tab=1;self.request("job.logs",json!({"job_id":id,"offset":0,"max_bytes":65536}),Purpose::Logs(id.clone(),0));}
                    if !ui_state::terminal(text(job,"state")) && ui.button("Cancel").clicked(){self.request("job.cancel",json!({"job_id":id}),Purpose::CancelJob);}
                });
            }
            if !intent.batch_id.is_empty() && ui.button("Show in run history").clicked(){self.sidebar_tab=1;self.state.active_batch=intent.batch_id.clone();self.request("batch.get",json!({"batch_id":intent.batch_id}),Purpose::Batch(intent.batch_id));}
            ui.weak("Design progress is stochastic. Accepted counts are not a percentage or a binding-affinity measurement.");
        });
        self.binder.progress_open = open;
    }

    pub(super) fn binder_library_target(&mut self, reference: &str) {
        self.binder.target_library_ref = reference.into();
        self.binder.target_runs.clear();
        self.binder.target_picker_open = true;
        self.request(
            "library.runs",
            json!({"ref":reference,"limit":100,"include_revisions":false}),
            Purpose::Binder(Request::LibraryTargets(reference.into())),
        );
    }

    pub(super) fn binder_target_dialog(&mut self, ctx: &egui::Context) {
        if !self.binder.target_picker_open {
            return;
        }
        let mut open = true;
        let mut selected = None;
        egui::Window::new("Choose a target folding result").open(&mut open).default_width(550.).show(ctx,|ui|{
            ui.strong(&self.binder.target_library_ref);
            let loading=self.busy(&Purpose::Binder(Request::LibraryTargets(self.binder.target_library_ref.clone())));
            if loading {ui.spinner();}
            let mut count=0;
            for job in &self.binder.target_runs {
                if job["structure_count"].as_u64().unwrap_or(0)==0 || !rows(job,"source_refs").iter().any(|r|r.as_str()==Some(&self.binder.target_library_ref)) {continue;}
                count+=1;
                if ui.button(format!("{} · {} · {}",text(job,"model"),text(job,"batch_name"),text(job,"created_at"))).clicked(){selected=Some(text(job,"job_id").to_owned());}
            }
            if count==0 && !loading {ui.label("No retained folding structure for this revision yet. Use Prepare prediction on the protein, then choose its completed result here.");}
        });
        if let Some(job) = selected {
            self.binder.pending_source_ref = Some(self.binder.target_library_ref.clone());
            self.open_job_new_tab(job, ctx);
            self.binder.draft.enabled = true;
            self.sidebar_tab = 0;
            let slot = self.state.selected_view;
            if self.views.contains_key(&slot) && !self.view_loading.contains_key(&slot) {
                let source = self.binder.pending_source_ref.take();
                self.binder_activate_target(source);
            } else {
                self.binder.pending_view = Some(slot);
            }
            open = false;
        }
        self.binder.target_picker_open = open;
    }
}
