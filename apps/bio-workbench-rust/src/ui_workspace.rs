use super::*;

// Tab IDs are request destinations, independent of their position in the dock.
// Never reuse one while an asynchronous parse or download can still finish.
impl Workbench {
    pub(super) fn view_reference(&self, slot: usize) -> Option<&Value> {
        self.state
            .view_refs
            .iter()
            .find(|r| r["slot"].as_u64() == Some(slot as u64))
    }

    pub(super) fn has_view(&self, slot: usize) -> bool {
        self.view_reference(slot).is_some()
    }

    pub(super) fn update_view_reference(&mut self, slot: usize, mut metadata: Value) {
        if let Some(reference) = self
            .state
            .view_refs
            .iter_mut()
            .find(|r| r["slot"].as_u64() == Some(slot as u64))
        {
            metadata["slot"] = json!(slot);
            *reference = metadata;
        }
    }

    pub(super) fn reserve_view(&mut self, mut metadata: Value) -> usize {
        let slot = self.next_view_id;
        self.next_view_id += 1;
        metadata["slot"] = json!(slot);
        self.state.view_refs.push(metadata);
        self.focus_view(slot);
        slot
    }

    pub(super) fn retire_renderer(&mut self, renderer: scene::Renderer) {
        // DockArea may already have queued this renderer's paint callback. Free
        // it at the next frame, after that callback has finished on the GL thread.
        self.retired_renderers.push(renderer);
    }

    pub(super) fn close_view(&mut self, slot: usize) {
        if let Some(location) = self.dock.find_tab(&slot) {
            self.dock.remove_tab(location);
        }
        if let Some(view) = self.views.remove(&slot) {
            self.retire_renderer(view.renderer);
        }
        self.view_loading.remove(&slot);
        self.view_errors.remove(&slot);
        self.state
            .view_refs
            .retain(|r| r["slot"].as_u64() != Some(slot as u64));
        let waiting: BTreeSet<_> = self
            .state
            .view_refs
            .iter()
            .filter(|r| text(r, "source_kind") == "job")
            .map(|r| text(r, "job_id").to_owned())
            .collect();
        let keep = |purpose: &Purpose| match purpose {
            Purpose::Artifact(ArtifactTarget::View(id)) => *id != slot,
            Purpose::Job(job) => waiting.contains(job),
            _ => true,
        };
        self.pending.retain(|_, pending| keep(&pending.purpose));
        self.failures.retain(|failure| keep(&failure.purpose));
        // Annotations and uncertain write receipts are deliberately independent
        // of tabs: closing a viewer never discards edits or cancels a cloud job.
        if self.state.selected_view == slot {
            let next = self
                .dock
                .find_active_focused()
                .map(|(_, id)| *id)
                .or_else(|| {
                    self.state
                        .view_refs
                        .first()?
                        .get("slot")?
                        .as_u64()
                        .map(|id| id as usize)
                });
            if let Some(next) = next {
                self.focus_view(next);
            } else {
                self.state.selected_view = 0;
            }
        }
        self.state.dock_layout = ui_dock::save_layout(&self.dock);
    }

    pub(super) fn open_local_tab(
        &mut self,
        path: PathBuf,
        mut metadata: Value,
        ctx: &egui::Context,
    ) {
        metadata["source_kind"] = json!("local");
        metadata["local_path"] = json!(path);
        let slot = self.reserve_view(metadata.clone());
        self.load_structure(path, metadata, slot, ctx);
    }

