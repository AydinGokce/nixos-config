use super::*;
use std::io::Read;

pub(super) struct View {
    pub molecule: scene::Molecule,
    pub renderer: scene::Renderer,
    pub camera: scene::Camera,
    pub style: scene::Representation,
    pub selected: Option<scene::ResidueKey>,
    pub measurement: Option<scene::ResidueKey>,
    pub chains: Vec<bool>,
    pub labels: bool,
    pub visible: bool,
    pub metadata: Value,
    pub bytes: Arc<Vec<u8>>,
}
pub(super) fn short_name(name: &str) -> &str {
    name.rsplit('/').next().unwrap_or(name)
}
pub(super) fn loading_status(metadata: Option<&Value>) -> String {
    if let Some(metadata) = metadata.filter(|value| text(value, "source_kind") == "job") {
        let status = [text(metadata, "job_state"), text(metadata, "job_phase")]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join(" · ");
        if status.is_empty() {
            "Waiting for run output…".into()
        } else {
            format!("Waiting for run output · {status}")
        }
    } else {
        "Downloading / reading structure…".into()
    }
}
pub(super) fn display_name(metadata: &Value) -> String {
    let input = text(metadata, "input_name");
    let name = if input.is_empty() {
        short_name(text(metadata, "name"))
    } else {
        input
    };
    let model = text(metadata, "model");
    if model.is_empty() {
        name.into()
    } else {
        format!("{model} · {name}")
    }
}
fn view_token(metadata: &Value) -> String {
    text(metadata, "load_token").into()
}
pub(super) fn view_state(view: &View) -> Value {
    json!({"camera":{"yaw":view.camera.yaw,"pitch":view.camera.pitch,"zoom":view.camera.zoom,"pan":[view.camera.pan.x,view.camera.pan.y],"ambient":view.camera.ambient,"bloom":view.camera.bloom},"style":view.style.name(),"selected":view.selected,"chains":view.chains,"labels":view.labels,"visible":view.visible})
}
fn restore_view(view: &mut View, value: &Value) {
    let camera = &value["camera"];
    for (name, dest) in [
        ("yaw", &mut view.camera.yaw),
        ("pitch", &mut view.camera.pitch),
        ("zoom", &mut view.camera.zoom),
    ] {
        if let Some(n) = camera[name]
            .as_f64()
            .map(|n| n as f32)
            .filter(|n| n.is_finite())
        {
            *dest = n;
        }
    }
    view.camera.zoom = view.camera.zoom.clamp(0.25, 4.);
    if let (Some(x), Some(y)) = (
        camera["pan"][0].as_f64().map(|n| n as f32),
        camera["pan"][1].as_f64().map(|n| n as f32),
    ) && x.is_finite()
        && y.is_finite()
    {
        view.camera.pan = Vec2::new(x, y);
    }
    view.camera.ambient = camera["ambient"].as_bool().unwrap_or(true);
    view.camera.bloom = camera["bloom"].as_bool().unwrap_or(true);
    view.style = match text(value, "style") {
        "sticks" => scene::Representation::Sticks,
        "spheres" => scene::Representation::Spheres,
        "backbone trace" => scene::Representation::Trace,
        _ => scene::Representation::Cartoon,
    };
    view.selected = serde_json::from_value(value["selected"].clone()).ok();
    if view
        .selected
        .as_ref()
        .is_some_and(|key| view.molecule.residue(key).is_none())
    {
        view.selected = None;
    }
    if let Some(chains) = value["chains"].as_array() {
        for (dest, source) in view.chains.iter_mut().zip(chains) {
            *dest = source.as_bool().unwrap_or(true);
        }
    }
    view.labels = value["labels"].as_bool().unwrap_or(false);
    view.visible = value["visible"].as_bool().unwrap_or(true);
}
impl Workbench {
    pub(super) fn copy_loaded_view(&mut self, source: usize, target: usize) -> bool {
        if self.view_loading.contains_key(&source) || self.view_errors.contains_key(&source) {
            return false;
        }
        let Some(original) = self.views.get(&source) else {
            return false;
        };
        let Some(metadata) = self.view_reference(target).cloned() else {
            return false;
        };
        let renderer = match scene::Renderer::new(&self.gl, &original.molecule) {
            Ok(renderer) => renderer,
            Err(error) => {
                let message = format!("GPU renderer: {error}");
                self.view_errors.insert(target, message.clone());
                self.log(message);
                return true;
            }
        };
        let view = View {
            molecule: original.molecule.clone(),
            renderer,
            camera: original.camera,
            style: original.style,
            selected: original.selected.clone(),
            measurement: original.measurement.clone(),
            chains: original.chains.clone(),
            labels: original.labels,
            visible: original.visible,
            metadata,
            bytes: original.bytes.clone(),
        };
        self.views.insert(target, view);
        true
    }

