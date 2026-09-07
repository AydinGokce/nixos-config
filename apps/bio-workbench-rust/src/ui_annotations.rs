use super::*;

fn note_key(metadata: &Value) -> String {
    if text(metadata, "artifact_id").is_empty() {
        format!("local:{}", text(metadata, "sha256"))
    } else {
        text(metadata, "artifact_id").into()
    }
}
fn selection(view: &ui_views::View, local_id: &str) -> Value {
    let Some(key) = view.selected.as_ref() else {
        return json!({"kind":"bio-workbench-note-v1","local_id":local_id,"artifact_sha256":text(&view.metadata,"sha256")});
    };
    let anchor = view
        .molecule
        .residue(key)
        .map(|r| &view.molecule.atoms[r.anchor]);
    let residue = json!({"chain":key.chain,"resi":key.sequence.parse::<i64>().map(Value::from).unwrap_or_else(|_|json!(key.sequence)),"icode":key.insertion,"resn":key.component,"atom":anchor.map(|a|a.name.as_str()).unwrap_or(""),"serial":anchor.and_then(|a|a.serial.parse::<u64>().ok())});
    json!({"kind":"bio-workbench-residue-v1","local_id":local_id,"artifact_sha256":text(&view.metadata,"sha256"),"residue":residue,"native_key":key,"label":key.to_string(),"note":"","color":"#E1BD71","deleted":false})
}
fn selection_key(selection: &Value, molecule: &scene::Molecule) -> Option<scene::ResidueKey> {
    if let Ok(key) = serde_json::from_value::<scene::ResidueKey>(selection["native_key"].clone())
        && molecule.residue(&key).is_some()
    {
        return Some(key);
    }
    let residue = &selection["residue"];
    let sequence = residue["resi"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| residue["resi"].to_string());
    let mut matched = molecule.residues.iter().filter(|r| {
        r.key.chain == text(residue, "chain")
            && r.key.sequence == sequence
            && r.key.insertion == text(residue, "icode")
            && (text(residue, "resn").is_empty() || r.key.component == text(residue, "resn"))
    });
    let first = matched.next()?;
    if matched.next().is_some() {
        return None;
    }
    Some(first.key.clone())
}
impl Workbench {
    fn preserve_note(&mut self, key: &str) {
        if let Some(note) = self
            .state
            .notes
            .get(key)
            .filter(|note| !note.text.trim().is_empty())
        {
            let value = json!({"artifact":key,"draft":note});
            let history = self
                .state
                .extra
                .entry("note_history".into())
                .or_insert_with(|| json!([]));
            if let Some(history) = history.as_array_mut()
                && history.last() != Some(&value)
            {
                history.push(value);
            }
        }
    }
    pub(super) fn annotations_panel(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        Self::section(ui, "ANNOTATIONS / RESEARCH NOTES");
        let slot = self.state.selected_view;
        let Some(view) = self.views.get(&slot) else {
            return;
        };
        let key = note_key(&view.metadata);
        let artifact = text(&view.metadata, "artifact_id").to_owned();
        let sha = text(&view.metadata, "sha256").to_owned();
        let records = self
            .annotation_records
            .get(&artifact)
            .cloned()
            .unwrap_or_default();
        let local_id = self
            .state
            .notes
            .get(&key)
            .filter(|n| !n.local_id.is_empty())
            .map(|n| n.local_id.clone())
            .unwrap_or_else(uid);
        let selected = selection(view, &local_id);
        let note = self
            .state
            .notes
            .entry(key.clone())
            .or_insert_with(|| ui_state::NoteDraft {
                local_id: local_id.clone(),
                selection: selected.clone(),
                ..Default::default()
            });
        if note.local_id.is_empty() {
            note.local_id = local_id;
        }
        let mut draft = note.clone();
        if artifact.is_empty() {
            ui.weak("Local structure: notes are saved in this desktop session.");
        }
        ui.horizontal(|ui| {
            if ui.button("New note").clicked() {
                self.preserve_note(&key);
                draft = ui_state::NoteDraft {
                    local_id: uid(),
                    ..Default::default()
                };
                draft.selection = selected.clone();
                draft.selection["local_id"] = json!(draft.local_id);
            }
            if ui.button("Attach current selection").clicked() {
                draft.selection = selected.clone();
                draft.selection["local_id"] = json!(draft.local_id);
            }
            if !artifact.is_empty() && ui.small_button("Refresh shared").clicked() {
                self.request(
                    "annotation.list",
                    json!({"artifact_id":artifact}),
                    Purpose::Annotations(artifact.clone()),
                );
            }
        });
        if let Some(native) = draft.selection.get("native_key") {
            if let Ok(key) = serde_json::from_value::<scene::ResidueKey>(native.clone()) {
                ui.small(format!("Attached: {key}"));
            }
        } else if let Some(residue) = draft.selection.get("residue") {
            ui.small(format!(
                "Attached: {} / {} {}",
                text(residue, "chain"),
                residue["resi"],
                text(residue, "resn")
            ));
        } else {
            ui.small("Whole-artifact note");
        }
        ui.add(
            egui::TextEdit::multiline(&mut draft.text)
                .desired_rows(4)
                .desired_width(f32::INFINITY)
                .hint_text("Purpose, interpretation, annotation, or next experiment…"),
        );
        let current = draft.annotation_id.as_ref().and_then(|id| {
            records
                .iter()
                .find(|record| text(record, "annotation_id") == id)
        });
        let conflict = current.is_some_and(|record| record["revision"].as_u64() != draft.revision);
        if conflict {
            ui.colored_label(AMBER,"Shared note changed. Your local edit is retained. Load the shared version below, or create a new note to save a separate interpretation.");
        }
        let operations = self
            .session
            .as_ref()
            .map(|s| s.retryable_operations())
            .unwrap_or_default();
        let uncertain = operations.iter().find(|op| {
            text(op, "id") == draft.operation
                && op.pointer("/error/uncertain") == Some(&Value::Bool(true))
        });
        let saving = self
            .pending
            .values()
            .any(|p| matches!(&p.purpose,Purpose::SaveNote(id,_) if id==&artifact));
        let mut save = false;
        let mut deletion = false;
        ui.horizontal(|ui|{if !artifact.is_empty(){if let Some(op)=uncertain{if ui.add_enabled(!saving,egui::Button::new("Recover exact note save")).clicked(){self.retry(text(op,"id"),Purpose::SaveNote(artifact.clone(),text(&op["params"],"text").into()),"annotation.put".into());}}else if ui.add_enabled(self.connected&&!saving&&!conflict&&!draft.text.trim().is_empty(),egui::Button::new("Save to head")).clicked(){save=true;}
            if ui.add_enabled(self.connected&&!saving&&!conflict&&uncertain.is_none()&&draft.annotation_id.is_some(),egui::Button::new("Delete shared note")).clicked(){deletion=true;save=true;}}
            if ui.button("Export notes…").clicked(){let bytes=serde_json::to_vec_pretty(&json!({"schema":1,"artifact_sha256":sha,"shared_annotations":records,"local_draft":draft,"retained_edits":self.state.extra.get("note_history"),"legacy_annotations":self.state.extra.get("legacy_annotations").and_then(|v|v.get(&sha))})).unwrap_or_default();self.export_bytes(Arc::new(bytes),"annotations.json".into(),ctx);}
        });
        if saving {
            ui.weak("Saving/reconciling shared note…");
        }
        ui.small("Local drafts save automatically. Head updates preserve revision history.");
        if save {
            if !draft.selection.is_object() {
                draft.selection = json!({"kind":"bio-workbench-note-v1","local_id":draft.local_id,"artifact_sha256":sha});
            }
            draft.selection["local_id"] = json!(draft.local_id);
            draft.selection["note"] = json!(draft.text);
            draft.selection["deleted"] = json!(deletion);
            let mut params =
                json!({"artifact_id":artifact,"text":draft.text,"selection":draft.selection});
            if let Some(id) = &draft.annotation_id {
                params["annotation_id"] = json!(id);
                params["expected_revision"] = json!(draft.revision);
            }
            if let Some(operation) = self.request(
                "annotation.put",
                params,
                Purpose::SaveNote(artifact.clone(), draft.text.clone()),
            ) {
                draft.operation = operation;
            }
        }
        self.state.notes.insert(key.clone(), draft);
        for record in records {
            let deleted = record.pointer("/selection/deleted") == Some(&Value::Bool(true));
            ui.separator();
            ui.horizontal_wrapped(|ui| {
                ui.weak(format!(
                    "{} · rev {}{}",
                    text(&record, "author"),
                    record["revision"],
                    if deleted { " · deleted" } else { "" }
                ));
                if ui.small_button("Load").clicked() {
                    self.preserve_note(&key);
                    let selection = record["selection"].clone();
                    let local_id = text(&selection, "local_id").to_owned();
                    self.state.notes.insert(
                        key.clone(),
                        ui_state::NoteDraft {
                            text: text(&record, "text").into(),
                            annotation_id: Some(text(&record, "annotation_id").into()),
                            revision: record["revision"].as_u64(),
                            selection: selection.clone(),
                            local_id: if local_id.is_empty() { uid() } else { local_id },
                            operation: String::new(),
                        },
                    );
                    if let Some(view) = self.views.get_mut(&slot) {
                        view.selected = selection_key(&selection, &view.molecule);
                    }
                }
            });
            if !deleted {
                ui.label(text(&record, "text"));
            }
        }
        if let Some(legacy) = self
            .state
            .extra
            .get("legacy_annotations")
            .and_then(|value| value.get(&sha))
            .cloned()
        {
            egui::CollapsingHeader::new("Imported Electron local annotations").show(ui,|ui|{
                ui.small("Shared records and tombstones are authoritative. Conflicting local copies remain available for inspection/export.");
                for record in rows(&legacy,"annotations") {
                    let id=text(record,"id");let existing=self.annotation_records.get(&artifact).and_then(|records|records.iter().find(|r|r.pointer("/selection/local_id").and_then(Value::as_str)==Some(id))).is_some();
                    let pending=rows(&legacy,"pending_deletions").iter().any(|value|value.as_str()==Some(id));
                    ui.label(text(record,"label"));
                    if existing||pending {ui.colored_label(AMBER,"Existing shared ID or retained deletion: no automatic replay.");}
                    let mut load=false;let mut copy=false;
                    ui.horizontal(|ui|{if !existing&&!pending&&ui.small_button("Load unsynced note").clicked(){load=true;}
if ui.small_button("Inspect local copy").clicked(){self.text_preview=Some(("Retained Electron annotation".into(),serde_json::to_string_pretty(record).unwrap_or_default()));}
if ui.small_button("Copy as new note").clicked(){copy=true;}});
                    if load||copy {self.preserve_note(&key);let local_id=if copy{uid()}else{id.to_owned()};let selection=json!({"kind":"bio-workbench-residue-v1","local_id":local_id,"artifact_sha256":sha,"residue":record["selection"],"label":record["label"],"note":record["note"],"color":record["color"],"deleted":false});self.state.notes.insert(key.clone(),ui_state::NoteDraft{text:text(record,"note").into(),selection,local_id,..Default::default()});}
                }
            });
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_residue_selection_maps_only_exact_existing_identity() {
        let molecule = scene::Molecule::reference();
        let residue = &molecule.residues[0];
        let selection = json!({"residue":{"chain":residue.key.chain,"resi":residue.key.sequence.parse::<i64>().unwrap(),"icode":residue.key.insertion,"resn":residue.key.component}});
        assert_eq!(
            selection_key(&selection, &molecule),
            Some(residue.key.clone())
        );
        let mut invalid = selection;
        invalid["residue"]["icode"] = json!("Z");
        assert!(selection_key(&invalid, &molecule).is_none());
    }
}
