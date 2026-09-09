//! Read-only prediction history, linked through exact library source revisions.
use super::*;

#[derive(Default)]
pub(super) struct RunControls {
    reference: String,
    records: Vec<Value>,
    next_cursor: Option<String>,
    loading_cursor: Option<String>,
    keep_older: bool,
    loaded: bool,
    error: String,
    last_refresh: Option<Instant>,
}

fn revision_family(reference: &str) -> Option<(&str, u64)> {
    let (family, revision) = reference.rsplit_once('@')?;
    let revision = revision.parse::<u64>().ok().filter(|n| *n > 0)?;
    family
        .starts_with("construct:")
        .then_some((family, revision))
}

fn resource_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

impl RunControls {
    fn accept(&mut self, reference: &str, value: Value) -> bool {
        if self.reference != reference {
            return false;
        }
        let records = rows(&value, "records");
        let family = revision_family(reference).map(|(family, _)| family);
        let next = value.get("next_cursor");
        let valid = text(&value, "ref") == reference
            && text(&value, "scope") == "current_actor"
            && text(&value, "match") == "explicit_library_reference"
            && value["records"].is_array()
            && records.len() <= 100
            && next.is_some_and(|next| next.is_null() || next.as_str().is_some_and(resource_id))
            && records.iter().all(|record| {
                let refs = rows(record, "source_refs");
                resource_id(text(record, "job_id"))
                    && resource_id(text(record, "batch_id"))
                    && !refs.is_empty()
                    && refs.iter().all(|source| {
                        source
                            .as_str()
                            .and_then(revision_family)
                            .is_some_and(|(found, _)| Some(found) == family)
                    })
                    && record["selected_revision"].as_bool()
                        == Some(refs.iter().any(|source| source.as_str() == Some(reference)))
            });
        if !valid {
            self.error = "The head returned incomplete or unrelated run history.".into();
            self.last_refresh = Some(Instant::now());
            return false;
        }
        if self.loading_cursor.is_none() && !self.keep_older {
            self.records.clear();
        }
        for record in records {
            let id = text(record, "job_id");
            if let Some(existing) = self.records.iter_mut().find(|r| text(r, "job_id") == id) {
                *existing = record.clone();
            } else {
                self.records.push(record.clone());
            }
        }
        if self.keep_older {
            self.records.sort_by(|a, b| {
                text(b, "created_at")
                    .cmp(text(a, "created_at"))
                    .then_with(|| text(b, "job_id").cmp(text(a, "job_id")))
            });
        } else {
            self.next_cursor = value["next_cursor"].as_str().map(str::to_owned);
        }
        self.loaded = true;
        self.error.clear();
        self.last_refresh = Some(Instant::now());
        true
    }
}

impl Workbench {
    pub(super) fn library_runs_refresh(&mut self, cursor: Option<String>) {
        self.library_runs_load(cursor, false);
    }

    fn library_runs_load(&mut self, cursor: Option<String>, keep_older: bool) {
        let reference = self.library_runs.reference.clone();
        if reference.is_empty() || self.busy(&Purpose::LibraryRuns(reference.clone())) {
            return;
        }
        self.library_runs.loading_cursor = cursor.clone();
        self.library_runs.keep_older = keep_older;
        self.library_runs.last_refresh = Some(Instant::now());
        self.library_runs.error.clear();
        let mut params = json!({"ref":reference,"limit":30,"include_revisions":true});
        if let Some(cursor) = cursor {
            params["cursor"] = json!(cursor);
        }
        if self
            .request("library.runs", params, Purpose::LibraryRuns(reference))
            .is_none()
        {
            self.library_runs.error =
                "Connect to the head to load this protein's run history.".into();
        }
    }

    pub(super) fn library_runs_received(&mut self, reference: &str, value: Value) {
        self.library_runs.accept(reference, value);
    }

    pub(super) fn library_runs_failed(&mut self, purpose: &Purpose, message: &str) {
        if let Purpose::LibraryRuns(reference) = purpose
            && *reference == self.library_runs.reference
        {
            self.library_runs.error = message.into();
            self.library_runs.last_refresh = Some(Instant::now());
        }
    }

    pub(super) fn library_runs_poll(&mut self) {
        if self.sidebar_tab == 2
            && self.library.selected == self.library_runs.reference
            && self.library_runs.loaded
            && self
                .library_runs
                .last_refresh
                .is_none_or(|when| when.elapsed() >= Duration::from_secs(20))
        {
            self.library_runs_load(None, true);
        }
    }

