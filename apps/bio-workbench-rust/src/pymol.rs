//! Explicit, local PyMOL interop for the embedded experimental reference.
//! No draft text, commands from the console, or cloud data enter this process.
use std::ffi::OsString;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const PDB: &[u8] = include_bytes!("../fixtures/4oo8.pdb");
const READY: &str = "BIO_WORKBENCH_PYMOL_REFERENCE_READY";
const SCRIPT: &str = r#"load reference.pdb, experimental_4oo8
remove not (experimental_4oo8 and polymer and chain A+B+C)
create Cas9_protein, experimental_4oo8 and chain A
create guide_RNA, experimental_4oo8 and chain B
create target_DNA, experimental_4oo8 and chain C
delete experimental_4oo8
hide everything, all
show cartoon, all
show sticks, guide_RNA or target_DNA
set_color bio_protein, [0.34902, 0.69020, 0.63529]
set_color bio_RNA, [0.90588, 0.63922, 0.24314]
set_color bio_DNA, [0.78039, 0.44706, 0.81176]
color bio_protein, Cas9_protein
color bio_RNA, guide_RNA
color bio_DNA, target_DNA
bg_color black
orient all
zoom all, 3
deselect
print("BIO_WORKBENCH_PYMOL_REFERENCE_READY")
"#;

struct Update {
    status: &'static str,
    detail: String,
    failed: bool,
    done: bool,
}

pub struct Launcher {
    updates: Option<Receiver<Update>>,
    pub status: &'static str,
    pub detail: String,
    pub failed: bool,
}

impl Default for Launcher {
    fn default() -> Self {
        Self {
            updates: None,
            status: "PyMOL: not opened",
            detail: "Open experimental 4OO8 chains A/B/C in a separate local PyMOL window.".into(),
            failed: false,
        }
    }
}

impl Launcher {
    pub fn active(&self) -> bool {
        self.updates.is_some()
    }

    pub fn launch(&mut self) {
        if self.active() {
            return;
        }
        self.status = "PyMOL: starting";
        self.detail = "Starting a separate local PyMOL process for experimental 4OO8.".into();
        self.failed = false;
        let (send, receive) = mpsc::channel();
        self.updates = Some(receive);
        let program = std::env::var_os("BIO_WORKBENCH_PYMOL")
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| OsString::from("pymol"));
        std::thread::spawn(move || {
            if let Err(error) = run(&program, &cache_root(), &send) {
                let _ = send.send(Update {
                    status: "PyMOL: failed",
                    detail: error,
                    failed: true,
                    done: true,
                });
            }
        });
    }

    pub fn poll(&mut self) -> Vec<String> {
        let mut messages = Vec::new();
        let mut done = false;
        if let Some(receive) = &self.updates {
            while let Ok(update) = receive.try_recv() {
                self.status = update.status;
                self.detail = update.detail;
                self.failed = update.failed;
                done |= update.done;
                messages.push(self.detail.clone());
            }
        }
        if done {
            self.updates = None;
        }
        messages
    }
}

fn cache_root() -> PathBuf {
    if let Some(path) = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
    {
        return path.join("bio-workbench-rust/pymol");
    }
    if cfg!(target_os = "windows")
        && let Some(path) = std::env::var_os("LOCALAPPDATA")
    {
        return PathBuf::from(path).join("bio-workbench-rust/pymol");
    }
    if let Some(path) = std::env::var_os("HOME") {
        let directory = if cfg!(target_os = "macos") {
            "Library/Caches/bio-workbench-rust/pymol"
        } else {
            ".cache/bio-workbench-rust/pymol"
        };
        return PathBuf::from(path).join(directory);
    }
    std::env::temp_dir().join("bio-workbench-rust-pymol")
}

