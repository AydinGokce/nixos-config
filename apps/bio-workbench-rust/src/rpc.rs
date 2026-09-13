//! Bounded SSH transport for the existing head JSONL protocol.
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{
    Deserialize, Serialize,
    de::{self, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    ffi::OsString,
    fmt,
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

pub const MAX_WIRE: usize = 2 * 1024 * 1024;
pub const CHUNK: usize = 524_288;
pub const MAX_UPLOAD: u64 = 256 * 1024 * 1024;
pub const MAX_ARTIFACT: u64 = 2 * 1024 * 1024 * 1024;
const HEAD: &str = "31.56.109.100";
const HOST_PIN: &str = "31.56.109.100 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAn+9pjF2dRZ5hDvjWrvht+Q+vfUTjyORY58GT3Su35Q\n";
const REMOTE: &str =
    "env BIO_WORKBENCH_ACTOR=harrison /run/current-system/sw/bin/bio-workbench rpc";
const METHODS: &[&str] = &[
    "catalog",
    "binder.catalog",
    "binder.inspect",
    "binder.run",
    "binder.candidates",
    "binder.context",
    "binder.save",
    "worker.status",
    "worker.capacity",
    "worker.extend",
    "worker.shutdown",
    "worker.control_get",
    "upload.begin",
    "upload.chunk",
    "upload.get",
    "upload.finish",
    "batch.run",
    "batch.validate",
    "batch.create",
    "batch.get",
    "batch.list",
    "batch.cancel",
    "job.get",
    "job.logs",
    "job.artifacts",
    "job.cancel",
    "artifact.read",
    "annotation.put",
    "annotation.list",
    "library.list",
    "library.get",
    "library.attachment",
    "library.structures",
    "library.structure_attach",
    "library.structure_visibility",
    "library.structure_read",
    "library.structure_links",
    "library.structure_thumbnail",
    "library.structure_thumbnail_read",
    "library.edit",
    "library.history",
    "library.undo",
    "library.redo",
    "library.runs",
    "library.sequence",
    "library.protein_domains",
    "library.product_preview",
    "library.product_create",
    "library.create",
];

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Connection {
    pub host: String,
    pub user: String,
    pub port: u16,
    #[serde(default)]
    pub key_path: String,
}
impl Default for Connection {
    fn default() -> Self {
        let key = home_directory()
            .map(|p| p.join(".ssh/datacrunch_ed25519"))
            .filter(|p| p.is_file());
        Self {
            host: HEAD.into(),
            user: "root".into(),
            port: 22,
            key_path: key
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default(),
        }
    }
}
impl Connection {
    pub fn validate(&self) -> Result<(), RpcError> {
        let valid_host = !self.host.is_empty()
            && self.host.len() <= 253
            && self.host.as_bytes()[0].is_ascii_alphanumeric()
            && self
                .host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".:-".contains(&b));
        let valid_user = !self.user.is_empty()
            && self.user.len() <= 64
            && (self.user.as_bytes()[0].is_ascii_alphabetic() || self.user.starts_with('_'))
            && self
                .user
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b));
        if !valid_host || !valid_user || self.port == 0 {
            return Err(RpcError::new(
                "connection",
                "Enter a valid SSH host, user, and port (1–65535).",
            ));
        }
        if self.key_path.len() > 4096 || self.key_path.chars().any(char::is_control) {
            return Err(RpcError::new(
                "connection",
                "The SSH key path contains invalid characters.",
            ));
        }
        if !self.key_path.is_empty() && !self.expanded_key()?.is_absolute() {
            return Err(RpcError::new(
                "connection",
                "The SSH key path must be absolute or start with ~/; leave it empty to use your SSH agent.",
            ));
        }
        Ok(())
    }
    fn expanded_key(&self) -> Result<PathBuf, RpcError> {
        if let Some(tail) = self.key_path.strip_prefix("~/") {
            Ok(home_directory()
                .ok_or_else(|| {
                    RpcError::new("connection", "Cannot resolve ~/ without a home directory.")
                })?
                .join(tail))
        } else {
            Ok(PathBuf::from(&self.key_path))
        }
    }
    pub fn identity(&self) -> String {
        format!("harrison@{}:{}:{}", self.user, self.host, self.port)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct RpcError {
    pub code: String,
    pub message: String,
    #[serde(default)]
    pub uncertain: bool,
}
impl RpcError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            uncertain: false,
        }
    }
    pub fn uncertain(mut self, uncertain: bool) -> Self {
        self.uncertain = uncertain;
        self
    }
}
impl fmt::Display for RpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}
impl std::error::Error for RpcError {}
impl From<std::io::Error> for RpcError {
    fn from(e: std::io::Error) -> Self {
        Self::new("local_io", e.to_string())
    }
}
impl From<serde_json::Error> for RpcError {
    fn from(e: serde_json::Error) -> Self {
        Self::new("json", e.to_string())
    }
}

pub fn home_directory() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .filter(|x| !x.is_empty())
        .map(PathBuf::from)
}

/// Private application state. Refuse symlink leaves before storing profile/receipts.
pub fn private_dir(path: &Path) -> Result<(), RpcError> {
    if let Ok(meta) = fs::symlink_metadata(path)
        && (meta.file_type().is_symlink() || !meta.is_dir())
    {
        return Err(RpcError::new(
            "local_io",
            format!(
                "State directory is not a regular directory: {}",
                path.display()
            ),
        ));
    }
    fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}
pub fn atomic_json(path: &Path, value: &Value) -> Result<(), RpcError> {
    atomic_bytes(path, &serde_json::to_vec_pretty(value)?)
}
pub fn atomic_bytes(path: &Path, bytes: &[u8]) -> Result<(), RpcError> {
    let parent = path
        .parent()
        .ok_or_else(|| RpcError::new("local_io", "A state file needs a parent directory."))?;
    private_dir(parent)?;
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        return Err(RpcError::new(
            "local_io",
            "Refusing to replace a state symlink.",
        ));
    }
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        tmp.as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    tmp.write_all(bytes)?;
    tmp.as_file().sync_all()?;
    tmp.persist(path).map_err(|e| RpcError::from(e.error))?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}
pub fn read_json(path: &Path, limit: usize) -> Result<Value, RpcError> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > limit as u64 {
        return Err(RpcError::new(
            "local_io",
            "State file is not a bounded regular file.",
        ));
    }
    let mut bytes = Vec::new();
    File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err(RpcError::new(
            "local_io",
            "State file exceeds its size limit.",
        ));
    }
    strict_json(&bytes)
}

