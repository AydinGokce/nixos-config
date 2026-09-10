use super::*;

fn sort_batch_history(batches: &mut [Value]) {
    batches.sort_by(|a, b| {
        text(b, "created_at")
            .cmp(text(a, "created_at"))
            .then_with(|| text(b, "batch_id").cmp(text(a, "batch_id")))
    });
}

fn merge_batch_history(batches: &mut Vec<Value>, mut value: Value) {
    if let Some(existing) = batches
        .iter_mut()
        .find(|batch| text(batch, "batch_id") == text(&value, "batch_id"))
    {
        if text(&value, "created_at").is_empty() {
            value["created_at"] = existing["created_at"].clone();
        }
        *existing = value;
    } else {
        batches.push(value);
    }
    sort_batch_history(batches);
}

impl Workbench {
    pub(super) fn run_endpoint(&self) -> String {
        self.session
            .as_ref()
            .map(|session| session.connection.identity())
            .unwrap_or_default()
    }
    pub(super) fn busy(&self, purpose: &Purpose) -> bool {
        self.pending.values().any(|p| &p.purpose == purpose)
    }
    pub(super) fn request(
        &mut self,
        method: &str,
        params: Value,
        purpose: Purpose,
    ) -> Option<String> {
        if self.busy(&purpose) {
            return None;
        }
        let Some(session) = self.session.as_mut() else {
            if let Purpose::Job(job) = &purpose {
                self.job_tab_error(
                    job,
                    "Local session unavailable. Reconnect to load this run.",
                );
            }
            self.log("The local session is unavailable; reopen it in Connection settings.");
            return None;
        };
        match session.request(method, params) {
            Ok(id) => {
                self.pending.insert(
                    id.clone(),
                    Pending {
                        purpose,
                        label: method.into(),
                        done: 0,
                        total: 0,
                    },
                );
                Some(id)
            }
            Err(error) => {
                if let Purpose::Job(job) = &purpose {
                    self.job_tab_error(job, &error.to_string());
                }
                self.log(format!("{method}: {error}"));
                None
            }
        }
    }
    pub(super) fn retry(&mut self, id: &str, purpose: Purpose, label: String) {
        if matches!(purpose, Purpose::Library(_)) {
            self.failures.retain(|failure| failure.id != id);
            self.library_refresh();
            return;
        }
        if let Purpose::Artifact(ArtifactTarget::View(slot)) = &purpose
            && !self.has_view(*slot)
        {
            return;
        }
        if self.pending.contains_key(id) {
            return;
        }
        if matches!(purpose, Purpose::LibraryWrite(_)) {
            self.library_invalidate_lists();
        }
        if let Some(session) = self.session.as_mut() {
            match session.retry(id) {
                Ok(id) => {
                    if let Purpose::Artifact(ArtifactTarget::View(slot)) = &purpose {
                        self.view_loading.insert(*slot, format!("download:{id}"));
                        self.view_errors.remove(slot);
                    }
                    self.pending.insert(
                        id.clone(),
                        Pending {
                            purpose,
                            label,
                            done: 0,
                            total: 0,
                        },
                    );
                    self.failures.retain(|failure| failure.id != id);
                }
                Err(error) => {
                    match &purpose {
                        Purpose::Run(run_id) => self.run_error(run_id, &error.to_string(), true),
                        Purpose::Upload(UploadTarget::Run(run_id, _)) => {
                            self.run_error(run_id, &error.to_string(), false)
                        }
                        _ => {}
                    }
                    self.log(error.to_string());
                }
            }
        }
    }
    fn failure(&mut self, id: String, pending: Pending, error: rpc::RpcError) {
        let message = error.to_string();
        if let Purpose::Artifact(ArtifactTarget::View(slot)) = &pending.purpose {
            if !self.has_view(*slot)
                || self.view_loading.get(slot) != Some(&format!("download:{id}"))
            {
                return;
            }
            self.view_loading.remove(slot);
            self.view_errors.insert(*slot, message.clone());
        }
        if let Purpose::Job(job) = &pending.purpose {
            self.job_tab_error(job, &message);
        }
        self.library_failed(&pending.purpose, &message);
        self.library_runs_failed(&pending.purpose, &message);
        self.worker_failed(&pending.purpose, &message);
        self.log(format!("{}: {message}", pending.label));
        if pending.purpose == Purpose::Catalog {
            self.connected = false;
        }
        match &pending.purpose {
            Purpose::Run(run_id) => self.run_error(run_id, &message, error.uncertain),
            Purpose::Upload(UploadTarget::Run(run_id, _)) => {
                self.run_error(run_id, &message, false)
            }
            _ => {}
        }
        self.failures.retain(|failure| failure.id != id);
        self.failures.push(Failure {
            id,
            label: pending.label,
            message,
            purpose: pending.purpose,
        });
        if self.failures.len() > 30 {
            self.failures.remove(0);
        }
    }
    pub(super) fn events(&mut self, ctx: &egui::Context) {
        let events = self
            .session
            .as_mut()
            .map(|session| session.drain_events())
            .unwrap_or_default();
        for event in events {
            match event {
                session::Event::Progress { id, done, total } => {
                    if let Some(pending) = self.pending.get_mut(&id) {
                        pending.done = done;
                        pending.total = total;
                    }
                }
                session::Event::Result {
                    id,
                    method: _,
                    result,
                } => {
                    let Some(pending) = self.pending.remove(&id) else {
                        continue;
                    };
                    match result {
                        Ok(value) => self.received(id, pending.purpose, value, ctx),
                        Err(error) => self.failure(id, pending, error),
                    }
                }
                session::Event::Artifact {
                    id,
                    artifact_id,
                    path,
                    metadata,
                } => {
                    let Some(pending) = self.pending.remove(&id) else {
                        continue;
                    };
                    if let Purpose::Artifact(target) = pending.purpose {
                        if let ArtifactTarget::View(slot) = target
                            && (!self.has_view(slot)
                                || self.view_loading.get(&slot) != Some(&format!("download:{id}")))
                        {
                            continue;
                        }
                        let mut metadata = metadata;
                        if let Some(known) = self
                            .artifact_metadata
                            .get(&artifact_id)
                            .and_then(Value::as_object)
                        {
                            for (key, value) in known {
                                if key != "view_state"
                                    && key != "slot"
                                    && metadata.get(key).is_none()
                                {
                                    metadata[key] = value.clone();
                                }
                            }
                        }
                        metadata["artifact_id"] = json!(artifact_id);
                        metadata["local_path"] = json!(path);
                        if let ArtifactTarget::View(slot) = target
                            && let Some(reference) = self.view_reference(slot)
                        {
                            metadata["view_state"] = reference["view_state"].clone();
                            for key in [
                                "job_id",
                                "input_name",
                                "model",
                                "job_state",
                                "job_provenance",
                            ] {
                                if metadata.get(key).is_none() && reference.get(key).is_some() {
                                    metadata[key] = reference[key].clone();
                                }
                            }
                        }
                        self.accept_artifact(path, metadata, target, ctx);
                    }
                }
            }
        }
        while let Ok(event) = self.ui_rx.try_recv() {
            match event {
                UiEvent::Files(kind, paths) => self.picked(kind, paths, ctx),
                UiEvent::Error(error) => self.log(error),
                UiEvent::Parsed {
                    slot,
                    metadata,
                    bytes,
                    molecule,
                } => self.accept_molecule(slot, metadata, bytes, molecule),
                UiEvent::Text(name, value) => self.text_preview = Some((name, value)),
                UiEvent::Exported(result) => match result {
                    Ok(path) => self.log(format!("Exported {}", path.display())),
                    Err(error) => self.log(format!("Export failed: {error}")),
                },
            }
        }
        while let Ok(link) = self.navigation.try_recv() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
            if !link.is_empty() {
                self.navigate(&link, ctx);
            }
        }
        self.continue_run();
        for event in self.pymol.poll() {
            self.log(event);
        }
    }
    fn received(&mut self, id: String, purpose: Purpose, value: Value, ctx: &egui::Context) {
        match purpose {
            Purpose::Catalog => {
                self.catalog = value;
                self.connected = true;
                self.connection_open = false;
                self.worker_refresh();
                self.log("Connected; loaded the head model catalog.");
                if self.state.models.is_empty() {
                    for model in rows(&self.catalog, "models") {
                        if model["enabled"] == true && text(model, "workflow") == "folding" {
                            self.state.models.insert(text(model, "id").into());
                        }
                    }
                }
                self.request("batch.list", json!({"limit":100}), Purpose::History);
                if !self.state.active_batch.is_empty() {
                    let id = self.state.active_batch.clone();
                    self.request("batch.get", json!({"batch_id":id}), Purpose::Batch(id));
                }
            }
            Purpose::History => {
                self.batches = rows(&value, "batches").to_vec();
                sort_batch_history(&mut self.batches);
            }
            Purpose::WorkerStatus(serial) => self.worker_received_status(serial, value),
            Purpose::WorkerControl(_) | Purpose::WorkerReceipt(_) => {
                self.worker_received_control(value);
            }
            Purpose::Batch(batch_id) => {
                for job in rows(&value, "jobs") {
                    self.worker_observed_job(job);
                }
                if self
                    .state
                    .run
                    .as_ref()
                    .is_some_and(|run| run.batch_id == batch_id)
                {
                    self.run_batch = Some(value.clone());
                }
                if self.state.active_batch == batch_id {
                    self.ingest_batch(value);
                }
            }
            Purpose::Job(job_id) => {
                self.worker_observed_job(&value);
                if text(&value, "job_id") == job_id {
                    self.ingest_job_view(&value);
                }
            }
            Purpose::Preview => {
                if let Some(preview) = self.state.preview.as_mut()
                    && preview.operation == id
                {
                    preview.batch_id = text(&value, "batch_id").into();
                    self.state.active_batch = preview.batch_id.clone();
                    self.ingest_batch(value);
                    self.sidebar_tab = 1;
                    self.log("Recovered a legacy validation receipt. Click Run from Inputs to start a new automatic run.");
                }
            }
            Purpose::Run(run_id) => {
                if let Some(run) = self
                    .state
                    .run
                    .as_mut()
                    .filter(|run| run.id == run_id && run.operation == id)
                {
                    if text(&value, "batch_id").is_empty() {
                        self.run_error(&run_id, "The run response did not identify its batch. Recover the exact saved request.", true);
                        return;
                    }
                    run.batch_id = text(&value, "batch_id").into();
                    run.stage = text(&value, "state").into();
                    run.error.clear();
                    self.run_batch = Some(value.clone());
                    self.ingest_batch(value);
                    self.log("Run accepted. Compatible jobs queue automatically and continue on the head.");
                    self.persist();
                }
            }
            Purpose::Commit => {
                self.ingest_batch(value);
                self.sidebar_tab = 1;
                self.log(
                    "Selected pairs submitted. Jobs remain on the head when this window closes.",
                );
            }
            Purpose::CancelBatch => self.ingest_batch(value),
            Purpose::CancelJob => {
                if let Some(batch) = self.batch.as_mut()
                    && let Some(jobs) = batch["jobs"].as_array_mut()
                    && let Some(job) = jobs
                        .iter_mut()
                        .find(|job| text(job, "job_id") == text(&value, "job_id"))
                {
                    *job = value;
                }
            }
            Purpose::Logs(job, offset) => {
                if self.focused_job == job {
                    if offset == 0 {
                        self.job_log.clear();
                    }
                    self.job_log.push_str(text(&value, "text"));
                    if self.job_log.len() > 300_000 {
                        let mut at = self.job_log.len() - 250_000;
                        while !self.job_log.is_char_boundary(at) {
                            at += 1;
                        }
                        self.job_log.drain(..at);
                    }
                    self.log_offset = value["next_offset"].as_u64().unwrap_or(offset);
                }
            }
            Purpose::Upload(target) => self.uploaded(target, value),
            Purpose::LibraryHistory => self.library_received_history(value),
            Purpose::LibraryWrite(sent) => self.library_received_write(value, &sent),
            Purpose::LibraryRuns(reference) => self.library_runs_received(&reference, value),
            Purpose::Library(request) => self.library_received_list(&request, value),
            Purpose::LibraryRecord(reference) => self.library_received_record(&reference, value),
            Purpose::LibrarySequence(params) => self.library_received_sequence(&params, value),
            Purpose::LibraryProductPreview(params) => {
                self.library.sequence.received_preview(&params, value)
            }
            Purpose::LibraryAttachment(reference, name) => {
                self.library_received_attachment(&reference, &name, value, ctx);
            }
            Purpose::Annotations(artifact) => {
                self.annotation_records
                    .insert(artifact, rows(&value, "annotations").to_vec());
            }
            Purpose::SaveNote(artifact, sent_text) => {
                let note = self.state.notes.entry(artifact.clone()).or_default();
                let same_note = note.annotation_id.as_deref()
                    == value.get("annotation_id").and_then(Value::as_str)
                    || (!note.local_id.is_empty()
                        && value.pointer("/selection/local_id").and_then(Value::as_str)
                            == Some(note.local_id.as_str()));
                if same_note {
                    note.annotation_id = Some(text(&value, "annotation_id").into());
                    note.revision = value["revision"].as_u64();
                }
                if same_note && note.text == sent_text {
                    self.log("Annotation saved on the head.");
                } else {
                    self.log(
                        "Annotation receipt recovered. Your current local editor was preserved.",
                    );
                }
                self.request(
                    "annotation.list",
                    json!({"artifact_id":artifact}),
                    Purpose::Annotations(artifact),
                );
            }
            Purpose::Artifact(_) => {
                self.log("Unexpected artifact reply; original results remain on the head.")
            }
        }
    }
    pub(super) fn ingest_batch(&mut self, value: Value) {
        let id = text(&value, "batch_id").to_owned();
        for job in rows(&value, "jobs") {
            self.ingest_job_view(job);
        }
        merge_batch_history(&mut self.batches, value.clone());
        self.state.active_batch = id;
        self.batch = Some(value);
    }
    pub(super) fn poll(&mut self) {
        if !self.connected {
            return;
        }
        self.worker_poll();
        if self.last_poll.elapsed() > Duration::from_secs(4) {
            self.last_poll = Instant::now();
            self.poll_job_tabs();
            self.library_runs_poll();
            if !self.state.active_batch.is_empty() {
                let id = self.state.active_batch.clone();
                self.request("batch.get", json!({"batch_id":id}), Purpose::Batch(id));
            }
            if let Some(run) = &self.state.run
                && !run.batch_id.is_empty()
                && run.batch_id != self.state.active_batch
                && (self.preview_open
                    || self
                        .run_batch
                        .as_ref()
                        .is_none_or(|batch| !ui_state::terminal(text(batch, "state"))))
            {
                let id = run.batch_id.clone();
                self.request("batch.get", json!({"batch_id":id}), Purpose::Batch(id));
            }
            if !self.focused_job.is_empty() {
                let id = self.focused_job.clone();
                self.request(
                    "job.logs",
                    json!({"job_id":id,"offset":self.log_offset,"max_bytes":65536}),
                    Purpose::Logs(id, self.log_offset),
                );
            }
        }
        if self.last_history.elapsed() > Duration::from_secs(20) {
            self.last_history = Instant::now();
            self.request("batch.list", json!({"limit":100}), Purpose::History);
        }
    }
    pub(super) fn persist(&mut self) {
        self.capture_views();
        self.state
            .extra
            .insert("library_explorer".into(), self.library.preferences());
        let value = match serde_json::to_value(&self.state) {
            Ok(value) => value,
            Err(error) => {
                self.save_error = error.to_string();
                return;
            }
        };
        let encoded = value.to_string();
        if encoded == self.saved_state {
            return;
        }
        if let Some(session) = self.session.as_mut() {
            match session.save_draft(value) {
                Ok(()) => {
                    self.saved_state = encoded;
                    self.save_error.clear();
                }
                Err(error) => self.save_error = error.to_string(),
            }
        }
    }
    fn navigate(&mut self, link: &str, ctx: &egui::Context) {
        if let Some(id) = link.strip_prefix("bio-workbench://batch/") {
            if id.is_empty()
                || id.len() > 160
                || !id
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
            {
                self.log("Invalid batch link.");
                return;
            }
            self.state.active_batch = id.into();
            self.sidebar_tab = 1;
            self.request(
                "batch.get",
                json!({"batch_id":id}),
                Purpose::Batch(id.into()),
            );
        } else {
            self.picked(Pick::Inputs, vec![PathBuf::from(link)], ctx);
        }
    }
    pub(super) fn choose_files(&self, kind: Pick, ctx: &egui::Context) {
        let sender = self.ui_tx.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let files = match &kind {
                Pick::Key | Pick::Labels(_) | Pick::Attachment(_) => {
                    rfd::FileDialog::new().pick_file().into_iter().collect()
                }
                _ => rfd::FileDialog::new().pick_files().unwrap_or_default(),
            };
            let _ = sender.send(UiEvent::Files(kind, files));
            ctx.request_repaint();
        });
    }
    pub(super) fn connection_dialog(&mut self, ctx: &egui::Context) {
        let mut open = self.connection_open;
        egui::Window::new("Head connection").open(&mut open).default_width(450.).resizable(false).show(ctx,|ui|{
            ui.label("Connect to the head with a local SSH key or agent.");egui::Grid::new("connection-fields").num_columns(2).show(ui,|ui|{
                ui.label("Hostname / IP");ui.text_edit_singleline(&mut self.connection.host);ui.end_row();ui.label("SSH user");ui.text_edit_singleline(&mut self.connection.user);ui.end_row();ui.label("Port");ui.add(egui::DragValue::new(&mut self.connection.port).range(1..=65535));ui.end_row();
                ui.label("Key path");ui.horizontal(|ui|{ui.text_edit_singleline(&mut self.connection.key_path);if ui.button("…").clicked(){self.choose_files(Pick::Key,ctx);}});ui.end_row();
            });ui.small("Leave key path empty for SSH agent authentication. Verify unknown host keys in SSH first.");
            if ui.add_enabled(!self.busy(&Purpose::Catalog),egui::Button::new("Save & connect")).clicked(){
                if let Some(session)=self.session.as_mut(){let old_endpoint=session.connection.identity();let changed=session.connection.host!=self.connection.host||session.connection.user!=self.connection.user||session.connection.port!=self.connection.port;
                    let mut next_state=self.state.clone();let detached=if changed {
                        if let Some(run) = next_state.run.take() {
                            let archive = next_state.extra.entry("detached_run_intents".into()).or_insert_with(||json!([]));
                            if let Some(archive) = archive.as_array_mut() { archive.push(serde_json::to_value(run).unwrap_or(Value::Null)); }
                        }
                        next_state.detach_library_sources(&old_endpoint)
                    } else { 0 };
                    let saved=if changed { serde_json::to_value(&next_state).map_err(rpc::RpcError::from).and_then(|draft|session.save_connection_with_draft(self.connection.clone(),draft)) } else {session.save_connection(self.connection.clone())};match saved{
                    Ok(())=>{self.state=next_state;if changed{self.connected=false;self.worker=ui_worker::Worker::default();self.batches.clear();self.batch=None;self.catalog=Value::Null;self.library=ui_library::Explorer::default();self.library_runs=ui_library_runs::RunControls::default();self.state.preview=None;self.run_batch=None;self.run_after_uploads=false;self.state.active_batch.clear();self.annotation_records.clear();self.artifact_metadata.clear();self.selected_artifacts.clear();self.pending.clear();self.detach_head_views();self.focused_job.clear();self.job_log.clear();
                    if detached>0 { self.log("Library references were detached from the previous head and retained in the local draft archive. Select them again from the new head's Library before running."); }
                    for input in &mut self.state.inputs{if text(&input.source,"kind")=="upload"{input.source["upload_id"]=json!("");input.source.as_object_mut().map(|m|m.remove("attachments"));}}for settings in self.state.settings.values_mut(){if let Some(settings)=settings.as_object_mut(){settings.remove("labels_upload_id");}}
                    self.log("Connection changed. Prior structures remain local; their annotations are detached from the new head. Re-upload files before running.");}self.request("catalog",json!({}),Purpose::Catalog);},Err(error)=>self.log(error.to_string()),
                }}else{match session::Session::open(ctx.clone()){Ok(session)=>{self.session=Some(session);self.log("Local session reopened; save connection settings to connect.");},Err(error)=>self.log(error.to_string())}}
            }
            for failure in self.failures.iter().filter(|failure|failure.purpose==Purpose::Catalog).rev().take(1){ui.colored_label(RED,&failure.message);}
        });
        self.connection_open = open;
    }
}

