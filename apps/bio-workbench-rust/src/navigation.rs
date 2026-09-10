//! Local single-instance navigation. No server, shell, or cloud mutation.
use eframe::egui;
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, SystemTime};

const MAX_MESSAGE: u64 = 128 * 1024;

#[cfg(target_os = "macos")]
#[path = "navigation_macos.rs"]
mod macos;

#[derive(Clone, Default)]
pub struct WakeHandle {
    context: Arc<Mutex<Option<egui::Context>>>,
    #[cfg(target_os = "macos")]
    sender: Option<mpsc::Sender<String>>,
}
impl WakeHandle {
    pub fn attach(&self, context: egui::Context) {
        #[cfg(target_os = "macos")]
        if let Some(sender) = &self.sender {
            macos::install(sender.clone(), context.clone());
        }
        if let Ok(mut value) = self.context.lock() {
            *value = Some(context);
        }
    }
    fn wake(&self) {
        if let Ok(value) = self.context.lock()
            && let Some(context) = &*value
        {
            context.request_repaint();
        }
    }
}

pub enum Launch {
    Forwarded,
    Primary(Navigation),
}

pub struct Navigation {
    _lock: File,
    receiver: Option<mpsc::Receiver<String>>,
    stop: Arc<AtomicBool>,
    wake: WakeHandle,
    thread: Option<thread::JoinHandle<()>>,
}
impl Navigation {
    pub fn take_receiver(&mut self) -> mpsc::Receiver<String> {
        self.receiver
            .take()
            .expect("navigation receiver already taken")
    }
    pub fn wake_handle(&self) -> WakeHandle {
        self.wake.clone()
    }
}
impl Drop for Navigation {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        // The OS releases the file lock on drop, including after an app crash.
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Message {
    version: u8,
    values: Vec<String>,
}

pub fn batch_id(value: &str) -> Option<&str> {
    let id = value.strip_prefix("bio-workbench://batch/")?;
    (1..=160).contains(&id.len()).then_some(())?;
    id.bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        .then_some(id)
}

fn navigation_value(value: OsString) -> Result<String, String> {
    let value = value
        .into_string()
        .map_err(|_| "Navigation arguments must be UTF-8".to_string())?;
    if batch_id(&value).is_some() {
        return Ok(value);
    }
    if value.starts_with("bio-workbench:") || value.starts_with('-') {
        return Err(
            "Expected a GC Protein Engineering Console batch link or an existing local file".into(),
        );
    }
    let path = fs::canonicalize(&value).map_err(|error| format!("Cannot open {value}: {error}"))?;
    if !path.is_file() {
        return Err("Only local files can be opened".into());
    }
    path.into_os_string()
        .into_string()
        .map_err(|_| "File path must be UTF-8".into())
}

pub fn start(arguments: impl IntoIterator<Item = OsString>) -> Result<Launch, String> {
    let directory = crate::session::state_directory().map_err(|e| e.to_string())?;
    start_in(&directory, arguments)
}

fn start_in(
    directory: &Path,
    arguments: impl IntoIterator<Item = OsString>,
) -> Result<Launch, String> {
    let values: Vec<String> = arguments
        .into_iter()
        .map(navigation_value)
        .collect::<Result<_, _>>()?;
    if values.len() > 128 {
        return Err("Open at most 128 files or links at once".into());
    }
    crate::rpc::private_dir(directory).map_err(|e| e.to_string())?;
    let inbox = directory.join("navigation-inbox");
    crate::rpc::private_dir(&inbox).map_err(|e| e.to_string())?;
    let mut options = OpenOptions::new();
    options.create(true).truncate(false).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let lock = options
        .open(directory.join("instance.lock"))
        .map_err(|e| e.to_string())?;
    if !lock.metadata().map_err(|e| e.to_string())?.is_file() {
        return Err("Instance lock must be a regular file".into());
    }
    match lock.try_lock_exclusive() {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
            let message =
                serde_json::to_value(Message { version: 1, values }).map_err(|e| e.to_string())?;
            if serde_json::to_vec(&message)
                .map_err(|e| e.to_string())?
                .len() as u64
                > MAX_MESSAGE
            {
                return Err("Navigation request is too large".into());
            }
            crate::rpc::atomic_json(
                &inbox.join(format!("{}.json", uuid::Uuid::new_v4())),
                &message,
            )
            .map_err(|e| e.to_string())?;
            return Ok(Launch::Forwarded);
        }
        Err(error) => return Err(format!("Cannot claim the desktop session: {error}")),
    }
    let (sender, receiver) = mpsc::channel();
    for value in values {
        let _ = sender.send(value);
    }
    let stop = Arc::new(AtomicBool::new(false));
    let wake = WakeHandle {
        #[cfg(target_os = "macos")]
        sender: Some(sender.clone()),
        ..WakeHandle::default()
    };
    let thread_stop = stop.clone();
    let thread_wake = wake.clone();
    let worker = thread::spawn(move || {
        while !thread_stop.load(Ordering::Acquire) {
            receive_messages(&inbox, &sender, &thread_wake);
            thread::sleep(Duration::from_millis(200));
        }
    });
    Ok(Launch::Primary(Navigation {
        _lock: lock,
        receiver: Some(receiver),
        stop,
        wake,
        thread: Some(worker),
    }))
}