// serde_json::Value normally accepts duplicate keys; the head contract forbids them.
pub fn strict_json(bytes: &[u8]) -> Result<Value, RpcError> {
    struct Strict(Value);
    impl<'de> Deserialize<'de> for Strict {
        fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
            struct V;
            impl<'de> Visitor<'de> for V {
                type Value = Strict;
                fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                    write!(f, "finite JSON without duplicate keys")
                }
                fn visit_bool<E: de::Error>(self, x: bool) -> Result<Strict, E> {
                    Ok(Strict(Value::Bool(x)))
                }
                fn visit_i64<E: de::Error>(self, x: i64) -> Result<Strict, E> {
                    Ok(Strict(x.into()))
                }
                fn visit_u64<E: de::Error>(self, x: u64) -> Result<Strict, E> {
                    Ok(Strict(x.into()))
                }
                fn visit_f64<E: de::Error>(self, x: f64) -> Result<Strict, E> {
                    serde_json::Number::from_f64(x)
                        .map(|v| Strict(Value::Number(v)))
                        .ok_or_else(|| E::custom("non-finite number"))
                }
                fn visit_str<E: de::Error>(self, x: &str) -> Result<Strict, E> {
                    Ok(Strict(x.into()))
                }
                fn visit_string<E: de::Error>(self, x: String) -> Result<Strict, E> {
                    Ok(Strict(x.into()))
                }
                fn visit_unit<E: de::Error>(self) -> Result<Strict, E> {
                    Ok(Strict(Value::Null))
                }
                fn visit_none<E: de::Error>(self) -> Result<Strict, E> {
                    Ok(Strict(Value::Null))
                }
                fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Strict, A::Error> {
                    let mut list = Vec::new();
                    while let Some(Strict(x)) = seq.next_element()? {
                        list.push(x);
                    }
                    Ok(Strict(Value::Array(list)))
                }
                fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Strict, A::Error> {
                    let mut obj = serde_json::Map::new();
                    while let Some(key) = map.next_key::<String>()? {
                        if obj.contains_key(&key) {
                            return Err(de::Error::custom(format!("duplicate key: {key}")));
                        }
                        let Strict(x) = map.next_value()?;
                        obj.insert(key, x);
                    }
                    Ok(Strict(Value::Object(obj)))
                }
            }
            d.deserialize_any(V)
        }
    }
    let mut decoder = serde_json::Deserializer::from_slice(bytes);
    let Strict(value) = Strict::deserialize(&mut decoder)?;
    decoder.end()?;
    Ok(value)
}

pub fn mutating(method: &str) -> bool {
    matches!(
        method,
        "upload.begin"
            | "worker.extend"
            | "worker.shutdown"
            | "upload.chunk"
            | "upload.finish"
            | "binder.run"
            | "binder.save"
            | "batch.run"
            | "batch.validate"
            | "batch.create"
            | "batch.cancel"
            | "job.cancel"
            | "annotation.put"
            | "local.upload"
            | "library.product_create"
            | "library.create"
            | "library.structure_attach"
            | "library.structure_visibility"
            | "library.edit"
            | "library.undo"
            | "library.redo"
    )
}
pub fn allowed(method: &str) -> bool {
    METHODS.contains(&method)
}

fn validate_worker_receipt(method: &str, params: &Value, result: &Value) -> Result<(), RpcError> {
    if !matches!(
        method,
        "worker.extend" | "worker.shutdown" | "worker.control_get"
    ) {
        return Ok(());
    }
    let control_id = result["control_id"]
        .as_str()
        .filter(|id| !id.is_empty() && id.len() <= 200);
    let envelope =
        control_id.is_some() && matches!(result["state"].as_str(), Some("pending" | "complete"));
    let identity = if method == "worker.control_get" {
        result["control_id"] == params["control_id"]
    } else {
        result["request_key"] == params["request_key"] && result["target"] == params["target"]
    };
    if !envelope || !identity {
        return Err(RpcError::new("protocol","The worker command receipt did not match its exact saved identity. Recover the same request.").uncertain(mutating(method)));
    }
    Ok(())
}

pub trait Backend: Send + Sync {
    fn call(&self, method: &str, params: Value) -> Result<Value, RpcError>;
    fn library(&self) -> Result<Value, RpcError>;
}

#[derive(Clone)]
pub struct Client {
    connection: Connection,
    state: PathBuf,
    ssh: OsString,
    timeout: Duration,
}
impl Client {
    pub fn new(connection: Connection, state: &Path) -> Result<Self, RpcError> {
        connection.validate()?;
        private_dir(state)?;
        if connection.host == HEAD && connection.port == 22 {
            atomic_bytes(&state.join("known_hosts"), HOST_PIN.as_bytes())?;
        }
        Ok(Self {
            connection,
            state: state.to_owned(),
            ssh: std::env::var_os("BIO_SSH").unwrap_or_else(|| "ssh".into()),
            timeout: Duration::from_secs(75),
        })
    }
    fn ssh_args(&self, remote: &str) -> Result<Vec<OsString>, RpcError> {
        let mut args: Vec<OsString> = [
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=12",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ClearAllForwardings=yes",
        ]
        .iter()
        .map(OsString::from)
        .collect();
        if self.connection.host == HEAD && self.connection.port == 22 {
            args.extend([
                "-o".into(),
                format!(
                    "UserKnownHostsFile=\"{}\"",
                    self.state
                        .join("known_hosts")
                        .to_string_lossy()
                        .replace('\\', "\\\\")
                        .replace('"', "\\\"")
                        .replace('%', "%%")
                )
                .into(),
            ]);
        }
        if !self.connection.key_path.is_empty() {
            args.extend([
                "-o".into(),
                "IdentitiesOnly=yes".into(),
                "-i".into(),
                self.connection.expanded_key()?.into_os_string(),
            ]);
        }
        args.extend([
            "-p".into(),
            self.connection.port.to_string().into(),
            "-l".into(),
            self.connection.user.clone().into(),
            "--".into(),
            self.connection.host.clone().into(),
            remote.into(),
        ]);
        Ok(args)
    }
    fn run(&self, remote: &str, input: Vec<u8>, uncertain: bool) -> Result<Vec<u8>, RpcError> {
        let mut command = Command::new(&self.ssh);
        command
            .args(self.ssh_args(remote)?)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command
            .spawn()
            .map_err(|e| RpcError::new("ssh", format!("Could not start SSH: {e}")))?;
        let pid = child.id();
        let mut stdin = child.stdin.take().unwrap();
        let (write_tx, write_rx) = mpsc::channel();
        thread::spawn(move || {
            let result = stdin.write_all(&input).and_then(|_| stdin.flush());
            drop(stdin);
            let _ = write_tx.send(result);
        });
        let overflow = Arc::new(AtomicBool::new(false));
        let stdout = bounded_reader(child.stdout.take().unwrap(), MAX_WIRE, overflow.clone());
        let stderr = bounded_reader(
            child.stderr.take().unwrap(),
            16 * 1024,
            Arc::new(AtomicBool::new(false)),
        );
        let start = Instant::now();
        let status = loop {
            if overflow.load(Ordering::Relaxed) || start.elapsed() >= self.timeout {
                kill_owned(&mut child, pid);
                let message = if overflow.load(Ordering::Relaxed) {
                    "SSH response exceeded the 2 MiB protocol limit."
                } else {
                    "SSH request timed out. The head may have accepted a submitted operation; use Recover to check the same request."
                };
                return Err(RpcError::new("transport", message).uncertain(uncertain));
            }
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) => thread::sleep(Duration::from_millis(20)),
                Err(e) => {
                    kill_owned(&mut child, pid);
                    return Err(RpcError::new("ssh", e.to_string()).uncertain(uncertain));
                }
            }
        };
        let bytes = match stdout.recv_timeout(Duration::from_secs(1)) {
            Ok(value) => {
                value.map_err(|e| RpcError::new("ssh", e.to_string()).uncertain(uncertain))?
            }
            Err(_) => {
                kill_owned(&mut child, pid);
                return Err(
                    RpcError::new("ssh", "SSH closed without a complete response.")
                        .uncertain(uncertain),
                );
            }
        };
        let errors = stderr
            .recv_timeout(Duration::from_millis(100))
            .ok()
            .and_then(Result::ok)
            .unwrap_or_default();
        if !status.success() {
            let detail = String::from_utf8_lossy(&errors);
            let detail = detail.trim();
            return Err(RpcError::new(
                "ssh",
                if detail.is_empty() {
                    format!("SSH exited with {status}.")
                } else {
                    format!(
                        "SSH connection failed: {}",
                        detail.chars().take(4000).collect::<String>()
                    )
                },
            )
            .uncertain(uncertain));
        }
        if bytes.len() > MAX_WIRE {
            return Err(
                RpcError::new("protocol", "SSH response exceeded the protocol limit.")
                    .uncertain(uncertain),
            );
        }
        if let Ok(Err(error)) = write_rx.try_recv() {
            return Err(RpcError::new(
                "ssh",
                format!("SSH request could not be sent completely: {error}"),
            )
            .uncertain(uncertain));
        }
        Ok(bytes)
    }
}
fn bounded_reader<R: Read + Send + 'static>(
    mut reader: R,
    limit: usize,
    overflow: Arc<AtomicBool>,
) -> mpsc::Receiver<std::io::Result<Vec<u8>>> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let result = (|| {
            let mut bytes = Vec::new();
            let mut buf = [0u8; 8192];
            loop {
                let n = reader.read(&mut buf)?;
                if n == 0 {
                    break;
                }
                let keep = n.min((limit + 1).saturating_sub(bytes.len()));
                bytes.extend_from_slice(&buf[..keep]);
                if bytes.len() > limit {
                    overflow.store(true, Ordering::Relaxed);
                }
            }
            Ok(bytes)
        })();
        let _ = tx.send(result);
    });
    rx
}
fn kill_owned(child: &mut std::process::Child, pid: u32) {
    #[cfg(unix)]
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
    #[cfg(not(unix))]
    let _ = pid;
    let _ = child.kill();
    let _ = child.wait();
}
impl Backend for Client {
    fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        if !allowed(method) || !params.is_object() {
            return Err(RpcError::new(
                "request",
                "Unknown RPC method or non-object parameters.",
            ));
        }
        let id = uuid::Uuid::new_v4().to_string();
        let mut input = serde_json::to_vec(&json!({"id":id,"method":method,"params":params}))?;
        input.push(b'\n');
        if input.len() > MAX_WIRE {
            return Err(RpcError::new(
                "request",
                "Request exceeds 2 MiB; upload the input as a file.",
            ));
        }
        let bytes = self.run(REMOTE, input, mutating(method))?;
        let response = strict_json(&bytes).map_err(|e| e.uncertain(mutating(method)))?;
        let obj = response.as_object().ok_or_else(|| {
            RpcError::new("protocol", "Head response is not an object.").uncertain(mutating(method))
        })?;
        if response.get("id").and_then(Value::as_str) != Some(&id)
            || obj.contains_key("result") == obj.contains_key("error")
            || obj.len() != 2
        {
            return Err(RpcError::new(
                "protocol",
                "Head returned an unmatched or malformed response.",
            )
            .uncertain(mutating(method)));
        }
        if let Some(error) = response.get("error") {
            let code = error.get("code").and_then(Value::as_str).ok_or_else(|| {
                RpcError::new("protocol", "Head returned a malformed error.")
                    .uncertain(mutating(method))
            })?;
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    RpcError::new("protocol", "Head returned a malformed error.")
                        .uncertain(mutating(method))
                })?;
            return Err(
                RpcError::new(code, message).uncertain(mutating(method) && code == "internal")
            );
        }
        let result = response["result"].clone();
        if !result.is_object() {
            return Err(
                RpcError::new("protocol", "Head returned a non-object result.")
                    .uncertain(mutating(method)),
            );
        }
        validate_worker_receipt(method, &params, &result)?;
        Ok(result)
    }
    fn library(&self) -> Result<Value, RpcError> {
        self.call("library.list", json!({"limit":500}))
    }
}

