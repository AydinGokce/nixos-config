use super::*;
use ui_state::{Editor, Preview, infer_file};
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
                    self.preview_after_uploads = false;
                    self.log(format!("Upload failed: {error}"));
                }
            }
        }
    }
    pub(super) fn uploaded(&mut self, target: UploadTarget, receipt: Value) {
        let id = text(&receipt, "upload_id");
        if id.is_empty() {
            self.preview_after_uploads = false;
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
        }
    }
    pub(super) fn begin_preview(&mut self) {
        if !self.connected {
            self.connection_open = true;
            self.log("Connect to the head before requesting a compatibility preview.");
            return;
        }
        let mut uploads = Vec::new();
        for input in &self.state.inputs {
            if input.needs_upload() {
                uploads.push((
                    PathBuf::from(input.local_path.as_ref().unwrap()),
                    UploadTarget::Input(input.id.clone()),
                ));
            }
            for (name, path) in &input.attachment_paths {
                if input.source["attachments"][name].as_str().is_none() {
                    uploads.push((
                        PathBuf::from(path),
                        UploadTarget::Attachment(input.id.clone(), name.clone()),
                    ));
                }
            }
        }
        if !uploads.is_empty() {
            self.preview_after_uploads = true;
            for (path, target) in uploads {
                self.start_upload(path, target);
            }
            return;
        }
        let payload = match self.state.payload() {
            Ok(value) => value,
            Err(error) => {
                self.log(error);
                return;
            }
        };
        if let Some(preview) = &self.state.preview
            && preview.snapshot == payload
            && !preview.batch_id.is_empty()
        {
            self.preview_open = true;
            let id = preview.batch_id.clone();
            self.state.active_batch = id.clone();
            self.request("batch.get", json!({"batch_id":id}), Purpose::Batch(id));
            return;
        }
        let mut request = payload.clone();
        request["request_key"] = json!(uid());
        if let Some(operation) = self.request("batch.validate", request, Purpose::Preview) {
            self.state.preview = Some(Preview {
                snapshot: payload,
                operation,
                ..Default::default()
            });
            self.preview_open = true;
            self.persist();
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
                        .hint_text(if self.state.editor.kind == "library" {
                            "construct:name@revision or assembly:name@revision"
                        } else {
                            "Paste sequence(s), FASTA, or the selected format"
                        }),
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
                "The active editor is included in Preview; Add input is optional.",
            );
        }
        let mut remove = None;
        let mut attachment = None;
        for (index, input) in self.state.inputs.iter_mut().enumerate() {
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
                                "Uploads when you request Preview"
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
                        egui::Checkbox::new(&mut selected, text(model, "name")),
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
            if !enabled {
                ui.weak(text(model, "disabled_reason"));
            }
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
        ui.small(if self.state.msa_backend == "public" {
            "Protein queries use the configured public search service."
        } else {
            "Uses the configured private databases; availability is checked on the head."
        });
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
        let busy = self.busy(&Purpose::Preview) || self.preview_after_uploads;
        if ui
            .add_enabled(
                !busy,
                egui::Button::new(if busy {
                    "Preparing preview…"
                } else {
                    "Check compatibility (CPU)"
                })
                .min_size(Vec2::new(ui.available_width(), 26.)),
            )
            .clicked()
        {
            self.begin_preview();
        }
        if self.state.preview.is_some() {
            ui.horizontal(|ui| {
                if ui.button("Review preview").clicked() {
                    self.preview_open = true;
                }
                if ui.button("New preview").clicked() {
                    self.state.preview = None;
                    self.begin_preview();
                }
            });
        }
        ui.small("Preview runs native CPU validation. Only the selected compatible pairs are submitted in the review window.");
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
    pub(super) fn preview_dialog(&mut self, ctx: &egui::Context) {
        let mut open = self.preview_open;
        egui::Window::new("Compatibility preview — select exact pairs").open(&mut open).default_size([780.,480.]).min_width(720.).show(ctx,|ui|{
            let Some(preview)=self.state.preview.clone()else{ui.label("Create a preview from the Inputs panel.");return;};
            ui.label(format!("{} · {}",text(&preview.snapshot,"name"),text(&preview.snapshot,"mode")));ui.small("Native CPU validation only; no inference has been launched by Preview.");
            if preview.batch_id.is_empty(){ui.spinner();ui.label("Waiting for the durable preview receipt. A failed transport can be retried from Operations without creating a second preview.");return;}
            let Some(batch)=self.batch.clone().filter(|batch|text(batch,"batch_id")==preview.batch_id)else{ui.label("Loading preview…");return;};
            ui.horizontal(|ui|{ui.label(format!("State: {}",text(&batch,"state")));if ui.button("Select all compatible").clicked() && let Some(preview)=self.state.preview.as_mut(){preview.selected_pairs=rows(&batch,"pairs").iter().filter(|pair|text(pair,"state")=="compatible").map(|pair|text(pair,"pair_id").into()).collect();}
if ui.button("Clear selection").clicked() && let Some(preview)=self.state.preview.as_mut(){preview.selected_pairs.clear();}});
            egui::ScrollArea::both().max_height(300.).show(ui,|ui|{egui::Grid::new("preview-pairs").striped(true).num_columns(4).min_col_width(110.).show(ui,|ui|{
                ui.strong("Submit");ui.strong("Input / model");ui.strong("Validation");ui.strong("Reason");ui.end_row();
                for pair in rows(&batch,"pairs"){let id=text(pair,"pair_id");let compatible=text(pair,"state")=="compatible";let mut selected=self.state.preview.as_ref().is_some_and(|p|p.selected_pairs.contains(id));
                    if ui.add_enabled(compatible,egui::Checkbox::without_text(&mut selected)).changed() && let Some(preview)=self.state.preview.as_mut(){if selected{preview.selected_pairs.insert(id.into());}else{preview.selected_pairs.remove(id);}}
                    ui.label(format!("{} / {}",text(pair,"input_name"),text(pair,"model")));ui.colored_label(if compatible{GREEN}else{AMBER},text(pair,"state"));ui.label(rows(pair,"reasons").iter().map(|v|v.as_str().map(str::to_owned).unwrap_or_else(||v.to_string())).collect::<Vec<_>>().join("; "));ui.end_row();
                }
            });});
            for error in rows(&batch,"errors"){ui.colored_label(RED,error.as_str().map(str::to_owned).unwrap_or_else(||error.to_string()));}
            let matches=self.state.preview_matches();if !matches{ui.colored_label(AMBER,"The draft changed after this preview. Create a new preview before submitting.");}
            let count=self.state.preview.as_ref().map_or(0,|p|p.selected_pairs.len());
            let can=matches&&text(&batch,"state")=="validated"&&count>0&&!self.busy(&Purpose::Commit);
            if !preview.create_operation.is_empty(){
                ui.colored_label(AMBER,"A submission request is already retained for this preview. Recover that exact request to resolve its outcome.");
                if ui.add_enabled(!self.busy(&Purpose::Commit),egui::Button::new("Recover exact submission")).clicked(){self.retry(&preview.create_operation,Purpose::Commit,"batch.create".into());}
            }else if ui.add_enabled(can,egui::Button::new(format!("Submit {count} selected model jobs"))).clicked(){
                let selected=self.state.preview.as_ref().unwrap();let payload=json!({"batch_id":selected.batch_id,"request_key":uid(),"pair_ids":selected.selected_pairs});
                if let Some(operation)=self.request("batch.create",payload.clone(),Purpose::Commit){if let Some(preview)=self.state.preview.as_mut(){preview.create_operation=operation;preview.create_payload=payload;}self.persist();}
            }
            ui.small("Submission may rent cloud compute. Retry an uncertain submission from Operations; its exact request key is retained.");
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
        egui::Window::new(format!("{} settings",text(&model,"name"))).open(&mut open).default_width(520.).show(ctx,|ui|{
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