    pub(super) fn duplicate_view(&mut self, source: usize, ctx: &egui::Context) -> Option<usize> {
        let reference = self.view_reference(source)?;
        let live = self.views.get(&source).filter(|_| {
            !self.view_loading.contains_key(&source) && !self.view_errors.contains_key(&source)
        });
        let metadata = duplicate_reference(reference, live.map(ui_views::view_state));
        // Reserve in the source's group even if its context menu was opened
        // while another group was active. A copy always receives a fresh ID.
        self.focus_view(source);
        let target = self.reserve_view(metadata.clone());
        if self.copy_loaded_view(source, target) {
            return Some(target);
        }
        let artifact = text(&metadata, "artifact_id");
        if !artifact.is_empty() {
            self.request_artifact(artifact, ArtifactTarget::View(target));
        } else {
            match text(&metadata, "source_kind") {
                "job" => {
                    let job = text(&metadata, "job_id").to_owned();
                    self.view_loading.insert(target, format!("job:{job}"));
                    self.request("job.get", json!({"job_id":job}), Purpose::Job(job));
                }
                "local" => {
                    let path = PathBuf::from(text(&metadata, "local_path"));
                    self.load_structure(path, metadata, target, ctx);
                }
                "demo" => {
                    let mut metadata = metadata;
                    metadata["load_token"] = json!(uid());
                    self.update_view_reference(target, metadata.clone());
                    self.view_loading
                        .insert(target, text(&metadata, "load_token").into());
                    self.accept_molecule(
                        target,
                        metadata,
                        Arc::new(scene::Molecule::reference_bytes()),
                        Ok(scene::Molecule::reference()),
                    );
                }
                _ => {
                    let error = self.view_errors.get(&source).cloned().unwrap_or_else(|| {
                        "This tab has no readable source. Reopen its file or run to load it.".into()
                    });
                    self.view_errors.insert(target, error);
                }
            }
        }
        Some(target)
    }

    pub(super) fn open_artifact_new_tab(&mut self, id: String, ctx: &egui::Context) {
        if let Some(source) = matching_view(
            &self.state.view_refs,
            self.state.selected_view,
            "artifact_id",
            &id,
        ) {
            self.duplicate_view(source, ctx);
        } else {
            self.open_artifact_id(id);
        }
    }

    pub(super) fn open_job_new_tab(&mut self, id: String, ctx: &egui::Context) {
        if let Some(source) = matching_view(
            &self.state.view_refs,
            self.state.selected_view,
            "job_id",
            &id,
        ) {
            self.duplicate_view(source, ctx);
        } else {
            self.open_job_tab(id);
        }
    }

    pub(super) fn open_artifact_id(&mut self, id: String) {
        if let Some(slot) = self
            .state
            .view_refs
            .iter()
            .find(|r| text(r, "artifact_id") == id)
            .and_then(|r| r["slot"].as_u64())
        {
            self.focus_view(slot as usize);
            if self.view_errors.contains_key(&(slot as usize)) {
                self.request_artifact(&id, ArtifactTarget::View(slot as usize));
            }
            return;
        }
        let mut metadata = self
            .artifact_metadata
            .get(&id)
            .cloned()
            .unwrap_or_else(|| json!({"artifact_id":id,"name":id}));
        metadata["source_kind"] = json!("artifact");
        let slot = self.reserve_view(metadata);
        self.request_artifact(&id, ArtifactTarget::View(slot));
    }

    pub(super) fn open_job_tab(&mut self, id: String) {
        if let Some(slot) = self
            .state
            .view_refs
            .iter()
            .find(|r| text(r, "job_id") == id)
            .and_then(|r| r["slot"].as_u64())
        {
            self.focus_view(slot as usize);
            if self.view_errors.contains_key(&(slot as usize)) {
                let artifact = self
                    .view_reference(slot as usize)
                    .map(|r| text(r, "artifact_id").to_owned())
                    .unwrap_or_default();
                if artifact.is_empty() {
                    self.request("job.get", json!({"job_id":id}), Purpose::Job(id));
                } else {
                    self.request_artifact(&artifact, ArtifactTarget::View(slot as usize));
                }
            }
            return;
        }
        let job = self
            .batch
            .as_ref()
            .and_then(|batch| {
                rows(batch, "jobs")
                    .iter()
                    .find(|job| text(job, "job_id") == id)
            })
            .cloned();
        let metadata = json!({"source_kind":"job","job_id":id,
            "name":job.as_ref().map(|j| text(j,"input_name")).filter(|name| !name.is_empty()).unwrap_or(&id),
            "model":job.as_ref().map(|j| text(j,"model")).unwrap_or(""),
            "job_state":job.as_ref().map(|j| text(j,"state")).unwrap_or("loading")});
        let slot = self.reserve_view(metadata);
        self.view_loading.insert(slot, format!("job:{id}"));
        self.request("job.get", json!({"job_id":id}), Purpose::Job(id));
    }