fn field_u64(v: &Value, name: &str) -> Result<u64, RpcError> {
    v.get(name)
        .and_then(Value::as_u64)
        .ok_or_else(|| RpcError::new("integrity", format!("Missing or invalid {name}.")))
}
fn valid_hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn file_hash(path: &Path) -> Result<(u64, String), RpcError> {
    let mut f = File::open(path)?;
    let mut hash = Sha256::new();
    let mut size = 0;
    let mut buffer = [0u8; 128 * 1024];
    loop {
        let count = f.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
        size += count as u64;
    }
    Ok((size, format!("{:x}", hash.finalize())))
}
fn chunk_bytes(
    value: &Value,
    id: &str,
    offset: u64,
    size: u64,
    sha: &str,
) -> Result<Vec<u8>, RpcError> {
    let raw = STANDARD
        .decode(
            value
                .get("data_base64")
                .and_then(Value::as_str)
                .ok_or_else(|| RpcError::new("integrity", "Artifact chunk has no data."))?,
        )
        .map_err(|_| RpcError::new("integrity", "Artifact chunk has invalid base64."))?;
    let next = offset
        .checked_add(raw.len() as u64)
        .ok_or_else(|| RpcError::new("integrity", "Artifact offset overflow."))?;
    if value.get("artifact_id").and_then(Value::as_str) != Some(id)
        || field_u64(value, "offset")? != offset
        || field_u64(value, "size")? != size
        || value.get("sha256").and_then(Value::as_str) != Some(sha)
        || field_u64(value, "next_offset")? != next
        || raw.len() > CHUNK
        || next > size
        || value.get("eof").and_then(Value::as_bool) != Some(next == size)
        || (raw.is_empty() && next < size)
    {
        return Err(RpcError::new(
            "integrity",
            "Artifact identity, offset, size, or checksum changed while downloading.",
        ));
    }
    Ok(raw)
}
pub fn download_artifact(
    backend: &dyn Backend,
    cache: &Path,
    id: &str,
    mut progress: impl FnMut(u64, u64),
) -> Result<(PathBuf, Value), RpcError> {
    if id.is_empty()
        || id.len() > 160
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    {
        return Err(RpcError::new("request", "Invalid artifact identifier."));
    }
    // Authorization and current sealed identity are checked even for a local cache hit.
    let first = backend.call(
        "artifact.read",
        json!({"artifact_id":id,"offset":0,"max_bytes":CHUNK}),
    )?;
    let size = field_u64(&first, "size")?;
    let sha = first
        .get("sha256")
        .and_then(Value::as_str)
        .filter(|s| valid_hash(s))
        .ok_or_else(|| RpcError::new("integrity", "Artifact has no valid SHA-256."))?
        .to_owned();
    if size > MAX_ARTIFACT {
        return Err(RpcError::new(
            "integrity",
            "Artifact exceeds the 2 GiB desktop download limit.",
        ));
    }
    let initial = chunk_bytes(&first, id, 0, size, &sha)?;
    private_dir(cache)?;
    let extension = first
        .get("name")
        .and_then(Value::as_str)
        .and_then(|n| Path::new(n).extension())
        .and_then(|s| s.to_str())
        .filter(|s| !s.is_empty() && s.len() <= 12 && s.bytes().all(|b| b.is_ascii_alphanumeric()))
        .unwrap_or("bin")
        .to_ascii_lowercase();
    let destination = cache.join(format!("{sha}.{extension}"));
    if let Ok(meta) = fs::symlink_metadata(&destination) {
        if meta.is_file()
            && !meta.file_type().is_symlink()
            && meta.len() == size
            && file_hash(&destination)? == (size, sha.clone())
        {
            progress(size, size);
            let mut metadata = first;
            metadata.as_object_mut().unwrap().remove("data_base64");
            return Ok((destination, metadata));
        }
        if meta.file_type().is_symlink() || !meta.is_file() {
            return Err(RpcError::new(
                "local_io",
                "Artifact cache entry is not a regular file.",
            ));
        }
    }
    let mut tmp = tempfile::NamedTempFile::new_in(cache)?;
    let mut hasher = Sha256::new();
    let mut raw = initial;
    let mut offset = 0;
    loop {
        tmp.write_all(&raw)?;
        hasher.update(&raw);
        offset += raw.len() as u64;
        progress(offset, size);
        if offset == size {
            break;
        }
        let value = backend.call(
            "artifact.read",
            json!({"artifact_id":id,"offset":offset,"max_bytes":CHUNK}),
        )?;
        raw = chunk_bytes(&value, id, offset, size, &sha)?;
    }
    if format!("{:x}", hasher.finalize()) != sha {
        return Err(RpcError::new(
            "integrity",
            "Downloaded artifact does not match the head's SHA-256.",
        ));
    }
    tmp.as_file().sync_all()?;
    tmp.persist(&destination)
        .map_err(|e| RpcError::from(e.error))?;
    let mut metadata = first;
    metadata.as_object_mut().unwrap().remove("data_base64");
    Ok((destination, metadata))
}

