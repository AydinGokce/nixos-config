use super::*;
use std::io::Read;

pub(super) struct View {
    pub molecule: scene::Molecule,
    pub renderer: scene::Renderer,
    pub camera: scene::Camera,
    pub style: scene::Representation,
    pub selected: Option<scene::ResidueKey>,
    pub hotspots: scene::Hotspots,
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
const ALIGNMENT_KEYS: [&str; 4] = [
    "binder_alignment",
    "binder_alignment_source_sha256",
    "binder_alignment_reference_sha256",
    "binder_alignment_center",
];
fn capture_display_alignment(metadata: &Value, state: &mut Value) {
    // Keep the display frame per tab. Artifact metadata is shared by duplicate
    // tabs, while each tab may have a different fit or use original coordinates.
    for key in ALIGNMENT_KEYS {
        state[key] = metadata[key].clone();
    }
}
fn clear_display_alignment(metadata: &mut Value, reset_camera: bool) {
    if let Some(object) = metadata.as_object_mut() {
        for key in ALIGNMENT_KEYS {
            object.remove(key);
        }
    }
    if let Some(state) = metadata["view_state"].as_object_mut() {
        for key in ALIGNMENT_KEYS {
            state.remove(key);
        }
        if reset_camera {
            state.remove("camera");
        }
    }
}
fn restore_display_alignment(
    molecule: &mut scene::Molecule,
    metadata: &mut Value,
    actual_sha: &str,
) -> Result<bool, String> {
    let saved = &metadata["view_state"];
    let source = if saved.get("binder_alignment").is_some() {
        saved
    } else {
        &*metadata
    };
    if ALIGNMENT_KEYS.iter().all(|key| source[*key].is_null()) {
        clear_display_alignment(metadata, false);
        return Ok(false);
    }
    let digest =
        |value: &str| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit());
    let source_sha = text(source, "binder_alignment_source_sha256");
    if !digest(source_sha) || !source_sha.eq_ignore_ascii_case(actual_sha) {
        return Err("Saved alignment does not match the original structure SHA-256".into());
    }
    if !digest(text(source, "binder_alignment_reference_sha256")) {
        return Err("Saved alignment has no valid reference structure identity".into());
    }
    let receipt = &source["binder_alignment"];
    if text(receipt, "status") != "aligned" {
        return Err("Saved alignment receipt does not describe a completed fit".into());
    }
    let rotation = serde_json::from_value(receipt["rotation"].clone())
        .map_err(|_| "Saved alignment rotation must be a finite 3 × 3 matrix".to_owned())?;
    let translation =
        serde_json::from_value(receipt["translation_angstrom"].clone()).map_err(|_| {
            "Saved alignment translation must contain three finite coordinates".to_owned()
        })?;
    let center =
        serde_json::from_value(source["binder_alignment_center"].clone()).map_err(|_| {
            "Saved alignment display center must contain three finite coordinates".to_owned()
        })?;
    let restored = ALIGNMENT_KEYS.map(|key| (key, source[key].clone()));
    alignment::apply_display_transform(molecule, rotation, translation, center)?;
    for (key, value) in restored {
        metadata[key] = value;
    }
    Ok(true)
}
pub(super) fn view_state(view: &View) -> Value {
    let mut state = json!({"camera":{"yaw":view.camera.yaw,"pitch":view.camera.pitch,"zoom":view.camera.zoom,"pan":[view.camera.pan.x,view.camera.pan.y],"ambient":view.camera.ambient,"bloom":view.camera.bloom,"distance":view.camera.distance,"span":view.camera.span},"style":view.style.name(),"selected":view.selected,"hotspots":view.hotspots,"chains":view.chains,"labels":view.labels,"visible":view.visible});
    capture_display_alignment(&view.metadata, &mut state);
    state
}
fn restore_camera(camera: &mut scene::Camera, value: &Value) {
    for (name, dest) in [
        ("yaw", &mut camera.yaw),
        ("pitch", &mut camera.pitch),
        ("zoom", &mut camera.zoom),
    ] {
        if let Some(n) = value[name]
            .as_f64()
            .map(|n| n as f32)
            .filter(|n| n.is_finite())
        {
            *dest = n;
        }
    }
    camera.zoom = camera.zoom.clamp(0.25, 4.);
    if let (Some(x), Some(y)) = (
        value["pan"][0].as_f64().map(|n| n as f32),
        value["pan"][1].as_f64().map(|n| n as f32),
    ) && x.is_finite()
        && y.is_finite()
    {
        camera.pan = Vec2::new(x, y);
    }
    for (name, dest) in [
        ("distance", &mut camera.distance),
        ("span", &mut camera.span),
    ] {
        if let Some(n) = value[name]
            .as_f64()
            .filter(|n| n.is_finite() && (0.01..=100_000_000.).contains(n))
        {
            *dest = n as f32;
        }
    }
    camera.ambient = value["ambient"].as_bool().unwrap_or(true);
    camera.bloom = value["bloom"].as_bool().unwrap_or(true);
}
fn restore_view(view: &mut View, value: &Value) {
    restore_camera(&mut view.camera, &value["camera"]);
    view.style = match text(value, "style") {
        "sticks" => scene::Representation::Sticks,
        "spheres" => scene::Representation::Spheres,
        "backbone trace" => scene::Representation::Trace,
        "surface" => scene::Representation::Surface,
        _ => scene::Representation::Cartoon,
    };
    view.selected = serde_json::from_value(value["selected"].clone()).ok();
    view.hotspots = serde_json::from_value(value["hotspots"].clone()).unwrap_or_default();
    view.hotspots.retain_existing(&view.molecule);
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
        let Some(mut metadata) = self.view_reference(target).cloned() else {
            return false;
        };
        // The copy already has live transformed coordinates. Its receipt must
        // follow that same live view, even before the next state capture.
        capture_display_alignment(&original.metadata, &mut metadata);
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
            hotspots: original.hotspots.clone(),
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
        let mut molecule = match molecule {
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
        if let Err(error) = restore_display_alignment(&mut molecule, &mut metadata, &actual_sha) {
            clear_display_alignment(&mut metadata, true);
            self.log(format!(
                "{}: {error}. Opened the original structure; display alignment requires a refit.",
                display_name(&metadata)
            ));
        }
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
            hotspots: scene::Hotspots::default(),
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
        // A hidden tab may finish its CPU surface. Upload/dispose that bounded
        // result too, so it cannot stall another tab's background builder.
        for view in self.views.values() {
            view.renderer.poll_surface(&self.gl);
        }
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
                    for key in ALIGNMENT_KEYS {
                        object.remove(key);
                    }
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

#[cfg(test)]
mod persistence_tests {
    use super::*;
    use sha2::Digest;

    const SOURCE: &[u8] = b"ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 80.00           C\nATOM      2  CA  GLY A   2       3.000   2.000   3.000  1.00 80.00           C\nATOM      3  CA  SER A   3       1.000   5.000   4.000  1.00 80.00           C\nEND\n";

    fn source() -> (scene::Molecule, String) {
        (
            scene::Molecule::parse(SOURCE, "pdb", "saved candidate").unwrap(),
            format!("{:x}", sha2::Sha256::digest(SOURCE)),
        )
    }
    fn metadata(sha: &str) -> Value {
        json!({
            "sha256": sha,
            "binder_alignment": {
                "status": "aligned",
                "rotation": [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]],
                "translation_angstrom": [10., -20., 30.]
            },
            "binder_alignment_source_sha256": sha,
            "binder_alignment_reference_sha256": "abcdef0123456789".repeat(4),
            "binder_alignment_center": [5., 7., 11.]
        })
    }
    #[test]
    fn per_tab_saved_fit_restores_source_coordinates_identity_and_camera_scale() {
        let (mut molecule, sha) = source();
        let original = molecule.clone();
        let fitted_metadata = metadata(&sha);
        let mut state = json!({"camera":{
            "yaw": 0.3, "pitch": -0.2, "zoom": 1.8,
            "pan": [0.05, -0.03], "distance": 500., "span": 370.
        }});
        capture_display_alignment(&fitted_metadata, &mut state);
        // Artifact delivery supplies fresh metadata; the per-tab view_state is
        // the only retained display frame and must recreate its full receipt.
        let mut downloaded = json!({"sha256":sha,"view_state":state});
        assert!(restore_display_alignment(&mut molecule, &mut downloaded, &sha).unwrap());
        for (before, after) in original.atoms.iter().zip(&molecule.atoms) {
            assert_eq!(
                after.p,
                scene::V3(-before.p.1 + 10., before.p.0 - 20., before.p.2 + 30.)
            );
            assert_eq!(after.name, before.name);
        }
        assert_eq!(molecule.center, scene::V3(5., 7., 11.));
        for key in ALIGNMENT_KEYS {
            assert_eq!(downloaded[key], fitted_metadata[key]);
        }
        let mut camera = scene::Camera::fit(&molecule);
        restore_camera(&mut camera, &downloaded["view_state"]["camera"]);
        assert_eq!(
            (camera.distance, camera.span, camera.zoom),
            (500., 370., 1.8)
        );
        assert_eq!((camera.yaw, camera.pitch), (0.3, -0.2));
        assert_eq!(camera.pan, Vec2::new(0.05, -0.03));
        // Restoring is always from original retained bytes, never from an
        // already fitted copy that would compound a transform after restart.
        let (mut again, _) = source();
        restore_display_alignment(&mut again, &mut downloaded, &sha).unwrap();
        assert_eq!(
            again.atoms.iter().map(|a| a.p).collect::<Vec<_>>(),
            molecule.atoms.iter().map(|a| a.p).collect::<Vec<_>>()
        );
    }
    #[test]
    fn saved_fit_rejects_changed_source_and_missing_reference_identity() {
        let (original, sha) = source();
        for (key, value) in [
            ("binder_alignment_source_sha256", json!("1".repeat(64))),
            ("binder_alignment_source_sha256", json!("")),
            ("binder_alignment_reference_sha256", json!("not-a-sha")),
            ("binder_alignment_reference_sha256", Value::Null),
        ] {
            let mut state = metadata(&sha);
            state[key] = value;
            let mut reopened = original.clone();
            assert!(restore_display_alignment(&mut reopened, &mut state, &sha).is_err());
            assert_eq!(
                reopened.atoms.iter().map(|a| a.p).collect::<Vec<_>>(),
                original.atoms.iter().map(|a| a.p).collect::<Vec<_>>()
            );
            assert_eq!(reopened.center, original.center);
        }
    }
    #[test]
    fn invalid_or_legacy_fit_is_removed_without_discarding_structure_or_display_preferences() {
        let (original, sha) = source();
        let mut bad_matrix = metadata(&sha);
        bad_matrix["binder_alignment"]["rotation"] =
            json!([[2., 0., 0.], [0., 1., 0.], [0., 0., 1.]]);
        let mut missing_center = metadata(&sha);
        missing_center["binder_alignment_center"] = Value::Null;
        let legacy = json!({"binder_alignment":metadata(&sha)["binder_alignment"]});
        for mut invalid in [bad_matrix, missing_center, legacy] {
            let mut state =
                json!({"camera":{"yaw":2.,"distance":500.},"style":"surface","visible":false});
            capture_display_alignment(&invalid, &mut state);
            invalid["view_state"] = state;
            let mut molecule = original.clone();
            assert!(restore_display_alignment(&mut molecule, &mut invalid, &sha).is_err());
            clear_display_alignment(&mut invalid, true);
            for key in ALIGNMENT_KEYS {
                assert!(invalid.get(key).is_none());
                assert!(invalid["view_state"].get(key).is_none());
            }
            assert!(invalid["view_state"].get("camera").is_none());
            assert_eq!(invalid["view_state"]["style"], "surface");
            assert_eq!(invalid["view_state"]["visible"], false);
            assert_eq!(
                molecule.atoms.iter().map(|a| a.p).collect::<Vec<_>>(),
                original.atoms.iter().map(|a| a.p).collect::<Vec<_>>()
            );
            let fitted = scene::Camera::fit(&molecule);
            let mut restored = fitted;
            restore_camera(&mut restored, &invalid["view_state"]["camera"]);
            assert_eq!(
                (restored.yaw, restored.distance),
                (fitted.yaw, fitted.distance)
            );
        }
    }
    #[test]
    fn an_unaligned_duplicate_tab_does_not_inherit_another_tabs_artifact_fit() {
        let (mut molecule, sha) = source();
        let before = molecule.atoms.iter().map(|a| a.p).collect::<Vec<_>>();
        let mut shared_metadata = metadata(&sha);
        let mut unaligned_state = json!({"camera":{"distance":120.,"span":90.}});
        capture_display_alignment(&json!({}), &mut unaligned_state);
        shared_metadata["view_state"] = unaligned_state;
        assert!(!restore_display_alignment(&mut molecule, &mut shared_metadata, &sha).unwrap());
        assert_eq!(
            molecule.atoms.iter().map(|a| a.p).collect::<Vec<_>>(),
            before
        );
        assert!(shared_metadata.get("binder_alignment").is_none());
        assert_eq!(shared_metadata["view_state"]["camera"]["distance"], 120.);
    }
    #[test]
    fn invalid_saved_camera_scales_keep_the_fitted_projection() {
        let (molecule, _) = source();
        let fitted = scene::Camera::fit(&molecule);
        for invalid in [json!(-1.), json!(0.), json!(1e300), Value::Null] {
            let mut camera = fitted;
            restore_camera(&mut camera, &json!({"distance":invalid,"span":invalid}));
            assert_eq!(
                (camera.distance, camera.span),
                (fitted.distance, fitted.span)
            );
        }
    }
}