fn receive_messages(inbox: &Path, sender: &mpsc::Sender<String>, wake: &WakeHandle) {
    let Ok(entries) = fs::read_dir(inbox) else {
        return;
    };
    let mut paths: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect();
    paths.sort();
    for path in paths.into_iter().take(128) {
        let valid = fs::symlink_metadata(&path).ok().is_some_and(|metadata| {
            metadata.file_type().is_file()
                && metadata.len() <= MAX_MESSAGE
                && metadata
                    .modified()
                    .ok()
                    .and_then(|time| SystemTime::now().duration_since(time).ok())
                    .is_some_and(|age| age < Duration::from_secs(86400))
        });
        let mut bytes = Vec::new();
        if valid
            && let Ok(file) = File::open(&path)
            && file.take(MAX_MESSAGE + 1).read_to_end(&mut bytes).is_ok()
            && bytes.len() as u64 <= MAX_MESSAGE
            && let Ok(message) = serde_json::from_slice::<Message>(&bytes)
            && message.version == 1
            && message.values.len() <= 128
        {
            let _ = sender.send(String::new()); // Focus, also for a launch without arguments.
            for value in message.values {
                if let Ok(value) = navigation_value(value.into()) {
                    let _ = sender.send(value);
                }
            }
            wake.wake();
        }
        let _ = fs::remove_file(&path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_exact_batch_links_are_accepted() {
        assert_eq!(
            batch_id("bio-workbench://batch/abc_123-Z"),
            Some("abc_123-Z")
        );
        for bad in [
            "https://batch/abc",
            "bio-workbench://batch/",
            "bio-workbench://batch/abc?submit=1",
            "bio-workbench://batch/abc#x",
            "bio-workbench://user@batch/abc",
            "bio-workbench://batch/../abc",
            "bio-workbench://batch/%61bc",
        ] {
            assert!(batch_id(bad).is_none(), "{bad}");
        }
    }
    #[test]
    fn second_instance_forwards_without_taking_the_writer_lock() {
        let directory = tempfile::tempdir().unwrap();
        let Launch::Primary(mut primary) = start_in(directory.path(), []).unwrap() else {
            panic!()
        };
        let receiver = primary.take_receiver();
        assert!(matches!(
            start_in(
                directory.path(),
                [OsString::from("bio-workbench://batch/abc")]
            )
            .unwrap(),
            Launch::Forwarded
        ));
        assert_eq!(receiver.recv_timeout(Duration::from_secs(3)).unwrap(), "");
        assert_eq!(
            receiver.recv_timeout(Duration::from_secs(3)).unwrap(),
            "bio-workbench://batch/abc"
        );
        drop(primary);
        assert!(matches!(
            start_in(directory.path(), []).unwrap(),
            Launch::Primary(_)
        ));
    }
    #[test]
    fn file_arguments_are_resolved_and_never_executed() {
        let directory = tempfile::tempdir().unwrap();
        let file = directory.path().join("reference ; $(literal).pdb");
        fs::write(&file, "ATOM").unwrap();
        assert_eq!(
            navigation_value(file.clone().into_os_string()).unwrap(),
            file.canonicalize().unwrap().to_str().unwrap()
        );
        assert!(navigation_value(directory.path().into()).is_err());
        assert!(navigation_value("--submit".into()).is_err());
    }
}