/// Stream a pinned library attachment, checking the record receipt and every chunk.
pub fn download_library_attachment(
    backend: &dyn Backend,
    cache: &Path,
    reference: &str,
    name: &str,
    receipt: &Value,
    mut progress: impl FnMut(u64, u64),
) -> Result<(PathBuf, Value), RpcError> {
    const LIBRARY_CHUNK: usize = 262_144;
    let size = field_u64(receipt, "bytes")?;
    let sha = text_hash(receipt)?;
    if size > MAX_UPLOAD
        || reference.is_empty()
        || !reference.contains('@')
        || name.is_empty()
        || name.len() > 255
        || name.contains(['/', '\\'])
        || matches!(name, "." | "..")
        || name.chars().any(char::is_control)
    {
        return Err(RpcError::new(
            "request",
            "Choose a pinned library attachment no larger than 256 MiB.",
        ));
    }
    private_dir(cache)?;
    let destination = cache.join(sha);
    if fs::symlink_metadata(&destination).is_ok_and(|m| !m.is_file() || m.file_type().is_symlink())
    {
        return Err(RpcError::new(
            "local_io",
            "Library cache entry is not a regular file.",
        ));
    }
    let mut tmp = tempfile::NamedTempFile::new_in(cache)?;
    let mut hasher = Sha256::new();
    let mut offset = 0;
    loop {
        let value = backend.call(
            "library.attachment",
            json!({
                "ref":reference,"name":name,"offset":offset,"length":LIBRARY_CHUNK
            }),
        )?;
        let raw = STANDARD
            .decode(
                value["data_b64"]
                    .as_str()
                    .ok_or_else(|| RpcError::new("integrity", "Missing attachment bytes."))?,
            )
            .map_err(|_| RpcError::new("integrity", "Invalid attachment encoding."))?;
        let next = offset + raw.len() as u64;
        if value["ref"].as_str() != Some(reference)
            || value["name"].as_str() != Some(name)
            || field_u64(&value, "offset")? != offset
            || field_u64(&value, "next_offset")? != next
            || field_u64(&value, "size")? != size
            || value["sha256"].as_str() != Some(sha)
            || value["eof"].as_bool() != Some(next == size)
            || next > size
            || raw.len() > LIBRARY_CHUNK
            || (raw.is_empty() && next < size)
        {
            return Err(RpcError::new(
                "integrity",
                "Library attachment identity, size, or offset differs from its pinned record.",
            ));
        }
        tmp.write_all(&raw)?;
        hasher.update(&raw);
        offset = next;
        progress(offset, size);
        if offset == size {
            break;
        }
    }
    if format!("{:x}", hasher.finalize()) != sha {
        return Err(RpcError::new(
            "integrity",
            "Library attachment bytes do not match their pinned SHA-256.",
        ));
    }
    tmp.as_file().sync_all()?;
    tmp.persist(&destination)
        .map_err(|e| RpcError::from(e.error))?;
    Ok((
        destination,
        json!({"ref":reference,"name":name,"size":size,"sha256":sha}),
    ))
}

fn text_hash(receipt: &Value) -> Result<&str, RpcError> {
    receipt["sha256"]
        .as_str()
        .filter(|sha| valid_hash(sha))
        .ok_or_else(|| {
            RpcError::new(
                "integrity",
                "Library attachment receipt has no valid SHA-256.",
            )
        })
}

const GALLERY_CHUNK: usize = 262_144;
// The gallery protocol and molecular parser both cap structure sources at 32 MiB.
const MAX_GALLERY_STRUCTURE: u64 = 32 * 1024 * 1024;
const MAX_THUMBNAIL: u64 = 2 * 1024 * 1024;

#[derive(Clone, Copy)]
enum GalleryFile {
    Structure,
    Thumbnail,
}

fn pinned_protein(reference: &str) -> bool {
    let Some((id, revision)) = reference
        .strip_prefix("construct:")
        .and_then(|value| value.split_once('@'))
    else {
        return false;
    };
    !id.is_empty()
        && id.len() <= 64
        && id.as_bytes()[0].is_ascii_lowercase()
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b))
        && revision
            .parse::<u64>()
            .is_ok_and(|n| n > 0 && n.to_string() == revision)
}

fn gallery_protein_context(receipt: &Value) -> bool {
    let protein = &receipt["protein"];
    if protein["ref"] != receipt["source_ref"]
        || protein["sequence_sha256"] != receipt["source_sequence_sha256"]
        || !valid_hash(protein["sha256"].as_str().unwrap_or(""))
    {
        return false;
    }
    match protein["derivation_kind"].as_str() {
        Some("explicit") => protein.get("parent") == Some(&Value::Null),
        Some("derived") => {
            let parent = &protein["parent"];
            pinned_protein(parent["ref"].as_str().unwrap_or(""))
                && valid_hash(parent["sha256"].as_str().unwrap_or(""))
                && matches!(parent["molecule_type"].as_str(), Some("dna" | "rna"))
                && parent
                    .get("molecular_form")
                    .is_some_and(|v| v.is_null() || v.is_string())
        }
        _ => false,
    }
}

fn gallery_chunk(value: &Value, identity: &Value, offset: u64) -> Result<Vec<u8>, RpcError> {
    let encoded = value["data_base64"]
        .as_str()
        .filter(|s| s.len() <= GALLERY_CHUNK.div_ceil(3) * 4)
        .ok_or_else(|| RpcError::new("integrity", "Missing or oversized gallery chunk."))?;
    let raw = STANDARD
        .decode(encoded)
        .map_err(|_| RpcError::new("integrity", "Invalid gallery chunk encoding."))?;
    let size = field_u64(identity, "size")?;
    let next = offset
        .checked_add(raw.len() as u64)
        .ok_or_else(|| RpcError::new("integrity", "Gallery offset overflow."))?;
    if identity
        .as_object()
        .unwrap()
        .iter()
        .any(|(key, expected)| value.get(key) != Some(expected))
        || field_u64(value, "offset")? != offset
        || field_u64(value, "next_offset")? != next
        || raw.len() > GALLERY_CHUNK
        || next > size
        || value["eof"].as_bool() != Some(next == size)
        || (raw.is_empty() && next < size)
    {
        return Err(RpcError::new(
            "integrity",
            "Gallery source, renderer, checksum, size or offset changed; refresh the gallery.",
        ));
    }
    Ok(raw)
}

/// Download only after the head confirms the pinned gallery association, even on cache hits.
pub fn download_library_structure(
    backend: &dyn Backend,
    cache: &Path,
    reference: &str,
    entry_id: &str,
    receipt: &Value,
    progress: impl FnMut(u64, u64),
) -> Result<(PathBuf, Value), RpcError> {
    download_gallery_file(
        backend,
        cache,
        (reference, entry_id),
        receipt,
        GalleryFile::Structure,
        progress,
    )
}

