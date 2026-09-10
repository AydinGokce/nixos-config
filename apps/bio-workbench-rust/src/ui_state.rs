use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub fn uid() -> String {
    uuid::Uuid::new_v4().to_string()
}
pub fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}
pub fn rows<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}
pub fn terminal(state: &str) -> bool {
    matches!(
        state,
        "complete" | "partial" | "failed" | "cancelled" | "interrupted" | "validation_failed"
    )
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Editor {
    pub id: String,
    pub name: String,
    pub text: String,
    pub molecule_type: String,
    pub format: String,
    pub kind: String,
    pub chain_id: String,
}
impl Default for Editor {
    fn default() -> Self {
        Self {
            id: uid(),
            name: String::new(),
            text: String::new(),
            molecule_type: "protein".into(),
            format: "sequence".into(),
            kind: "text".into(),
            chain_id: String::new(),
        }
    }
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Input {
    pub id: String,
    pub name: String,
    pub molecule_type: String,
    pub chain_id: String,
    pub source: Value,
    pub local_path: Option<String>,
    pub attachment_paths: BTreeMap<String, String>,
}
impl Input {
    pub fn wire(&self, assembly: bool) -> Value {
        let mut value = json!({"id":self.id,"name":self.name,"molecule_type":self.molecule_type,"source":self.source});
        if assembly {
            value["chain_id"] = json!(self.chain_id);
        }
        value
    }
    pub fn needs_upload(&self) -> bool {
        self.local_path.is_some() && text(&self.source, "upload_id").is_empty()
    }
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct NoteDraft {
    pub text: String,
    pub annotation_id: Option<String>,
    pub revision: Option<u64>,
    pub selection: Value,
    pub local_id: String,
    pub operation: String,
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Preview {
    pub snapshot: Value,
    pub operation: String,
    pub batch_id: String,
    pub selected_pairs: BTreeSet<String>,
    pub create_operation: String,
    pub create_payload: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum RunUploadTarget {
    Input(usize),
    Attachment(usize, String),
    Labels(String),
}
#[derive(Clone, Serialize, Deserialize)]
pub struct RunUpload {
    pub target: RunUploadTarget,
    pub path: String,
    pub operation: String,
    pub complete: bool,
}
/// A Run click fixes its molecular inputs and settings before any asynchronous
/// upload. Only immutable upload receipts may fill the captured request later.
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct RunIntent {
    pub id: String,
    pub endpoint: String,
    pub request: Value,
    pub uploads: Vec<RunUpload>,
    pub operation: String,
    pub batch_id: String,
    pub stage: String,
    pub error: String,
    pub submission_attempted: bool,
}
impl RunIntent {
    pub fn capture(state: &UiState, endpoint: String) -> Result<Self, String> {
        let mut request = state.payload_allow_uploads(true)?;
        request["request_key"] = json!(uid());
        let mut uploads = Vec::new();
        let mut add = |target, path: &str| {
            uploads.push(RunUpload {
                target,
                path: path.into(),
                operation: String::new(),
                complete: false,
            })
        };
        for (index, input) in state.effective_inputs().iter().enumerate() {
            if input.needs_upload() {
                add(
                    RunUploadTarget::Input(index),
                    input.local_path.as_deref().unwrap(),
                );
            }
            for (name, path) in &input.attachment_paths {
                if input.source["attachments"][name]
                    .as_str()
                    .is_none_or(str::is_empty)
                {
                    add(RunUploadTarget::Attachment(index, name.clone()), path);
                }
            }
        }
        if let Some(paths) = state.extra.get("label_paths").and_then(Value::as_object) {
            for (model, path) in paths {
                if state.models.contains(model)
                    && request["settings"][model]["labels_upload_id"]
                        .as_str()
                        .is_none_or(str::is_empty)
                    && let Some(path) = path.as_str()
                {
                    add(RunUploadTarget::Labels(model.clone()), path);
                }
            }
        }
        Ok(Self {
            id: uid(),
            endpoint,
            request,
            uploads,
            stage: "uploading".into(),
            ..Default::default()
        })
    }
    pub fn accepts_new_run(&self) -> bool {
        !self.batch_id.is_empty() || self.stage == "rejected"
    }
    pub fn can_discard_preparation(&self) -> bool {
        !self.submission_attempted && self.operation.is_empty() && !self.error.is_empty()
    }
    pub fn accept_upload_for_draft(
        &mut self,
        index: usize,
        receipt: &Value,
        draft: &mut UiState,
    ) -> Result<(), String> {
        let upload = self
            .uploads
            .get(index)
            .ok_or("Unknown captured upload.")?
            .clone();
        let before = match &upload.target {
            RunUploadTarget::Input(index) | RunUploadTarget::Attachment(index, _) => {
                self.request["inputs"][*index]["source"].clone()
            }
            RunUploadTarget::Labels(model) => self.request["settings"][model].clone(),
        };
        self.accept_upload(index, receipt)?;
        match &upload.target {
            RunUploadTarget::Input(index) | RunUploadTarget::Attachment(index, _) => {
                let captured = &self.request["inputs"][*index];
                if let Some(input) = draft
                    .inputs
                    .iter_mut()
                    .find(|input| input.id == text(captured, "id") && input.source == before)
                {
                    let same_path = match &upload.target {
                        RunUploadTarget::Input(_) => {
                            input.local_path.as_deref() == Some(upload.path.as_str())
                        }
                        RunUploadTarget::Attachment(_, name) => {
                            input.attachment_paths.get(name) == Some(&upload.path)
                        }
                        _ => false,
                    };
                    if same_path {
                        input.source = captured["source"].clone();
                    }
                }
            }
            RunUploadTarget::Labels(model) => {
                if draft.settings.get(model).unwrap_or(&Value::Null) == &before
                    && draft
                        .extra
                        .get("label_paths")
                        .and_then(|paths| paths.get(model))
                        .and_then(Value::as_str)
                        == Some(upload.path.as_str())
                {
                    draft
                        .settings
                        .insert(model.clone(), self.request["settings"][model].clone());
                }
            }
        }
        Ok(())
    }
    pub fn accept_upload(&mut self, index: usize, receipt: &Value) -> Result<(), String> {
        let id = text(receipt, "upload_id");
        if id.is_empty() {
            return Err("Upload did not return a completed receipt.".into());
        }
        let upload = self
            .uploads
            .get_mut(index)
            .ok_or("Unknown captured upload.")?;
        match &upload.target {
            RunUploadTarget::Input(index) => {
                self.request["inputs"][*index]["source"]["upload_id"] = json!(id)
            }
            RunUploadTarget::Attachment(index, name) => {
                let source = &mut self.request["inputs"][*index]["source"];
                if !source["attachments"].is_object() {
                    source["attachments"] = json!({});
                }
                source["attachments"][name] = json!(id);
            }
            RunUploadTarget::Labels(model) => {
                if !self.request["settings"][model].is_object() {
                    self.request["settings"][model] = json!({});
                }
                self.request["settings"][model]["labels_upload_id"] = json!(id);
            }
        }
        upload.complete = true;
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct UiState {
    pub schema: u32,
    pub name: String,
    pub mode: String,
    pub msa_backend: String,
    pub msa_default_version: u32,
    pub execution: String,
    pub editor: Editor,
    pub inputs: Vec<Input>,
    pub models: BTreeSet<String>,
    pub settings: BTreeMap<String, Value>,
    pub active_batch: String,
    pub preview: Option<Preview>,
    pub run: Option<RunIntent>,
    pub view_refs: Vec<Value>,
    pub dock_layout: Value,
    pub view_count: usize,
    pub selected_view: usize,
    pub link_views: bool,
    pub show_axes: bool,
    pub notes: BTreeMap<String, NoteDraft>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}
impl Default for UiState {
    fn default() -> Self {
        Self {
            schema: 1,
            name: String::new(),
            mode: "batch".into(),
            msa_backend: "private".into(),
            msa_default_version: 1,
            execution: "auto".into(),
            editor: Editor::default(),
            inputs: Vec::new(),
            models: BTreeSet::new(),
            settings: BTreeMap::new(),
            active_batch: String::new(),
            preview: None,
            run: None,
            view_refs: Vec::new(),
            dock_layout: Value::Null,
            view_count: 2,
            selected_view: 0,
            link_views: true,
            show_axes: true,
            notes: BTreeMap::new(),
            extra: BTreeMap::new(),
        }
    }
}
impl UiState {
    pub fn restore(value: &Value) -> Self {
        let mut value = value.clone();
        // One upgrade switches the former public default, including existing
        // drafts. Subsequent explicit public choices retain this version marker.
        if value
            .get("msa_default_version")
            .and_then(Value::as_u64)
            .unwrap_or(0)
            < 1
            && value.is_object()
        {
            value["msa_backend"] = json!("private");
            value["msa_default_version"] = json!(1);
        }
        // Retain both editors from Electron, including inactive drafts.
        if value.pointer("/editor/paste").is_some() {
            let original = value["editor"].clone();
            for (source, kind) in [("paste", "text"), ("library", "library")] {
                let mut editor = original[source].clone();
                editor["kind"] = json!(kind);
                value["saved_editors"][kind] = editor;
            }
            let tab = text(&original, "tab");
            value["editor"] = if tab == "upload" {
                serde_json::to_value(Editor::default()).unwrap()
            } else {
                value["saved_editors"][if tab == "library" { "library" } else { "text" }].clone()
            };
        }
        let selected_view = value.get("selected_view").and_then(view_id);
        if let Some(object) = value.as_object_mut() {
            // A damaged view selector must not discard input drafts and notes.
            object.insert("selected_view".into(), json!(0));
            if object.get("view_count").is_some_and(|count| {
                count
                    .as_u64()
                    .and_then(|n| usize::try_from(n).ok())
                    .is_none()
            }) {
                object.insert("view_count".into(), json!(2));
            }
        }
        let mut state = serde_json::from_value::<Self>(value.clone()).unwrap_or_else(|_| {
            let mut state = Self::default();
            state.extra.insert("unparsed_original_draft".into(), value);
            state
        });
        let ids = normalize_view_ids(&mut state.view_refs);
        state.selected_view = selected_view
            .filter(|id| ids.contains(id))
            .or_else(|| {
                state
                    .view_refs
                    .first()
                    .and_then(|view| view.get("slot"))
                    .and_then(view_id)
            })
            .unwrap_or(0);
        state
    }
    pub fn switch_editor(&mut self, kind: &str) {
        if self.editor.kind == kind {
            return;
        }
        let saved = self
            .extra
            .entry("saved_editors".into())
            .or_insert_with(|| json!({}));
        saved[&self.editor.kind] = serde_json::to_value(&self.editor).unwrap_or(Value::Null);
        self.editor = serde_json::from_value(saved[kind].clone()).unwrap_or_else(|_| Editor {
            kind: kind.into(),
            ..Default::default()
        });
    }
    pub fn next_chain(&self) -> String {
        for i in 0.. {
            let chain = if i < 26 {
                ((b'A' + i as u8) as char).to_string()
            } else {
                format!("C{}", i + 1)
            };
            if self.inputs.iter().all(|input| input.chain_id != chain) {
                return chain;
            }
        }
        unreachable!()
    }
    /// References have meaning only on their original head. Preserve detached
    /// drafts locally, but require an explicit selection from the new library.
    pub fn detach_library_sources(&mut self, endpoint: &str) -> usize {
        let mut detached = Vec::new();
        self.inputs.retain(|input| {
            if text(&input.source, "kind") == "library" {
                detached.push(json!({"endpoint":endpoint,"input":input}));
                false
            } else {
                true
            }
        });
        if self.editor.kind == "library" && !self.editor.text.trim().is_empty() {
            detached.push(json!({"endpoint":endpoint,"editor":self.editor}));
            self.editor = Editor {
                kind: "library".into(),
                ..Default::default()
            };
        }
        if let Some(saved) = self
            .extra
            .get_mut("saved_editors")
            .and_then(Value::as_object_mut)
            && let Some(editor) = saved.remove("library")
            && !text(&editor, "text").trim().is_empty()
        {
            detached.push(json!({"endpoint":endpoint,"editor":editor}));
        }
        let count = detached.len();
        if count > 0 {
            let archive = self
                .extra
                .entry("detached_library_sources".into())
                .or_insert_with(|| json!([]));
            if !archive.is_array() {
                *archive = json!([archive.clone()]);
            }
            archive.as_array_mut().unwrap().extend(detached);
        }
        count
    }
    pub fn active_input(&self) -> Option<Input> {
        if self.editor.text.trim().is_empty() {
            return None;
        }
        let format =
            if self.editor.format == "sequence" && self.editor.text.trim_start().starts_with('>') {
                "fasta"
            } else {
                &self.editor.format
            };
        let source = if self.editor.kind == "library" {
            json!({"kind":"library","ref":self.editor.text.trim()})
        } else {
            json!({"kind":"text","format":format,"text":self.editor.text})
        };
        Some(Input {
            id: self.editor.id.clone(),
            name: if self.editor.name.trim().is_empty() {
                format!("Input {}", self.inputs.len() + 1)
            } else {
                self.editor.name.clone()
            },
            molecule_type: self.editor.molecule_type.clone(),
            chain_id: if self.editor.chain_id.trim().is_empty() {
                self.next_chain()
            } else {
                self.editor.chain_id.clone()
            },
            source,
            ..Default::default()
        })
    }
    pub fn effective_inputs(&self) -> Vec<Input> {
        let mut inputs = self.inputs.clone();
        if let Some(input) = self.active_input() {
            inputs.push(input);
        }
        inputs
    }
    pub fn add_active(&mut self) {
        if let Some(input) = self.active_input() {
            self.inputs.push(input);
            self.editor = Editor {
                molecule_type: self.editor.molecule_type.clone(),
                format: self.editor.format.clone(),
                kind: self.editor.kind.clone(),
                ..Default::default()
            };
        }
    }
    #[cfg(test)]
    pub fn payload(&self) -> Result<Value, String> {
        self.payload_allow_uploads(false)
    }
    fn payload_allow_uploads(&self, allow_uploads: bool) -> Result<Value, String> {
        let inputs = self.effective_inputs();
        if inputs.is_empty() {
            return Err("Add a sequence, file, or library reference.".into());
        }
        if inputs.len() > 128 {
            return Err("At most 128 declared inputs are supported.".into());
        }
        if self.models.is_empty() {
            return Err("Select at least one model from the head catalog.".into());
        }
        if inputs.iter().any(|input| {
            text(&input.source, "kind") == "upload"
                && text(&input.source, "upload_id").is_empty()
                && !(allow_uploads && input.local_path.is_some())
        }) {
            return Err("An input file is awaiting upload. Reload any file imported from a different head before running.".into());
        }
        if self.mode == "assembly" {
            let chains: BTreeSet<_> = inputs.iter().map(|input| &input.chain_id).collect();
            if chains.len() != inputs.len() || chains.contains(&String::new()) {
                return Err("Assembly chain IDs must be unique and nonempty.".into());
            }
        }
        let settings: BTreeMap<_, _> = self
            .settings
            .iter()
            .filter(|(model, _)| self.models.contains(*model))
            .collect();
        Ok(
            json!({"name":if self.name.trim().is_empty(){"Molecular run"}else{&self.name},"mode":self.mode,"inputs":inputs.iter().map(|input|input.wire(self.mode=="assembly")).collect::<Vec<_>>(),"models":self.models,"msa_backend":self.msa_backend,"execution":self.execution,"settings":settings}),
        )
    }
    #[cfg(test)]
    pub fn preview_matches(&self) -> bool {
        self.preview.as_ref().is_some_and(|preview| {
            self.payload()
                .is_ok_and(|payload| payload == preview.snapshot)
        })
    }
}

fn view_id(value: &Value) -> Option<usize> {
    value
        .as_u64()
        .and_then(|id| usize::try_from(id).ok())
        .filter(|id| *id < usize::MAX / 2)
}

fn normalize_view_ids(views: &mut [Value]) -> BTreeSet<usize> {
    // Reserve every valid ID before assigning replacements, including IDs that
    // occur later in the saved order. Dock order never determines identity.
    let mut reserved: BTreeSet<_> = views
        .iter()
        .filter_map(|view| view.get("slot").and_then(view_id))
        .collect();
    let mut used = BTreeSet::new();
    let mut next = 0;
    for view in views {
        let id = match view.get("slot").and_then(view_id) {
            Some(id) if used.insert(id) => id,
            _ => {
                while reserved.contains(&next) {
                    next += 1;
                }
                reserved.insert(next);
                used.insert(next);
                next
            }
        };
        if !view.is_object() {
            *view = json!({"unparsed_original_view_ref": std::mem::take(view)});
        }
        view["slot"] = json!(id);
    }
    used
}

pub fn infer_file(path: &std::path::Path) -> (&'static str, &'static str) {
    match path
        .extension()
        .and_then(|v| v.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "pdb" | "ent" => ("pdb", "structure"),
        "cif" | "mmcif" => ("mmcif", "structure"),
        "sdf" | "mol" => ("sdf", "ligand"),
        "smi" | "smiles" => ("smiles", "ligand"),
        "json" => ("library-json", "assembly"),
        "fa" | "fasta" | "faa" | "fna" | "fas" => ("fasta", "protein"),
        _ => ("sequence", "protein"),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn private_msa_upgrade_runs_once_and_retains_later_explicit_public_choice() {
        assert_eq!(UiState::default().msa_backend, "private");
        let mut upgraded = UiState::restore(
            &json!({"msa_backend":"public","name":"Existing draft","editor":{"text":"MAG"}}),
        );
        assert_eq!(upgraded.msa_backend, "private");
        assert_eq!(upgraded.editor.text, "MAG");
        assert_eq!(upgraded.name, "Existing draft");
        upgraded.msa_backend = "public".into();
        let restored = UiState::restore(&serde_json::to_value(upgraded).unwrap());
        assert_eq!(restored.msa_backend, "public");
        assert_eq!(restored.msa_default_version, 1);
    }
    #[test]
    fn captured_run_survives_changed_draft_and_receipts_only_fill_saved_slots() {
        let mut state = UiState::default();
        state.models.insert("rf3".into());
        state.editor.text = "MAG\n".into();
        state.inputs.push(Input {
            id: "file".into(),
            name: "Original".into(),
            molecule_type: "protein".into(),
            source: json!({"kind":"upload","format":"fasta","upload_id":""}),
            local_path: Some("/original.fasta".into()),
            attachment_paths: BTreeMap::from([("ligand.sdf".into(), "/ligand.sdf".into())]),
            ..Default::default()
        });
        state
            .extra
            .insert("label_paths".into(), json!({"rf3":"/labels.json"}));
        let mut run = RunIntent::capture(&state, "head-one".into()).unwrap();
        let mut expected = run.request.clone();
        assert_eq!(run.uploads.len(), 3);
        assert!(!run.accepts_new_run());
        state.inputs.clear();
        state.editor.text = "CHANGED".into();
        state.models.clear();
        state.msa_backend = "public".into();
        for index in 0..3 {
            run.accept_upload(index, &json!({"upload_id":format!("upload-{index}")}))
                .unwrap();
        }
        expected["inputs"][0]["source"]["upload_id"] = json!("upload-0");
        expected["inputs"][0]["source"]["attachments"] = json!({"ligand.sdf":"upload-1"});
        expected["settings"]["rf3"] = json!({"labels_upload_id":"upload-2"});
        assert_eq!(run.request, expected);
        assert_eq!(run.request["inputs"][1]["source"]["text"], "MAG\n");
        assert_eq!(run.request["msa_backend"], "private");
        assert!(run.uploads.iter().all(|upload| upload.complete));
        run.operation = "durable-operation".into();
        state.run = Some(run.clone());
        let restored = UiState::restore(&serde_json::to_value(state).unwrap());
        assert_eq!(restored.run.unwrap().request, expected);
        run.batch_id = "accepted-batch".into();
        assert!(run.accepts_new_run());
    }
    #[test]
    fn every_deliberate_run_has_a_new_key_but_incomplete_upload_cannot_be_ready() {
        let mut state = UiState::default();
        state.models.insert("rf3".into());
        state.editor.text = "MAG".into();
        let first = RunIntent::capture(&state, "head".into()).unwrap();
        let second = RunIntent::capture(&state, "head".into()).unwrap();
        assert_ne!(first.request["request_key"], second.request["request_key"]);
        assert_ne!(first.id, second.id);
        let mut run = first;
        assert!(run.accept_upload(0, &json!({"state":"uploading"})).is_err());
        run.stage = "uncertain".into();
        assert!(!run.accepts_new_run());
        run.stage = "rejected".into();
        assert!(run.accepts_new_run());
    }
    #[test]
    fn interrupted_submission_before_ui_operation_id_cannot_be_discarded() {
        let mut run = RunIntent {
            stage: "upload_failed".into(),
            error: "File unavailable".into(),
            ..Default::default()
        };
        assert!(run.can_discard_preparation());
        run.submission_attempted = true;
        let restored: RunIntent =
            serde_json::from_value(serde_json::to_value(&run).unwrap()).unwrap();
        assert!(restored.operation.is_empty());
        assert!(!restored.can_discard_preparation());
        assert!(!restored.accepts_new_run());
    }
    #[test]
    fn upload_receipts_reuse_unchanged_draft_but_never_overwrite_changed_sources_or_paths() {
        let mut state = UiState::default();
        state.models.insert("rf3".into());
        state.inputs.push(Input {
            id: "file".into(),
            source: json!({"kind":"upload","format":"fasta","upload_id":""}),
            local_path: Some("/original.fasta".into()),
            ..Default::default()
        });
        state
            .extra
            .insert("label_paths".into(), json!({"rf3":"/labels.json"}));
        let original = state.clone();
        let mut run = RunIntent::capture(&state, "head".into()).unwrap();
        run.accept_upload_for_draft(0, &json!({"upload_id":"file-receipt"}), &mut state)
            .unwrap();
        run.accept_upload_for_draft(1, &json!({"upload_id":"label-receipt"}), &mut state)
            .unwrap();
        assert_eq!(state.inputs[0].source["upload_id"], "file-receipt");
        assert_eq!(state.settings["rf3"]["labels_upload_id"], "label-receipt");
        assert!(
            RunIntent::capture(&state, "head".into())
                .unwrap()
                .uploads
                .is_empty()
        );
        for change_path in [false, true] {
            let mut changed = original.clone();
            let mut run = RunIntent::capture(&changed, "head".into()).unwrap();
            if change_path {
                changed.inputs[0].local_path = Some("/replacement.fasta".into());
            } else {
                changed.inputs[0].source["format"] = json!("sequence");
            }
            changed.settings.insert("rf3".into(), json!({"seed":7}));
            let expected = serde_json::to_value(&changed).unwrap();
            run.accept_upload_for_draft(0, &json!({"upload_id":"original-file"}), &mut changed)
                .unwrap();
            run.accept_upload_for_draft(1, &json!({"upload_id":"original-labels"}), &mut changed)
                .unwrap();
            assert_eq!(serde_json::to_value(&changed).unwrap(), expected);
            assert_eq!(
                run.request["inputs"][0]["source"]["upload_id"],
                "original-file"
            );
        }
    }
    #[test]
    fn legacy_validation_is_retained_without_an_automatic_run_intent() {
        let state = UiState::restore(
            &json!({"preview":{"batch_id":"legacy-validated","operation":"old-validate","snapshot":{"msa_backend":"public"}}}),
        );
        assert!(state.run.is_none());
        assert_eq!(state.preview.unwrap().batch_id, "legacy-validated");
    }
    #[test]
    fn changing_heads_detaches_equal_named_refs_but_preserves_molecular_text_and_archive() {
        let mut state = UiState::default();
        state.inputs.push(Input {
            id: "library".into(),
            name: "Stored protein".into(),
            source: json!({"kind":"library","ref":"construct:same-id@1"}),
            ..Default::default()
        });
        state.inputs.push(Input {
            id: "pasted".into(),
            name: "Pasted sequence".into(),
            source: json!({"kind":"text","text":"ACDE","format":"sequence"}),
            ..Default::default()
        });
        state.editor = Editor {
            kind: "library".into(),
            text: "construct:same-id@1".into(),
            ..Default::default()
        };
        state.extra.insert("saved_editors".into(), json!({"library":{"kind":"library","text":"assembly:same-id@1"},"text":{"kind":"text","text":"ACGT"}}));
        assert_eq!(
            state.detach_library_sources("harrison@root:first-head:22"),
            3
        );
        assert_eq!(state.inputs.len(), 1);
        assert_eq!(state.inputs[0].id, "pasted");
        assert_eq!(state.inputs[0].source["text"], "ACDE");
        assert!(state.active_input().is_none());
        assert!(state.extra["saved_editors"].get("library").is_none());
        assert_eq!(state.extra["saved_editors"]["text"]["text"], "ACGT");
        let archive = state.extra["detached_library_sources"].as_array().unwrap();
        assert_eq!(archive[0]["input"]["source"]["ref"], "construct:same-id@1");
        assert!(
            archive
                .iter()
                .all(|item| item["endpoint"] == "harrison@root:first-head:22")
        );
        assert_eq!(
            state.detach_library_sources("harrison@root:second-head:22"),
            0
        );
    }
    #[test]
    fn preview_includes_uncommitted_paste_and_preserves_original_bytes() {
        let mut state = UiState::default();
        state.editor.text = " >original\nM A G\n".into();
        state.models.insert("boltz2".into());
        let value = state.payload().unwrap();
        assert_eq!(value["inputs"][0]["source"]["format"], "fasta");
        assert_eq!(value["inputs"][0]["source"]["text"], " >original\nM A G\n");
        assert_eq!(state.inputs.len(), 0);
        state.add_active();
        assert_eq!(state.payload().unwrap(), value);
    }
    #[test]
    fn draft_changes_invalidate_compatibility_commit() {
        let mut state = UiState::default();
        state.editor.text = "MAG".into();
        state.models.insert("rf3".into());
        state.preview = Some(Preview {
            snapshot: state.payload().unwrap(),
            ..Default::default()
        });
        assert!(state.preview_matches());
        state.editor.text.push('K');
        assert!(!state.preview_matches());
    }
    #[test]
    fn switching_editor_preserves_both_original_drafts() {
        let mut state = UiState::default();
        state.editor.text = " M A G ".into();
        let id = state.editor.id.clone();
        state.switch_editor("library");
        state.editor.text = "construct:editor@7".into();
        state.switch_editor("text");
        assert_eq!(state.editor.text, " M A G ");
        assert_eq!(state.editor.id, id);
        state.switch_editor("library");
        assert_eq!(state.editor.text, "construct:editor@7");
    }
    #[test]
    fn migration_retains_inactive_editor_without_submitting_it() {
        let old = json!({"editor":{"tab":"upload","paste":{"text":"MAGA"},"library":{"text":"construct:editor@7"}},"legacy_annotations":{"sha":{"annotations":[]}}});
        let mut state = UiState::restore(&old);
        assert!(state.active_input().is_none());
        state.switch_editor("library");
        assert_eq!(state.editor.text, "construct:editor@7");
        assert!(state.extra.contains_key("legacy_annotations"));
    }
    #[test]
    fn docking_migration_retains_hidden_legacy_views_and_selection() {
        let original = json!({
            "view_count":2,
            "selected_view":3,
            "view_refs":[
                {"slot":0,"artifact_id":"same-artifact","view_state":{"style":"cartoon","camera":{"yaw":0.1}}},
                {"slot":1,"artifact_id":"same-artifact","view_state":{"style":"sticks","camera":{"yaw":0.9}}},
                {"slot":2,"source_kind":"local","local_path":"/tmp/editor.cif","sha256":"editor-sha"},
                {"slot":3,"source_kind":"demo","view_state":{"selected":{"chain":"A","sequence":"20"}}}
            ],
            "notes":{"same-artifact":{"text":"Keep both independent views","operation":"pending-save"}}
        });
        let state = UiState::restore(&original);
        assert_eq!(state.view_count, 2);
        assert_eq!(state.selected_view, 3);
        assert_eq!(json!(state.view_refs), original["view_refs"]);
        assert_eq!(state.notes["same-artifact"].operation, "pending-save");
        assert!(state.dock_layout.is_null());
    }
    #[test]
    fn docking_migration_repairs_ids_without_losing_view_metadata_or_drafts() {
        let large = usize::MAX / 2 - 1;
        let original = json!({
            "name":"Editor design",
            "editor":{"text":"  MAGK\n","id":"draft-sequence"},
            "selected_view":u64::MAX,
            "view_refs":[
                {"slot":-1,"artifact_id":"negative","view_state":{"style":"sticks"}},
                {"slot":1,"artifact_id":"first-one","sha256":"first-sha"},
                {"slot":1,"artifact_id":"second-one","sha256":"second-sha"},
                {"slot":"2","artifact_id":"string-id"},
                {"slot":null,"artifact_id":"null-id"},
                {"slot":u64::MAX,"artifact_id":"overflow-id"},
                {"artifact_id":"missing-id"},
                {"slot":large,"artifact_id":"large-valid-id"},
                {"slot":0,"artifact_id":"valid-zero-later"},
                "retained malformed reference"
            ],
            "notes":{"first-one":{"text":"Pending local edit","local_id":"keep-note","operation":"exact-save"}},
            "note_history":[{"text":"Earlier edit"}]
        });
        let state = UiState::restore(&original);
        assert_eq!(state.name, "Editor design");
        assert_eq!(state.editor.text, "  MAGK\n");
        assert_eq!(state.editor.id, "draft-sequence");
        assert_eq!(state.view_refs.len(), 10);
        let ids: BTreeSet<_> = state
            .view_refs
            .iter()
            .map(|view| view_id(&view["slot"]).unwrap())
            .collect();
        assert_eq!(ids.len(), state.view_refs.len());
        assert!(ids.contains(&state.selected_view));
        assert_eq!(state.view_refs[1]["slot"], 1);
        assert_eq!(state.view_refs[7]["slot"], large);
        assert_eq!(state.view_refs[8]["slot"], 0);
        for (before, after) in original["view_refs"]
            .as_array()
            .unwrap()
            .iter()
            .zip(&state.view_refs)
        {
            if let Some(object) = before.as_object() {
                for (key, value) in object.iter().filter(|(key, _)| key.as_str() != "slot") {
                    assert_eq!(&after[key], value);
                }
            } else {
                assert_eq!(&after["unparsed_original_view_ref"], before);
            }
        }
        assert_eq!(state.notes["first-one"].text, "Pending local edit");
        assert_eq!(state.notes["first-one"].operation, "exact-save");
        assert_eq!(state.extra["note_history"], original["note_history"]);
        let again = UiState::restore(&json!(state));
        assert_eq!(again.view_refs, state.view_refs);
        assert_eq!(again.selected_view, state.selected_view);
    }
    #[test]
    fn docking_migration_keeps_sparse_ids_and_has_no_four_view_cap() {
        let large = usize::MAX / 2 - 1;
        let state = UiState::restore(&json!({
            "view_count":8,"selected_view":large,
            "view_refs":[{"slot":0},{"slot":large}]
        }));
        assert_eq!(state.view_count, 8);
        assert_eq!(state.view_refs.len(), 2);
        assert_eq!(state.selected_view, large);
    }
    #[test]
    fn malformed_dock_layout_does_not_reset_valid_session_state() {
        for layout in [
            json!("broken layout"),
            json!([null, false]),
            json!({"tree":{"tabs":"wrong type"}}),
        ] {
            let state = UiState::restore(&json!({
                "dock_layout":layout,
                "editor":{"text":"Preserve unsent input"},
                "inputs":[{"id":"existing-input","name":"Editor"}],
                "notes":{"artifact":{"text":"Keep annotation"}},
                "view_refs":[{"slot":4,"artifact_id":"artifact"}],
                "selected_view":-9
            }));
            assert_eq!(state.dock_layout, layout);
            assert_eq!(state.editor.text, "Preserve unsent input");
            assert_eq!(state.inputs[0].id, "existing-input");
            assert_eq!(state.notes["artifact"].text, "Keep annotation");
            assert_eq!(state.selected_view, 4);
            assert!(!state.extra.contains_key("unparsed_original_draft"));
        }
    }
    #[test]
    fn never_initialized_and_intentionally_empty_docks_stay_distinct() {
        let fresh = UiState::restore(&json!({"view_refs":[]}));
        assert!(fresh.dock_layout.is_null());
        assert_eq!(fresh.selected_view, 0);
        let empty_layout = json!({"schema":1,"groups":[]});
        let closed = UiState::restore(&json!({
            "view_refs":[],"dock_layout":empty_layout,"selected_view":14,
            "notes":{"local:sha":{"text":"Keep notes after closing every tab"}}
        }));
        assert_eq!(closed.dock_layout, empty_layout);
        assert_eq!(closed.selected_view, 0);
        assert!(closed.view_refs.is_empty());
        let restored = UiState::restore(&json!(closed));
        assert_eq!(restored.dock_layout, empty_layout);
        assert!(restored.view_refs.is_empty());
        assert_eq!(
            restored.notes["local:sha"].text,
            "Keep notes after closing every tab"
        );
    }
    #[test]
    fn duplicate_assembly_chains_are_rejected_before_submission() {
        let mut state = UiState {
            mode: "assembly".into(),
            ..Default::default()
        };
        state.models.insert("protenix".into());
        state.editor.text = "MAGK".into();
        state.add_active();
        state.editor.text = "ATGC".into();
        state.editor.chain_id = "A".into();
        assert!(state.payload().unwrap_err().contains("unique"));
    }
}
