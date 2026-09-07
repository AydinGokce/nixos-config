//! One-shot structure figures, using the desktop's studio renderer unchanged.
//! Run on a local display, or with bio-render-headless on Linux (private Xvfb).
use crate::scene;
use eframe::egui::{self, Color32, Vec2};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const MAX_SOURCE: u64 = 32 * 1024 * 1024;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Source {
    path: PathBuf,
    title: Option<String>,
    format: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    structures: Vec<Source>,
    output: PathBuf,
    #[serde(default = "width")]
    width: u32,
    #[serde(default = "height")]
    height: u32,
    #[serde(default = "style")]
    style: String,
}
fn width() -> u32 {
    1600
}
fn height() -> u32 {
    1000
}
fn style() -> String {
    "cartoon".into()
}
fn representation(value: &str) -> Result<scene::Representation, String> {
    match value {
        "cartoon" => Ok(scene::Representation::Cartoon),
        "sticks" => Ok(scene::Representation::Sticks),
        "spheres" => Ok(scene::Representation::Spheres),
        "trace" => Ok(scene::Representation::Trace),
        _ => Err("style must be cartoon, sticks, spheres, or trace".into()),
    }
}
fn read_bounded(path: &Path, limit: u64) -> Result<Vec<u8>, String> {
    let file = std::fs::File::open(path).map_err(|_| "Cannot open render input")?;
    let info = file.metadata().map_err(|_| "Cannot inspect render input")?;
    if !info.is_file() || info.len() == 0 || info.len() > limit {
        return Err("Render input is empty, not a regular file, or exceeds its size limit".into());
    }
    let mut bytes = Vec::with_capacity(info.len() as usize);
    file.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Cannot read render input")?;
    if bytes.len() as u64 > limit {
        return Err("Render input exceeds its size limit".into());
    }
    Ok(bytes)
}
fn resolve(base: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.into()
    } else {
        base.join(path)
    }
}
#[derive(Serialize, Clone)]
struct StructureReceipt {
    title: String,
    sha256: String,
    format: String,
    atoms: usize,
    residues: usize,
    chains: usize,
    notes: Vec<String>,
}
#[derive(Serialize, Clone)]
struct Receipt {
    renderer: &'static str,
    version: &'static str,
    output: PathBuf,
    width: usize,
    height: usize,
    sha256: String,
    structures: Vec<StructureReceipt>,
}
struct View {
    molecule: scene::Molecule,
    renderer: scene::Renderer,
    camera: scene::Camera,
    chains: Vec<bool>,
}
type Outcome = Arc<Mutex<Option<Result<Receipt, String>>>>;
struct Capture {
    views: Vec<View>,
    sources: Vec<StructureReceipt>,
    style: scene::Representation,
    output: PathBuf,
    outcome: Outcome,
    frames: usize,
    size: [u32; 2],
    started: Instant,
}
impl Capture {
    fn finish(&self, result: Result<Receipt, String>, ctx: &egui::Context) {
        *self.outcome.lock().expect("capture outcome") = Some(result);
        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
    }
    fn save_image(&self, image: &egui::ColorImage) -> Result<Receipt, String> {
        if image.width() != self.size[0] as usize || image.height() != self.size[1] as usize {
            return Err("Display did not honor the requested physical image dimensions".into());
        }
        let parent = self.output.parent().ok_or("Output directory is required")?;
        let mut file =
            tempfile::NamedTempFile::new_in(parent).map_err(|_| "Cannot create PNG output")?;
        let pixels: Vec<u8> = image
            .pixels
            .iter()
            .flat_map(|pixel| pixel.to_array())
            .collect();
        let mut encoded = std::io::Cursor::new(Vec::new());
        image::write_buffer_with_format(
            &mut encoded,
            &pixels,
            image.width() as u32,
            image.height() as u32,
            image::ColorType::Rgba8,
            image::ImageFormat::Png,
        )
        .map_err(|_| "Cannot encode structure PNG")?;
        let bytes = encoded.into_inner();
        file.write_all(&bytes)
            .map_err(|_| "Cannot write structure PNG")?;
        file.as_file()
            .sync_all()
            .map_err(|_| "Cannot save structure PNG")?;
        // Never replace a prior result, even when two callers race.
        file.persist_noclobber(&self.output)
            .map_err(|_| "Output already exists or cannot be saved")?;
        Ok(Receipt {
            renderer: "bio-workbench-studio",
            version: env!("CARGO_PKG_VERSION"),
            output: self.output.clone(),
            width: image.width(),
            height: image.height(),
            sha256: format!("{:x}", Sha256::digest(&bytes)),
            structures: self.sources.clone(),
        })
    }
}
impl eframe::App for Capture {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.frames == 0 {
            // Native viewport scale is only known on the first update. Egui's
            // InnerSize uses points times pixels_per_point, so this is physical.
            ctx.set_pixels_per_point(1.);
            ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(Vec2::new(
                self.size[0] as f32,
                self.size[1] as f32,
            )));
        }
        for view in &self.views {
            if let Some(error) = view.renderer.error() {
                self.finish(Err(error), ctx);
                return;
            }
        }
        let screenshot = ctx.input(|input| {
            input.events.iter().find_map(|event| {
                if let egui::Event::Screenshot { image, .. } = event {
                    Some(image.clone())
                } else {
                    None
                }
            })
        });
        if let Some(image) = screenshot {
            self.finish(self.save_image(&image), ctx);
            return;
        }
        if self.started.elapsed() > Duration::from_secs(45) {
            self.finish(Err("Timed out waiting for a completed render".into()), ctx);
            return;
        }
        egui::CentralPanel::default()
            .frame(egui::Frame::NONE.fill(Color32::from_rgb(3, 5, 7)))
            .show(ctx, |ui| {
                let count = self.views.len();
                let columns = if count == 1 { 1 } else { 2 };
                let row_count = count.div_ceil(columns);
                let full = ui.available_rect_before_wrap();
                let size = Vec2::new(
                    full.width() / columns as f32,
                    full.height() / row_count as f32,
                );
                for (index, view) in self.views.iter_mut().enumerate() {
                    let offset = Vec2::new(
                        (index % columns) as f32 * size.x,
                        (index / columns) as f32 * size.y,
                    );
                    let rect = egui::Rect::from_min_size(full.min + offset, size).shrink(3.);
                    let mut child = ui.new_child(egui::UiBuilder::new().max_rect(rect));
                    scene::viewport(
                        &mut child,
                        &view.molecule,
                        &view.renderer,
                        &mut view.camera,
                        self.style,
                        index,
                        &mut None,
                        true,
                        false,
                        true,
                        &view.chains,
                    );
                }
            });
        self.frames += 1;
        if self.frames == 3 {
            ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
        }
        ctx.request_repaint_after(Duration::from_millis(25));
    }
    fn on_exit(&mut self, gl: Option<&eframe::glow::Context>) {
        if let Some(gl) = gl {
            for view in &self.views {
                view.renderer.destroy(gl);
            }
        }
    }
}