pub fn download_library_structure_thumbnail(
    backend: &dyn Backend,
    cache: &Path,
    reference: &str,
    entry_id: &str,
    receipt: &Value,
    progress: impl FnMut(u64, u64),
) -> Result<(PathBuf, Value), RpcError> {
    download_gallery_file(
        backend,
        cache,
        (reference, entry_id),
        receipt,
        GalleryFile::Thumbnail,
        progress,
    )
}

fn download_gallery_file(
    backend: &dyn Backend,
    cache: &Path,
    (reference, entry_id): (&str, &str),
    receipt: &Value,
    kind: GalleryFile,
    mut progress: impl FnMut(u64, u64),
) -> Result<(PathBuf, Value), RpcError> {
    let size = field_u64(receipt, "size")?;
    let sha = text_hash(receipt)?;
    if !pinned_protein(reference)
        || entry_id.is_empty()
        || entry_id.len() > 160
        || !entry_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        || receipt["entry_id"].as_str() != Some(entry_id)
        || size == 0
    {
        return Err(RpcError::new(
            "request",
            "Choose a pinned protein structure with a valid receipt.",
        ));
    }
    let mut identity = json!({"ref":reference,"entry_id":entry_id,"sha256":sha,"size":size});
    let mut params = json!({"ref":reference,"entry_id":entry_id,"offset":0,"length":GALLERY_CHUNK});
    let (method, filename) = match kind {
        GalleryFile::Structure => {
            let source_ref = receipt["source_ref"].as_str().unwrap_or("");
            let format = receipt["format"].as_str().unwrap_or("");
            if size > MAX_GALLERY_STRUCTURE
                || !pinned_protein(source_ref)
                || source_ref.split('@').next() != reference.split('@').next()
                || !valid_hash(receipt["source_sequence_sha256"].as_str().unwrap_or(""))
                || !matches!(format, "pdb" | "cif" | "mmcif")
                || !gallery_protein_context(receipt)
            {
                return Err(RpcError::new(
                    "request",
                    "Choose a PDB/mmCIF structure no larger than 32 MiB with its exact source protein receipt.",
                ));
            }
            for key in ["source_ref", "source_sequence_sha256", "format", "protein"] {
                identity[key] = receipt[key].clone();
            }
            ("library.structure_read", format!("{sha}.{format}"))
        }
        GalleryFile::Thumbnail => {
            if size > MAX_THUMBNAIL
                || receipt["schema"] != 1
                || receipt["ref"].as_str() != Some(reference)
                || receipt["state"] != "ready"
                || receipt["width"] != 640
                || receipt["height"] != 480
                || receipt["style"] != "cartoon"
                || ["source_sha256", "renderer_fingerprint", "cache_key"]
                    .iter()
                    .any(|key| !valid_hash(receipt[key].as_str().unwrap_or("")))
            {
                return Err(RpcError::new(
                    "request",
                    "Choose a ready gallery preview no larger than 2 MiB with its source and renderer receipt.",
                ));
            }
            for key in [
                "schema",
                "state",
                "source_sha256",
                "renderer_fingerprint",
                "cache_key",
                "width",
                "height",
                "style",
            ] {
                identity[key] = receipt[key].clone();
            }
            params["cache_key"] = receipt["cache_key"].clone();
            params["sha256"] = receipt["sha256"].clone();
            (
                "library.structure_thumbnail_read",
                format!("{}-{sha}.png", receipt["cache_key"].as_str().unwrap()),
            )
        }
    };
    let first = backend.call(method, params.clone())?;
    let initial = gallery_chunk(&first, &identity, 0)?;
    if matches!(kind, GalleryFile::Structure) {
        let name = first["name"]
            .as_str()
            .filter(|name| !name.is_empty() && name.len() <= 1024 && !name.chars().any(|c| c < ' '))
            .ok_or_else(|| RpcError::new("integrity", "Structure response has no valid name."))?;
        identity["name"] = json!(name);
    }
    let mut metadata = first;
    metadata.as_object_mut().unwrap().remove("data_base64");
    private_dir(cache)?;
    let destination = cache.join(filename);
    if let Ok(meta) = fs::symlink_metadata(&destination) {
        if !meta.is_file() || meta.file_type().is_symlink() {
            return Err(RpcError::new(
                "local_io",
                "Gallery cache entry is not a regular file.",
            ));
        }
        if meta.len() == size && file_hash(&destination)? == (size, sha.to_owned()) {
            let mut prefix = vec![0; initial.len()];
            File::open(&destination)?.read_exact(&mut prefix)?;
            if prefix != initial {
                return Err(RpcError::new(
                    "integrity",
                    "The authorized gallery bytes differ from their checksum-verified cache entry.",
                ));
            }
            progress(size, size);
            return Ok((destination, metadata));
        }
    }
    let mut tmp = tempfile::NamedTempFile::new_in(cache)?;
    let mut hasher = Sha256::new();
    let mut raw = initial;
    let mut offset = 0;
    loop {
        tmp.write_all(&raw)?;
        hasher.update(&raw);
        offset += raw.len() as u64;
        progress(offset, size);
        if offset == size {
            break;
        }
        params["offset"] = json!(offset);
        raw = gallery_chunk(&backend.call(method, params.clone())?, &identity, offset)?;
    }
    if format!("{:x}", hasher.finalize()) != sha {
        return Err(RpcError::new(
            "integrity",
            "Downloaded gallery bytes do not match their pinned SHA-256.",
        ));
    }
    tmp.as_file().sync_all()?;
    tmp.persist(&destination)
        .map_err(|error| RpcError::from(error.error))?;
    Ok((destination, metadata))
}

