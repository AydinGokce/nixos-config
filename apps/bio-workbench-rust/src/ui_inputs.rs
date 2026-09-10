use super::*;
use ui_state::{Editor, RunIntent, infer_file};

fn model_name(model: &Value) -> String {
    let name = text(model, "name");
    if text(model, "id") == "rfaa" {
        name.replace(" (parked)", "").replace(" — parked", "")
    } else {
        name.into()
    }
}
fn pair_status<'a>(pair: &'a Value, job: Option<&'a Value>) -> &'a str {
    if matches!(text(pair, "state"), "incompatible" | "rejected") {
        "Skipped (incompatible)"
    } else if let Some(job) = job {
        text(job, "state")
    } else {
        match text(pair, "state") {
            "compatible" => "Queued for dispatch",
            "pending" => "Validating",
            state => state,
        }
    }
}
fn select(ui: &mut egui::Ui, id: impl std::hash::Hash, value: &mut String, choices: &[&str]) {
    egui::ComboBox::from_id_salt(id)
        .width(130.)
        .selected_text(value.as_str())
        .show_ui(ui, |ui| {
            for choice in choices {
                ui.selectable_value(value, (*choice).into(), *choice);
            }
        });
}
impl Workbench {
    pub(super) fn picked(&mut self, kind: Pick, paths: Vec<PathBuf>, ctx: &egui::Context) {
        for path in paths {
            let path = std::fs::canonicalize(&path).unwrap_or(path);
            match &kind {
                Pick::Key => self.connection.key_path = path.to_string_lossy().into_owned(),
                Pick::Labels(model) => {
                    if self.busy(&Purpose::Upload(UploadTarget::Labels(model.clone()))) {
                        self.log("The prior label upload is still active; select the replacement after it finishes.");
                        continue;
                    }
                    if let Some(settings) = self
                        .state
                        .settings
                        .get_mut(model)
                        .and_then(Value::as_object_mut)
                    {
                        settings.remove("labels_upload_id");
                    }
                    self.state
                        .extra
                        .entry("label_paths".into())
                        .or_insert_with(|| json!({}))[model] = json!(path);
                    self.start_upload(path, UploadTarget::Labels(model.clone()));
                }
                Pick::Attachment(input_id) => {
                    let name = path
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into_owned();
                    if self.busy(&Purpose::Upload(UploadTarget::Attachment(
                        input_id.clone(),
                        name.clone(),
                    ))) {
                        self.log("The prior attachment upload is still active; select the replacement after it finishes.");
                        continue;
                    }
                    if let Some(input) = self
                        .state
                        .inputs
                        .iter_mut()
                        .find(|input| &input.id == input_id)
                    {
                        let name = path
                            .file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .into_owned();
                        if let Some(attachments) = input
                            .source
                            .get_mut("attachments")
                            .and_then(Value::as_object_mut)
                        {
                            attachments.remove(&name);
                        }
                        input
                            .attachment_paths
                            .insert(name, path.to_string_lossy().into_owned());
                    }
                }
                Pick::Structure => {
                    let (format, _) = infer_file(&path);
                    let metadata = json!({"name":path.file_name().unwrap_or_default().to_string_lossy(),"format":format,"source_kind":"local","local_path":path});
                    self.open_local_tab(path, metadata, ctx);
                }
                Pick::Inputs => {
                    if self.state.inputs.len() >= 128 {
                        self.log("Input limit is 128; remaining files were not added.");
                        break;
                    }
                    let (format, kind) = infer_file(&path);
                    let id = uid();
                    let chain_id = self.state.next_chain();
                    self.state.inputs.push(Input {
                        id,
                        name: path
                            .file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .into_owned(),
                        molecule_type: kind.into(),
                        chain_id,
                        source: json!({"kind":"upload","format":format,"upload_id":""}),
                        local_path: Some(path.to_string_lossy().into_owned()),
                        ..Default::default()
                    });
                }
            }
        }
    }
    fn start_upload(&mut self, path: PathBuf, target: UploadTarget) {
        if self.busy(&Purpose::Upload(target.clone())) {
            return;
        }
        if let Some(session) = self.session.as_mut() {
            match session.upload(path.clone()) {
                Ok(id) => {
                    if let UploadTarget::Run(run_id, index) = &target
                        && let Some(run) = self.state.run.as_mut().filter(|run| &run.id == run_id)
                        && let Some(upload) = run.uploads.get_mut(*index)
                    {
                        upload.operation = id.clone();
                    }
                    self.pending.insert(
                        id,
                        Pending {
                            purpose: Purpose::Upload(target),
                            label: format!(
                                "Upload {}",
                                path.file_name().unwrap_or_default().to_string_lossy()
                            ),
                            done: 0,
                            total: 0,
                        },
                    );
                }
                Err(error) => {
                    if let UploadTarget::Run(run_id, _) = &target {
                        self.run_error(run_id, &error.to_string(), false);
                    }
                    self.log(format!("Upload failed: {error}"));
                }
            }
        }
    }
    pub(super) fn uploaded(&mut self, target: UploadTarget, receipt: Value) {
        if let UploadTarget::Run(run_id, index) = &target {
            if let Some(mut run) = self.state.run.take() {
                let result = if &run.id == run_id {
                    run.accept_upload_for_draft(*index, &receipt, &mut self.state)
                } else {
                    Ok(())
                };
                self.state.run = Some(run);
                if let Err(error) = result {
                    self.run_error(run_id, &error, false);
                }
            }
            return;
        }
        let id = text(&receipt, "upload_id");
        if id.is_empty() {
            self.log("Upload did not return a completed receipt.");
            return;
        }
        match target {
            UploadTarget::Input(input_id) => {
                if let Some(input) = self
                    .state
                    .inputs
                    .iter_mut()
                    .find(|input| input.id == input_id)
                {
                    input.source["upload_id"] = json!(id);
                }
            }
            UploadTarget::Attachment(input_id, name) => {
                if let Some(input) = self
                    .state
                    .inputs
                    .iter_mut()
                    .find(|input| input.id == input_id)
                {
                    if !input.source["attachments"].is_object() {
                        input.source["attachments"] = json!({});
                    }
                    input.source["attachments"][name] = json!(id);
                }
            }
            UploadTarget::Labels(model) => {
                self.state
                    .settings
                    .entry(model)
                    .or_insert_with(|| json!({}))["labels_upload_id"] = json!(id);
            }
            UploadTarget::Run(_, _) => unreachable!(),
        }
    }
    pub(super) fn flash_input(&mut self, id: &str) {
        self.input_flash = Some((id.into(), Instant::now(), true));
        self.sidebar_tab = 0;
    }
    pub(super) fn run_error(&mut self, id: &str, message: &str, uncertain: bool) {
        if let Some(run) = self.state.run.as_mut().filter(|run| run.id == id) {
            run.error = message.into();
            run.stage = if run.operation.is_empty() && !run.submission_attempted {
                "upload_failed"
            } else if uncertain {
                "uncertain"
            } else {
                "rejected"
            }
            .into();
            self.run_after_uploads = false;
        }
    }
    pub(super) fn begin_run(&mut self) {
        self.run_input_error.clear();
        if !self.connected {
            self.connection_open = true;
            self.log("Connect to the head before running predictions.");
            return;
        }
        if self
            .state
            .run
            .as_ref()
            .is_some_and(|run| !run.accepts_new_run())
        {
            self.preview_open = true;
            if !self.run_pending() {
                self.resume_run();
            }
            return;
        }
        let endpoint = self.run_endpoint();
        let run = match RunIntent::capture(&self.state, endpoint) {
            Ok(run) => run,
            Err(error) => {
                self.run_input_error = error.clone();
                self.preview_open = false;
                self.log(error);
                return;
            }
        };
        self.preview_open = true;
        self.state.add_active();
        self.state.run = Some(run);
        self.run_batch = None;
        self.persist();
        if !self.save_error.is_empty() {
            self.log(
                "Run preparation could not be saved. Resolve the local save error before retrying.",
            );
            return;
        }
        self.resume_run();
    }
    pub(super) fn run_pending(&self) -> bool {
        self.state.run.as_ref().is_some_and(|run| {
            self.pending.values().any(|pending| match &pending.purpose {
                Purpose::Run(id) | Purpose::Upload(UploadTarget::Run(id, _)) => id == &run.id,
                _ => false,
            })
        })
    }
    pub(super) fn resume_run(&mut self) {
        let Some(run) = self.state.run.clone() else {
            return;
        };
        if run.endpoint != self.run_endpoint() {
            self.log("This run belongs to another head. Reconnect to its original endpoint to recover it.");
            return;
        }
        self.preview_open = true;
        if !run.batch_id.is_empty() {
            let id = run.batch_id;
            self.request("batch.get", json!({"batch_id":id}), Purpose::Batch(id));
            return;
        }
        if self.run_pending() {
            return;
        }
        if let Some(current) = self.state.run.as_mut() {
            current.error.clear();
        }
        if !run.operation.is_empty() {
            if let Some(current) = self.state.run.as_mut() {
                current.stage = "requesting".into();
            }
            self.retry(&run.operation, Purpose::Run(run.id), "batch.run".into());
            return;
        }
        self.run_after_uploads = true;
        if let Some(current) = self.state.run.as_mut() {
            current.stage = "uploading".into();
        }
        for (index, upload) in run
            .uploads
            .iter()
            .enumerate()
            .filter(|(_, upload)| !upload.complete)
        {
            let purpose = Purpose::Upload(UploadTarget::Run(run.id.clone(), index));
            if upload.operation.is_empty() {
                self.start_upload(
                    PathBuf::from(&upload.path),
                    UploadTarget::Run(run.id.clone(), index),
                );
            } else {
                self.retry(&upload.operation, purpose, "Resume captured upload".into());
            }
        }
        self.persist();
        self.continue_run();
    }
    pub(super) fn continue_run(&mut self) {
        if !self.run_after_uploads || self.run_pending() {
            return;
        }
        let Some(run) = self.state.run.clone() else {
            return;
        };
        if !run.error.is_empty() || run.uploads.iter().any(|upload| !upload.complete) {
            return;
        }
        self.run_after_uploads = false;
        if run.endpoint != self.run_endpoint()
            || !run.operation.is_empty()
            || !run.batch_id.is_empty()
        {
            return;
        }
        if run.request.to_string().len() + 256 > rpc::MAX_WIRE {
            self.run_error(
                &run.id,
                "Input exceeds the request limit. Use a file upload for large inputs.",
                false,
            );
            return;
        }
        // Persist the attempted-send boundary before calling the durable session:
        // a crash can otherwise leave a sent journal entry without its UI ID.
        if let Some(current) = self.state.run.as_mut() {
            current.submission_attempted = true;
        }
        self.persist();
        if !self.save_error.is_empty() {
            self.run_error(
                &run.id,
                "Could not save the prepared run locally. Retry after resolving the save error.",
                true,
            );
            return;
        }
        if let Some(current) = self.state.run.as_mut() {
            current.stage = "requesting".into();
        }
        if let Some(operation) =
            self.request("batch.run", run.request, Purpose::Run(run.id.clone()))
        {
            if let Some(current) = self.state.run.as_mut() {
                current.operation = operation;
            }
            self.persist();
        } else {
            self.run_error(
                &run.id,
                "Could not send the saved run request. Retry keeps its exact request key.",
                true,
            );
        }
    }
    pub(super) fn inputs_panel(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        Self::section(ui, "INPUT DRAFT");
        ui.horizontal(|ui| {
            ui.label("Run name");
            ui.add(
                egui::TextEdit::singleline(&mut self.state.name)
                    .hint_text("Molecular run")
                    .desired_width(f32::INFINITY),
            );
        });
        ui.horizontal(|ui| {
            ui.selectable_value(&mut self.state.mode, "batch".into(), "Independent inputs");
            ui.selectable_value(&mut self.state.mode, "assembly".into(), "Assembly");
        });
        ui.horizontal(|ui| {
            if ui
                .selectable_label(self.state.editor.kind == "text", "Paste")
                .clicked()
            {
                self.state.switch_editor("text");
            }
            if ui
                .selectable_label(self.state.editor.kind == "library", "Library ref")
                .clicked()
            {
                self.state.switch_editor("library");
            }
            if ui.button("Files…").clicked() {
                self.choose_files(Pick::Inputs, ctx);
            }
        });
        ui.horizontal(|ui| {
            ui.label("Name");
            ui.add(
                egui::TextEdit::singleline(&mut self.state.editor.name)
                    .desired_width(f32::INFINITY),
            );
        });
        ui.horizontal(|ui| {
            ui.label("Type");
            select(
                ui,
                "editor-type",
                &mut self.state.editor.molecule_type,
                &["protein", "dna", "rna", "ligand", "assembly", "structure"],
            );
        });
        if self.state.editor.kind == "text" {
            ui.horizontal(|ui| {
                ui.label("Format");
                select(
                    ui,
                    "editor-format",
                    &mut self.state.editor.format,
                    &[
                        "sequence",
                        "fasta",
                        "smiles",
                        "ccd",
                        "sdf",
                        "pdb",
                        "mmcif",
                        "library-json",
                        "contigs",
                    ],
                );
            });
        }
        if self.state.mode == "assembly" {
            ui.horizontal(|ui| {
                ui.label("Chain");
                ui.add(
                    egui::TextEdit::singleline(&mut self.state.editor.chain_id)
                        .hint_text("Automatic unique ID")
                        .desired_width(130.),
                );
            });
        }
        egui::ScrollArea::vertical()
            .id_salt("paste-editor")
            .max_height(135.)
            .show(ui, |ui| {
                ui.add(
                    egui::TextEdit::multiline(&mut self.state.editor.text)
                        .font(egui::TextStyle::Monospace)
                        .desired_rows(7)
                        .desired_width(f32::INFINITY)
                        .hint_text(
                            RichText::new(if self.state.editor.kind == "library" {
                                "construct:name@revision or assembly:name@revision"
                            } else {
                                "Paste sequence(s), FASTA, or the selected format"
                            })
                            .color(Color32::GRAY),
                        ),
                );
            });
        ui.horizontal(|ui| {
            if ui.button("Add input").clicked() {
                self.state.add_active();
            }
            if ui.button("Clear editor").clicked() {
                self.state.editor = Editor {
                    kind: self.state.editor.kind.clone(),
                    molecule_type: self.state.editor.molecule_type.clone(),
                    format: self.state.editor.format.clone(),
                    ..Default::default()
                };
            }
            if ui.button("Browse library").clicked() {
                self.open_library();
            }
        });
        if self.state.active_input().is_some() {
            ui.colored_label(
                GREEN,
                "The active editor is included in Run; Add input is optional.",
            );
        }
        let mut remove = None;
        let mut attachment = None;
        for (index, input) in self.state.inputs.iter_mut().enumerate() {
            let flash = self
                .input_flash
                .as_ref()
                .filter(|(id, _, _)| id == &input.id);
            let intensity = flash.map_or(0., |(_, started, _)| {
                (1. - started.elapsed().as_secs_f32() / 2.2).clamp(0., 1.)
            });
            let scroll = flash.is_some_and(|(_, _, scroll)| *scroll);
            let row = egui::Frame::new()
                .fill(Color32::from_rgba_unmultiplied(
                    50,
                    210,
                    100,
                    (intensity * 115.) as u8,
                ))
                .inner_margin(3.)
                .show(ui, |ui| {
                    ui.push_id(input.id.clone(), |ui| {
                        egui::CollapsingHeader::new(format!("{} · {}", index + 1, input.name))
                            .default_open(false)
                            .show(ui, |ui| {
                                ui.text_edit_singleline(&mut input.name);
                                select(
                                    ui,
                                    "input-type",
                                    &mut input.molecule_type,
                                    &["protein", "dna", "rna", "ligand", "assembly", "structure"],
                                );
                                if self.state.mode == "assembly" {
                                    ui.horizontal(|ui| {
                                        ui.label("Chain");
                                        ui.text_edit_singleline(&mut input.chain_id);
                                    });
                                }
                                if text(&input.source, "kind") != "library" {
                                    let mut format = text(&input.source, "format").to_owned();
                                    select(
                                        ui,
                                        "input-format",
                                        &mut format,
                                        &[
                                            "sequence",
                                            "fasta",
                                            "smiles",
                                            "ccd",
                                            "sdf",
                                            "pdb",
                                            "mmcif",
                                            "library-json",
                                            "contigs",
                                        ],
                                    );
                                    input.source["format"] = json!(format);
                                }
                                if text(&input.source, "kind") == "text" {
                                    let mut value = text(&input.source, "text").to_owned();
                                    if ui
                                        .add(
                                            egui::TextEdit::multiline(&mut value)
                                                .desired_rows(3)
                                                .desired_width(f32::INFINITY)
                                                .font(egui::TextStyle::Monospace),
                                        )
                                        .changed()
                                    {
                                        input.source["text"] = json!(value);
                                    }
                                }
                                if let Some(path) = &input.local_path {
                                    ui.small(path);
                                    ui.small(if input.needs_upload() {
                                        "Uploads when you click Run"
                                    } else {
                                        "Immutable upload ready"
                                    });
                                } else if text(&input.source, "kind") == "library" {
                                    ui.monospace(text(&input.source, "ref"));
                                }
                                for name in input.attachment_paths.keys() {
                                    ui.small(format!("Attachment: {name}"));
                                }
                                ui.horizontal(|ui| {
                                    if text(&input.source, "format") == "library-json"
                                        && ui.button("Attach SDF…").clicked()
                                    {
                                        attachment = Some(input.id.clone());
                                    }
                                    if ui.button("Remove input").clicked() {
                                        remove = Some(index);
                                    }
                                });
                            });
                    });
                });
            if scroll {
                row.response.scroll_to_me(Some(egui::Align::Center));
                if let Some((_, _, scroll)) = self.input_flash.as_mut() {
                    *scroll = false;
                }
            }
        }
        if let Some((_, started, _)) = &self.input_flash {
            if started.elapsed() < Duration::from_millis(2200) {
                ctx.request_repaint_after(Duration::from_millis(16));
            } else {
                self.input_flash = None;
            }
        }
        if let Some(index) = remove {
            self.state.inputs.remove(index);
        }
        if let Some(id) = attachment {
            self.choose_files(Pick::Attachment(id), ctx);
        }
        Self::section(ui, "INSTALLED MODELS");
        if rows(&self.catalog, "models").is_empty() {
            ui.small("Connect to load supported models and settings.");
        }
        for model in rows(&self.catalog, "models") {
            let id = text(model, "id");
            let mut selected = self.state.models.contains(id);
            let enabled = model["enabled"] == true;
            ui.horizontal(|ui| {
                if ui
                    .add_enabled(
                        enabled || selected,
                        egui::Checkbox::new(&mut selected, model_name(model)),
                    )
                    .on_hover_text(if enabled {
                        text(model, "description")
                    } else {
                        text(model, "disabled_reason")
                    })
                    .changed()
                {
                    if selected {
                        self.state.models.insert(id.into());
                    } else {
                        self.state.models.remove(id);
                    }
                }
                if selected && ui.small_button("Settings…").clicked() {
                    self.settings_model = Some(id.into());
                }
            });
        }
        ui.horizontal(|ui| {
            ui.label("MSA");
            select(
                ui,
                "msa-backend",
                &mut self.state.msa_backend,
                &["public", "private"],
            );
        });
        if self.state.msa_backend == "public" {
            ui.small("Protein queries use the configured public search service.");
        }
        ui.horizontal(|ui| {
            ui.label("Execution");
            select(
                ui,
                "execution",
                &mut self.state.execution,
                &["auto", "resident", "ephemeral"],
            );
        });
        ui.separator();
        let preparing = self.run_pending();
        let response = ui.add(
            egui::Button::new(
                // Reserve the leading icon area in this left-aligned sidebar.
                RichText::new(if preparing {
                    "    Run status"
                } else {
                    "    Run"
                })
                .strong()
                .size(16.)
                .color(Color32::WHITE),
            )
            .fill(Color32::from_rgb(28, 116, 66))
            .min_size(Vec2::new(ui.available_width(), 36.)),
        );
        // A drawn triangle uses the same native palette and works with every font.
        let center = egui::pos2(response.rect.left() + 18., response.rect.center().y);
        ui.painter().add(egui::Shape::convex_polygon(
            vec![
                center + egui::vec2(-4., -6.),
                center + egui::vec2(-4., 6.),
                center + egui::vec2(6., 0.),
            ],
            Color32::WHITE,
            egui::Stroke::NONE,
        ));
        if response.clicked() {
            self.begin_run();
        }
        if !self.run_input_error.is_empty() {
            ui.colored_label(RED, &self.run_input_error);
        }
        if self.state.run.is_some() && ui.small_button("Run status").clicked() {
            self.preview_open = true;
        }
        for pending in self
            .pending
            .values()
            .filter(|p| matches!(p.purpose, Purpose::Upload(_)))
        {
            ui.small(&pending.label);
            if pending.total > 0 {
                ui.add(
                    egui::ProgressBar::new(pending.done as f32 / pending.total as f32)
                        .text(format!("{} / {} bytes", pending.done, pending.total)),
                );
            }
        }
        self.recovery_panel(ui);
    }
    pub(super) fn run_dialog(&mut self, ctx: &egui::Context) {
        let mut open = self.preview_open;
        egui::Window::new("Run status").open(&mut open).default_size([780.,480.]).min_width(620.).show(ctx, |ui| {
            let Some(run) = self.state.run.clone() else {
                ui.label("Paste or load inputs, select models, then click Run.");
                return;
            };
            ui.heading(text(&run.request, "name"));
            ui.small(format!("{} inputs · {} models · {} MSA", rows(&run.request,"inputs").len(), rows(&run.request,"models").len(), text(&run.request,"msa_backend")));
            if run.batch_id.is_empty() {
                ui.horizontal(|ui| {
                    if self.run_pending() { ui.spinner(); }
                    ui.strong(match run.stage.as_str() {
                        "uploading" => "Uploading molecular inputs",
                        "upload_failed" => "Preparation needs attention",
                        "uncertain" => "Connection interrupted — recover the saved run",
                        "rejected" => "Run request rejected",
                        _ => "Waiting for the head to accept this run",
                    });
                });
                for (index, upload) in run.uploads.iter().enumerate() {
                    ui.horizontal(|ui| {
                        ui.colored_label(if upload.complete { GREEN } else { AMBER }, if upload.complete { "Ready" } else { "Upload" });
                        ui.label(PathBuf::from(&upload.path).file_name().unwrap_or_default().to_string_lossy());
                    });
                    if let Some(pending) = self.pending.values().find(|pending| pending.purpose == Purpose::Upload(UploadTarget::Run(run.id.clone(),index))) {
                        if pending.total > 0 { ui.add(egui::ProgressBar::new(pending.done as f32 / pending.total as f32).show_percentage()); }
                        else { ui.small("Preparing upload…"); }
                    }
                }
                if !run.error.is_empty() { ui.colored_label(RED, &run.error); }
                if !self.run_pending() && run.stage != "rejected" && ui.button(if !run.submission_attempted && run.operation.is_empty() { "Resume preparation" } else { "Recover exact run" }).clicked() {
                    self.resume_run();
                }
                if run.stage == "rejected" { ui.small("Edit the inputs if needed, then click Run to start a new request."); }
                if run.can_discard_preparation() && !self.run_pending()
                    && ui.button("Discard failed preparation").clicked()
                {
                    self.state.run = None; self.run_after_uploads = false;
                    self.log("Failed local preparation discarded. Edit the inputs and click Run when ready.");
                }
            } else if let Some(batch) = self.run_batch.clone().filter(|batch| text(batch,"batch_id") == run.batch_id) {
                let state = text(&batch,"state");
                ui.horizontal(|ui| {
                    if !ui_state::terminal(state) { ui.spinner(); }
                    ui.colored_label(if state == "complete" { GREEN } else if matches!(state,"failed"|"validation_failed") { RED } else { AMBER }, state);
                    if ui.button("View in run history").clicked() {
                        self.state.active_batch = run.batch_id.clone(); self.sidebar_tab = 1; self.ingest_batch(batch.clone());
                    }
                });
                ui.small(&run.batch_id);
                egui::ScrollArea::both().max_height(330.).show(ui, |ui| {
                    egui::Grid::new("run-status-pairs").striped(true).num_columns(3).min_col_width(100.).show(ui, |ui| {
                        ui.strong("Input / model"); ui.strong("Progress"); ui.strong("Details"); ui.end_row();
                        for pair in rows(&batch,"pairs") {
                            let job = rows(&batch,"jobs").iter().find(|job| text(job,"pair_id") == text(pair,"pair_id") && !text(pair,"pair_id").is_empty());
                            let status = if job.is_none() && text(pair,"state") == "pending" && ui_state::terminal(state) { state } else { pair_status(pair, job) };
                            ui.label(format!("{} / {}", text(pair,"input_name"), text(pair,"model")));
                            ui.colored_label(if status == "complete" { GREEN } else if status == "failed" { RED } else { AMBER }, status);
                            let mut details = rows(pair,"reasons").iter().map(|v| v.as_str().map(str::to_owned).unwrap_or_else(||v.to_string())).collect::<Vec<_>>();
                            if let Some(job) = job {
                                let progress = text(&job["progress"],"message");
                                if !progress.is_empty() { details.push(progress.into()); }
                                if job.pointer("/provenance/msa_applicable") == Some(&json!(false)) { details.push("MSA not applicable".into()); }
                                for key in ["message","error"] { if let Some(value) = job.get(key).filter(|v| !v.is_null()) { details.push(value.get("message").and_then(Value::as_str).or_else(||value.as_str()).map(str::to_owned).unwrap_or_else(||value.to_string())); } } }
                            ui.label(details.join("; ")); ui.end_row();
                        }
                    });
                });
                for error in rows(&batch,"errors") { ui.colored_label(RED, error.as_str().map(str::to_owned).unwrap_or_else(||error.to_string())); }
                if let Some(error) = batch.get("error").filter(|v| !v.is_null()) { ui.colored_label(RED, error.get("message").and_then(Value::as_str).map(str::to_owned).unwrap_or_else(|| error.to_string())); }
            } else { ui.spinner(); ui.label("Loading run status…"); }
            ui.separator();
            ui.small("You can close this window. Accepted runs continue on the head; closing does not cancel them.");
        });
        self.preview_open = open;
    }
    pub(super) fn settings_dialog(&mut self, ctx: &egui::Context) {
        let Some(id) = self.settings_model.clone() else {
            return;
        };
        let Some(model) = rows(&self.catalog, "models")
            .iter()
            .find(|model| text(model, "id") == id)
            .cloned()
        else {
            return;
        };
        let mut open = true;
        let mut labels = false;
        egui::Window::new(format!("{} settings",model_name(&model))).open(&mut open).default_width(520.).show(ctx,|ui|{
            ui.label(text(&model,"description"));ui.small("Unchecked options retain native defaults. Only catalog-approved settings are accepted.");
            let values=self.state.settings.entry(id.clone()).or_insert_with(||json!({}));if !values.is_object(){*values=json!({});}
            if let Some(specs)=model["settings"].as_object(){for(key,spec)in specs{
                ui.push_id(key,|ui|{ui.horizontal(|ui|{let mut enabled=values.get(key).is_some();if ui.checkbox(&mut enabled,key).on_hover_text(text(spec,"description")).changed(){if enabled{values[key]=spec.get("default").cloned().unwrap_or_else(||match text(spec,"type"){"integer"|"number"=>spec.get("minimum").cloned().unwrap_or(json!(0)),"boolean"=>json!(false),_=>json!("")});}else{values.as_object_mut().unwrap().remove(key);}}
                    if enabled{
                        if let Some(options)=spec["enum"].as_array(){egui::ComboBox::from_id_salt("enum").selected_text(values[key].as_str().map(str::to_owned).unwrap_or_else(||values[key].to_string())).show_ui(ui,|ui|{for option in options{ui.selectable_value(&mut values[key],option.clone(),option.as_str().map(str::to_owned).unwrap_or_else(||option.to_string()));}});}else{match text(spec,"type"){
                            "integer"=>{let mut value=values[key].as_i64().unwrap_or(0);if ui.add(egui::DragValue::new(&mut value).range(spec["minimum"].as_i64().unwrap_or(i64::MIN)..=spec["maximum"].as_i64().unwrap_or(i64::MAX))).changed(){values[key]=json!(value);}},
                            "number"=>{let mut value=values[key].as_f64().unwrap_or(0.);if ui.add(egui::DragValue::new(&mut value).speed(0.01).range(spec["minimum"].as_f64().unwrap_or(-1e9)..=spec["maximum"].as_f64().unwrap_or(1e9))).changed(){values[key]=json!(value);}},
                            "boolean"=>{let mut value=values[key].as_bool().unwrap_or(false);if ui.checkbox(&mut value,"").changed(){values[key]=json!(value);}},
                            _=>{let mut value=values[key].as_str().unwrap_or("").to_owned();if ui.text_edit_singleline(&mut value).changed(){values[key]=json!(value);}},
                        }}
                    }
                    if key=="labels_upload_id"&&ui.button("Upload CSV…").clicked(){labels=true;}
                });});
            }}
        });
        if labels {
            self.choose_files(Pick::Labels(id), ctx);
        }
        if !open {
            self.settings_model = None;
        }
    }
}

#[cfg(test)]
mod run_tests {
    use super::*;
    #[test]
    fn run_status_keeps_incompatibility_visible_alongside_live_job_states() {
        let rejected = json!({"state":"rejected","reasons":["Unsupported RNA"]});
        assert_eq!(pair_status(&rejected, None), "Skipped (incompatible)");
        let compatible = json!({"state":"compatible"});
        for state in ["queued", "running", "complete", "failed", "cancelled"] {
            let job = json!({"state":state});
            assert_eq!(pair_status(&compatible, Some(&job)), state);
        }
        assert_eq!(pair_status(&json!({"state":"pending"}), None), "Validating");
        assert_eq!(
            model_name(
                &json!({"id":"rfaa","name":"RoseTTAFold All-Atom (parked)","enabled":false})
            ),
            "RoseTTAFold All-Atom"
        );
    }
}
