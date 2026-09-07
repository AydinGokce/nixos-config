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
#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct UiState {
    pub schema: u32,
    pub name: String,
    pub mode: String,
    pub msa_backend: String,
    pub execution: String,
    pub editor: Editor,
    pub inputs: Vec<Input>,
    pub models: BTreeSet<String>,
    pub settings: BTreeMap<String, Value>,
    pub active_batch: String,
    pub preview: Option<Preview>,
    pub view_refs: Vec<Value>,
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
            msa_backend: "public".into(),
            execution: "auto".into(),
            editor: Editor::default(),
            inputs: Vec::new(),
            models: BTreeSet::new(),
            settings: BTreeMap::new(),
            active_batch: String::new(),
            preview: None,
            view_refs: Vec::new(),
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
        let mut state = serde_json::from_value::<Self>(value.clone()).unwrap_or_else(|_| {
            let mut state = Self::default();
            state.extra.insert("unparsed_original_draft".into(), value);
            state
        });
        state.view_count = state.view_count.clamp(1, 4);
        state.selected_view = state.selected_view.min(state.view_count - 1);
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
    pub fn payload(&self) -> Result<Value, String> {
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
            text(&input.source, "kind") == "upload" && text(&input.source, "upload_id").is_empty()
        }) {
            return Err("An input file is awaiting upload. Reload any file imported from a different head before previewing.".into());
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
    pub fn preview_matches(&self) -> bool {
        self.preview.as_ref().is_some_and(|preview| {
            self.payload()
                .is_ok_and(|payload| payload == preview.snapshot)
        })
    }
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