pub fn run(args: Vec<OsString>) -> Result<(), String> {
    if args.len() == 1 && args[0] == "--help" {
        println!(
            "bio-render --manifest request.json\nManifest: {{\"structures\":[{{\"path\":\"structure.cif\",\"title\":\"Model / sample\"}}],\"output\":\"figure.png\",\"style\":\"cartoon\",\"width\":1600,\"height\":1000}}\nOne to four PDB/mmCIF files. Relative paths resolve beside the manifest. Existing output is never overwritten. Linux servers: bio-render-headless --manifest request.json. Prints a PNG/source SHA256 receipt; no network or cloud calls."
        );
        return Ok(());
    }
    if args.len() != 2 || args[0] != "--manifest" {
        return Err("Use --manifest request.json or --help".into());
    }
    let manifest =
        std::fs::canonicalize(Path::new(&args[1])).map_err(|_| "Cannot open render manifest")?;
    let request: Request = serde_json::from_slice(&read_bounded(&manifest, 64 * 1024)?)
        .map_err(|_| "Invalid render manifest")?;
    if !(1..=4).contains(&request.structures.len())
        || !(640..=2560).contains(&request.width)
        || !(480..=2160).contains(&request.height)
    {
        return Err("Use 1–4 structures and dimensions 640–2560 × 480–2160".into());
    }
    let style = representation(&request.style)?;
    let base = manifest.parent().ok_or("Manifest directory missing")?;
    let output = resolve(base, &request.output);
    if output
        .extension()
        .is_none_or(|extension| extension != "png")
        || output.exists()
    {
        return Err("Output must be a new .png path".into());
    }
    let mut molecules = Vec::new();
    let mut sources = Vec::new();
    for source in request.structures {
        let path = resolve(base, &source.path);
        let bytes = read_bounded(&path, MAX_SOURCE)?;
        let format = source.format.unwrap_or_else(|| {
            path.extension()
                .and_then(|x| x.to_str())
                .unwrap_or("")
                .to_owned()
        });
        let title: String = source
            .title
            .unwrap_or_else(|| {
                path.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned()
            })
            .chars()
            .filter(|c| !c.is_control())
            .take(100)
            .collect();
        let molecule = scene::Molecule::parse(&bytes, &format, &title)?;
        sources.push(StructureReceipt {
            title,
            sha256: format!("{:x}", Sha256::digest(&bytes)),
            format: molecule.format.clone(),
            atoms: molecule.atoms.len(),
            residues: molecule.residues.len(),
            chains: molecule.chains.len(),
            notes: std::iter::once(molecule.secondary_source.clone())
                .chain(molecule.warnings.clone())
                .collect(),
        });
        molecules.push(molecule);
    }
    let outcome: Outcome = Arc::new(Mutex::new(None));
    let shared = outcome.clone();
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([request.width as f32, request.height as f32])
            .with_resizable(false)
            .with_decorations(false),
        renderer: eframe::Renderer::Glow,
        centered: false,
        ..Default::default()
    };
    eframe::run_native(
        "Bio Workbench figure renderer",
        options,
        Box::new(move |cc| {
            cc.egui_ctx.set_pixels_per_point(1.);
            let gl = cc.gl.as_ref().ok_or("OpenGL is unavailable")?;
            let mut views = Vec::new();
            for molecule in molecules {
                let renderer = scene::Renderer::new(gl, &molecule)?;
                let camera = scene::Camera::fit(&molecule);
                let chains = vec![true; molecule.chains.len()];
                views.push(View {
                    molecule,
                    renderer,
                    camera,
                    chains,
                });
            }
            Ok(Box::new(Capture {
                views,
                sources,
                style,
                output,
                outcome: shared,
                frames: 0,
                size: [request.width, request.height],
                started: Instant::now(),
            }))
        }),
    )
    .map_err(|error| format!("Renderer failed: {error}"))?;
    let result = outcome
        .lock()
        .map_err(|_| "Capture state unavailable")?
        .take()
        .ok_or("Render closed before a PNG was produced")??;
    println!(
        "{}",
        serde_json::to_string(&result).map_err(|_| "Cannot serialize render receipt")?
    );
    Ok(())
}
