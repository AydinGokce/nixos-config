//! Explicit local PyMOL interop using immutable result bytes and fixed commands.
use std::ffi::OsString;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const READY: &str = "BIO_WORKBENCH_PYMOL_STRUCTURE_READY";
const SCRIPT: &str = r#"hide everything, all
show cartoon, polymer
show sticks, polymer.nucleic or organic
show spheres, inorganic
set_color bio_protein, [0.34902, 0.69020, 0.63529]
set_color bio_RNA, [0.90588, 0.63922, 0.24314]
set_color bio_DNA, [0.78039, 0.44706, 0.81176]
color bio_protein, polymer.protein
color bio_RNA, polymer.nucleic
color bio_DNA, polymer.nucleic and resn DA+DC+DG+DT+DI+DU
bg_color black
orient all
zoom all, 3
deselect
print("BIO_WORKBENCH_PYMOL_STRUCTURE_READY")
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
            detail: "Open the selected coordinate artifact in a separate local PyMOL window."
                .into(),
            failed: false,
        }
    }
}

impl Launcher {
    pub fn active(&self) -> bool {
        self.updates.is_some()
    }

    pub fn launch_structure(
        &mut self,
        name: &str,
        bytes: &[u8],
        format: &str,
    ) -> Result<(), String> {
        if self.active() {
            return Err("The launched PyMOL process is still open; close it before opening another artifact.".into());
        }
        let extension = extension(format)?;
        if bytes.is_empty() || bytes.len() > 32 * 1024 * 1024 {
            return Err("PyMOL requires a coordinate artifact of 1 byte to 32 MiB.".into());
        }
        self.status = "PyMOL: starting";
        self.detail = format!("Starting a separate local PyMOL process for {name}.");
        self.failed = false;
        let (send, receive) = mpsc::channel();
        self.updates = Some(receive);
        let program = std::env::var_os("BIO_WORKBENCH_PYMOL")
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| OsString::from("pymol"));
        let bytes = bytes.to_vec();
        let name = name.to_owned();
        std::thread::spawn(move || {
            if let Err(error) = run(&program, &cache_root(), &name, &bytes, extension, &send) {
                let _ = send.send(Update {
                    status: "PyMOL: failed",
                    detail: error,
                    failed: true,
                    done: true,
                });
            }
        });
        Ok(())
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

fn extension(format: &str) -> Result<&'static str, String> {
    match format.to_ascii_lowercase().as_str() {
        "pdb" => Ok("pdb"),
        "cif" | "mmcif" => Ok("cif"),
        _ => Err("PyMOL supports the selected PDB/mmCIF coordinate artifacts.".into()),
    }
}
fn prepare(root: &Path, bytes: &[u8], extension: &str) -> Result<PathBuf, String> {
    fs::create_dir_all(root).map_err(|error| format!("PyMOL cache unavailable: {error}"))?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let directory = root.join(format!("structure-{}-{stamp}", std::process::id()));
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder
        .create(&directory)
        .map_err(|error| format!("Cannot create PyMOL artifact directory: {error}"))?;
    // Only one of two fixed filenames can enter PML; names and user paths never do.
    let filename = match extension {
        "pdb" => "structure.pdb",
        "cif" => "structure.cif",
        _ => return Err("Unsupported coordinate format".into()),
    };
    let path = directory.join(filename);
    fs::write(&path, bytes)
        .and_then(|()| {
            fs::write(
                directory.join("structure.pml"),
                format!("load {filename}, selected_structure\n{SCRIPT}"),
            )
        })
        .map_err(|error| format!("Cannot write PyMOL artifact: {error}"))?;
    let mut permissions = fs::metadata(&path)
        .map_err(|error| error.to_string())?
        .permissions();
    permissions.set_readonly(true);
    fs::set_permissions(path, permissions).map_err(|error| error.to_string())?;
    Ok(directory)
}

fn run(
    program: &OsString,
    root: &Path,
    name: &str,
    bytes: &[u8],
    extension: &str,
    send: &Sender<Update>,
) -> Result<(), String> {
    let directory = prepare(root, bytes, extension)?;
    let log_path = directory.join("pymol.log");
    let log = File::create(&log_path).map_err(|error| error.to_string())?;
    // Fixed relative filenames keep user paths out of PyMOL command syntax.
    // -k ignores startup scripts/plugins; -y reports command failures as exits.
    let mut child = Command::new(program)
        .args(["-k", "-q", "-y", "structure.pml"])
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
        status: "PyMOL: loading artifact",
        detail: format!(
            "PyMOL process {} started; loading {name}. Log: {}",
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
                status: "PyMOL: structure ready",
                detail: format!("PyMOL loaded {name} from its exact coordinate bytes. Protein cartoon, nucleic acids and ligands are visible. Snapshot: {}",directory.display()),
                failed: false,
                done: false,
            });
        }
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            if !status.success() || !ready {
                return Err(format!(
                    "PyMOL exited ({status}) {}. Inspect {}",
                    if ready {
                        "after loading the artifact"
                    } else {
                        "before confirming artifact load"
                    },
                    log_path.display()
                ));
            }
            let _ = send.send(Update {
                status: "PyMOL: closed",
                detail:
                    "The launched PyMOL process has closed. You can open another selected artifact."
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
            "named artifact",
            b"data_actual\n",
            "cif",
            &send,
        )
        .expect_err("an absent PyMOL cannot start");
        assert!(error.contains("Could not start PyMOL"));
        assert!(receive.try_recv().is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn snapshots_preserve_exact_selected_bytes_and_cannot_inject_commands() {
        let root = std::env::temp_dir().join(format!(
            "bio-pymol-fixture-{} spaces ; $literal",
            std::process::id()
        ));
        let bytes = b"data_actual\n# original payload remains unchanged\n";
        let first = prepare(&root, bytes, "cif").unwrap();
        fs::write(first.join("user-note.txt"), "keep").unwrap();
        let second = prepare(&root, b"END\n", "pdb").unwrap();
        assert_ne!(first, second);
        assert_eq!(fs::read(first.join("structure.cif")).unwrap(), bytes);
        assert_eq!(fs::read(second.join("structure.pdb")).unwrap(), b"END\n");
        assert_eq!(
            fs::read_to_string(first.join("user-note.txt")).unwrap(),
            "keep"
        );
        let script = fs::read_to_string(first.join("structure.pml")).unwrap();
        assert!(script.starts_with("load structure.cif, selected_structure\n"));
        assert!(!script.contains("$literal"));
        assert!(!script.contains("remove"));
        assert!(extension("cif; quit").is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
