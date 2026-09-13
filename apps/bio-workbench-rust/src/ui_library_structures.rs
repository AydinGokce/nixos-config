//! Revision-bound protein structure cards, uploads, and static studio thumbnails.
use super::*;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Scope {
    endpoint: String,
    reference: String,
    token: String,
}
#[derive(Clone, Debug, PartialEq)]
pub(super) enum Request {
    List(Scope, Option<String>),
    Thumbnail(Scope, String),
    Image(Scope, String, Value),
    Open(Scope, String, Value),
    Links {
        endpoint: String,
        slot: usize,
        sha: String,
        artifact: String,
    },
}
impl Request {
    fn scope(&self) -> Option<&Scope> {
        match self {
            Self::List(s, _)
            | Self::Thumbnail(s, _)
            | Self::Image(s, _, _)
            | Self::Open(s, _, _) => Some(s),
            Self::Links { .. } => None,
        }
    }
}
#[derive(Default)]
struct Thumbnail {
    receipt: Value,
    error: String,
    checked: Option<Instant>,
    texture: Option<egui::TextureHandle>,
}
struct Attach {
    reference: String,
    sha256: String,
    files: ui_structure_uploads::Files,
}
#[derive(Default)]
pub(super) struct Panel {
    scope: Option<Scope>,
    entries: Vec<Value>,
    next_cursor: Option<String>,
    loaded: bool,
    checked: Option<Instant>,
    hidden: bool,
    error: String,
    thumbnails: BTreeMap<String, Thumbnail>,
    attach: Option<Attach>,
    link_requests: BTreeMap<String, Instant>,
    pub(super) pick: Option<String>,
}

fn protein(detail: &Value) -> bool {
    text(&detail["record"]["identity"], "molecule_type") == "protein"
}
fn family(reference: &str) -> &str {
    reference.split('@').next().unwrap_or(reference)
}
fn hex_sha(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}
fn entry_valid(entry: &Value, reference: &str) -> bool {
    !text(entry, "entry_id").is_empty()
        && text(entry, "entry_id").len() <= 512
        && family(text(entry, "source_ref")) == family(reference)
        && hex_sha(text(entry, "sha256"))
        && entry["size"].as_u64().is_some_and(|n| n > 0)
        && matches!(text(entry, "format"), "pdb" | "cif" | "mmcif")
        && text(&entry["protein"], "ref") == text(entry, "source_ref")
        && hex_sha(text(&entry["protein"], "sha256"))
}
fn trash(ui: &mut egui::Ui, enabled: bool) -> egui::Response {
    let response = ui.add_enabled(enabled, egui::Button::new(" ").min_size(Vec2::splat(23.)));
    let r = response.rect.shrink(6.);
    let color = ui.style().interact(&response).fg_stroke.color;
    let s = egui::Stroke::new(1., color);
    ui.painter().rect_stroke(
        egui::Rect::from_min_max(r.min + Vec2::new(1., 2.), r.max),
        1.,
        s,
        egui::StrokeKind::Inside,
    );
    ui.painter().line_segment(
        [r.min + Vec2::new(0., 1.), egui::pos2(r.max.x, r.min.y + 1.)],
        s,
    );
    ui.painter()
        .line_segment([r.min + Vec2::new(3., -1.), r.min + Vec2::new(7., -1.)], s);
    response.on_hover_text("Remove this structure from the protein gallery (Undo restores it)")
}

fn gallery_card<R>(
    ui: &mut egui::Ui,
    width: f32,
    contents: impl FnOnce(&mut egui::Ui) -> R,
) -> egui::InnerResponse<R> {
    egui::Frame::group(ui.style()).show(ui, |ui| {
        ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
            ui.set_width(width - 16.);
            contents(ui)
        })
        .inner
    })
}