/// Hydrate paginated results without silently omitting jobs or artifacts.
pub fn workflow_call(
    backend: &dyn Backend,
    method: &str,
    params: Value,
) -> Result<Value, RpcError> {
    let mut result = backend.call(method, params.clone())?;
    if method == "batch.list" && params.get("cursor").is_none() {
        collect_pages(backend, method, &params, &mut result, "batches")?;
    }
    if method == "job.artifacts" && params.get("cursor").is_none() {
        collect_pages(backend, method, &params, &mut result, "artifacts")?;
    }
    if method == "job.get" {
        hydrate_job(backend, &mut result)?;
    }
    if method == "batch.get"
        && let Some(jobs) = result.get_mut("jobs").and_then(Value::as_array_mut)
    {
        for job in jobs.iter_mut() {
            let id = job
                .get("job_id")
                .and_then(Value::as_str)
                .ok_or_else(|| RpcError::new("protocol", "Batch job has no identifier."))?
                .to_owned();
            *job = backend.call("job.get", json!({"job_id":id}))?;
            hydrate_job(backend, job)?;
        }
    }
    Ok(result)
}
fn hydrate_job(backend: &dyn Backend, job: &mut Value) -> Result<(), RpcError> {
    let have = job
        .get("artifacts")
        .and_then(Value::as_array)
        .map_or(0, Vec::len) as u64;
    if job
        .get("artifact_count")
        .and_then(Value::as_u64)
        .unwrap_or(have)
        > have
    {
        let params =
            json!({"job_id":job.get("job_id").cloned().unwrap_or(Value::Null),"limit":100});
        let mut result = backend.call("job.artifacts", params.clone())?;
        collect_pages(backend, "job.artifacts", &params, &mut result, "artifacts")?;
        job["artifacts"] = result["artifacts"].clone();
    }
    Ok(())
}
fn collect_pages(
    backend: &dyn Backend,
    method: &str,
    params: &Value,
    result: &mut Value,
    field: &str,
) -> Result<(), RpcError> {
    let mut all = result
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| RpcError::new("protocol", "Paginated response has no item list."))?
        .clone();
    let mut cursor = result.get("next_cursor").cloned().unwrap_or(Value::Null);
    let mut seen = HashSet::new();
    while !cursor.is_null() {
        let key = cursor
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| RpcError::new("protocol", "Invalid pagination cursor."))?;
        if !seen.insert(key.to_owned()) || seen.len() > 1000 {
            return Err(RpcError::new(
                "protocol",
                "Repeated or excessive pagination cursor.",
            ));
        }
        let mut next = params.clone();
        next["cursor"] = cursor;
        next["limit"] = json!(100);
        let page = backend.call(method, next)?;
        all.extend(
            page.get(field)
                .and_then(Value::as_array)
                .ok_or_else(|| RpcError::new("protocol", "Paginated response has no item list."))?
                .iter()
                .cloned(),
        );
        if all.len() > 100_000 {
            return Err(RpcError::new(
                "protocol",
                "Paginated response exceeded the desktop item limit.",
            ));
        }
        cursor = page.get("next_cursor").cloned().unwrap_or(Value::Null);
    }
    result[field] = Value::Array(all);
    result["next_cursor"] = Value::Null;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn worker_receipts_reject_retargeted_or_incomplete_acknowledgements_as_uncertain() {
        let params = json!({"request_key":"one","target":{"session_id":"original"}});
        let valid = json!({"control_id":"control-one","request_key":"one","target":params["target"],"state":"pending"});
        assert!(validate_worker_receipt("worker.extend", &params, &valid).is_ok());
        for bad in [
            json!({}),
            json!({"control_id":"control-one","request_key":"one","target":{"session_id":"replacement"},"state":"pending"}),
        ] {
            assert!(
                validate_worker_receipt("worker.extend", &params, &bad)
                    .unwrap_err()
                    .uncertain
            );
        }
        assert!(
            validate_worker_receipt(
                "worker.control_get",
                &json!({"control_id":"different"}),
                &valid
            )
            .is_err()
        );
    }
    use std::sync::Mutex;

    struct GalleryMock {
        receipt: Value,
        data: Vec<u8>,
        calls: Mutex<Vec<(String, Value)>>,
        tamper: Option<(&'static str, Value)>,
        from_offset: u64,
        denied: bool,
    }
    impl GalleryMock {
        fn new(thumbnail: bool) -> Self {
            let data = vec![73; GALLERY_CHUNK + 19];
            let mut receipt = json!({"entry_id":"m-0123456789abcdef0123456789abcdef","size":data.len(),
                "sha256":format!("{:x}",Sha256::digest(&data))});
            let fields = if thumbnail {
                json!({"schema":1,"ref":"construct:example@3","source_sha256":"a".repeat(64),
                    "renderer_fingerprint":"b".repeat(64),"cache_key":"c".repeat(64),
                    "state":"ready","width":640,"height":480,"style":"cartoon"})
            } else {
                json!({"source_ref":"construct:example@2","source_sequence_sha256":"d".repeat(64),
                    "format":"mmcif","label":"Retained model", "protein":{
                        "ref":"construct:example@2","sha256":"e".repeat(64),
                        "sequence_sha256":"d".repeat(64),"derivation_kind":"derived",
                        "parent":{"ref":"construct:plasmid@1","sha256":"f".repeat(64),
                            "molecule_type":"dna","molecular_form":"plasmid"}}})
            };
            receipt
                .as_object_mut()
                .unwrap()
                .extend(fields.as_object().unwrap().clone());
            Self {
                receipt,
                data,
                calls: Mutex::default(),
                tamper: None,
                from_offset: 1,
                denied: false,
            }
        }
        fn download(&self, cache: &Path, thumbnail: bool) -> Result<(PathBuf, Value), RpcError> {
            download_gallery_file(
                self,
                cache,
                (
                    "construct:example@3",
                    self.receipt["entry_id"].as_str().unwrap(),
                ),
                &self.receipt,
                if thumbnail {
                    GalleryFile::Thumbnail
                } else {
                    GalleryFile::Structure
                },
                |_, _| {},
            )
        }
    }
    impl Backend for GalleryMock {
        fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
            self.calls
                .lock()
                .unwrap()
                .push((method.into(), params.clone()));
            if self.denied {
                return Err(RpcError::new("not_found", "Association unavailable"));
            }
            assert!(matches!(
                method,
                "library.structure_read" | "library.structure_thumbnail_read"
            ));
            assert_eq!(params["length"], GALLERY_CHUNK);
            if method == "library.structure_thumbnail_read" {
                assert_eq!(params["cache_key"], self.receipt["cache_key"]);
                assert_eq!(params["sha256"], self.receipt["sha256"]);
            }
            let offset = params["offset"].as_u64().unwrap() as usize;
            let next = (offset + GALLERY_CHUNK).min(self.data.len());
            let mut result = self.receipt.clone();
            for (key, value) in json!({"ref":params["ref"],"name":"Retained model","offset":offset,
                "next_offset":next,"eof":next==self.data.len(),"data_base64":STANDARD.encode(&self.data[offset..next])}).as_object().unwrap() {
                result[key] = value.clone();
            }
            if offset as u64 >= self.from_offset
                && let Some((key, value)) = &self.tamper
            {
                result[key] = value.clone();
            }
            Ok(result)
        }
        fn library(&self) -> Result<Value, RpcError> {
            unreachable!()
        }
    }

    #[test]
    fn gallery_downloads_reauthorize_and_verify_cached_source_bytes() {
        for thumbnail in [false, true] {
            let cache = tempfile::tempdir().unwrap();
            let mut backend = GalleryMock::new(thumbnail);
            let (path, metadata) = backend.download(cache.path(), thumbnail).unwrap();
            assert_eq!(fs::read(&path).unwrap(), backend.data);
            assert_eq!(metadata["ref"], "construct:example@3");
            assert_eq!(metadata["entry_id"], backend.receipt["entry_id"]);
            if !thumbnail {
                assert_eq!(metadata["protein"], backend.receipt["protein"]);
            }
            assert!(metadata.get("data_base64").is_none());
            assert_eq!(backend.calls.lock().unwrap().len(), 2);
            backend.download(cache.path(), thumbnail).unwrap();
            assert_eq!(
                backend.calls.lock().unwrap().len(),
                3,
                "cache reuse still authorizes offset zero"
            );
            backend.denied = true;
            assert_eq!(
                backend.download(cache.path(), thumbnail).unwrap_err().code,
                "not_found"
            );
            backend.denied = false;
            backend.from_offset = 0;
            backend.tamper = Some((
                if thumbnail {
                    "renderer_fingerprint"
                } else {
                    "protein"
                },
                Value::Null,
            ));
            assert_eq!(
                backend.download(cache.path(), thumbnail).unwrap_err().code,
                "integrity"
            );
            backend.tamper = Some((
                "data_base64",
                json!(STANDARD.encode(vec![99; GALLERY_CHUNK])),
            ));
            assert_eq!(
                backend.download(cache.path(), thumbnail).unwrap_err().code,
                "integrity"
            );
            assert_eq!(
                fs::read(&path).unwrap(),
                backend.data,
                "failed authorization bytes cannot replace a valid cache"
            );
            backend.tamper = None;
            fs::write(&path, vec![0; backend.data.len()]).unwrap();
            backend.download(cache.path(), thumbnail).unwrap();
            assert_eq!(
                fs::read(&path).unwrap(),
                backend.data,
                "corrupt regular cache files are replaced only after a complete verified download"
            );
        }
    }

    #[test]
    fn gallery_keeps_full_unicode_labels_and_historical_parent_identity() {
        let cache = tempfile::tempdir().unwrap();
        let mut backend = GalleryMock::new(false);
        let label = "🧬".repeat(200);
        backend.from_offset = 0;
        backend.tamper = Some(("name", json!(label)));
        let (_, metadata) = backend.download(cache.path(), false).unwrap();
        assert_eq!(metadata["name"], label);
        assert_eq!(metadata["protein"]["parent"]["ref"], "construct:plasmid@1");
        let mut changed = backend.receipt["protein"].clone();
        changed["parent"]["ref"] = json!("construct:plasmid@2");
        backend.tamper = Some(("protein", changed));
        assert_eq!(
            backend.download(cache.path(), false).unwrap_err().code,
            "integrity",
            "A parent updated after the structure was associated must not silently replace its historical backlink"
        );
    }

    #[test]
    fn gallery_streams_reject_source_renderer_and_chunk_changes_without_publishing() {
        for thumbnail in [false, true] {
            let mut fields = vec![
                ("ref", json!("construct:other@3")),
                ("entry_id", json!("p-different")),
                ("sha256", json!("f".repeat(64))),
                ("size", json!(1)),
                ("offset", json!(0)),
                ("next_offset", json!(u64::MAX)),
                ("eof", json!(false)),
                ("data_base64", json!(STANDARD.encode([0; 19]))),
            ];
            if thumbnail {
                fields.extend([
                    ("source_sha256", json!("e".repeat(64))),
                    ("renderer_fingerprint", json!("e".repeat(64))),
                    ("cache_key", json!("e".repeat(64))),
                    ("width", json!(1)),
                    ("height", json!(1)),
                    ("style", json!("spheres")),
                    ("state", json!("rendering")),
                ]);
            } else {
                fields.extend([
                    ("source_ref", json!("construct:example@1")),
                    ("source_sequence_sha256", json!("e".repeat(64))),
                    ("format", json!("pdb")),
                    ("name", json!("Another model")),
                    ("protein", Value::Null),
                ]);
            }
            for (field, value) in fields {
                let cache = tempfile::tempdir().unwrap();
                let mut backend = GalleryMock::new(thumbnail);
                backend.tamper = Some((field, value));
                assert_eq!(
                    backend.download(cache.path(), thumbnail).unwrap_err().code,
                    "integrity",
                    "{field}"
                );
                assert_eq!(
                    fs::read_dir(cache.path()).unwrap().count(),
                    0,
                    "failed {field} must leave no partial file"
                );
            }
        }
    }

    #[test]
    fn gallery_rejects_invalid_receipts_before_network_or_cache_access() {
        for thumbnail in [false, true] {
            let cache = tempfile::tempdir().unwrap();
            let mut cases = vec![
                ("size", json!(0)),
                ("sha256", json!("invalid")),
                ("entry_id", json!("../entry")),
            ];
            cases.push((
                "size",
                json!(if thumbnail {
                    MAX_THUMBNAIL + 1
                } else {
                    MAX_GALLERY_STRUCTURE + 1
                }),
            ));
            if thumbnail {
                cases.extend([
                    ("state", json!("queued")),
                    ("renderer_fingerprint", Value::Null),
                    ("source_sha256", Value::Null),
                    ("cache_key", json!("../cache")),
                    ("ref", json!("construct:example@2")),
                ]);
            } else {
                cases.extend([
                    ("source_ref", json!("construct:other@2")),
                    ("source_sequence_sha256", Value::Null),
                    ("format", json!("exe")),
                    ("protein", Value::Null),
                ]);
            }
            for (field, value) in cases {
                let mut backend = GalleryMock::new(thumbnail);
                backend.receipt[field] = value;
                assert!(
                    backend.download(cache.path(), thumbnail).is_err(),
                    "{field}"
                );
                assert!(backend.calls.lock().unwrap().is_empty(), "{field}");
                assert_eq!(fs::read_dir(cache.path()).unwrap().count(), 0);
            }
            let backend = GalleryMock::new(thumbnail);
            for reference in [
                "construct:example",
                "construct:example@0",
                "construct:example@03",
                "project:example@3",
                "construct:../example@3",
            ] {
                assert!(
                    download_gallery_file(
                        &backend,
                        cache.path(),
                        (reference, backend.receipt["entry_id"].as_str().unwrap()),
                        &backend.receipt,
                        if thumbnail {
                            GalleryFile::Thumbnail
                        } else {
                            GalleryFile::Structure
                        },
                        |_, _| {}
                    )
                    .is_err()
                );
            }
            assert!(backend.calls.lock().unwrap().is_empty());
        }
    }

    #[test]
    fn gallery_read_methods_and_durable_mutations_are_explicitly_allowed() {
        for method in [
            "library.structures",
            "library.structure_read",
            "library.structure_links",
            "library.structure_thumbnail",
            "library.structure_thumbnail_read",
        ] {
            assert!(allowed(method));
            assert!(!mutating(method));
        }
        for method in [
            "library.structure_attach",
            "library.structure_visibility",
            "library.create",
        ] {
            assert!(allowed(method));
            assert!(mutating(method));
        }
    }

    struct LibraryMock {
        data: Vec<u8>,
        tamper: Option<&'static str>,
    }
    impl Backend for LibraryMock {
        fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
            assert_eq!(method, "library.attachment");
            let offset = params["offset"].as_u64().unwrap() as usize;
            let end = (offset + params["length"].as_u64().unwrap() as usize).min(self.data.len());
            let mut result = json!({"ref":params["ref"],"name":params["name"],"offset":offset,"next_offset":end,
                "size":self.data.len(),"sha256":format!("{:x}",Sha256::digest(&self.data)),
                "eof":end==self.data.len(),"data_b64":STANDARD.encode(&self.data[offset..end])});
            if offset > 0 {
                match self.tamper {
                    Some("ref") => result["ref"] = json!("construct:other@1"),
                    Some("offset") => result["offset"] = json!(0),
                    Some("bytes") => {
                        result["data_b64"] = json!(STANDARD.encode(vec![99; end - offset]))
                    }
                    Some("sha256") => result["sha256"] = json!("0".repeat(64)),
                    _ => {}
                }
            }
            Ok(result)
        }
        fn library(&self) -> Result<Value, RpcError> {
            unreachable!()
        }
    }

    #[test]
    fn library_attachment_streams_exact_receipt_and_rejects_tampered_continuations() {
        let data = vec![42; 262_144 + 17];
        let receipt = json!({"bytes":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))});
        for tamper in [
            None,
            Some("ref"),
            Some("offset"),
            Some("bytes"),
            Some("sha256"),
        ] {
            let cache = tempfile::tempdir().unwrap();
            let backend = LibraryMock {
                data: data.clone(),
                tamper,
            };
            let mut progress = Vec::new();
            let result = download_library_attachment(
                &backend,
                cache.path(),
                "construct:example@3",
                "source.dna",
                &receipt,
                |done, total| progress.push((done, total)),
            );
            if tamper.is_none() {
                let (path, metadata) = result.unwrap();
                assert_eq!(fs::read(path).unwrap(), data);
                assert_eq!(metadata["ref"], "construct:example@3");
                assert_eq!(
                    progress.last(),
                    Some(&(data.len() as u64, data.len() as u64))
                );
            } else {
                assert_eq!(result.unwrap_err().code, "integrity");
                assert_eq!(fs::read_dir(cache.path()).unwrap().count(), 0);
            }
        }
    }

    #[test]
    fn library_attachment_rejects_floating_refs_paths_and_oversize_receipts() {
        let backend = LibraryMock {
            data: vec![],
            tamper: None,
        };
        let cache = tempfile::tempdir().unwrap();
        let receipt = json!({"bytes":0,"sha256":format!("{:x}",Sha256::digest([]))});
        for (reference, name) in [
            ("construct:example", "source.dna"),
            ("construct:example@1", "../source.dna"),
            ("construct:example@1", "a\\b"),
        ] {
            assert!(
                download_library_attachment(
                    &backend,
                    cache.path(),
                    reference,
                    name,
                    &receipt,
                    |_, _| {}
                )
                .is_err()
            );
        }
        let big = json!({"bytes":MAX_UPLOAD+1,"sha256":receipt["sha256"]});
        assert!(
            download_library_attachment(
                &backend,
                cache.path(),
                "construct:example@1",
                "source.dna",
                &big,
                |_, _| {}
            )
            .is_err()
        );
    }
    struct Mock {
        calls: Mutex<Vec<(String, Value)>>,
        data: Vec<u8>,
        damage: bool,
        reject: bool,
    }
    impl Backend for Mock {
        fn call(&self, method: &str, p: Value) -> Result<Value, RpcError> {
            self.calls.lock().unwrap().push((method.into(), p.clone()));
            if self.reject {
                return Err(RpcError::new("not_found", "No access"));
            }
            let start = p["offset"].as_u64().unwrap_or(0) as usize;
            let end = (start + CHUNK).min(self.data.len());
            let mut bytes = self.data[start..end].to_vec();
            if self.damage && !bytes.is_empty() {
                bytes[0] ^= 1;
            }
            Ok(
                json!({"artifact_id":p["artifact_id"],"offset":start,"next_offset":end,"eof":end==self.data.len(),"size":self.data.len(),"sha256":format!("{:x}",Sha256::digest(&self.data)),"name":"model.cif","media_type":"chemical/x-mmcif","data_base64":STANDARD.encode(bytes)}),
            )
        }
        fn library(&self) -> Result<Value, RpcError> {
            unreachable!()
        }
    }
    #[test]
    fn profiles_reject_shell_injection_and_keep_keys_as_arguments() {
        for host in [
            "-oProxyCommand=bad",
            "host;touch /tmp/x",
            "$(id)",
            "host\nother",
            "",
        ] {
            let c = Connection {
                host: host.into(),
                ..Default::default()
            };
            assert!(c.validate().is_err());
        }
        let dir = tempfile::tempdir().unwrap();
        let c = Connection {
            key_path: "/tmp/key with 'quotes' $(literal)".into(),
            ..Default::default()
        };
        let client = Client::new(c, dir.path()).unwrap();
        let args = client.ssh_args(REMOTE).unwrap();
        assert!(args.contains(&OsString::from("/tmp/key with 'quotes' $(literal)")));
        assert_eq!(args.last().unwrap(), REMOTE);
    }
    #[test]
    fn strict_protocol_rejects_duplicates_and_trailing_data() {
        for b in [
            br#"{"id":"a","id":"b"}"#.as_slice(),
            br#"{"x":{"y":1,"y":2}}"#,
            b"{}\n{}",
            b"NaN",
        ] {
            assert!(strict_json(b).is_err());
        }
        assert_eq!(
            strict_json(b"{\"valid\":[1,true,null]}\n").unwrap()["valid"][0],
            1
        );
    }
    #[test]
    fn verifies_multichunk_download_and_reauthorizes_cache() {
        let dir = tempfile::tempdir().unwrap();
        let mock = Mock {
            calls: Mutex::default(),
            data: vec![72; CHUNK + 17],
            damage: false,
            reject: false,
        };
        let (file, meta) = download_artifact(&mock, dir.path(), "abc", |_, _| {}).unwrap();
        assert_eq!(fs::read(&file).unwrap(), mock.data);
        assert_eq!(meta["size"], CHUNK + 17);
        assert_eq!(mock.calls.lock().unwrap().len(), 2);
        download_artifact(&mock, dir.path(), "abc", |_, _| {}).unwrap();
        assert_eq!(mock.calls.lock().unwrap().len(), 3);
        let denied = Mock {
            calls: Mutex::default(),
            data: mock.data.clone(),
            damage: false,
            reject: true,
        };
        assert!(download_artifact(&denied, dir.path(), "abc", |_, _| {}).is_err());
        fs::write(&file, vec![0; CHUNK + 17]).unwrap();
        download_artifact(&mock, dir.path(), "abc", |_, _| {}).unwrap();
        assert_eq!(fs::read(file).unwrap(), mock.data);
    }
    #[test]
    fn corrupt_download_never_enters_cache() {
        let dir = tempfile::tempdir().unwrap();
        let mock = Mock {
            calls: Mutex::default(),
            data: vec![72; CHUNK + 17],
            damage: true,
            reject: false,
        };
        assert_eq!(
            download_artifact(&mock, dir.path(), "abc", |_, _| {})
                .unwrap_err()
                .code,
            "integrity"
        );
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 0);
    }
    #[test]
    fn invalid_offsets_or_empty_progress_are_rejected() {
        let sha = "a".repeat(64);
        let v = json!({"artifact_id":"abc","offset":0,"size":10,"sha256":sha,"data_base64":"","next_offset":0,"eof":false});
        assert!(chunk_bytes(&v, "abc", 0, 10, &sha).is_err());
    }
    #[test]
    fn pagination_hydrates_artifacts_and_rejects_repeated_cursors() {
        struct Pages {
            repeated: bool,
        }
        impl Backend for Pages {
            fn call(&self, method: &str, p: Value) -> Result<Value, RpcError> {
                Ok(match method {
                    "batch.get" => {
                        json!({"jobs":[{"job_id":"j","artifacts":[],"artifact_count":2}]})
                    }
                    "job.get" => {
                        json!({"job_id":"j","artifacts":[{"artifact_id":"a"}],"artifact_count":2})
                    }
                    "job.artifacts" if p.get("cursor").is_none() => {
                        json!({"artifacts":[{"artifact_id":"a"}],"next_cursor":"a"})
                    }
                    "job.artifacts" => {
                        json!({"artifacts":[{"artifact_id":"b"}],"next_cursor":if self.repeated{json!("a")}else{Value::Null}})
                    }
                    _ => unreachable!(),
                })
            }
            fn library(&self) -> Result<Value, RpcError> {
                unreachable!()
            }
        }
        let result = workflow_call(
            &Pages { repeated: false },
            "batch.get",
            json!({"batch_id":"b"}),
        )
        .unwrap();
        assert_eq!(result["jobs"][0]["artifacts"].as_array().unwrap().len(), 2);
        assert_eq!(
            workflow_call(
                &Pages { repeated: true },
                "batch.get",
                json!({"batch_id":"b"})
            )
            .unwrap_err()
            .code,
            "protocol"
        );
    }
    #[cfg(unix)]
    #[test]
    fn transport_validates_response_identity_and_times_out_owned_processes() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("fake-ssh");
        let shell = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .map(|p| p.join("sh"))
            .find(|p| p.is_file())
            .expect("test requires POSIX sh in PATH");
        fs::write(
            &script,
            format!(
                "#!{}\ncat >/dev/null\nprintf '%s\\n' '{{\"id\":\"wrong\",\"result\":{{}}}}'\n",
                shell.display()
            ),
        )
        .unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        let mut client = Client::new(Connection::default(), dir.path()).unwrap();
        client.ssh = script.clone().into();
        assert_eq!(
            client.call("catalog", json!({})).unwrap_err().code,
            "protocol"
        );
        fs::write(&script, format!("#!{}\nsleep 30\n", shell.display())).unwrap();
        client.timeout = Duration::from_millis(120);
        let start = Instant::now();
        let err = client.call("batch.create", json!({})).unwrap_err();
        assert!(err.uncertain);
        assert!(start.elapsed() < Duration::from_secs(2));
    }
}
