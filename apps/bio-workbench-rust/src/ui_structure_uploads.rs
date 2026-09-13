//! Optional structure files staged for one library form, separate from run inputs.
use super::*;

pub(super) struct File {
    pub id: String,
    pub path: PathBuf,
    pub operation: String,
    pub receipt: Value,
    pub done: u64,
    pub size: u64,
    pub error: String,
}

pub(super) struct Files {
    pub token: String,
    pub rows: Vec<File>,
    pub error: String,
}

impl Default for Files {
    fn default() -> Self {
        Self {
            token: uid(),
            rows: Vec::new(),
            error: String::new(),
        }
    }
}

impl Files {
    pub fn add(&mut self, paths: Vec<PathBuf>) {
        self.error.clear();
        for path in paths {
            let path = std::fs::canonicalize(&path).unwrap_or(path);
            if self.rows.iter().any(|f| f.path == path) {
                continue;
            }
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            let format = path
                .extension()
                .unwrap_or_default()
                .to_string_lossy()
                .to_lowercase();
            let size = std::fs::metadata(&path)
                .ok()
                .filter(|m| m.is_file())
                .map(|m| m.len())
                .unwrap_or(0);
            if !matches!(format.as_str(), "pdb" | "cif" | "mmcif")
                || size == 0
                || size > 32 * 1024 * 1024
            {
                self.error = format!("{name}: choose a nonempty PDB or mmCIF file up to 32 MiB.");
                continue;
            }
            if self.rows.len() >= 16 {
                self.error = "Add at most 16 structure files at a time.".into();
                break;
            }
            self.rows.push(File {
                id: uid(),
                path,
                operation: String::new(),
                receipt: Value::Null,
                done: 0,
                size,
                error: String::new(),
            });
        }
    }
    pub fn ready(&self) -> bool {
        self.rows
            .iter()
            .all(|f| !text(&f.receipt, "upload_id").is_empty() && f.error.is_empty())
    }
    pub fn descriptors(&self) -> Value {
        json!(self.rows.iter().map(|f| json!({
            "source":{"kind":"upload","id":f.receipt["upload_id"],"sha256":f.receipt["sha256"]},
            "label":f.path.file_name().unwrap_or_default().to_string_lossy()
        })).collect::<Vec<_>>())
    }
    pub fn accept(&mut self, id: &str, receipt: Value) {
        if let Some(file) = self.rows.iter_mut().find(|f| f.id == id) {
            let sha = text(&receipt, "sha256");
            if text(&receipt, "upload_id").is_empty()
                || sha.len() != 64
                || !sha.bytes().all(|b| b.is_ascii_hexdigit())
                || receipt["size"].as_u64() != Some(file.size)
            {
                file.error = "The upload returned an incomplete or changed file receipt.".into();
                return;
            }
            file.receipt = receipt;
            file.done = file.size;
            file.error.clear();
        }
    }
    /// Returns true when the form asks for the native multiple-file picker.
    pub fn show(&mut self, ui: &mut egui::Ui, writing: bool) -> bool {
        let pick = ui
            .add_enabled(
                !writing && self.rows.len() < 16,
                egui::Button::new("Upload PDBs… (optional)"),
            )
            .clicked();
        let mut remove = None;
        for file in &mut self.rows {
            ui.push_id(&file.id, |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.label(file.path.file_name().unwrap_or_default().to_string_lossy());
                    if !file.error.is_empty() {
                        ui.colored_label(RED, &file.error);
                        if ui
                            .add_enabled(!writing, egui::Button::new("Retry").small())
                            .clicked()
                        {
                            file.operation.clear();
                            file.error.clear();
                            file.receipt = Value::Null;
                            file.done = 0;
                        }
                    } else if file.receipt.is_null() {
                        ui.spinner();
                        ui.weak(format!(
                            "{:.1} / {:.1} MiB",
                            file.done as f64 / 1048576.,
                            file.size as f64 / 1048576.
                        ));
                    } else {
                        ui.colored_label(GREEN, "Ready");
                    }
                    if ui
                        .add_enabled(!writing, egui::Button::new("×").small())
                        .on_hover_text("Remove from this form")
                        .clicked()
                    {
                        remove = Some(file.id.clone());
                    }
                });
            });
        }
        if let Some(id) = remove {
            self.rows.retain(|f| f.id != id);
        }
        if !self.error.is_empty() {
            ui.colored_label(RED, &self.error);
        }
        pick
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn receipt_must_match_the_staged_file_before_a_library_write() {
        let mut files = Files::default();
        files.rows.push(File {
            id: "one".into(),
            path: "target.pdb".into(),
            operation: "op".into(),
            receipt: Value::Null,
            done: 0,
            size: 42,
            error: String::new(),
        });
        files.accept(
            "one",
            json!({"upload_id":"upload","sha256":"a".repeat(64),"size":43}),
        );
        assert!(!files.ready());
        files.accept(
            "other",
            json!({"upload_id":"other","sha256":"b".repeat(64),"size":42}),
        );
        assert!(!files.ready());
        files.accept(
            "one",
            json!({"upload_id":"upload","sha256":"a".repeat(64),"size":42}),
        );
        assert!(files.ready());
        assert_eq!(files.descriptors()[0]["source"]["sha256"], "a".repeat(64));
    }
}