#[cfg(test)]
mod history_tests {
    use super::*;
    #[test]
    fn opening_or_refreshing_an_older_run_preserves_creation_order() {
        let newest =
            json!({"batch_id":"new","created_at":"2026-09-10T03:00:00Z","state":"running"});
        let older = json!({"batch_id":"old","created_at":"2026-09-09T03:00:00Z","state":"running"});
        let mut history = vec![newest, older.clone()];
        let mut opened = older;
        opened["updated_at"] = json!("2026-09-11T00:00:00Z");
        opened["state"] = json!("complete");
        merge_batch_history(&mut history, opened.clone());
        merge_batch_history(&mut history, opened);
        assert_eq!(
            history
                .iter()
                .map(|v| text(v, "batch_id"))
                .collect::<Vec<_>>(),
            vec!["new", "old"]
        );
        assert_eq!(history[1]["state"], "complete");
        merge_batch_history(
            &mut history,
            json!({"batch_id":"latest","created_at":"2026-09-10T04:00:00Z"}),
        );
        assert_eq!(text(&history[0], "batch_id"), "latest");
    }
    #[test]
    fn history_partial_update_keeps_known_creation_and_ties_are_stable() {
        let mut history = vec![
            json!({"batch_id":"a","created_at":"2026-09-10T03:00:00Z"}),
            json!({"batch_id":"b","created_at":"2026-09-10T03:00:00Z"}),
        ];
        merge_batch_history(&mut history, json!({"batch_id":"a","state":"complete"}));
        assert_eq!(
            history
                .iter()
                .map(|v| text(v, "batch_id"))
                .collect::<Vec<_>>(),
            vec!["b", "a"]
        );
        assert_eq!(text(&history[1], "created_at"), "2026-09-10T03:00:00Z");
    }
}
