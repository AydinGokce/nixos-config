//! Nonblocking native workflows and durable submission intents.
use crate::rpc::{self, Backend, CHUNK, Client, Connection, MAX_UPLOAD, RpcError};
use base64::{Engine, engine::general_purpose::STANDARD};
use eframe::egui;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, mpsc},
    thread,
};

const STATE_LIMIT: usize = 32 * 1024 * 1024;
pub fn state_directory() -> Result<PathBuf, RpcError> {
    if let Some(path) = std::env::var_os("BIO_WORKBENCH_NATIVE_STATE_DIR").filter(|p| !p.is_empty())
    {
        return Ok(PathBuf::from(path));
    }
    Ok(platform_config()?.join("bio-workbench-native"))
}
fn platform_config() -> Result<PathBuf, RpcError> {
    #[cfg(target_os = "macos")]
    {
        return Ok(rpc::home_directory()
            .ok_or_else(|| RpcError::new("local_io", "No home directory is available."))?
            .join("Library/Application Support"));
    }
    #[cfg(target_os = "windows")]
    {
        if let Some(path) = std::env::var_os("APPDATA") {
            return Ok(PathBuf::from(path));
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        if let Some(path) = std::env::var_os("XDG_CONFIG_HOME").filter(|p| !p.is_empty()) {
            return Ok(PathBuf::from(path));
        }
        Ok(rpc::home_directory()
            .ok_or_else(|| RpcError::new("local_io", "No home directory is available."))?
            .join(".config"))
    }
}

#[derive(Debug)]
pub enum Event {
    Result {
        id: String,
        method: String,
        result: Result<Value, RpcError>,
    },
    Progress {
        id: String,
        done: u64,
        total: u64,
    },
    Artifact {
        id: String,
        artifact_id: String,
        path: PathBuf,
        metadata: Value,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct UploadState {
    path: PathBuf,
    name: String,
    size: u64,
    sha256: String,
    #[serde(default)]
    upload_id: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Operation {
    id: String,
    method: String,
    params: Value,
    endpoint: String,
    status: String,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<RpcError>,
    #[serde(default)]
    upload: Option<UploadState>,
}
#[derive(Default, Serialize, Deserialize)]
struct Journal {
    #[serde(default)]
    operations: BTreeMap<String, Operation>,
}
impl Journal {
    fn save(&self, path: &Path) -> Result<(), RpcError> {
        let value = serde_json::to_value(self)?;
        if serde_json::to_vec(&value)?.len() > STATE_LIMIT {
            return Err(RpcError::new(
                "state_full",
                "The request receipt store exceeds 32 MiB. Keep a backup before archiving old receipts.",
            ));
        }
        rpc::atomic_json(path, &value)
    }
}
struct Task {
    operation: Operation,
    backend: Arc<dyn Backend>,
    durable: bool,
}
struct Delivery {
    endpoint: String,
    event: Event,
}
pub struct Session {
    pub connection: Connection,
    pub draft: Value,
    pub notices: Vec<String>,
    directory: PathBuf,
    journal: Arc<Mutex<Journal>>,
    tasks: mpsc::Sender<Task>,
    events: mpsc::Receiver<Delivery>,
    backend_override: Option<Arc<dyn Backend>>,
}
impl Session {
    pub fn open(ctx: egui::Context) -> Result<Self, RpcError> {
        Self::open_in(ctx, state_directory()?)
    }
    pub fn open_in(ctx: egui::Context, directory: PathBuf) -> Result<Self, RpcError> {
        Self::open_internal(ctx, directory, None, true)
    }
    fn open_internal(
        ctx: egui::Context,
        directory: PathBuf,
        backend_override: Option<Arc<dyn Backend>>,
        migrate: bool,
    ) -> Result<Self, RpcError> {
        rpc::private_dir(&directory)?;
        let mut notices = Vec::new();
        if migrate {
            migrate_legacy(&directory, &mut notices)?;
        }
        let connection_path = directory.join("connection.json");
        let connection = if connection_path.exists() {
            serde_json::from_value::<Connection>(rpc::read_json(&connection_path, 16 * 1024)?)?
        } else {
            Connection::default()
        };
        connection.validate()?;
        let draft_path = directory.join("draft.json");
        let draft = if draft_path.exists() {
            rpc::read_json(&draft_path, STATE_LIMIT)?
        } else {
            json!({})
        };
        if !draft.is_object() {
            return Err(RpcError::new(
                "local_io",
                "The saved native draft is not an object.",
            ));
        }
        let journal_path = directory.join("requests.json");
        let mut journal = if journal_path.exists() {
            serde_json::from_value::<Journal>(rpc::read_json(&journal_path, STATE_LIMIT)?)?
        } else {
            Journal::default()
        };
        let mut recovered = 0;
        for op in journal.operations.values_mut() {
            if matches!(op.status.as_str(), "queued" | "running") {
                op.status = "uncertain".into();
                op.error=Some(RpcError::new("interrupted","The desktop closed before recording a final response. Recover this exact operation to reconcile it.").uncertain(true));
                recovered += 1;
            }
        }
        if recovered > 0 {
            journal.save(&journal_path)?;
            notices.push(format!("{recovered} interrupted operation(s) were retained for explicit recovery; none were resubmitted."));
        }
        let journal = Arc::new(Mutex::new(journal));
        let (tasks_tx, tasks_rx) = mpsc::channel::<Task>();
        let tasks_rx = Arc::new(Mutex::new(tasks_rx));
        let (events_tx, events_rx) = mpsc::channel();
        for index in 0..4 {
            let queue = tasks_rx.clone();
            let events = events_tx.clone();
            let journal = journal.clone();
            let directory = directory.clone();
            let ctx = ctx.clone();
            thread::Builder::new()
                .name(format!("bio-rpc-{index}"))
                .spawn(move || {
                    loop {
                        let task = match queue.lock() {
                            Ok(q) => match q.recv() {
                                Ok(t) => t,
                                Err(_) => break,
                            },
                            Err(_) => break,
                        };
                        run_task(task, &directory, &journal, &events, &ctx);
                    }
                })?;
        }
        Ok(Self {
            connection,
            draft,
            notices,
            directory,
            journal,
            tasks: tasks_tx,
            events: events_rx,
            backend_override,
        })
    }
    pub fn save_connection(&mut self, connection: Connection) -> Result<(), RpcError> {
        connection.validate()?;
        rpc::atomic_json(
            &self.directory.join("connection.json"),
            &serde_json::to_value(&connection)?,
        )?;
        self.connection = connection;
        Ok(())
    }
    /// Persist detached reference state before a new endpoint can become active,
    /// including after a crash between the two atomic file replacements.
    pub fn save_connection_with_draft(
        &mut self,
        connection: Connection,
        draft: Value,
    ) -> Result<(), RpcError> {
        connection.validate()?;
        self.save_draft(draft)?;
        self.save_connection(connection)
    }
    pub fn save_draft(&mut self, mut draft: Value) -> Result<(), RpcError> {
        if !draft.is_object() {
            return Err(RpcError::new("draft", "Draft must be an object."));
        }
        for key in ["legacy_electron", "legacy_annotations"] {
            if draft.get(key).is_none()
                && let Some(old) = self.draft.get(key)
            {
                draft[key] = old.clone();
            }
        }
        if serde_json::to_vec(&draft)?.len() > STATE_LIMIT {
            return Err(RpcError::new("draft", "Draft exceeds 32 MiB."));
        }
        rpc::atomic_json(&self.directory.join("draft.json"), &draft)?;
        self.draft = draft;
        Ok(())
    }
    pub fn request(&mut self, method: &str, mut params: Value) -> Result<String, RpcError> {
        if !rpc::allowed(method) || !params.is_object() {
            return Err(RpcError::new(
                "request",
                "Unknown RPC method or non-object parameters.",
            ));
        }
        self.connection.validate()?;
        let endpoint = self.connection.identity();
        if matches!(method, "batch.validate" | "batch.create") {
            let explicit = params.get("request_key").is_some();
            if !explicit {
                let journal = self.journal.lock().map_err(|_| poisoned())?;
                // An automatic key denotes the same intent until the caller deliberately supplies a new key.
                if let Some(old) = journal.operations.values().find(|op| {
                    op.endpoint == endpoint
                        && op.method == method
                        && without_key(&op.params) == params
                }) {
                    let id = old.id.clone();
                    drop(journal);
                    return self.retry(&id);
                }
                params["request_key"] = json!(uuid::Uuid::new_v4().to_string());
            }
            let key = params["request_key"]
                .as_str()
                .filter(|k| !k.is_empty() && k.len() <= 200)
                .ok_or_else(|| RpcError::new("request", "A nonempty request_key is required."))?;
            let journal = self.journal.lock().map_err(|_| poisoned())?;
            if let Some(old) = journal.operations.values().find(|op| {
                op.endpoint == endpoint
                    && op.method == method
                    && op.params.get("request_key").and_then(Value::as_str) == Some(key)
            }) {
                if old.params != params {
                    return Err(RpcError::new(
                        "conflict",
                        "This request key is already associated with a different payload. Start a new preview for a new intent.",
                    ));
                }
                let id = old.id.clone();
                drop(journal);
                return self.retry(&id);
            }
        }
        if serde_json::to_vec(&params)?.len() + 256 > rpc::MAX_WIRE {
            return Err(RpcError::new(
                "request",
                "Input exceeds the wire limit; upload a file.",
            ));
        }
        self.enqueue_new(method, params, rpc::mutating(method))
    }
    pub fn upload(&mut self, path: PathBuf) -> Result<String, RpcError> {
        // Copying/hashing/uploading happen on a worker; nothing blocks a frame on file bytes.
        let path = fs::canonicalize(path)?;
        let meta = fs::metadata(&path)?;
        if !meta.is_file() || meta.len() == 0 || meta.len() > MAX_UPLOAD {
            return Err(RpcError::new(
                "upload",
                "Choose a nonempty regular file no larger than 256 MiB.",
            ));
        }
        self.enqueue_new("local.upload", json!({"path":path}), true)
    }
    pub fn artifact(&mut self, artifact_id: &str) -> Result<String, RpcError> {
        self.enqueue_new("local.artifact", json!({"artifact_id":artifact_id}), false)
    }
    pub fn library_attachment(
        &mut self,
        reference: &str,
        name: &str,
        receipt: &Value,
    ) -> Result<String, RpcError> {
        self.enqueue_new(
            "local.library_attachment",
            json!({"ref":reference,"name":name,"receipt":receipt}),
            false,
        )
    }
    fn backend(&self) -> Result<Arc<dyn Backend>, RpcError> {
        if let Some(backend) = &self.backend_override {
            Ok(backend.clone())
        } else {
            Ok(Arc::new(Client::new(
                self.connection.clone(),
                &self.directory.join("ssh"),
            )?))
        }
    }
    fn enqueue_new(
        &mut self,
        method: &str,
        params: Value,
        durable: bool,
    ) -> Result<String, RpcError> {
        let backend = self.backend()?;
        let operation = Operation {
            id: uuid::Uuid::new_v4().to_string(),
            method: method.into(),
            params,
            endpoint: self.connection.identity(),
            status: "queued".into(),
            result: None,
            error: None,
            upload: None,
        };
        if durable {
            let mut journal = self.journal.lock().map_err(|_| poisoned())?;
            journal
                .operations
                .insert(operation.id.clone(), operation.clone());
            if let Err(error) = journal.save(&self.directory.join("requests.json")) {
                journal.operations.remove(&operation.id);
                return Err(error);
            }
        }
        let id = operation.id.clone();
        self.tasks
            .send(Task {
                operation,
                backend,
                durable,
            })
            .map_err(|_| {
                RpcError::new(
                    "worker",
                    "Native request workers are unavailable; your saved request was not discarded.",
                )
            })?;
        Ok(id)
    }
    pub fn retry(&mut self, id: &str) -> Result<String, RpcError> {
        let backend = self.backend()?;
        let mut journal = self.journal.lock().map_err(|_| poisoned())?;
        let mut operation = journal.operations.get(id).cloned().ok_or_else(|| {
            RpcError::new(
                "recovery",
                "No durable operation exists with this identifier.",
            )
        })?;
        if operation.endpoint != self.connection.identity() {
            return Err(RpcError::new(
                "connection_changed",
                "This request belongs to a different head/user/port. Restore its connection before recovering it.",
            ));
        }
        if matches!(operation.status.as_str(), "queued" | "running") {
            return Ok(id.to_owned());
        }
        // The server has no idempotency key for a brand-new annotation. Never duplicate an uncertain insert.
        if operation.method == "annotation.put"
            && operation.params.get("annotation_id").is_none()
            && operation.error.as_ref().is_some_and(|e| e.uncertain)
            && operation
                .params
                .pointer("/selection/local_id")
                .and_then(Value::as_str)
                .is_none()
        {
            return Err(RpcError::new("annotation_recovery","The annotation may already be saved. Refresh its shared annotations before creating another note; a new annotation cannot safely be replayed automatically.").uncertain(true));
        }
        if operation.error.as_ref().is_some_and(|e| e.uncertain)
            && (operation.method == "upload.begin"
                || (operation.method == "local.upload"
                    && operation
                        .upload
                        .as_ref()
                        .is_none_or(|u| u.upload_id.is_none())))
        {
            return Err(RpcError::new("upload_recovery","The upload may have begun, but no upload ID was received. Start a new explicit file upload; this uncertain begin will not be replayed.").uncertain(true));
        }
        if operation.status != "complete" {
            operation.status = "queued".into();
            journal.operations.insert(id.to_owned(), operation.clone());
            journal.save(&self.directory.join("requests.json"))?;
        }
        drop(journal);
        self.tasks
            .send(Task {
                operation,
                backend,
                durable: true,
            })
            .map_err(|_| RpcError::new("worker", "Native request workers are unavailable."))?;
        Ok(id.to_owned())
    }
    pub fn retryable_operations(&self) -> Vec<Value> {
        let Ok(journal) = self.journal.lock() else {
            return Vec::new();
        };
        journal.operations.values().filter(|op| op.status!="complete" || matches!(op.method.as_str(),"batch.validate"|"batch.create"|"local.upload")).map(|op| json!({"id":op.id,"method":op.method,"params":op.params,"status":op.status,"error":op.error,"result":op.result,"endpoint":op.endpoint,"current_connection":op.endpoint==self.connection.identity()})).collect()
    }
    pub fn drain_events(&mut self) -> Vec<Event> {
        self.events.try_iter().map(|delivery| {
            if delivery.endpoint==self.connection.identity() { delivery.event } else {
                let (id,method)=match delivery.event {Event::Result{id,method,..}=>(id,method),Event::Artifact{id,..}=>(id,"local.artifact".into()),Event::Progress{id,..}=>(id,"local.upload".into())};
                Event::Result{id,method,result:Err(RpcError::new("connection_changed","A request from the previous connection finished. Its durable receipt remains in recovery; refresh the current connection."))}
            }
        }).collect()
    }
}
fn poisoned() -> RpcError {
    RpcError::new(
        "state",
        "The request store became unavailable; restart the desktop to recover saved intents.",
    )
}
fn without_key(params: &Value) -> Value {
    let mut value = params.clone();
    if let Some(obj) = value.as_object_mut() {
        obj.remove("request_key");
    }
    value
}
fn update_operation(
    journal: &Arc<Mutex<Journal>>,
    directory: &Path,
    operation: &Operation,
) -> Result<(), RpcError> {
    let mut journal = journal.lock().map_err(|_| poisoned())?;
    journal
        .operations
        .insert(operation.id.clone(), operation.clone());
    journal.save(&directory.join("requests.json"))
}
fn deliver(events: &mpsc::Sender<Delivery>, ctx: &egui::Context, endpoint: &str, event: Event) {
    let _ = events.send(Delivery {
        endpoint: endpoint.into(),
        event,
    });
    ctx.request_repaint();
}
fn run_task(
    task: Task,
    directory: &Path,
    journal: &Arc<Mutex<Journal>>,
    events: &mpsc::Sender<Delivery>,
    ctx: &egui::Context,
) {
    let Task {
        mut operation,
        backend,
        durable,
    } = task;
    let id = operation.id.clone();
    let endpoint = operation.endpoint.clone();
    let method = operation.method.clone();
    let progress = |done, total| {
        deliver(
            events,
            ctx,
            &endpoint,
            Event::Progress {
                id: id.clone(),
                done,
                total,
            },
        )
    };
    let result = (|| {
        if operation.status == "complete" {
            return operation
                .result
                .clone()
                .ok_or_else(|| RpcError::new("recovery", "The completed receipt has no result."));
        }
        if durable {
            operation.status = "running".into();
            update_operation(journal, directory, &operation)?;
        }
        match method.as_str() {
            "local.library" => backend.library(),
            "local.library_attachment" => {
                let reference = operation.params["ref"]
                    .as_str()
                    .ok_or_else(|| RpcError::new("request", "Missing library reference."))?;
                let name = operation.params["name"]
                    .as_str()
                    .ok_or_else(|| RpcError::new("request", "Missing attachment name."))?;
                let (path, metadata) = rpc::download_library_attachment(
                    backend.as_ref(),
                    &directory.join("library-attachments"),
                    reference,
                    name,
                    &operation.params["receipt"],
                    progress,
                )?;
                Ok(json!({"local_path":path,"metadata":metadata}))
            }
            "local.artifact" => {
                let artifact_id = operation.params["artifact_id"]
                    .as_str()
                    .ok_or_else(|| RpcError::new("request", "Missing artifact ID."))?;
                let (path, metadata) = rpc::download_artifact(
                    backend.as_ref(),
                    &directory.join("artifacts"),
                    artifact_id,
                    progress,
                )?;
                deliver(
                    events,
                    ctx,
                    &endpoint,
                    Event::Artifact {
                        id: id.clone(),
                        artifact_id: artifact_id.into(),
                        path,
                        metadata,
                    },
                );
                Ok(Value::Null)
            }
            "local.upload" => upload_file(
                backend.as_ref(),
                directory,
                &mut operation,
                |op| update_operation(journal, directory, op),
                progress,
            ),
            "annotation.put" if operation.params.get("annotation_id").is_some() => {
                reconcile_annotation(backend.as_ref(), &operation.params)
            }
            "annotation.put" if operation.error.as_ref().is_some_and(|e| e.uncertain) => {
                reconcile_new_annotation(backend.as_ref(), &operation.params)
            }
            _ => rpc::workflow_call(backend.as_ref(), &method, operation.params.clone()),
        }
    })();
    if method == "local.artifact" && result.is_ok() {
        return;
    }
    let result = if durable {
        match &result {
            Ok(value) => {
                operation.status = "complete".into();
                operation.result = Some(value.clone());
                operation.error = None;
            }
            Err(error) => {
                operation.status = if error.uncertain {
                    "uncertain"
                } else {
                    "error"
                }
                .into();
                operation.error = Some(error.clone());
            }
        }
        match update_operation(journal,directory,&operation) { Ok(())=>result,Err(error)=>Err(RpcError::new("receipt",format!("The operation finished, but its receipt could not be saved: {error}. Recover the same request key.")).uncertain(true)) }
    } else {
        result
    };
    deliver(events, ctx, &endpoint, Event::Result { id, method, result });
}

fn reconcile_annotation(backend: &dyn Backend, params: &Value) -> Result<Value, RpcError> {
    // A versioned update can be reconciled after losing its reply without writing twice.
    let list = backend.call(
        "annotation.list",
        json!({"artifact_id":params["artifact_id"]}),
    )?;
    if let Some(existing) = list
        .get("annotations")
        .and_then(Value::as_array)
        .and_then(|rows| {
            rows.iter()
                .find(|row| row.get("annotation_id") == params.get("annotation_id"))
        })
        && let Some(expected) = params.get("expected_revision").and_then(Value::as_u64)
        && existing.get("revision").and_then(Value::as_u64) == expected.checked_add(1)
        && existing.get("text") == params.get("text")
        && existing.get("selection").unwrap_or(&Value::Null)
            == params.get("selection").unwrap_or(&Value::Null)
    {
        return Ok(existing.clone());
    }
    backend.call("annotation.put", params.clone())
}
fn reconcile_new_annotation(backend: &dyn Backend, params: &Value) -> Result<Value, RpcError> {
    let list = backend.call(
        "annotation.list",
        json!({"artifact_id":params["artifact_id"]}),
    )?;
    let local_id = params
        .pointer("/selection/local_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let matches: Vec<_> = list
        .get("annotations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|row| {
            local_id.is_some()
                && row.pointer("/selection/local_id").and_then(Value::as_str) == local_id
                && row.get("text") == params.get("text")
                && row.get("selection") == params.get("selection")
        })
        .collect();
    if matches.len() == 1 {
        return Ok(matches[0].clone());
    }
    Err(RpcError::new("annotation_recovery","An exact saved annotation could not be identified. Refresh the shared notes and resolve the uncertain save explicitly; no duplicate was created.").uncertain(true))
}
fn upload_file(
    backend: &dyn Backend,
    directory: &Path,
    operation: &mut Operation,
    mut checkpoint: impl FnMut(&Operation) -> Result<(), RpcError>,
    mut progress: impl FnMut(u64, u64),
) -> Result<Value, RpcError> {
    let staging = directory.join("uploads");
    rpc::private_dir(&staging)?;
    if operation.upload.is_none() {
        let path = operation
            .params
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| RpcError::new("upload", "Missing source file path."))?;
        let path = Path::new(path);
        let meta = fs::metadata(path)?;
        if !meta.is_file() || meta.len() == 0 || meta.len() > MAX_UPLOAD {
            return Err(RpcError::new(
                "upload",
                "Input file must be nonempty, regular, and no larger than 256 MiB.",
            ));
        }
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| RpcError::new("upload", "Input filename is not valid UTF-8."))?
            .to_owned();
        if name.len() > 200 || name.chars().any(char::is_control) {
            return Err(RpcError::new(
                "upload",
                "Input filename must be at most 200 UTF-8 bytes with no control characters.",
            ));
        }
        let mut source = File::open(path)?;
        let mut tmp = tempfile::NamedTempFile::new_in(&staging)?;
        let copied = std::io::copy(
            &mut Read::by_ref(&mut source).take(MAX_UPLOAD + 1),
            &mut tmp,
        )?;
        if copied > MAX_UPLOAD {
            return Err(RpcError::new(
                "upload",
                "Input grew beyond the upload limit while copying.",
            ));
        }
        tmp.as_file().sync_all()?;
        let snapshot = staging.join(format!("{}.input", operation.id));
        tmp.persist(&snapshot)
            .map_err(|e| RpcError::from(e.error))?;
        let (size, sha256) = rpc::file_hash(&snapshot)?;
        operation.upload = Some(UploadState {
            path: snapshot,
            name,
            size,
            sha256,
            upload_id: None,
        });
        checkpoint(operation)?;
    }
    let mut upload = operation.upload.clone().unwrap();
    let meta = fs::symlink_metadata(&upload.path)?;
    if !meta.is_file()
        || meta.file_type().is_symlink()
        || rpc::file_hash(&upload.path)? != (upload.size, upload.sha256.clone())
    {
        return Err(RpcError::new(
            "integrity",
            "The retained upload snapshot changed; refusing to resume with different bytes.",
        ));
    }
    let receipt = if let Some(id) = &upload.upload_id {
        backend.call("upload.get", json!({"upload_id":id}))?
    } else {
        let receipt = backend.call(
            "upload.begin",
            json!({"name":upload.name,"size":upload.size,"sha256":upload.sha256}),
        )?;
        let id = receipt
            .get("upload_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| RpcError::new("protocol", "Upload begin returned no ID."))?;
        upload.upload_id = Some(id.into());
        operation.upload = Some(upload.clone());
        checkpoint(operation)?;
        receipt
    };
    let id = upload.upload_id.as_deref().unwrap();
    if receipt.get("upload_id").and_then(Value::as_str) != Some(id)
        || receipt.get("size").and_then(Value::as_u64) != Some(upload.size)
    {
        return Err(RpcError::new(
            "integrity",
            "The head returned a different upload identity or size.",
        ));
    }
    if receipt.get("state").and_then(Value::as_str) == Some("complete") {
        if receipt.get("sha256").and_then(Value::as_str) != Some(&upload.sha256) {
            return Err(RpcError::new(
                "integrity",
                "Completed upload checksum differs from retained bytes.",
            ));
        }
        progress(upload.size, upload.size);
        return Ok(receipt);
    }
    let mut offset = receipt
        .get("offset")
        .and_then(Value::as_u64)
        .ok_or_else(|| RpcError::new("protocol", "Upload receipt has no byte offset."))?;
    if offset > upload.size {
        return Err(RpcError::new(
            "integrity",
            "Upload offset exceeds its declared size.",
        ));
    }
    let mut file = File::open(&upload.path)?;
    file.seek(SeekFrom::Start(offset))?;
    progress(offset, upload.size);
    let mut buffer = vec![0u8; CHUNK];
    while offset < upload.size {
        let count = (upload.size - offset).min(CHUNK as u64) as usize;
        file.read_exact(&mut buffer[..count])?;
        let receipt = backend.call(
            "upload.chunk",
            json!({"upload_id":id,"offset":offset,"data_base64":STANDARD.encode(&buffer[..count])}),
        )?;
        offset += count as u64;
        if receipt.get("upload_id").and_then(Value::as_str) != Some(id)
            || receipt.get("offset").and_then(Value::as_u64) != Some(offset)
        {
            return Err(RpcError::new(
                "integrity",
                "The head did not confirm the exact uploaded chunk.",
            ));
        }
        progress(offset, upload.size);
    }
    let receipt = backend.call(
        "upload.finish",
        json!({"upload_id":id,"sha256":upload.sha256}),
    )?;
    if receipt.get("upload_id").and_then(Value::as_str) != Some(id)
        || receipt.get("size").and_then(Value::as_u64) != Some(upload.size)
        || receipt.get("sha256").and_then(Value::as_str) != Some(&upload.sha256)
        || receipt.get("state").and_then(Value::as_str) != Some("complete")
    {
        return Err(RpcError::new(
            "integrity",
            "The head did not seal the upload with the expected size and checksum.",
        ));
    }
    Ok(receipt)
}

fn migrate_legacy(directory: &Path, notices: &mut Vec<String>) -> Result<(), RpcError> {
    if directory.join("migration.json").exists() {
        return Ok(());
    }
    let Some(home) = rpc::home_directory() else {
        return Ok(());
    };
    let legacy_config = std::env::var_os("XDG_CONFIG_HOME")
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"))
        .join("bio-workbench/connection.json");
    if !directory.join("connection.json").exists() && legacy_config.is_file() {
        match rpc::read_json(&legacy_config, 16 * 1024).and_then(|v| {
            let c: Connection = serde_json::from_value(v)?;
            c.validate()?;
            serde_json::to_value(c).map_err(RpcError::from)
        }) {
            Ok(value) => {
                rpc::atomic_json(&directory.join("connection.json"), &value)?;
                notices.push("Imported the existing desktop SSH connection.".into());
            }
            Err(error) => notices.push(format!("Could not import the old SSH connection: {error}")),
        }
    }
    if !directory.join("draft.json").exists() {
        let candidate = platform_config()?.join("bio-workbench-desktop/molecular-state.json");
        if candidate.is_file() {
            match rpc::read_json(&candidate, STATE_LIMIT)
                .and_then(|value| migrate_molecular_state(&value))
            {
                Ok(draft) => {
                    rpc::atomic_json(&directory.join("draft.json"), &draft)?;
                    notices.push("Imported the Electron input draft and retained local annotation documents. Existing files were left intact.".into());
                }
                Err(error) => {
                    notices.push(format!("Could not import the old molecular draft: {error}"))
                }
            }
        }
    }
    rpc::atomic_json(
        &directory.join("migration.json"),
        &json!({"schema":1,"attempted":true}),
    )?;
    Ok(())
}
fn migrate_molecular_state(state: &Value) -> Result<Value, RpcError> {
    let object = state
        .as_object()
        .ok_or_else(|| RpcError::new("migration", "Electron molecular state is not an object."))?;
    let mut draft = json!({});
    let mut annotations = serde_json::Map::new();
    for (key, encoded) in object {
        let Some(encoded) = encoded.as_str().filter(|s| s.len() <= rpc::MAX_WIRE) else {
            continue;
        };
        if key == "bio-workbench.input-draft.v1" {
            let old = rpc::strict_json(encoded.as_bytes())?;
            if let Some(fields) = old.as_object() {
                for name in ["inputs", "editor", "name", "mode"] {
                    if let Some(value) = fields.get(name) {
                        draft[name] = value.clone();
                    }
                }
            }
            draft["legacy_electron"] = old;
        } else if let Some(sha) = key.strip_prefix("bio-workbench.annotations.v1.")
            && sha.len() == 64
            && sha.bytes().all(|b| b.is_ascii_hexdigit())
        {
            annotations.insert(sha.to_owned(), rpc::strict_json(encoded.as_bytes())?);
        }
    }
    draft["legacy_annotations"] = Value::Object(annotations);
    Ok(draft)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::atomic::{AtomicUsize, Ordering},
        time::{Duration, Instant},
    };
    struct Mock {
        calls: Mutex<Vec<(String, Value)>>,
        fail_first: AtomicUsize,
        upload: Mutex<Vec<u8>>,
        begins: AtomicUsize,
        expected: Vec<u8>,
    }
    impl Mock {
        fn new() -> Self {
            Self {
                calls: Mutex::default(),
                fail_first: AtomicUsize::new(0),
                upload: Mutex::default(),
                begins: AtomicUsize::new(0),
                expected: vec![42; CHUNK + 31],
            }
        }
    }
    impl Backend for Mock {
        fn call(&self, method: &str, p: Value) -> Result<Value, RpcError> {
            self.calls.lock().unwrap().push((method.into(), p.clone()));
            match method {
                "batch.validate" | "batch.create" => {
                    if self.fail_first.swap(0, Ordering::SeqCst) > 0 {
                        Err(RpcError::new("ssh", "lost response").uncertain(true))
                    } else {
                        Ok(json!({"batch_id":"batch-1","request_key":p["request_key"]}))
                    }
                }
                "upload.begin" => {
                    self.begins.fetch_add(1, Ordering::SeqCst);
                    Ok(
                        json!({"upload_id":"up-1","size":self.expected.len(),"offset":0,"state":"uploading"}),
                    )
                }
                "upload.get" => Ok(
                    json!({"upload_id":"up-1","size":self.expected.len(),"offset":self.upload.lock().unwrap().len(),"state":"uploading"}),
                ),
                "upload.chunk" => {
                    let bytes = STANDARD.decode(p["data_base64"].as_str().unwrap()).unwrap();
                    let mut upload = self.upload.lock().unwrap();
                    assert_eq!(upload.len() as u64, p["offset"].as_u64().unwrap());
                    upload.extend(bytes);
                    if self.fail_first.swap(0, Ordering::SeqCst) > 0 {
                        Err(RpcError::new("ssh", "chunk accepted, reply lost").uncertain(true))
                    } else {
                        Ok(json!({"upload_id":"up-1","offset":upload.len()}))
                    }
                }
                "upload.finish" => Ok(
                    json!({"upload_id":"up-1","size":self.expected.len(),"sha256":p["sha256"],"state":"complete"}),
                ),
                _ => Ok(json!({})),
            }
        }
        fn library(&self) -> Result<Value, RpcError> {
            Ok(json!({"records":[]}))
        }
    }
    fn next_result(session: &mut Session) -> (String, Result<Value, RpcError>) {
        let start = Instant::now();
        loop {
            for event in session.drain_events() {
                if let Event::Result { id, result, .. } = event {
                    return (id, result);
                }
            }
            assert!(start.elapsed() < Duration::from_secs(5));
            thread::sleep(Duration::from_millis(10));
        }
    }
    #[test]
    fn endpoint_change_requires_durable_detachment_before_profile_publication() {
        let directory = tempfile::tempdir().unwrap();
        let mut session = Session::open_internal(
            egui::Context::default(),
            directory.path().into(),
            Some(Arc::new(Mock::new())),
            false,
        )
        .unwrap();
        let original = session.connection.clone();
        session.save_connection(original.clone()).unwrap();
        let mut changed = original.clone();
        changed.host = "another-head.example".into();
        fs::create_dir(directory.path().join("draft.json")).unwrap();
        assert!(
            session
                .save_connection_with_draft(changed.clone(), json!({"inputs":[]}))
                .is_err()
        );
        assert_eq!(session.connection, original);
        let persisted: Connection = serde_json::from_value(
            rpc::read_json(&directory.path().join("connection.json"), 16384).unwrap(),
        )
        .unwrap();
        assert_eq!(persisted, original);
        fs::remove_dir(directory.path().join("draft.json")).unwrap();
        let detached = json!({"inputs":[],"detached_library_sources":[{"endpoint":original.identity(),"input":{"source":{"kind":"library","ref":"construct:same-id@1"}}}]});
        session
            .save_connection_with_draft(changed.clone(), detached.clone())
            .unwrap();
        assert_eq!(session.connection, changed);
        assert_eq!(
            rpc::read_json(&directory.path().join("draft.json"), STATE_LIMIT).unwrap()["detached_library_sources"],
            detached["detached_library_sources"]
        );
        assert!(
            rpc::read_json(&directory.path().join("draft.json"), STATE_LIMIT).unwrap()["inputs"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }
    #[test]
    fn durable_request_retry_keeps_key_across_restart_and_never_autoreplays() {
        let dir = tempfile::tempdir().unwrap();
        let mock = Arc::new(Mock::new());
        mock.fail_first.store(1, Ordering::SeqCst);
        let mut session = Session::open_internal(
            egui::Context::default(),
            dir.path().into(),
            Some(mock.clone()),
            false,
        )
        .unwrap();
        let params = json!({"request_key":"stable-key","inputs":[{"source":{"text":"EXACT\n"}}]});
        let id = session.request("batch.validate", params.clone()).unwrap();
        assert!(next_result(&mut session).1.unwrap_err().uncertain);
        drop(session);
        let mut session = Session::open_internal(
            egui::Context::default(),
            dir.path().into(),
            Some(mock.clone()),
            false,
        )
        .unwrap();
        assert_eq!(mock.calls.lock().unwrap().len(), 1);
        assert_eq!(session.retryable_operations()[0]["params"], params);
        assert_eq!(session.retry(&id).unwrap(), id);
        assert!(next_result(&mut session).1.is_ok());
        let calls = mock.calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].1, calls[1].1);
        drop(calls);
        let mut changed = params;
        changed["inputs"] = json!([]);
        assert_eq!(
            session.request("batch.validate", changed).unwrap_err().code,
            "conflict"
        );
        let count = mock.calls.lock().unwrap().len();
        session.retry(&id).unwrap();
        assert!(next_result(&mut session).1.is_ok());
        assert_eq!(mock.calls.lock().unwrap().len(), count);
    }
    #[test]
    fn old_operation_cannot_replay_to_new_head() {
        let dir = tempfile::tempdir().unwrap();
        let mock = Arc::new(Mock::new());
        let mut session = Session::open_internal(
            egui::Context::default(),
            dir.path().into(),
            Some(mock),
            false,
        )
        .unwrap();
        let id = session
            .request("batch.validate", json!({"request_key":"same"}))
            .unwrap();
        next_result(&mut session).1.unwrap();
        let mut connection = session.connection.clone();
        connection.host = "another-head.example".into();
        session.save_connection(connection).unwrap();
        assert_eq!(session.retry(&id).unwrap_err().code, "connection_changed");
    }
    #[test]
    fn upload_recovers_accepted_chunk_without_duplicate_begin_or_changed_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let mock = Arc::new(Mock::new());
        let source = dir.path().join("input.fasta");
        fs::write(&source, &mock.expected).unwrap();
        mock.fail_first.store(1, Ordering::SeqCst);
        let mut session = Session::open_internal(
            egui::Context::default(),
            dir.path().join("state"),
            Some(mock.clone()),
            false,
        )
        .unwrap();
        let id = session.upload(source.clone()).unwrap();
        assert!(next_result(&mut session).1.unwrap_err().uncertain);
        fs::write(&source, b"changed source").unwrap();
        session.retry(&id).unwrap();
        assert!(next_result(&mut session).1.is_ok());
        assert_eq!(mock.begins.load(Ordering::SeqCst), 1);
        assert_eq!(*mock.upload.lock().unwrap(), mock.expected);
    }
    #[test]
    fn startup_marks_inflight_uncertain_without_network() {
        let dir = tempfile::tempdir().unwrap();
        let operation = Operation {
            id: "op".into(),
            method: "batch.create".into(),
            params: json!({"request_key":"k"}),
            endpoint: Connection::default().identity(),
            status: "running".into(),
            result: None,
            error: None,
            upload: None,
        };
        let mut journal = Journal::default();
        journal.operations.insert("op".into(), operation);
        journal.save(&dir.path().join("requests.json")).unwrap();
        let mock = Arc::new(Mock::new());
        let session = Session::open_internal(
            egui::Context::default(),
            dir.path().into(),
            Some(mock.clone()),
            false,
        )
        .unwrap();
        assert_eq!(session.retryable_operations()[0]["status"], "uncertain");
        assert!(mock.calls.lock().unwrap().is_empty());
    }
    #[test]
    fn migration_preserves_sequence_and_annotation_documents() {
        let old = json!({"inputs":[{"source":{"kind":"text","format":"sequence","text":"ACGT\n"}}],"editor":{"paste":{"text":"Unfinished"}},"mode":"assembly","name":"Research"});
        let sha = "a".repeat(64);
        let annotations =
            json!({"schema":1,"artifact_sha256":sha,"annotations":[{"note":"Keep this"}]});
        let state = json!({"bio-workbench.input-draft.v1":old.to_string(),format!("bio-workbench.annotations.v1.{sha}"):annotations.to_string()});
        let migrated = migrate_molecular_state(&state).unwrap();
        assert_eq!(migrated["inputs"], old["inputs"]);
        assert_eq!(migrated["legacy_annotations"][&sha], annotations);
    }
    #[test]
    fn uncertain_non_idempotent_inserts_are_reconciled_without_second_write() {
        struct Notes {
            writes: AtomicUsize,
            params: Value,
        }
        impl Backend for Notes {
            fn call(&self, method: &str, _: Value) -> Result<Value, RpcError> {
                if method == "annotation.list" {
                    let mut note = self.params.clone();
                    note["annotation_id"] = json!("note-1");
                    note["revision"] = json!(1);
                    Ok(json!({"annotations":[note]}))
                } else {
                    self.writes.fetch_add(1, Ordering::SeqCst);
                    Err(RpcError::new("ssh", "reply lost after insert").uncertain(true))
                }
            }
            fn library(&self) -> Result<Value, RpcError> {
                unreachable!()
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let params = json!({"artifact_id":"a","text":"Retain this","selection":{"kind":"bio-workbench-residue-v1","local_id":"stable-local-note"}});
        let mock = Arc::new(Notes {
            writes: AtomicUsize::new(0),
            params: params.clone(),
        });
        let mut session = Session::open_internal(
            egui::Context::default(),
            dir.path().into(),
            Some(mock.clone()),
            false,
        )
        .unwrap();
        let id = session.request("annotation.put", params).unwrap();
        assert!(next_result(&mut session).1.unwrap_err().uncertain);
        session.retry(&id).unwrap();
        assert_eq!(
            next_result(&mut session).1.unwrap()["annotation_id"],
            "note-1"
        );
        assert_eq!(mock.writes.load(Ordering::SeqCst), 1);
        let id = session
            .request("upload.begin", json!({"name":"file","size":12}))
            .unwrap();
        assert!(next_result(&mut session).1.unwrap_err().uncertain);
        assert_eq!(session.retry(&id).unwrap_err().code, "upload_recovery");
        assert_eq!(mock.writes.load(Ordering::SeqCst), 2);
    }
    #[test]
    fn versioned_annotation_reply_reconciliation_preserves_compare_and_swap() {
        struct Notes {
            calls: Mutex<Vec<String>>,
            record: Value,
        }
        impl Backend for Notes {
            fn call(&self, method: &str, _: Value) -> Result<Value, RpcError> {
                self.calls.lock().unwrap().push(method.into());
                if method == "annotation.list" {
                    Ok(json!({"annotations":[self.record]}))
                } else {
                    Err(RpcError::new("conflict", "revision advanced"))
                }
            }
            fn library(&self) -> Result<Value, RpcError> {
                unreachable!()
            }
        }
        let params = json!({"artifact_id":"a","annotation_id":"note-1","expected_revision":1,"text":"edited","selection":{"local_id":"local"}});
        let mut record = params.clone();
        record["revision"] = json!(2);
        let mock = Notes {
            calls: Mutex::default(),
            record,
        };
        assert_eq!(reconcile_annotation(&mock, &params).unwrap()["revision"], 2);
        assert_eq!(*mock.calls.lock().unwrap(), vec!["annotation.list"]);
        let mut different = params;
        different["text"] = json!("another edit");
        assert_eq!(
            reconcile_annotation(&mock, &different).unwrap_err().code,
            "conflict"
        );
    }
}
