use super::*;

fn state_color(state: &str) -> Color32 {
    match state {
        "complete" => GREEN,
        "failed" | "interrupted" | "validation_failed" => RED,
        "running" | "starting" | "validating" => AMBER,
        _ => Color32::LIGHT_GRAY,
    }
}
pub(super) fn is_structure(artifact: &Value) -> bool {
    matches!(text(artifact, "format"), "pdb" | "mmcif" | "cif")
        || matches!(
            std::path::Path::new(text(artifact, "name"))
                .extension()
                .and_then(|v| v.to_str()),
            Some("pdb" | "cif" | "mmcif")
        )
}
fn table_rows(content: &str, delimiter: char) -> Result<Vec<Vec<String>>, String> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut cell = String::new();
    let mut quoted = false;
    let mut chars = content.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '"' if quoted => {
                if chars.peek() == Some(&'"') {
                    chars.next();
                    cell.push('"');
                } else {
                    quoted = false;
                }
            }
            '"' if cell.is_empty() => quoted = true,
            c if !quoted && c == delimiter => {
                row.push(std::mem::take(&mut cell));
                if row.len() > 100 {
                    return Err("Table has more than 100 columns; use raw text/export.".into());
                }
            }
            '\n' if !quoted => {
                row.push(std::mem::take(&mut cell));
                rows.push(std::mem::take(&mut row));
                if rows.len() >= 200 {
                    return Ok(rows);
                }
            }
            '\r' if !quoted && chars.peek() == Some(&'\n') => {}
            c => cell.push(c),
        }
    }
    if quoted {
        return Err(
            "Incomplete quoted CSV record; inspect raw text or export the original.".into(),
        );
    }
    if !cell.is_empty() || !row.is_empty() {
        row.push(cell);
        rows.push(row);
    }
    Ok(rows)
}
impl Workbench {
    pub(super) fn jobs_panel(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        Self::section(ui, "RUN HISTORY");
        ui.horizontal(|ui| {
            if ui.button("Refresh").clicked() {
                self.request("batch.list", json!({"limit":100}), Purpose::History);
            }
            ui.small(format!("{} batches", self.batches.len()));
        });
        egui::ScrollArea::vertical()
            .id_salt("history-list")
            .max_height(175.)
            .show(ui, |ui| {
                for batch in self.batches.clone() {
                    let id = text(&batch, "batch_id");
                    ui.horizontal(|ui| {
                        ui.colored_label(state_color(text(&batch, "state")), text(&batch, "state"));
                        if ui
                            .add(
                                egui::Button::selectable(
                                    self.state.active_batch == id,
                                    if text(&batch, "name").is_empty() {
                                        id
                                    } else {
                                        text(&batch, "name")
                                    },
                                )
                                .truncate(),
                            )
                            .on_hover_text(id)
                            .clicked()
                        {
                            self.state.active_batch = id.into();
                            self.batch = None;
                            self.selected_artifacts.clear();
                            self.request(
                                "batch.get",
                                json!({"batch_id":id}),
                                Purpose::Batch(id.into()),
                            );
                        }
                    });
                }
            });
        if let Some(batch) = self.batch.clone() {
            Self::section(ui, "SELECTED BATCH");
            ui.strong(text(&batch, "name"));
            ui.horizontal(|ui| {
                ui.colored_label(state_color(text(&batch, "state")), text(&batch, "state"));
                if ui.small_button("Copy link").clicked() {
                    ctx.copy_text(format!(
                        "bio-workbench://batch/{}",
                        text(&batch, "batch_id")
                    ));
                }
                if !ui_state::terminal(text(&batch, "state"))
                    && ui.small_button("Cancel batch").clicked()
                {
                    self.request(
                        "batch.cancel",
                        json!({"batch_id":text(&batch,"batch_id")}),
                        Purpose::CancelBatch,
                    );
                }
            });
            ui.small(text(&batch, "batch_id"));
            if let Some(error) = batch.get("error").filter(|v| !v.is_null()) {
                ui.colored_label(RED, error.to_string());
            }
            if batch["auto_run"] == true && ui.button("Run status").clicked() {
                if self
                    .state
                    .run
                    .as_ref()
                    .is_none_or(|run| run.accepts_new_run())
                {
                    self.state.run = Some(ui_state::RunIntent {
                        id: uid(),
                        endpoint: self.run_endpoint(),
                        request: batch.clone(),
                        batch_id: text(&batch, "batch_id").into(),
                        stage: text(&batch, "state").into(),
                        ..Default::default()
                    });
                    self.run_batch = Some(batch.clone());
                }
                self.preview_open = true;
            }
            if !self.selected_artifacts.is_empty() {
                ui.horizontal(|ui| {
                    if ui
                        .button(format!("Open {} selected", self.selected_artifacts.len()))
                        .clicked()
                    {
                        let ids: Vec<_> = self.selected_artifacts.iter().cloned().collect();
                        for id in ids {
                            self.open_artifact_new_tab(id, ctx);
                        }
                    }
                    if ui.small_button("Clear").clicked() {
                        self.selected_artifacts.clear();
                    }
                });
            }
            for job in rows(&batch, "jobs") {
                let id = text(job, "job_id").to_owned();
                ui.push_id(&id, |ui| {
                    egui::Frame::group(ui.style())
                        .inner_margin(6)
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                let status = text(job, "state");
                                let status_width = ui
                                    .painter()
                                    .layout_no_wrap(
                                        status.into(),
                                        egui::TextStyle::Body.resolve(ui.style()),
                                        state_color(status),
                                    )
                                    .size()
                                    .x;
                                let title_width = (ui.available_width()
                                    - status_width
                                    - ui.spacing().item_spacing.x)
                                    .max(40.);
                                if ui
                                    .add_sized(
                                        [title_width, ui.spacing().interact_size.y],
                                        egui::Button::new(format!(
                                            "{} · {}",
                                            text(job, "model"),
                                            text(job, "input_name")
                                        ))
                                        .truncate(),
                                    )
                                    .on_hover_text(format!(
                                        "{} · {}\nJob {}\nOpen this run in a viewer tab, or focus its existing tab.",
                                        text(job, "model"),
                                        text(job, "input_name"),
                                        id
                                    ))
                                    .clicked()
                                {
                                    self.open_job_tab(id.clone());
                                }
                                ui.colored_label(
                                    state_color(text(job, "state")),
                                    text(job, "state"),
                                );
                            });
                            if !text(job, "phase").is_empty() {
                                ui.small(text(job, "phase"));
                            }
                            if let Some(progress) = job.get("progress").filter(|v| !v.is_null()) {
                                let message = text(progress, "message");
                                if !message.is_empty() {
                                    ui.label(message);
                                }
                                if let Some(eta) = self.worker_job_eta(job) { ui.small(eta); }
                                let done = progress
                                    .get("completed")
                                    .or_else(|| progress.get("done"))
                                    .and_then(Value::as_u64);
                                let total = progress.get("total").and_then(Value::as_u64);
                                if let (Some(done), Some(total)) = (done, total)
                                    && total > 0
                                {
                                    ui.add(
                                        egui::ProgressBar::new(
                                            (done as f32 / total as f32).clamp(0., 1.),
                                        )
                                        .text(format!("{done} / {total}")),
                                    );
                                }
                                if let Some(position) = progress.get("queue_position") {
                                    ui.small(format!("Queue position: {position}"));
                                }
                                if !text(progress, "observed_at").is_empty() {
                                    ui.weak(format!("Observed {}", text(progress, "observed_at")));
                                }
                            }
                            if let Some(error) = job.get("error").filter(|v| !v.is_null()) {
                                ui.colored_label(RED, error.to_string());
                            }
                            ui.horizontal(|ui| {
                                if text(job, "model") == "bindcraft" && ui.small_button("Candidates").clicked() {
                                    self.binder_open_candidates(id.clone());
                                }
                                if ui.small_button("Open tab").clicked() {
                                    self.open_job_new_tab(id.clone(),ctx);
                                }
                                if ui.small_button("Log").clicked() {
                                    self.focused_job = id.clone();
                                    self.log_offset = 0;
                                    self.job_log.clear();
                                    self.console_tab = 1;
                                    self.request(
                                        "job.logs",
                                        json!({"job_id":id,"offset":0,"max_bytes":65536}),
                                        Purpose::Logs(id.clone(), 0),
                                    );
                                }
                                if !ui_state::terminal(text(job, "state"))
                                    && ui.small_button("Cancel").clicked()
                                {
                                    self.request(
                                        "job.cancel",
                                        json!({"job_id":id}),
                                        Purpose::CancelJob,
                                    );
                                }
                                if ui.small_button("Details").clicked() {
                                    self.text_preview = Some((
                                        format!("Job {id}"),
                                        serde_json::to_string_pretty(job).unwrap_or_default(),
                                    ));
                                }
                            });
                            for artifact in rows(job, "artifacts")
                                .iter()
                                .filter(|artifact| is_structure(artifact))
                            {
                                self.artifact_row(ui, artifact);
                            }
                            let others: Vec<_> = rows(job, "artifacts")
                                .iter()
                                .filter(|artifact| !is_structure(artifact))
                                .collect();
                            if !others.is_empty() {
                                egui::CollapsingHeader::new(format!(
                                    "Other artifacts ({})",
                                    others.len()
                                ))
                                .show(ui, |ui| {
                                    for artifact in others {
                                        self.artifact_row(ui, artifact);
                                    }
                                });
                            }
                        });
                });
            }
            if rows(&batch, "jobs").is_empty() {
                ui.weak("No submitted jobs in this batch.");
            }
        } else {
            ui.weak(if self.state.active_batch.is_empty() {
                "Select a batch to see jobs and artifacts."
            } else {
                "Loading selected batch…"
            });
        }
        self.recovery_panel(ui);
    }
    fn artifact_row(&mut self, ui: &mut egui::Ui, artifact: &Value) {
        let artifact_id = text(artifact, "artifact_id").to_owned();
        ui.push_id(&artifact_id, |ui| {
            ui.separator();
            let structure = is_structure(artifact);
            ui.horizontal(|ui| {
                if structure {
                    let mut selected = self.selected_artifacts.contains(&artifact_id);
                    if ui
                        .add(egui::Checkbox::without_text(&mut selected))
                        .on_hover_text(
                            "Select this structure to open with the other selected tabs.",
                        )
                        .changed()
                    {
                        if selected {
                            self.selected_artifacts.insert(artifact_id.clone());
                        } else {
                            self.selected_artifacts.remove(&artifact_id);
                        }
                    }
                }
                let label = egui::Label::new(ui_views::short_name(text(artifact, "name")))
                    .truncate()
                    .sense(if structure {
                        egui::Sense::click()
                    } else {
                        egui::Sense::hover()
                    });
                let response = ui.add(label).on_hover_text(format!(
                    "{}\n{} · {} bytes\nSHA256 {}",
                    text(artifact, "name"),
                    text(artifact, "role"),
                    artifact["size"],
                    text(artifact, "sha256")
                ));
                if structure
                    && response
                        .on_hover_cursor(egui::CursorIcon::PointingHand)
                        .clicked()
                {
                    self.open_artifact_id(artifact_id.clone());
                }
            });
            ui.horizontal_wrapped(|ui| {
                if structure && ui.small_button("Open tab").clicked() {
                    self.open_artifact_new_tab(artifact_id.clone(), ui.ctx());
                }
                if !structure && ui.small_button("Preview text / table").clicked() {
                    self.request_artifact(&artifact_id, ArtifactTarget::Text);
                }
                if ui.small_button("Export…").clicked() {
                    self.request_artifact(&artifact_id, ArtifactTarget::Export);
                }
            });
        });
    }
    pub(super) fn recovery_panel(&mut self, ui: &mut egui::Ui) {
        let failures: Vec<_> = self
            .failures
            .iter()
            .rev()
            .take(5)
            .map(|f| {
                (
                    f.id.clone(),
                    f.label.clone(),
                    f.message.clone(),
                    f.purpose.clone(),
                )
            })
            .collect();
        if !failures.is_empty() {
            Self::section(ui, "CLIENT REQUEST ERRORS");
            for (id, label, message, purpose) in failures {
                ui.colored_label(RED, &label);
                ui.small(message);
                ui.horizontal(|ui| {
                    if ui.small_button("Retry / recover").clicked() {
                        self.recover_failure(&id, purpose, label);
                    }
                    if ui.small_button("Dismiss locally").clicked() {
                        self.failures.retain(|f| f.id != id);
                    }
                });
            }
        }
        let operations = self
            .session
            .as_ref()
            .map(|s| s.retryable_operations())
            .unwrap_or_default();
        egui::CollapsingHeader::new(format!("Saved request receipts ({})",operations.len())).id_salt("request-recovery").show(ui,|ui|{
            ui.small("Recover reuses the exact saved payload. Completed receipts are read locally; jobs are not submitted again.");
            for op in operations.iter().rev().take(60){let id=text(op,"id");ui.push_id(id,|ui|{ui.horizontal_wrapped(|ui|{ui.monospace(text(op,"method"));ui.colored_label(state_color(text(op,"status")),text(op,"status"));});if let Some(error)=op.get("error").filter(|v|!v.is_null()){ui.small(error.to_string());}
if ui.add_enabled(op["current_connection"]==true&&!self.pending.contains_key(id),egui::Button::new(if text(op,"status")=="complete"{"Open receipt"}else{"Recover exact request"})).clicked(){self.recover_operation(op);}ui.separator();});}
        });
    }
    fn recover_failure(&mut self, id: &str, purpose: Purpose, label: String) {
        match purpose.clone() {
            Purpose::Run(_) | Purpose::Upload(UploadTarget::Run(_, _)) => {
                if let Some(op) = self.session.as_ref().and_then(|session| {
                    session
                        .retryable_operations()
                        .into_iter()
                        .find(|op| text(op, "id") == id)
                }) {
                    self.recover_operation(&op);
                }
            }
            Purpose::LibraryRuns(_) => self.library_runs_refresh(None),
            Purpose::LibraryHistory => {
                self.request("library.history", json!({}), Purpose::LibraryHistory);
            }
            Purpose::Catalog => {
                self.request("catalog", json!({}), purpose);
            }
            Purpose::History => {
                self.request("batch.list", json!({}), purpose);
            }
            Purpose::WorkerStatus(_) => self.worker_refresh(),
            Purpose::WorkerReceipt(control_id) => {
                self.request(
                    "worker.control_get",
                    json!({"control_id":control_id}),
                    purpose,
                );
            }
            Purpose::Batch(batch) => {
                self.request("batch.get", json!({"batch_id":batch}), purpose);
            }
            Purpose::Job(job) => {
                self.request("job.get", json!({"job_id":job}), purpose);
            }
            Purpose::Logs(job, offset) => {
                self.request(
                    "job.logs",
                    json!({"job_id":job,"offset":offset,"max_bytes":65536}),
                    purpose,
                );
            }
            Purpose::Artifact(target) => {
                let artifact = if let ArtifactTarget::View(slot) = &target {
                    self.view_reference(*slot)
                        .and_then(|r| r["artifact_id"].as_str())
                        .map(str::to_owned)
                } else {
                    self.artifact_metadata
                        .iter()
                        .find(|(_, v)| text(v, "download_operation") == id)
                        .map(|(id, _)| id.clone())
                };
                if let Some(artifact) = artifact {
                    self.request_artifact(&artifact, target);
                } else {
                    self.log("Select the original artifact again to retry its verified download.");
                }
            }
            Purpose::Library(_) => self.library_refresh(),
            Purpose::LibraryRecord(reference) => self.library_select(&reference),
            Purpose::LibrarySequence(params) => {
                self.request(
                    "library.sequence",
                    params.clone(),
                    Purpose::LibrarySequence(params),
                );
            }
            Purpose::LibraryProductPreview(params) => {
                self.request(
                    "library.product_preview",
                    params.clone(),
                    Purpose::LibraryProductPreview(params),
                );
            }
            Purpose::LibraryAttachment(_, _) => {
                self.log("Select the attachment again in Library to retry its verified download.");
            }
            Purpose::Annotations(artifact) => {
                self.request("annotation.list", json!({"artifact_id":artifact}), purpose);
            }
            _ => self.retry(id, purpose, label),
        }
    }
    fn recover_operation(&mut self, op: &Value) {
        let id = text(op, "id").to_owned();
        let params = &op["params"];
        let purpose = match text(op, "method") {
            "batch.run" => {
                if self.state.run.as_ref().is_some_and(|run| {
                    !run.accepts_new_run() && run.operation != id && run.request != *params
                }) {
                    self.log(
                        "Resolve the current pending run before opening another saved run request.",
                    );
                    self.preview_open = true;
                    return;
                }
                let run_id = self
                    .state
                    .run
                    .as_ref()
                    .filter(|run| run.operation == id || run.request == *params)
                    .map_or_else(uid, |run| run.id.clone());
                self.state.run = Some(ui_state::RunIntent {
                    id: run_id.clone(),
                    endpoint: text(op, "endpoint").into(),
                    request: params.clone(),
                    operation: id.clone(),
                    submission_attempted: true,
                    stage: "requesting".into(),
                    ..Default::default()
                });
                self.run_batch = None;
                self.preview_open = true;
                self.persist();
                Purpose::Run(run_id)
            }
            "batch.validate" => {
                let mut snapshot = params.clone();
                snapshot.as_object_mut().map(|v| v.remove("request_key"));
                self.state.preview = Some(ui_state::Preview {
                    snapshot,
                    operation: id.clone(),
                    ..Default::default()
                });
                Purpose::Preview
            }
            "batch.create" => Purpose::Commit,
            "worker.extend" | "worker.shutdown" => {
                self.worker.open = true;
                Purpose::WorkerControl(text(op, "method").into())
            }
            "library.edit"
            | "library.undo"
            | "library.redo"
            | "library.product_create"
            | "library.create" => Purpose::LibraryWrite(params.clone()),
            "batch.cancel" => Purpose::CancelBatch,
            "job.cancel" => Purpose::CancelJob,
            "annotation.put" => Purpose::SaveNote(
                text(params, "artifact_id").into(),
                text(params, "text").into(),
            ),
            "local.upload" => {
                if let Some(run) = &self.state.run
                    && run.uploads.iter().any(|upload| upload.operation == id)
                {
                    self.resume_run();
                    return;
                }
                let path = text(params, "path");
                let target = self
                    .state
                    .inputs
                    .iter()
                    .find(|input| input.local_path.as_deref() == Some(path))
                    .map(|input| UploadTarget::Input(input.id.clone()))
                    .or_else(|| {
                        self.state.inputs.iter().find_map(|input| {
                            input
                                .attachment_paths
                                .iter()
                                .find(|(_, value)| value.as_str() == path)
                                .map(|(name, _)| {
                                    UploadTarget::Attachment(input.id.clone(), name.clone())
                                })
                        })
                    })
                    .or_else(|| {
                        self.state
                            .extra
                            .get("label_paths")
                            .and_then(Value::as_object)
                            .and_then(|paths| {
                                paths
                                    .iter()
                                    .find(|(_, value)| value.as_str() == Some(path))
                                    .map(|(model, _)| UploadTarget::Labels(model.clone()))
                            })
                    });
                let Some(target) = target else {
                    self.text_preview = Some((
                        "Retained upload receipt".into(),
                        serde_json::to_string_pretty(op).unwrap_or_default(),
                    ));
                    self.log("This upload's original input is no longer in the draft. Its retained receipt is available for inspection.");
                    return;
                };
                Purpose::Upload(target)
            }
            _ => {
                self.text_preview = Some((
                    "Saved request".into(),
                    serde_json::to_string_pretty(op).unwrap_or_default(),
                ));
                return;
            }
        };
        self.retry(&id, purpose, text(op, "method").into());
    }
    pub(super) fn text_dialog(&mut self, ctx: &egui::Context) {
        let Some((name, content)) = self.text_preview.clone() else {
            return;
        };
        let mut open = true;
        egui::Window::new(&name).id(egui::Id::new("text-preview")).open(&mut open).default_size([760.,580.]).show(ctx,|ui|{
            ui.horizontal(|ui|{if ui.button("Copy text").clicked(){ctx.copy_text(content.clone());}ui.small("Use Export for the complete original artifact.");});
            let delimiter=if name.ends_with(".csv"){Some(',')}else if name.ends_with(".tsv"){Some('\t')}else{None};
            if let Some(delimiter)=delimiter {match table_rows(&content,delimiter){Ok(rows)=>{ui.small("Table preview: first 200 rows; hover a cell for its full contents.");egui::ScrollArea::both().id_salt("table-data").max_height(430.).show(ui,|ui|{egui::Grid::new("artifact-table").striped(true).min_col_width(65.).show(ui,|ui|{for row in rows{for cell in row{let short:String=cell.chars().take(100).collect();ui.add(egui::Label::new(short).truncate()).on_hover_text(cell);}ui.end_row();}});});},Err(error)=>{ui.colored_label(AMBER,error);}}}
            egui::CollapsingHeader::new("Original text").default_open(delimiter.is_none()).show(ui,|ui|{egui::ScrollArea::both().max_height(500.).show(ui,|ui|{let mut value=content.as_str();ui.add(egui::TextEdit::multiline(&mut value).font(egui::TextStyle::Monospace).desired_width(f32::INFINITY).desired_rows(25));});});
        });
        if !open {
            self.text_preview = None;
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn table_preview_preserves_quoted_fields_and_embedded_newlines() {
        assert_eq!(
            table_rows("id,note\r\n1,\"binding, \"\"editor\"\"\ncomplex\"\r\n", ',').unwrap(),
            vec![
                vec!["id", "note"],
                vec!["1", "binding, \"editor\"\ncomplex"]
            ]
        );
    }
    #[test]
    fn table_preview_rejects_incomplete_quotes() {
        assert!(table_rows("a,\"incomplete", ',').is_err());
    }
}