impl Workbench {
    fn structure_scope_matches(&self, scope: &Scope) -> bool {
        self.run_endpoint() == scope.endpoint
            && self.library_structures.scope.as_ref() == Some(scope)
            && self.library.selected == scope.reference
    }
    pub(super) fn library_structures_refresh(&mut self, cursor: Option<String>) {
        let Some(scope) = self.library_structures.scope.clone() else {
            return;
        };
        let purpose = Purpose::LibraryStructures(Request::List(scope.clone(), cursor.clone()));
        if self.busy(&purpose) {
            return;
        }
        let mut params = json!({"ref":scope.reference,"include_revisions":true,"include_hidden":self.library_structures.hidden,"limit":24});
        if let Some(cursor) = &cursor {
            params["cursor"] = json!(cursor);
        }
        self.library_structures.checked = Some(Instant::now());
        self.library_structures.error.clear();
        self.request("library.structures", params, purpose);
    }
    pub(super) fn library_structures_received(
        &mut self,
        request: Request,
        value: Value,
        ctx: &egui::Context,
    ) {
        if let Request::Links {
            endpoint,
            slot,
            sha,
            artifact,
        } = &request
        {
            if self.run_endpoint() != *endpoint
                || text(&value, "artifact_id") != artifact
                || text(&value, "sha256") != sha
            {
                return;
            }
            if let Some(view) = self.views.get_mut(slot).filter(|v| {
                text(&v.metadata, "sha256") == sha && text(&v.metadata, "artifact_id") == artifact
            }) {
                view.metadata["library_proteins"] = value["proteins"].clone();
                view.metadata["library_endpoint"] = json!(endpoint);
            }
            return;
        }
        if request
            .scope()
            .is_none_or(|s| !self.structure_scope_matches(s))
        {
            return;
        }
        match request {
            Request::List(scope, cursor) => {
                let entries = rows(&value, "entries");
                if value["schema"] != 1
                    || text(&value, "ref") != scope.reference
                    || !value["entries"].is_array()
                    || entries.len() > 100
                    || !entries.iter().all(|e| entry_valid(e, &scope.reference))
                {
                    self.library_structures.error =
                        "The head returned incomplete or unrelated structure associations.".into();
                    return;
                }
                let first_page = !self.library_structures.loaded;
                if cursor.is_none() && first_page {
                    self.library_structures.entries.clear();
                }
                for entry in entries {
                    if let Some(old) = self
                        .library_structures
                        .entries
                        .iter_mut()
                        .find(|e| e["entry_id"] == entry["entry_id"])
                    {
                        *old = entry.clone();
                    } else if self.library_structures.entries.len() < 240 {
                        self.library_structures.entries.push(entry.clone());
                    }
                }
                self.library_structures.entries.sort_by(|a, b| {
                    text(b, "created_at")
                        .cmp(text(a, "created_at"))
                        .then_with(|| text(b, "entry_id").cmp(text(a, "entry_id")))
                });
                if cursor.is_some() || first_page {
                    self.library_structures.next_cursor =
                        value["next_cursor"].as_str().map(str::to_owned);
                }
                self.library_structures.loaded = true;
                self.library_structures.error.clear();
            }
            Request::Thumbnail(scope, id) => {
                if text(&value, "ref") != scope.reference || text(&value, "entry_id") != id {
                    return;
                }
                let Some(entry) = self
                    .library_structures
                    .entries
                    .iter()
                    .find(|e| text(e, "entry_id") == id)
                else {
                    return;
                };
                if text(&value, "source_sha256") != text(entry, "sha256") {
                    return;
                }
                let thumb = self.library_structures.thumbnails.entry(id).or_default();
                thumb.receipt = value;
                thumb.checked = Some(Instant::now());
                thumb.error.clear();
            }
            Request::Image(_, id, receipt) => {
                let result = (|| -> Result<egui::ColorImage, String> {
                    let path = PathBuf::from(text(&value, "local_path"));
                    let (size, sha) = rpc::file_hash(&path).map_err(|e| e.to_string())?;
                    if Some(size) != receipt["size"].as_u64() || sha != text(&receipt, "sha256") {
                        return Err("Thumbnail cache identity changed.".into());
                    }
                    let mut reader = image::ImageReader::open(path).map_err(|e| e.to_string())?;
                    reader.set_format(image::ImageFormat::Png);
                    let mut limits = image::Limits::default();
                    limits.max_image_width = Some(640);
                    limits.max_image_height = Some(480);
                    limits.max_alloc = Some(4 * 1024 * 1024);
                    reader.limits(limits);
                    let image = reader.decode().map_err(|e| e.to_string())?.into_rgba8();
                    if image.width() != 640 || image.height() != 480 {
                        return Err("Unexpected thumbnail dimensions.".into());
                    }
                    Ok(egui::ColorImage::from_rgba_unmultiplied(
                        [640, 480],
                        image.as_raw(),
                    ))
                })();
                if self
                    .library_structures
                    .thumbnails
                    .values()
                    .filter(|t| t.texture.is_some())
                    .count()
                    >= 16
                    && let Some(key) = self
                        .library_structures
                        .thumbnails
                        .iter()
                        .filter(|(key, t)| *key != &id && t.texture.is_some())
                        .min_by_key(|(_, t)| t.checked)
                        .map(|(key, _)| key.clone())
                {
                    self.library_structures.thumbnails.remove(&key);
                }
                let thumb = self.library_structures.thumbnails.entry(id).or_default();
                match result {
                    Ok(image) => {
                        thumb.texture = Some(ctx.load_texture(
                            text(&receipt, "cache_key"),
                            image,
                            egui::TextureOptions::LINEAR,
                        ));
                        thumb.error.clear();
                    }
                    Err(error) => thumb.error = error,
                }
            }
            Request::Open(scope, id, entry) => {
                let mut metadata = json!({"name":entry["label"],"format":entry["format"],"sha256":entry["sha256"],"size":entry["size"],"source_kind":"local","local_path":value["local_path"],"library_entry_id":id,"library_proteins":[entry["protein"]],"library_endpoint":scope.endpoint,"model":entry["model"],"structure_origin":entry["origin"]});
                if !entry["protein"].is_object() {
                    metadata["library_proteins"] = json!([]);
                }
                let path = PathBuf::from(text(&value, "local_path"));
                self.open_local_tab(path, metadata, ctx);
                self.sidebar_tab = 0;
            }
            Request::Links { .. } => unreachable!(),
        }
    }
    pub(super) fn library_structures_failed(&mut self, purpose: &Purpose, error: &str) {
        if let Purpose::Upload(UploadTarget::LibraryStructure(group, id)) = purpose
            && let Some(files) = self.library_structure_files(group)
            && let Some(file) = files.rows.iter_mut().find(|f| f.id == *id)
        {
            file.error = error.into();
        }
        let Purpose::LibraryStructures(request) = purpose else {
            return;
        };
        if request
            .scope()
            .is_some_and(|s| !self.structure_scope_matches(s))
        {
            return;
        }
        match request {
            Request::Thumbnail(_, id) | Request::Image(_, id, _) => {
                let thumb = self
                    .library_structures
                    .thumbnails
                    .entry(id.clone())
                    .or_default();
                thumb.error = error.into();
                thumb.checked = Some(Instant::now());
            }
            Request::Links { .. } => {}
            _ => self.library_structures.error = error.into(),
        }
    }
    pub(super) fn library_structure_files(
        &mut self,
        token: &str,
    ) -> Option<&mut ui_structure_uploads::Files> {
        if let Some(files) = self.library.sequence.standalone_structure_files()
            && files.token == token
        {
            return Some(files);
        }
        self.library_structures
            .attach
            .as_mut()
            .map(|a| &mut a.files)
            .filter(|f| f.token == token)
    }
    pub(super) fn library_structure_uploads_poll(&mut self) {
        let mut uploads = Vec::new();
        for files in [
            self.library.sequence.standalone_structure_files(),
            self.library_structures
                .attach
                .as_mut()
                .map(|a| &mut a.files),
        ]
        .into_iter()
        .flatten()
        {
            for file in &mut files.rows {
                if file.operation.is_empty() && file.receipt.is_null() && file.error.is_empty() {
                    file.operation = "starting".into();
                    uploads.push((
                        file.path.clone(),
                        UploadTarget::LibraryStructure(files.token.clone(), file.id.clone()),
                    ));
                }
            }
        }
        for (path, target) in uploads {
            self.start_upload(path, target.clone());
            let operation = self
                .pending
                .iter()
                .find(|(_, p)| p.purpose == Purpose::Upload(target.clone()))
                .map(|(id, _)| id.clone());
            if let UploadTarget::LibraryStructure(group, id) = target
                && let Some(files) = self.library_structure_files(&group)
                && let Some(file) = files.rows.iter_mut().find(|f| f.id == id)
            {
                if let Some(operation) = operation {
                    file.operation = operation;
                } else {
                    file.error =
                        "Could not start this upload. Connect to the head and retry.".into();
                }
            }
        }
    }
    pub(super) fn library_structure_upload_progress(
        &mut self,
        target: &UploadTarget,
        done: u64,
        total: u64,
    ) {
        if let UploadTarget::LibraryStructure(group, id) = target
            && let Some(files) = self.library_structure_files(group)
            && let Some(file) = files.rows.iter_mut().find(|f| &f.id == id)
        {
            file.done = done.min(total);
        }
    }
    pub(super) fn library_structures_saved(&mut self, sent: &Value) {
        if sent["structures"].is_array() {
            if self
                .library_structures
                .attach
                .as_ref()
                .is_some_and(|a| a.reference == text(sent, "ref"))
            {
                self.library_structures.attach = None;
            }
            self.library.tab = 5;
        }
        self.library_structures.loaded = false;
        self.library_structures.checked = None;
    }
    pub(super) fn library_structures_panel(&mut self, ui: &mut egui::Ui, detail: &Value) {
        if !protein(detail) {
            return;
        }
        let reference = text(detail, "ref");
        let endpoint = self.run_endpoint();
        if self
            .library_structures
            .scope
            .as_ref()
            .is_none_or(|s| s.reference != reference || s.endpoint != endpoint)
        {
            self.library_structures.scope = Some(Scope {
                endpoint,
                reference: reference.into(),
                token: uid(),
            });
            self.library_structures.entries.clear();
            self.library_structures.thumbnails.clear();
            self.library_structures.loaded = false;
            self.library_structures.checked = None;
            self.library_structures.next_cursor = None;
            self.library_structures.error.clear();
        }
        if self
            .library_structures
            .checked
            .is_none_or(|t| t.elapsed() >= Duration::from_secs(20))
        {
            self.library_structures_refresh(None);
        }
        let writing = self.library_writing();
        let editable = detail["is_latest"] != false && !writing;
        let mut refresh = false;
        ui.horizontal_wrapped(|ui| {
            ui.strong("PROTEIN STRUCTURES");
            if ui
                .add_enabled(editable, egui::Button::new("Upload PDBs…"))
                .clicked()
            {
                self.library_structures.attach = Some(Attach {
                    reference: reference.into(),
                    sha256: text(&detail["record"], "sha256").into(),
                    files: ui_structure_uploads::Files::default(),
                });
            }
            refresh = ui.button("Refresh").clicked();
            refresh |= ui
                .checkbox(&mut self.library_structures.hidden, "Show removed")
                .changed();
        });
        ui.weak("Uploaded structures and prediction outputs linked to this protein. Click a thumbnail to open it.");
        if refresh {
            if let Some(scope) = &mut self.library_structures.scope {
                scope.token = uid();
            }
            self.library_structures.loaded = false;
            self.library_structures.entries.clear();
            self.library_structures.next_cursor = None;
            self.library_structures.thumbnails.clear();
            self.library_structures_refresh(None);
        }
        if !self.library_structures.error.is_empty() {
            ui.colored_label(RED, &self.library_structures.error);
        }
        if !self.library_structures.loaded {
            ui.spinner();
        } else if self.library_structures.entries.is_empty() {
            ui.weak("No associated structures yet.");
        }
        let scope = self.library_structures.scope.clone().unwrap();
        let entries = self.library_structures.entries.clone();
        let columns = ((ui.available_width() / 244.).floor() as usize).clamp(1, 4);
        let width = ((ui.available_width() - ui.spacing().item_spacing.x * (columns - 1) as f32)
            / columns as f32)
            .clamp(150., 300.);
        let mut thumbnails = Vec::new();
        let mut open = None;
        let mut visibility = None;
        egui::Grid::new(("protein-structures", reference))
            .num_columns(columns)
            .spacing([8., 8.])
            .show(ui, |ui| {
                for (index, entry) in entries.iter().enumerate() {
                    let id = text(entry, "entry_id");
                    gallery_card(ui, width, |ui| {
                        ui.horizontal(|ui| {
                            ui.add_sized(
                                [ui.available_width() - 32., 22.],
                                egui::Label::new(RichText::new(text(entry, "label")).strong())
                                    .truncate(),
                            )
                            .on_hover_text(text(entry, "label"));
                            if entry["hidden"] == true {
                                if ui
                                    .add_enabled(editable, egui::Button::new("↶").small())
                                    .on_hover_text("Restore structure")
                                    .clicked()
                                {
                                    visibility = Some((id.to_owned(), false));
                                }
                            } else if trash(ui, editable).clicked() {
                                visibility = Some((id.to_owned(), true));
                            }
                        });
                        let (rect, response) = ui.allocate_exact_size(
                            Vec2::new(width - 16., (width - 16.) * 0.75),
                            egui::Sense::click(),
                        );
                        ui.painter()
                            .rect_filled(rect, 1., Color32::from_rgb(9, 13, 15));
                        if let Some(texture) = self
                            .library_structures
                            .thumbnails
                            .get(id)
                            .and_then(|t| t.texture.as_ref())
                        {
                            ui.painter().image(
                                texture.id(),
                                rect,
                                egui::Rect::from_min_max(egui::Pos2::ZERO, egui::pos2(1., 1.)),
                                Color32::WHITE,
                            );
                        } else {
                            let label = self
                                .library_structures
                                .thumbnails
                                .get(id)
                                .map(|t| {
                                    if !t.error.is_empty() {
                                        "Preview unavailable"
                                    } else {
                                        match text(&t.receipt, "state") {
                                            "failed" => "Preview unavailable",
                                            "unavailable" => "Renderer unavailable",
                                            "rendering" => "Rendering…",
                                            _ => "Preparing preview…",
                                        }
                                    }
                                })
                                .unwrap_or("Preparing preview…");
                            ui.painter().text(
                                rect.center(),
                                egui::Align2::CENTER_CENTER,
                                label,
                                egui::FontId::proportional(12.),
                                Color32::GRAY,
                            );
                        }
                        if response.clicked() {
                            open = Some(entry.clone());
                        }
                        response.on_hover_text("Open this original structure in a new viewer tab");
                        if ui.is_rect_visible(rect) && entry["hidden"] != true {
                            thumbnails.push(id.to_owned());
                        }
                        ui.horizontal_wrapped(|ui| {
                            ui.label(if text(entry, "origin") == "prediction" {
                                text(entry, "model")
                            } else {
                                "Uploaded"
                            });
                            ui.weak(text(entry, "source_ref"));
                        });
                        if text(entry, "sequence_relation") == "historical_library_sequence" {
                            ui.colored_label(AMBER, "Earlier protein sequence");
                        }
                        ui.weak(format!(
                            "{} · {:.2} MiB",
                            text(entry, "format").to_uppercase(),
                            entry["size"].as_u64().unwrap_or(0) as f64 / 1048576.
                        ));
                        if let Some(thumb) = self.library_structures.thumbnails.get(id)
                            && !thumb.error.is_empty()
                        {
                            ui.small(&thumb.error);
                        }
                    });
                    if (index + 1) % columns == 0 {
                        ui.end_row();
                    }
                }
            });
        for id in thumbnails {
            if self
                .pending
                .values()
                .filter(|p| {
                    matches!(
                        p.purpose,
                        Purpose::LibraryStructures(Request::Thumbnail(..) | Request::Image(..))
                    )
                })
                .count()
                >= 2
            {
                break;
            }
            let thumb = self
                .library_structures
                .thumbnails
                .entry(id.clone())
                .or_default();
            if thumb.texture.is_some() {
                continue;
            }
            if text(&thumb.receipt, "state") == "ready" && thumb.error.is_empty() {
                let receipt = thumb.receipt.clone();
                let purpose = Purpose::LibraryStructures(Request::Image(
                    scope.clone(),
                    id.clone(),
                    receipt.clone(),
                ));
                if !self.busy(&purpose)
                    && let Some(session) = self.session.as_mut()
                {
                    match session.library_structure_thumbnail(reference, &id, &receipt) {
                        Ok(operation) => {
                            self.pending.insert(
                                operation,
                                Pending {
                                    purpose,
                                    label: "Load structure thumbnail".into(),
                                    done: 0,
                                    total: receipt["size"].as_u64().unwrap_or(0),
                                },
                            );
                        }
                        Err(error) => {
                            self.library_structures
                                .thumbnails
                                .entry(id)
                                .or_default()
                                .error = error.to_string()
                        }
                    }
                }
            } else if thumb.checked.is_none_or(|t| {
                t.elapsed()
                    >= Duration::from_secs(
                        thumb.receipt["retry_after_seconds"]
                            .as_u64()
                            .unwrap_or(10)
                            .clamp(2, 60),
                    )
            }) {
                thumb.checked = Some(Instant::now());
                self.request(
                    "library.structure_thumbnail",
                    json!({"ref":reference,"entry_id":id}),
                    Purpose::LibraryStructures(Request::Thumbnail(scope.clone(), id)),
                );
            }
        }
        if let Some(entry) = open
            && let Some(session) = self.session.as_mut()
        {
            let id = text(&entry, "entry_id").to_owned();
            match session.library_structure(reference, &id, &entry) {
                Ok(operation) => {
                    self.pending.insert(
                        operation,
                        Pending {
                            purpose: Purpose::LibraryStructures(Request::Open(scope, id, entry)),
                            label: "Open protein structure".into(),
                            done: 0,
                            total: 0,
                        },
                    );
                }
                Err(error) => self.library_structures.error = error.to_string(),
            }
        }
        if let Some((id, hidden)) = visibility {
            self.library_write("library.structure_visibility",json!({"ref":reference,"expected_sha256":detail["record"]["sha256"],"entry_id":id,"hidden":hidden}));
        }
        if let Some(cursor) = self.library_structures.next_cursor.clone()
            && self.library_structures.entries.len() < 240
            && ui.button("Load more structures").clicked()
        {
            self.library_structures_refresh(Some(cursor));
        }
    }
    pub(super) fn library_structure_dialog(&mut self, ctx: &egui::Context) {
        let writing = self.library_writing();
        let mut close = false;
        let mut save = None;
        let mut pick = self.library_structures.pick.take();
        if let Some(attach) = &mut self.library_structures.attach {
            egui::Window::new("Add protein structures").resizable(true).default_width(600.).show(ctx,|ui| {
                ui.label(&attach.reference);
                if attach.files.show(ui,writing){pick=Some(attach.files.token.clone());}
                ui.horizontal(|ui| {
                    if ui.add_enabled(!writing && !attach.files.rows.is_empty() && attach.files.ready(),egui::Button::new("Add structures")).clicked(){save=Some(json!({"ref":attach.reference,"expected_sha256":attach.sha256,"structures":attach.files.descriptors()}));}
                    close=ui.add_enabled(!writing,egui::Button::new("Cancel")).clicked();
                    if writing{ui.spinner();}
                });
                if !self.library.write_error.is_empty(){ui.colored_label(RED,&self.library.write_error);}
            });
        }
        if close {
            self.library_structures.attach = None;
        }
        if let Some(token) = pick {
            self.choose_files(Pick::LibraryStructures(token), ctx);
        }
        if let Some(params) = save {
            self.library_write("library.structure_attach", params);
        }
    }
    pub(super) fn structure_links_panel(&mut self, ui: &mut egui::Ui) {
        let slot = self.state.selected_view;
        let endpoint = self.run_endpoint();
        let mut request = None;
        let mut navigate = None;
        let Some(view) = self.views.get(&slot) else {
            return;
        };
        let artifact = text(&view.metadata, "artifact_id");
        let sha = text(&view.metadata, "sha256");
        if !artifact.is_empty()
            && self.artifact_metadata.contains_key(artifact)
            && !view.metadata["library_proteins"].is_array()
        {
            let key = format!("{endpoint}:{slot}:{sha}:{artifact}");
            if self
                .library_structures
                .link_requests
                .get(&key)
                .is_none_or(|t| t.elapsed() >= Duration::from_secs(10))
            {
                self.library_structures
                    .link_requests
                    .insert(key, Instant::now());
                request = Some(Request::Links {
                    endpoint: endpoint.clone(),
                    slot,
                    sha: sha.into(),
                    artifact: artifact.into(),
                });
            }
        }
        let enabled = text(&view.metadata, "library_endpoint") == endpoint && self.connected;
        for protein in rows(&view.metadata, "library_proteins") {
            ui.horizontal_wrapped(|ui| {
                ui.weak("Protein");
                if ui
                    .add_enabled(enabled, egui::Button::new(text(protein, "ref")).small())
                    .clicked()
                {
                    navigate = Some(text(protein, "ref").to_owned());
                }
            });
            let parent = &protein["parent"];
            if !text(parent, "ref").is_empty() {
                ui.horizontal_wrapped(|ui| {
                    ui.weak(if text(parent, "molecular_form") == "plasmid" {
                        "Plasmid"
                    } else {
                        "Encoded by"
                    });
                    if ui
                        .add_enabled(enabled, egui::Button::new(text(parent, "ref")).small())
                        .clicked()
                    {
                        navigate = Some(text(parent, "ref").to_owned());
                    }
                });
            }
        }
        if let Some(request) = request {
            self.request(
                "library.structure_links",
                json!({"artifact_id":artifact}),
                Purpose::LibraryStructures(request),
            );
        }
        if let Some(reference) = navigate {
            self.sidebar_tab = 2;
            self.library_select(&reference);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cards_keep_header_image_and_metadata_vertical_inside_a_horizontal_grid() {
        let ctx = egui::Context::default();
        let mut cards = Vec::new();
        for _ in 0..3 {
            cards.clear();
            let _ = ctx.run(
                egui::RawInput {
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        Vec2::new(900., 500.),
                    )),
                    ..Default::default()
                },
                |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| {
                        egui::Grid::new("cards").num_columns(3).show(ui, |ui| {
                            for _ in 0..3 {
                                cards.push(gallery_card(ui, 240., |ui| {
                                    let header = ui.label("Protein structure").rect;
                                    let image = ui
                                        .allocate_exact_size(
                                            Vec2::new(224., 168.),
                                            egui::Sense::click(),
                                        )
                                        .0;
                                    let metadata = ui.label("PDB · 2.50 MiB").rect;
                                    (header, image, metadata)
                                }));
                            }
                        });
                    });
                },
            );
        }
        for card in &cards {
            let (header, image, metadata) = card.inner;
            assert!(image.top() >= header.bottom());
            assert!(metadata.top() >= image.bottom());
            assert!(image.width() > 200.);
            assert!(card.response.rect.width() < 250.);
        }
        assert!(cards[0].response.rect.right() <= cards[1].response.rect.left());
        assert!(cards[1].response.rect.right() <= cards[2].response.rect.left());
    }
    #[test]
    fn gallery_checks_exact_source_family_and_receipts_before_offering_downloads() {
        let mut entry = json!({"entry_id":"manual:one","source_ref":"construct:p@2","sha256":"a".repeat(64),"size":40,"format":"pdb","protein":{"ref":"construct:p@2","sha256":"b".repeat(64)}});
        assert!(entry_valid(&entry, "construct:p@3"));
        assert!(!entry_valid(&entry, "construct:q@3"));
        entry["protein"]["ref"] = json!("construct:q@2");
        assert!(!entry_valid(&entry, "construct:p@3"));
    }
}