fn prepare(root: &Path) -> Result<PathBuf, String> {
    fs::create_dir_all(root).map_err(|error| format!("PyMOL cache unavailable: {error}"))?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let directory = root.join(format!("4oo8-{}-{stamp}", std::process::id()));
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder
        .create(&directory)
        .map_err(|error| format!("Cannot create PyMOL reference directory: {error}"))?;
    fs::write(directory.join("reference.pdb"), PDB)
        .and_then(|()| fs::write(directory.join("reference.pml"), SCRIPT))
        .map_err(|error| format!("Cannot write PyMOL reference: {error}"))?;
    Ok(directory)
}

fn run(program: &OsString, root: &Path, send: &Sender<Update>) -> Result<(), String> {
    let directory = prepare(root)?;
    let log_path = directory.join("pymol.log");
    let log = File::create(&log_path).map_err(|error| error.to_string())?;
    // Fixed relative filenames keep user paths out of PyMOL command syntax.
    // -k ignores startup scripts/plugins; -y reports command failures as exits.
    let mut child = Command::new(program)
        .args(["-k", "-q", "-y", "reference.pml"])
        .current_dir(&directory)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stderr(log.try_clone().map_err(|error| error.to_string())?)
        .stdout(log)
        .spawn()
        .map_err(|error| {
            format!(
                "Could not start PyMOL ({program:?}): {error}. Install PyMOL or set BIO_WORKBENCH_PYMOL to its executable path."
            )
        })?;
    let _ = send.send(Update {
        status: "PyMOL: loading reference",
        detail: format!(
            "PyMOL process {} started; loading experimental 4OO8. Log: {}",
            child.id(),
            log_path.display()
        ),
        failed: false,
        done: false,
    });
    let mut ready = false;
    loop {
        // The final PML command confirms processing; spawn alone is not ready.
        if !ready
            && fs::read_to_string(&log_path)
                .unwrap_or_default()
                .lines()
                .any(|line| line.trim() == READY)
        {
            ready = true;
            let _ = send.send(Update {
                status: "PyMOL: reference ready",
                detail: "PyMOL loaded experimental 4OO8: Cas9 cartoon (teal), guide RNA (amber), target DNA (violet). This is the reference, not the edited draft or a prediction.".into(),
                failed: false,
                done: false,
            });
        }
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            if !status.success() || !ready {
                return Err(format!(
                    "PyMOL exited ({status}) {}. Inspect {}",
                    if ready {
                        "after loading the reference"
                    } else {
                        "before confirming reference load"
                    },
                    log_path.display()
                ));
            }
            let _ = send.send(Update {
                status: "PyMOL: closed",
                detail: "The launched PyMOL process has closed. You can open the reference again."
                    .into(),
                failed: false,
                done: true,
            });
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_executable_is_an_error_without_success_event() {
        let root = std::env::temp_dir().join(format!("bio-pymol-missing-{}", std::process::id()));
        let (send, receive) = mpsc::channel();
        let error = run(
            &root.join("no-such-executable").into_os_string(),
            &root,
            &send,
        )
        .expect_err("an absent PyMOL cannot start");
        assert!(error.contains("Could not start PyMOL"));
        assert!(receive.try_recv().is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn separate_launches_preserve_existing_files_and_fixed_reference() {
        let root = std::env::temp_dir().join(format!(
            "bio-pymol-fixture-{} spaces ; $literal",
            std::process::id()
        ));
        let first = prepare(&root).unwrap();
        fs::write(first.join("user-note.txt"), "keep this note").unwrap();
        let second = prepare(&root).unwrap();
        assert_ne!(first, second);
        assert_eq!(fs::read(first.join("reference.pdb")).unwrap(), PDB);
        assert_eq!(fs::read(second.join("reference.pdb")).unwrap(), PDB);
        assert_eq!(
            fs::read_to_string(first.join("user-note.txt")).unwrap(),
            "keep this note"
        );
        assert!(
            !fs::read_to_string(second.join("reference.pml"))
                .unwrap()
                .contains("$literal")
        );
        fs::remove_dir_all(root).unwrap();
    }
}