    pub(super) fn demo_views(&mut self) {
        let molecule = scene::Molecule::reference();
        let bytes = Arc::new(scene::Molecule::reference_bytes());
        let metadata = json!({"name":"Cas9 · 4OO8 / A+B+C","format":"pdb","source_kind":"demo","source_url":"https://www.rcsb.org/structure/4OO8","load_token":uid()});
        let slot = self.reserve_view(metadata.clone());
        self.view_loading.insert(slot, view_token(&metadata));
        self.accept_molecule(slot, metadata, bytes, Ok(molecule));
    }
    pub(super) fn reset_view(&mut self) {
        let slot = self.state.selected_view;
        if let Some(view) = self.views.get_mut(&slot) {
            view.camera = scene::Camera::fit(&view.molecule);
        }
        if self.state.link_views {
            for view in self.views.values_mut() {
                view.camera = scene::Camera::fit(&view.molecule);
            }
        }
    }
    pub(super) fn load_structure(
        &mut self,
        path: PathBuf,
        mut metadata: Value,
        slot: usize,
        ctx: &egui::Context,
    ) {
        if !self.has_view(slot) {
            return;
        }
        metadata["view_state"] = self
            .view_reference(slot)
            .and_then(|reference| reference.get("view_state"))
            .cloned()
            .unwrap_or(Value::Null);
        metadata["load_token"] = json!(uid());
        self.update_view_reference(slot, metadata.clone());
        self.view_loading.insert(slot, view_token(&metadata));
        self.view_errors.remove(&slot);
        let sender = self.ui_tx.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let result = (|| {
                let file = std::fs::File::open(&path).map_err(|e| e.to_string())?;
                let mut bytes = Vec::new();
                file.take(32 * 1024 * 1024 + 1)
                    .read_to_end(&mut bytes)
                    .map_err(|e| e.to_string())?;
                if bytes.len() > 32 * 1024 * 1024 {
                    return Err("Structure exceeds the 32 MiB viewer limit. Export still preserves the full original.".into());
                }
                Ok(bytes)
            })();
            let (bytes, molecule) = match result {
                Ok(bytes) => {
                    let result = scene::Molecule::parse(
                        &bytes,
                        text(&metadata, "format"),
                        &display_name(&metadata),
                    );
                    (bytes, result)
                }
                Err(error) => (Vec::new(), Err(error)),
            };
            let _ = sender.send(UiEvent::Parsed {
                slot,
                metadata,
                bytes: Arc::new(bytes),
                molecule,
            });
            ctx.request_repaint();
        });
    }
    pub(super) fn accept_molecule(
        &mut self,
        slot: usize,
        mut metadata: Value,
        bytes: Arc<Vec<u8>>,
        molecule: Result<scene::Molecule, String>,
    ) {
        if !self.has_view(slot)
            || self.view_loading.get(&slot).map(String::as_str)
                != Some(text(&metadata, "load_token"))
        {
            return;
        }
        self.view_loading.remove(&slot);
        let molecule = match molecule {
            Ok(molecule) => molecule,
            Err(error) => {
                let message = format!("Cannot display {}: {error}", text(&metadata, "name"));
                self.view_errors.insert(slot, message.clone());
                self.log(message);
                return;
            }
        };
        use sha2::Digest;
        let actual_sha = format!("{:x}", sha2::Sha256::digest(bytes.as_slice()));
        if !text(&metadata, "sha256").is_empty() && text(&metadata, "sha256") != actual_sha {
            let message = format!(
                "Structure identity changed for {}. Reopen the local file explicitly to load its new revision; retained annotations remain bound to the old SHA.",
                text(&metadata, "name")
            );
            self.view_errors.insert(slot, message.clone());
            self.log(message);
            return;
        }
        metadata["sha256"] = json!(actual_sha);
        let renderer = match scene::Renderer::new(&self.gl, &molecule) {
            Ok(renderer) => renderer,
            Err(error) => {
                let message = format!("GPU renderer: {error}");
                self.view_errors.insert(slot, message.clone());
                self.log(message);
                return;
            }
        };
        let chains = vec![true; molecule.chains.len()];
        let camera = scene::Camera::fit(&molecule);
        let mut view = View {
            molecule,
            renderer,
            camera,
            style: scene::Representation::Cartoon,
            selected: None,
            measurement: None,
            chains,
            labels: false,
            visible: true,
            metadata,
            bytes,
        };
        let saved = view.metadata["view_state"].clone();
        restore_view(&mut view, &saved);
        let artifact = text(&view.metadata, "artifact_id").to_owned();
        for warning in &view.molecule.warnings {
            self.log(format!("{}: {warning}", view.molecule.name));
        }
        self.update_view_reference(slot, view.metadata.clone());
        self.view_errors.remove(&slot);
        if let Some(old) = self.views.insert(slot, view) {
            self.retire_renderer(old.renderer);
        }
        if !artifact.is_empty() {
            self.request(
                "annotation.list",
                json!({"artifact_id":artifact}),
                Purpose::Annotations(artifact),
            );
        }
    }
    pub(super) fn request_artifact(&mut self, id: &str, target: ArtifactTarget) {
        if let ArtifactTarget::View(slot) = &target
            && !self.has_view(*slot)
        {
            return;
        }
        let purpose = Purpose::Artifact(target.clone());
        if self.busy(&purpose) {
            self.log(
                "That structure tab is still downloading. Wait for its current request to finish.",
            );
            return;
        }
        let Some(session) = self.session.as_mut() else {
            let message = "Local session is unavailable. Reconnect, then close this tab and reopen the result.";
            if let ArtifactTarget::View(slot) = target {
                self.view_loading.remove(&slot);
                self.view_errors.insert(slot, message.into());
            }
            self.log(message);
            return;
        };
        match session.artifact(id) {
            Ok(operation) => {
                self.artifact_metadata
                    .entry(id.into())
                    .or_insert_with(|| json!({"artifact_id":id}))["download_operation"] =
                    json!(operation);
                if let ArtifactTarget::View(slot) = target {
                    self.view_loading
                        .insert(slot, format!("download:{operation}"));
                    self.view_errors.remove(&slot);
                }
                self.pending.insert(
                    operation,
                    Pending {
                        purpose,
                        label: format!("Download {id}"),
                        done: 0,
                        total: 0,
                    },
                );
            }
            Err(error) => {
                let message = error.to_string();
                if let ArtifactTarget::View(slot) = target {
                    self.view_loading.remove(&slot);
                    self.view_errors.insert(slot, message.clone());
                }
                self.log(message);
            }
        }
    }
    pub(super) fn accept_artifact(
        &mut self,
        path: PathBuf,
        mut metadata: Value,
        target: ArtifactTarget,
        ctx: &egui::Context,
    ) {
        metadata["source_kind"] = json!("artifact");
        if text(&metadata, "format").is_empty() {
            metadata["format"] =
                json!(ui_state::infer_file(std::path::Path::new(text(&metadata, "name"))).0);
        }
        match target {
            ArtifactTarget::View(slot) => self.load_structure(path, metadata, slot, ctx),
            ArtifactTarget::Export => {
                let sender = self.ui_tx.clone();
                let ctx = ctx.clone();
                let name = short_name(text(&metadata, "name")).to_owned();
                std::thread::spawn(move || {
                    if let Some(destination) =
                        rfd::FileDialog::new().set_file_name(&name).save_file()
                    {
                        let result = std::fs::copy(path, &destination)
                            .map(|_| destination)
                            .map_err(|e| e.to_string());
                        let _ = sender.send(UiEvent::Exported(result));
                        ctx.request_repaint();
                    }
                });
            }
            ArtifactTarget::Text => {
                let sender = self.ui_tx.clone();
                let ctx = ctx.clone();
                std::thread::spawn(move || {
                    let result = (|| {
                        let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
                        let mut bytes = Vec::new();
                        file.take(2 * 1024 * 1024 + 1)
                            .read_to_end(&mut bytes)
                            .map_err(|e| e.to_string())?;
                        let truncated = bytes.len() > 2 * 1024 * 1024;
                        bytes.truncate(2 * 1024 * 1024);
                        let mut value = String::from_utf8_lossy(&bytes).into_owned();
                        if truncated {
                            value.push_str("\n[Preview truncated at 2 MiB. Export the artifact for the complete original.]");
                        }
                        Ok(value)
                    })();
                    let event = match result {
                        Ok(value) => UiEvent::Text(text(&metadata, "name").into(), value),
                        Err(error) => UiEvent::Error(error),
                    };
                    let _ = sender.send(event);
                    ctx.request_repaint();
                });
            }
        }
    }
    pub(super) fn export_active(&mut self, ctx: &egui::Context) {
        if let Some(view) = self.views.get(&self.state.selected_view) {
            let name = if text(&view.metadata, "source_kind") == "demo" {
                "experimental-4oo8-ABC.pdb".into()
            } else if std::path::Path::new(text(&view.metadata, "name"))
                .extension()
                .is_some()
            {
                short_name(text(&view.metadata, "name")).into()
            } else {
                format!("{}.{}", view.molecule.name, view.molecule.format)
            };
            self.export_bytes(view.bytes.clone(), name, ctx);
        }
    }
    pub(super) fn export_bytes(&self, bytes: Arc<Vec<u8>>, name: String, ctx: &egui::Context) {
        let sender = self.ui_tx.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            if let Some(path) = rfd::FileDialog::new().set_file_name(&name).save_file() {
                let result = std::fs::write(&path, bytes.as_slice())
                    .map(|_| path)
                    .map_err(|error| error.to_string());
                let _ = sender.send(UiEvent::Exported(result));
                ctx.request_repaint();
            }
        });
    }
    pub(super) fn pymol_active(&mut self) {
        if let Some(view) = self.views.get(&self.state.selected_view)
            && let Err(error) = self.pymol.launch_structure(
                &view.molecule.name,
                view.bytes.as_slice(),
                &view.molecule.format,
            )
        {
            self.log(format!("PyMOL: {error}"));
        }
    }
    pub(super) fn capture_views(&mut self) {
        if self.restoring_views {
            return;
        }
        for reference in &mut self.state.view_refs {
            let Some(slot) = reference["slot"]
                .as_u64()
                .and_then(|slot| usize::try_from(slot).ok())
            else {
                continue;
            };
            if !self.view_loading.contains_key(&slot)
                && !self.view_errors.contains_key(&slot)
                && let Some(view) = self.views.get(&slot)
            {
                *reference = view.metadata.clone();
                reference["view_state"] = view_state(view);
                reference["slot"] = json!(slot);
            }
            if let Some(object) = reference.as_object_mut() {
                object.remove("load_token");
                object.remove("download_operation");
            }
        }
        self.state.dock_layout = ui_dock::save_layout(&self.dock);
    }
    pub(super) fn restore_views(&mut self, ctx: &egui::Context) {
        let saved = self.state.view_refs.clone();
        for mut metadata in saved {
            let Some(slot) = metadata["slot"]
                .as_u64()
                .and_then(|slot| usize::try_from(slot).ok())
            else {
                continue;
            };
            if !self.has_view(slot) {
                continue;
            }
            let artifact = text(&metadata, "artifact_id").to_owned();
            if !artifact.is_empty() {
                let mut shared = metadata.clone();
                if let Some(object) = shared.as_object_mut() {
                    object.remove("slot");
                    object.remove("view_state");
                    object.remove("load_token");
                }
                self.artifact_metadata.insert(artifact.clone(), shared);
                self.request_artifact(&artifact, ArtifactTarget::View(slot));
            } else if text(&metadata, "source_kind") == "local" {
                let path = PathBuf::from(text(&metadata, "local_path"));
                self.load_structure(path, metadata, slot, ctx);
            } else if text(&metadata, "source_kind") == "demo" {
                let bytes = Arc::new(scene::Molecule::reference_bytes());
                let molecule = scene::Molecule::reference();
                metadata["load_token"] = json!(uid());
                self.update_view_reference(slot, metadata.clone());
                self.view_loading.insert(slot, view_token(&metadata));
                self.accept_molecule(slot, metadata, bytes, Ok(molecule));
            } else if text(&metadata, "source_kind") == "job" {
                let job = text(&metadata, "job_id").to_owned();
                if job.is_empty() {
                    self.view_errors.insert(slot,"The saved run has no job identifier. Close this tab and reopen the run from the sidebar.".into());
                } else {
                    self.view_loading.insert(slot, format!("job:{job}"));
                    self.request("job.get", json!({"job_id":job}), Purpose::Job(job));
                }
            } else {
                self.view_errors.insert(slot,"The saved structure has no readable source. Close this tab and reopen its file or result.".into());
            }
        }
    }
    pub(super) fn inspector(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        let slot = self.state.selected_view;
        Self::section(ui, "OBJECTS / ACTIVE TAB");
        if self.view_loading.contains_key(&slot) {
            let status = loading_status(self.view_reference(slot));
            ui.horizontal(|ui| {
                ui.spinner();
                ui.add(egui::Label::new(&status).truncate())
                    .on_hover_text(status);
            });
        }
        if let Some(error) = self.view_errors.get(&slot) {
            ui.colored_label(RED, error);
        }
        let Some(view) = self.views.get_mut(&slot) else {
            ui.label("No structure in the active tab.");
            return;
        };
        ui.horizontal(|ui| {
            ui.checkbox(&mut view.visible, "Structure");
            ui.checkbox(&mut view.labels, "Labels");
        });
        ui.small(format!(
            "{} atoms · {} residues",
            view.molecule.atoms.len(),
            view.molecule.residues.len()
        ));
        for (index, chain) in view.molecule.chains.iter().enumerate() {
            ui.push_id(index, |ui| {
                if !chain.label_id.is_empty() && chain.label_id != chain.id {
                    ui.small(format!("Label chain {}", chain.label_id));
                }
                ui.horizontal(|ui| {
                    ui.checkbox(&mut view.chains[index], "");
                    ui.colored_label(
                        chain.color,
                        format!(
                            "{} · {}",
                            if chain.id.is_empty() {
                                "(blank)"
                            } else {
                                &chain.id
                            },
                            chain.kind
                        ),
                    );
                    ui.menu_button("A", |ui| {
                        if ui.button("Select first residue").clicked() {
                            view.selected = chain
                                .residues
                                .first()
                                .map(|&index| view.molecule.residues[index].key.clone());
                            ui.close();
                        }
                        if ui.button("Isolate chain").clicked() {
                            view.chains.fill(false);
                            view.chains[index] = true;
                            ui.close();
                        }
                    })
                    .response
                    .on_hover_text("Actions for this chain");
                    if ui
                        .small_button("S")
                        .on_hover_text("Show only this chain")
                        .clicked()
                    {
                        view.chains.fill(false);
                        view.chains[index] = true;
                    }
                    if ui
                        .small_button("H")
                        .on_hover_text("Hide this chain")
                        .clicked()
                    {
                        view.chains[index] = false;
                    }
                });
                ui.small(format!(
                    "{} residues / {} atoms",
                    chain.residues.len(),
                    chain.atom_count
                ));
            });
        }
        if ui.small_button("Show all chains").clicked() {
            view.chains.fill(true);
        }
        Self::section(ui, "SELECTION / MEASURE");
        if let Some(key) = view.selected.clone() {
            ui.monospace(key.to_string());
            if let Some(residue) = view.molecule.residue(&key) {
                let atom = &view.molecule.atoms[residue.anchor];
                ui.small(format!(
                    "Anchor {} · ({:.2}, {:.2}, {:.2}) Å",
                    atom.name, atom.p.0, atom.p.1, atom.p.2
                ));
                ui.small(format!(
                    "Occupancy {:.2} · native B/temperature field {} · {}",
                    atom.occupancy,
                    atom.b_factor
                        .map(|n| format!("{n:.2}"))
                        .unwrap_or_else(|| "absent".into()),
                    if atom.hetero { "HETATM" } else { "ATOM" }
                ));
            }
            ui.horizontal(|ui| {
                if ui.small_button("Set distance origin").clicked() {
                    view.measurement = Some(key.clone());
                }
                if ui.small_button("Clear selection").clicked() {
                    view.selected = None;
                }
            });
            if let Some(origin) = view.measurement.as_ref() {
                ui.small(format!("From {origin}"));
                if let Ok(distance) = view.molecule.distance(origin, &key) {
                    ui.strong(format!("Anchor distance: {distance:.2} Å"));
                }
                if ui.small_button("Clear distance").clicked() {
                    view.measurement = None;
                }
            }
        } else {
            ui.weak("Click a residue in the structure or sequence strip.");
        }
        Self::section(ui, "SOURCE / QUALITY");
        ui.label(&view.molecule.name);
        if text(&view.metadata, "source_kind") == "demo" {
            ui.colored_label(AMBER, "Experimental reference — no prediction metrics");
            ui.hyperlink_to(
                "RCSB 4OO8 · Cas9 / guide RNA / target DNA",
                "https://www.rcsb.org/structure/4OO8",
            );
        } else {
            ui.small(format!(
                "{} · {}",
                text(&view.metadata, "model"),
                text(&view.metadata, "job_state")
            ));
        }
        ui.small(format!("Cartoon: {}", view.molecule.secondary_source));
        for warning in &view.molecule.warnings {
            ui.colored_label(AMBER, warning);
        }
        if let Some(error) = view.renderer.error() {
            ui.colored_label(RED, error);
        }
        for (key, label) in [
            ("confidence", "Native confidence"),
            ("qa", "Native chemistry / RF3 QA"),
            ("job_provenance", "Run provenance"),
        ] {
            egui::CollapsingHeader::new(label)
                .id_salt((slot, key))
                .show(ui, |ui| {
                    if view.metadata.get(key).is_none_or(Value::is_null) {
                        ui.weak("No native metadata supplied.");
                    } else {
                        ui.monospace(
                            serde_json::to_string_pretty(&view.metadata[key]).unwrap_or_default(),
                        );
                    }
                });
        }
        ui.collapsing("Artifact identity", |ui| {
            ui.monospace(text(&view.metadata, "artifact_id"));
            ui.small(format!("SHA256 {}", text(&view.metadata, "sha256")));
        });
        self.annotations_panel(ui, ctx);
    }
}