    pub(super) fn ingest_job_view(&mut self, job: &Value) {
        let id = text(job, "job_id");
        let artifacts = rows(job, "artifacts")
            .iter()
            .map(|artifact| {
                let mut artifact = artifact.clone();
                artifact["job_id"] = json!(id);
                artifact["model"] = job["model"].clone();
                artifact["input_name"] = job["input_name"].clone();
                artifact["job_state"] = job["state"].clone();
                artifact["job_provenance"] = job["provenance"].clone();
                artifact
            })
            .collect::<Vec<_>>();
        for artifact in &artifacts {
            self.artifact_metadata
                .insert(text(artifact, "artifact_id").into(), artifact.clone());
        }
        let preferred = preferred_structure(&artifacts);
        let slots: Vec<usize> = self
            .state
            .view_refs
            .iter()
            .filter(|r| text(r, "source_kind") == "job" && text(r, "job_id") == id)
            .filter_map(|r| r["slot"].as_u64().map(|slot| slot as usize))
            .collect();
        for slot in slots {
            let mut metadata = self.view_reference(slot).unwrap().clone();
            metadata["job_state"] = job["state"].clone();
            metadata["job_phase"] = job["phase"].clone();
            if let Some(artifact) = preferred {
                let saved = metadata["view_state"].clone();
                metadata = artifact.clone();
                metadata["source_kind"] = json!("artifact");
                metadata["view_state"] = saved;
                self.update_view_reference(slot, metadata);
                self.view_errors.remove(&slot);
                self.request_artifact(text(artifact, "artifact_id"), ArtifactTarget::View(slot));
            } else {
                self.update_view_reference(slot, metadata);
                if ui_state::terminal(text(job, "state")) {
                    self.view_loading.remove(&slot);
                    let error = format!(
                        "Run {}: no predicted structure is available. {}",
                        text(job, "state"),
                        job.get("error")
                            .filter(|e| !e.is_null())
                            .map(Value::to_string)
                            .unwrap_or_default()
                    );
                    self.view_errors.insert(slot, error);
                } else {
                    self.view_loading.insert(slot, format!("job:{id}"));
                    self.view_errors.remove(&slot);
                }
            }
        }
    }

    pub(super) fn poll_job_tabs(&mut self) {
        for id in waiting_jobs(&self.state.view_refs) {
            self.request("job.get", json!({"job_id":id}), Purpose::Job(id));
        }
    }

    pub(super) fn job_tab_error(&mut self, job: &str, error: &str) {
        for reference in &self.state.view_refs {
            if text(reference, "source_kind") == "job" && text(reference, "job_id") == job {
                let slot = reference["slot"].as_u64().unwrap() as usize;
                self.view_loading.remove(&slot);
                self.view_errors.insert(slot, error.into());
            }
        }
    }

    pub(super) fn detach_head_views(&mut self) {
        self.capture_views();
        for reference in &mut self.state.view_refs {
            if !matches!(text(reference, "source_kind"), "artifact" | "job") {
                continue;
            }
            let slot = reference["slot"].as_u64().unwrap() as usize;
            self.view_loading.remove(&slot);
            reference["previous_head_artifact"] = reference["artifact_id"].clone();
            if let Some(object) = reference.as_object_mut() {
                object.remove("artifact_id");
                object.remove("job_id");
            }
            if let Some(view) = self.views.get_mut(&slot) {
                reference["source_kind"] = json!("local");
                view.metadata = reference.clone();
            } else {
                reference["source_kind"] = json!("detached");
                self.view_errors.insert(slot,"This tab belongs to the previous head. Reconnect to that head and reopen its run.".into());
            }
        }
        self.failures.retain(|failure| {
            !matches!(
                failure.purpose,
                Purpose::Artifact(ArtifactTarget::View(_)) | Purpose::Job(_)
            )
        });
    }
}

fn duplicate_reference(reference: &Value, live_view_state: Option<Value>) -> Value {
    let mut metadata = reference.clone();
    if let Some(object) = metadata.as_object_mut() {
        for transient in ["slot", "load_token", "download_operation"] {
            object.remove(transient);
        }
    }
    if let Some(view_state) = live_view_state {
        metadata["view_state"] = view_state;
    }
    metadata
}

fn matching_view(refs: &[Value], selected: usize, key: &str, id: &str) -> Option<usize> {
    refs.iter()
        .filter(|reference| text(reference, key) == id)
        .filter_map(|reference| {
            reference["slot"]
                .as_u64()
                .and_then(|id| usize::try_from(id).ok())
        })
        .min_by_key(|slot| (*slot != selected, *slot))
}

