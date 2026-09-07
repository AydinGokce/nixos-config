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
fn display_name(metadata: &Value) -> String {
    let name = short_name(text(metadata, "name"));
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
fn view_state(view: &View) -> Value {
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
    pub(super) fn demo_views(&mut self) {
        for slot in 0..self.state.view_count {
            let molecule = scene::Molecule::reference();
            let bytes = Arc::new(scene::Molecule::reference_bytes());
            let metadata = json!({"name":"Cas9 · 4OO8 / A+B+C","format":"pdb","source_kind":"demo","source_url":"https://www.rcsb.org/structure/4OO8","load_token":uid()});
            self.view_loading[slot] = Some(view_token(&metadata));
            self.accept_molecule(slot, metadata, bytes, Ok(molecule));
        }
    }
    pub(super) fn reset_view(&mut self) {
        let slot = self.state.selected_view;
        if let Some(view) = self.views[slot].as_mut() {
            view.camera = scene::Camera::fit(&view.molecule);
        }
        if self.state.link_views {
            for view in self.views.iter_mut().flatten() {
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
        if slot >= 4 {
            return;
        }
        metadata["load_token"] = json!(uid());
        self.view_loading[slot] = Some(view_token(&metadata));
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
        if slot >= 4 || self.view_loading[slot].as_deref() != Some(&view_token(&metadata)) {
            return;
        }
        self.view_loading[slot] = None;
        let molecule = match molecule {
            Ok(molecule) => molecule,
            Err(error) => {
                self.log(format!(
                    "Cannot display {}: {error}",
                    text(&metadata, "name")
                ));
                return;
            }
        };
        use sha2::Digest;
        let actual_sha = format!("{:x}", sha2::Sha256::digest(bytes.as_slice()));
        if !text(&metadata, "sha256").is_empty() && text(&metadata, "sha256") != actual_sha {
            self.log(format!("Structure identity changed for {}. Reopen the local file explicitly to load its new revision; retained annotations remain bound to the old SHA.",text(&metadata,"name")));
            return;
        }
        metadata["sha256"] = json!(actual_sha);
        let renderer = match scene::Renderer::new(&self.gl, &molecule) {
            Ok(renderer) => renderer,
            Err(error) => {
                self.log(format!("GPU renderer: {error}"));
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
        if let Some(old) = self.views[slot].replace(view) {
            old.renderer.destroy(&self.gl);
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
        let purpose = Purpose::Artifact(target.clone());
        if self.busy(&purpose) {
            self.log(
                "That artifact slot is still downloading. Wait for its current request to finish.",
            );
            return;
        }
        let Some(session) = self.session.as_mut() else {
            return;
        };
        match session.artifact(id) {
            Ok(operation) => {
                self.artifact_metadata
                    .entry(id.into())
                    .or_insert_with(|| json!({"artifact_id":id}))["download_operation"] =
                    json!(operation);
                if let ArtifactTarget::View(slot) = target {
                    self.view_loading[slot] = Some(format!("download:{operation}"));
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
            Err(error) => self.log(error.to_string()),
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
        if let Some(view) = self.views[self.state.selected_view].as_ref() {
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
        if let Some(view) = self.views[self.state.selected_view].as_ref()
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
        let previous = self.state.view_refs.clone();
        self.state.view_refs = (0..4)
            .filter_map(|slot| {
                if self.view_loading[slot].is_some() {
                    return previous
                        .iter()
                        .find(|v| v["slot"].as_u64() == Some(slot as u64))
                        .cloned();
                }
                self.views[slot].as_ref().map(|view| {
                    let mut value = view.metadata.clone();
                    if let Some(object) = value.as_object_mut() {
                        object.remove("load_token");
                    }
                    value["slot"] = json!(slot);
                    value["view_state"] = view_state(view);
                    value
                })
            })
            .collect();
    }
    pub(super) fn restore_views(&mut self, ctx: &egui::Context) {
        let saved = self.state.view_refs.clone();
        for mut metadata in saved {
            let slot = metadata["slot"].as_u64().unwrap_or(0) as usize;
            if slot >= 4 {
                continue;
            }
            let artifact = text(&metadata, "artifact_id").to_owned();
            if !artifact.is_empty() {
                self.artifact_metadata.insert(artifact.clone(), metadata);
                self.request_artifact(&artifact, ArtifactTarget::View(slot));
            } else if text(&metadata, "source_kind") == "local" {
                let path = PathBuf::from(text(&metadata, "local_path"));
                self.load_structure(path, metadata, slot, ctx);
            } else if text(&metadata, "source_kind") == "demo"
                && let Some(view) = self.views[slot].as_mut()
            {
                restore_view(view, &metadata["view_state"]);
                metadata["load_token"] = view.metadata["load_token"].clone();
                view.metadata = metadata;
            }
        }
    }
    pub(super) fn viewports(&mut self, ui: &mut egui::Ui) {
        let count = self.state.view_count;
        let columns = if count == 1 { 1 } else { 2 };
        let grid_rows = count.div_ceil(columns);
        let width = (ui.available_width() - (columns - 1) as f32 * 4.) / columns as f32;
        let height = (ui.available_height() - (grid_rows - 1) as f32 * 4.) / grid_rows as f32;
        let mut linked = None;
        for row in 0..grid_rows {
            ui.horizontal(|ui|{for column in 0..columns{let slot=row*columns+column;if slot>=count{continue;}ui.allocate_ui_with_layout(Vec2::new(width,height),egui::Layout::top_down(egui::Align::Min),|ui|{
            let active=self.state.selected_view==slot;let name=self.views[slot].as_ref().map(|view|view.molecule.name.as_str()).unwrap_or("No structure selected");let header=ui.add_sized([width,24.],egui::Button::new(RichText::new(format!("{}  {}",slot+1,name)).color(if active{AMBER}else{Color32::LIGHT_GRAY})).selected(active));if header.clicked(){self.state.selected_view=slot;}
            if let Some(view)=self.views[slot].as_mut(){
                let source=if text(&view.metadata,"source_kind")=="demo"{"EXPERIMENTAL DEMO · 4OO8 · 2.50 Å · not a prediction".into()}else if text(&view.metadata,"source_kind")=="local"{"LOCAL STRUCTURE · original coordinates".into()}else{format!("{} · {} · {}",text(&view.metadata,"model"),short_name(text(&view.metadata,"sample_id")),text(&view.metadata,"job_state"))};ui.label(RichText::new(source).size(10.).color(if text(&view.metadata,"source_kind")=="demo"{AMBER}else{GREEN}));
                egui::ScrollArea::horizontal().id_salt(("sequence",slot)).max_height(22.).show(ui,|ui|{ui.horizontal(|ui|{for residue in &view.molecule.residues{if !view.chains[residue.chain]{continue;}let selected=view.selected.as_ref()==Some(&residue.key);if ui.add(egui::Button::new(RichText::new(residue.letter.to_string()).monospace().color(view.molecule.chains[residue.chain].color)).min_size(Vec2::new(9.,17.)).selected(selected)).on_hover_text(residue.key.to_string()).clicked(){view.selected=Some(residue.key.clone());self.state.selected_view=slot;}}});});
                let before=view.selected.clone();let changed=scene::viewport(ui,&view.molecule,&view.renderer,&mut view.camera,view.style,slot,&mut view.selected,view.visible,view.labels,self.state.show_axes,&view.chains);if before!=view.selected{self.state.selected_view=slot;}
if changed{self.state.selected_view=slot;if self.state.link_views{linked=Some(view.camera);}}
            }else{ui.centered_and_justified(|ui|{ui.label("Choose an actual result under Runs / results, or open a local PDB / mmCIF.");});}
        });}});
        }
        if let Some(camera) = linked {
            for view in self.views.iter_mut().flatten() {
                view.camera.yaw = camera.yaw;
                view.camera.pitch = camera.pitch;
                view.camera.zoom = camera.zoom;
                view.camera.pan = camera.pan;
                view.camera.ambient = camera.ambient;
                view.camera.bloom = camera.bloom;
            }
        }
    }
    pub(super) fn inspector(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        let slot = self.state.selected_view;
        Self::section(ui, &format!("OBJECTS / VIEW {}", slot + 1));
        if self.view_loading[slot].is_some() {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label("Downloading / reading structure…");
            });
        }
        let Some(view) = self.views[slot].as_mut() else {
            ui.label("No structure in this viewport.");
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
                    });
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