    pub(super) fn library_run_controls(&mut self, ui: &mut egui::Ui, detail: &Value) {
        let record = &detail["record"];
        if text(record, "kind") != "construct"
            || text(&record["identity"], "molecule_type") != "protein"
        {
            return;
        }
        let reference = text(detail, "ref");
        if revision_family(reference).is_none() {
            return;
        }
        if self.library_runs.reference != reference {
            self.library_runs = RunControls {
                reference: reference.into(),
                ..Default::default()
            };
        }
        if !self.library_runs.loaded && self.library_runs.last_refresh.is_none() {
            self.library_runs_refresh(None);
        }
        let busy = self.busy(&Purpose::LibraryRuns(reference.into()));
        ui.horizontal(|ui| {
            ui.label(RichText::new("RUN HISTORY").strong().size(11.));
            if ui
                .add_enabled(!busy, egui::Button::new("Refresh").small())
                .clicked()
            {
                self.library_runs_refresh(None);
            }
            if busy {
                ui.spinner();
            }
        });
        ui.small("Runs from this head connection, across this protein's revisions.");
        if !self.library_runs.error.is_empty() {
            ui.colored_label(RED, &self.library_runs.error);
        }
        if self.library_runs.loaded && self.library_runs.records.is_empty() {
            ui.weak("No runs linked to this library protein yet.");
            ui.small("Earlier runs submitted by pasted sequence or file are not assigned by name or sequence similarity.");
        }
        let mut open_job = None;
        egui::ScrollArea::vertical()
            .id_salt(("library-run-history", reference))
            .max_height(250.)
            .auto_shrink([false, true])
            .show(ui, |ui| {
                for job in &self.library_runs.records {
                    ui.push_id(text(job, "job_id"), |ui| {
                        let state = text(job, "state");
                        let color = match state {
                            "complete" => GREEN,
                            "failed" | "interrupted" => RED,
                            "running" | "starting" | "queued" => AMBER,
                            _ => Color32::LIGHT_GRAY,
                        };
                        ui.horizontal_wrapped(|ui| {
                            let model = text(job, "model");
                            let structures = job["structure_count"].as_u64().unwrap_or(0);
                            let label = if structures > 0 {
                                format!("▶ {model} · Open structure")
                            } else {
                                format!("{model} · Open run")
                            };
                            if ui
                                .button(label)
                                .on_hover_text(format!(
                                    "{}\nJob {}",
                                    text(job, "batch_name"),
                                    text(job, "job_id")
                                ))
                                .clicked()
                            {
                                open_job = Some(text(job, "job_id").to_owned());
                            }
                            ui.colored_label(color, state);
                            ui.weak(
                                text(job, "created_at")
                                    .replace('T', " ")
                                    .split('.')
                                    .next()
                                    .unwrap_or(""),
                            );
                        });
                        ui.horizontal_wrapped(|ui| {
                            for source in rows(job, "source_refs").iter().filter_map(Value::as_str)
                            {
                                let revision = revision_family(source).map(|(_, n)| n).unwrap_or(0);
                                ui.weak(if source == reference {
                                    format!("Revision {revision} · selected")
                                } else {
                                    format!("Revision {revision}")
                                })
                                .on_hover_text(source);
                            }
                            let count = job["artifact_count"].as_u64().unwrap_or(0);
                            ui.weak(format!("{count} retained files"));
                        });
                        ui.separator();
                    });
                }
            });
        if let Some(cursor) = self.library_runs.next_cursor.clone()
            && ui
                .add_enabled(!busy, egui::Button::new("Load older runs"))
                .clicked()
        {
            self.library_runs_refresh(Some(cursor));
        }
        if let Some(job) = open_job {
            self.open_job_new_tab(job, ui.ctx());
            self.sidebar_tab = 1;
        }
        ui.separator();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn response(reference: &str, id: char, source: &str) -> Value {
        json!({"ref":reference,"scope":"current_actor","match":"explicit_library_reference",
            "records":[{"job_id":id.to_string().repeat(32),"batch_id":"b".repeat(32),
                "source_refs":[source],"selected_revision":source==reference}],"next_cursor":null})
    }

    #[test]
    fn history_rejects_stale_selection_and_unrelated_source() {
        let reference = "construct:editor@2";
        let mut state = RunControls {
            reference: reference.into(),
            ..Default::default()
        };
        assert!(!state.accept(
            "construct:other@1",
            response("construct:other@1", 'a', "construct:other@1")
        ));
        assert!(state.error.is_empty());
        assert!(!state.accept(reference, response(reference, 'a', "construct:other@1")));
        assert!(state.records.is_empty());
        assert!(!state.error.is_empty());
    }

    #[test]
    fn history_preserves_revision_markers_and_deduplicates_pages() {
        let reference = "construct:editor@2";
        let mut state = RunControls {
            reference: reference.into(),
            ..Default::default()
        };
        assert!(state.accept(reference, response(reference, 'a', "construct:editor@1")));
        assert_eq!(state.records[0]["selected_revision"], false);
        state.loading_cursor = Some("a".repeat(32));
        assert!(state.accept(reference, response(reference, 'a', "construct:editor@1")));
        assert!(state.accept(reference, response(reference, 'c', reference)));
        assert_eq!(state.records.len(), 2);
        assert_eq!(state.records[1]["selected_revision"], true);
        state.loading_cursor = None;
        assert!(state.accept(reference, response(reference, 'c', reference)));
        assert_eq!(state.records.len(), 1);
    }

    #[test]
    fn history_requires_actor_scope_and_well_formed_job_ids() {
        let reference = "construct:editor@2";
        let mut state = RunControls {
            reference: reference.into(),
            ..Default::default()
        };
        for invalid in ['z', '/'] {
            assert!(!state.accept(reference, response(reference, invalid, reference)));
        }
        let mut value = response(reference, 'a', reference);
        value["scope"] = json!("all_actors");
        assert!(!state.accept(reference, value));
        assert_eq!(revision_family("construct:editor@0"), None);
        assert_eq!(revision_family("project:editor@1"), None);
    }

    #[test]
    fn background_refresh_keeps_loaded_older_rows_and_pagination() {
        let reference = "construct:editor@2";
        let mut state = RunControls {
            reference: reference.into(),
            ..Default::default()
        };
        assert!(state.accept(reference, response(reference, 'a', "construct:editor@1")));
        state.next_cursor = Some("a".repeat(32));
        state.keep_older = true;
        assert!(state.accept(reference, response(reference, 'c', reference)));
        assert_eq!(state.records.len(), 2);
        assert_eq!(state.next_cursor, Some("a".repeat(32)));
    }
}