fn waiting_jobs(refs: &[Value]) -> BTreeSet<String> {
    refs.iter()
        .filter(|r| text(r, "source_kind") == "job" && !ui_state::terminal(text(r, "job_state")))
        .map(|r| text(r, "job_id").to_owned())
        .filter(|id| !id.is_empty())
        .collect()
}

fn preferred_structure(artifacts: &[Value]) -> Option<&Value> {
    artifacts
        .iter()
        .filter(|artifact| {
            ui_jobs::is_structure(artifact)
                && matches!(
                    text(artifact, "role"),
                    "structure" | "prediction" | "selected_structure"
                )
        })
        .min_by_key(|artifact| if artifact["selected"] == true { 0 } else { 1 })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duplicate_keeps_source_identity_and_live_preferences_without_request_tokens() {
        let original = json!({"slot":7,"source_kind":"artifact","job_id":"j",
            "artifact_id":"a","sha256":"retained-sha","load_token":"parse:old",
            "download_operation":"old-download","view_state":{"style":"cartoon"}});
        let live = json!({"style":"sticks","camera":{"yaw":1.2},"chains":[true,false]});
        let mut copy = duplicate_reference(&original, Some(live.clone()));
        assert_eq!(copy["artifact_id"], "a");
        assert_eq!(copy["job_id"], "j");
        assert_eq!(copy["sha256"], "retained-sha");
        assert_eq!(copy["view_state"], live);
        for key in ["slot", "load_token", "download_operation"] {
            assert!(copy.get(key).is_none());
        }
        copy["view_state"]["camera"]["yaw"] = json!(9.);
        assert_eq!(original["view_state"], json!({"style":"cartoon"}));
    }

    #[test]
    fn pending_duplicate_retains_job_and_saved_preferences_for_independent_loading() {
        let original = json!({"slot":12,"source_kind":"job","job_id":"queued-job",
            "job_state":"running","job_phase":"predicting","load_token":"old",
            "view_state":{"camera":{"yaw":0.3},"style":"spheres"}});
        let copy = duplicate_reference(&original, None);
        assert_eq!(copy["job_id"], "queued-job");
        assert_eq!(copy["job_phase"], "predicting");
        assert_eq!(copy["view_state"], original["view_state"]);
        assert!(copy.get("slot").is_none() && copy.get("load_token").is_none());
    }

    #[test]
    fn reopening_uses_selected_matching_copy_without_confusing_another_run() {
        let refs = vec![
            json!({"slot":7,"job_id":"j","artifact_id":"a"}),
            json!({"slot":13,"job_id":"other","artifact_id":"b"}),
            json!({"slot":21,"job_id":"j","artifact_id":"a"}),
        ];
        assert_eq!(matching_view(&refs, 21, "job_id", "j"), Some(21));
        assert_eq!(matching_view(&refs, 13, "job_id", "j"), Some(7));
        assert_eq!(matching_view(&refs, 7, "artifact_id", "b"), Some(13));
        assert_eq!(matching_view(&refs, 7, "artifact_id", "missing"), None);
    }

    #[test]
    fn selected_prediction_wins_and_input_coordinates_are_excluded() {
        let artifacts = vec![
            json!({"format":"pdb","role":"input","selected":true,"artifact_id":"input"}),
            json!({"format":"cif","role":"structure","artifact_id":"sample1"}),
            json!({"format":"cif","role":"structure","selected":true,"artifact_id":"best"}),
        ];
        assert_eq!(
            text(preferred_structure(&artifacts).unwrap(), "artifact_id"),
            "best"
        );
        assert!(preferred_structure(&artifacts[..1]).is_none());
    }
    #[test]
    fn watches_follow_live_job_tabs_across_batches() {
        let refs = vec![
            json!({"slot":17,"source_kind":"job","job_id":"active","job_state":"running"}),
            json!({"slot":21,"source_kind":"job","job_id":"active","job_state":"running"}),
            json!({"source_kind":"job","job_id":"failed","job_state":"failed"}),
            json!({"source_kind":"artifact","job_id":"loaded","job_state":"complete"}),
        ];
        assert_eq!(waiting_jobs(&refs), BTreeSet::from(["active".into()]));
    }
}
