use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Vec2};
use std::sync::Arc;
#[path = "scene_geometry.rs"]
mod geometry;
#[path = "scene_gpu.rs"]
mod gpu;
#[path = "structure.rs"]
mod structure;
pub use structure::{Atom, Molecule, MoleculeKind, ResidueKey, Secondary};

pub struct Renderer(Arc<egui::mutex::Mutex<gpu::Renderer>>);
impl Renderer {
    pub fn new(gl: &eframe::glow::Context, molecule: &Molecule) -> Result<Self, String> {
        gpu::Renderer::new(gl, molecule)
            .map(|renderer| Self(Arc::new(egui::mutex::Mutex::new(renderer))))
    }
    pub fn error(&self) -> Option<String> {
        self.0.lock().error.clone()
    }
    pub fn destroy(&self, gl: &eframe::glow::Context) {
        self.0.lock().destroy(gl);
    }
}
#[derive(Clone, Copy, Default, Debug, PartialEq)]
pub struct V3(pub f32, pub f32, pub f32);
impl V3 {
    fn add(self, b: Self) -> Self {
        Self(self.0 + b.0, self.1 + b.1, self.2 + b.2)
    }
    fn sub(self, b: Self) -> Self {
        Self(self.0 - b.0, self.1 - b.1, self.2 - b.2)
    }
    fn mul(self, n: f32) -> Self {
        Self(self.0 * n, self.1 * n, self.2 * n)
    }
    fn dot(self, b: Self) -> f32 {
        self.0 * b.0 + self.1 * b.1 + self.2 * b.2
    }
    fn length(self) -> f32 {
        (self.0 * self.0 + self.1 * self.1 + self.2 * self.2).sqrt()
    }
    fn unit(self) -> Self {
        self.mul(1. / self.length().max(0.00001))
    }
    fn cross(self, b: Self) -> Self {
        Self(
            self.1 * b.2 - self.2 * b.1,
            self.2 * b.0 - self.0 * b.2,
            self.0 * b.1 - self.1 * b.0,
        )
    }
}
#[derive(Clone, Copy, PartialEq)]
pub enum Representation {
    Cartoon,
    Sticks,
    Spheres,
    Trace,
}
impl Representation {
    pub fn name(self) -> &'static str {
        match self {
            Self::Cartoon => "cartoon",
            Self::Sticks => "sticks",
            Self::Spheres => "spheres",
            Self::Trace => "backbone trace",
        }
    }
}
#[derive(Clone, Copy)]
pub struct Camera {
    pub yaw: f32,
    pub pitch: f32,
    pub zoom: f32,
    pub pan: Vec2,
    pub ambient: bool,
    pub bloom: bool,
    pub distance: f32,
    pub span: f32,
}
impl Default for Camera {
    fn default() -> Self {
        Self {
            yaw: -0.35,
            pitch: 0.15,
            zoom: 0.98,
            pan: Vec2::ZERO,
            ambient: true,
            bloom: true,
            distance: 210.,
            span: 135.,
        }
    }
}
impl Camera {
    pub fn fit(molecule: &Molecule) -> Self {
        Self {
            distance: (molecule.radius * 3.2).max(12.),
            span: (molecule.radius * 2.3).max(8.),
            ..Self::default()
        }
    }
    fn rotate(self, p: V3) -> V3 {
        let x = p.0 * self.yaw.cos() + p.2 * self.yaw.sin();
        let z = -p.0 * self.yaw.sin() + p.2 * self.yaw.cos();
        V3(
            x,
            p.1 * self.pitch.cos() - z * self.pitch.sin(),
            p.1 * self.pitch.sin() + z * self.pitch.cos(),
        )
    }
    fn project(self, p: V3, rect: Rect, center: V3) -> (Pos2, f32) {
        let p = self.rotate(p.sub(center));
        let scale = rect.width().min(rect.height()) / self.span * self.zoom;
        let perspective = self.distance / (self.distance - p.2);
        (
            rect.center() + self.pan + Vec2::new(p.0, -p.1) * scale * perspective,
            p.2,
        )
    }
}

#[allow(clippy::too_many_arguments)]
pub fn viewport(
    ui: &mut egui::Ui,
    molecule: &Molecule,
    renderer: &Renderer,
    camera: &mut Camera,
    representation: Representation,
    index: usize,
    selected: &mut Option<ResidueKey>,
    visible: bool,
    labels: bool,
    axes: bool,
    chains: &[bool],
) -> bool {
    let (rect, response) = ui.allocate_exact_size(
        ui.available_size().max(Vec2::splat(20.)),
        Sense::click_and_drag(),
    );
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 0, Color32::from_rgb(3, 5, 7));
    let mut changed = false;
    if response.dragged() {
        let delta = ui.input(|i| i.pointer.delta());
        if response.dragged_by(egui::PointerButton::Secondary) || ui.input(|i| i.modifiers.shift) {
            camera.pan += delta;
        } else {
            camera.yaw += delta.x * 0.008;
            camera.pitch += delta.y * 0.008;
        }
        changed = true;
    }
    if response.hovered() {
        let scroll = ui.input(|i| i.smooth_scroll_delta.y);
        if scroll != 0. {
            camera.zoom = (camera.zoom * (scroll * 0.002).exp()).clamp(0.25, 4.);
            changed = true;
        }
    }
    if response.double_clicked() {
        *camera = Camera::fit(molecule);
        changed = true;
    }
    response.context_menu(|ui| {
        ui.strong("Studio rendering");
        changed |= ui
            .checkbox(&mut camera.ambient, "Contact shading")
            .changed();
        changed |= ui
            .checkbox(&mut camera.bloom, "Soft highlight glow")
            .changed();
        ui.small("GPU rasterization / presentation effects");
        if ui.button("Reset camera").clicked() {
            *camera = Camera::fit(molecule);
            changed = true;
            ui.close();
        }
    });
    let draw = Rect::from_min_max(rect.min + Vec2::new(8., 43.), rect.max - Vec2::new(8., 32.));
    if draw.is_positive() && visible {
        let gpu = renderer.0.clone();
        let camera = *camera;
        let selected = selected
            .as_ref()
            .and_then(|key| molecule.residues.iter().position(|r| &r.key == key))
            .map_or(0, |index| index + 1);
        let chains = chains.to_vec();
        let callback = eframe::egui_glow::CallbackFn::new(move |info, painter| {
            gpu.lock().paint(
                painter.gl(),
                info,
                painter.intermediate_fbo(),
                camera,
                representation,
                index,
                selected,
                &chains,
            );
        });
        painter.add(egui::PaintCallback {
            rect: draw,
            callback: Arc::new(callback),
        });
    }
    if visible && draw.is_positive() {
        if response.clicked()
            && let Some(pointer) = response.interact_pointer_pos()
        {
            let nearest = molecule
                .residues
                .iter()
                .filter(|r| chains.get(r.chain).copied().unwrap_or(false))
                .map(|r| {
                    let (point, depth) =
                        camera.project(molecule.atoms[r.anchor].p, draw, molecule.center);
                    (r, point.distance(pointer), depth)
                })
                .filter(|(_, distance, _)| *distance < 18.)
                .min_by(|a, b| {
                    if (a.1 - b.1).abs() < 2. {
                        b.2.total_cmp(&a.2)
                    } else {
                        a.1.total_cmp(&b.1)
                    }
                });
            if let Some((residue, _, _)) = nearest {
                *selected = Some(residue.key.clone());
            }
        }
        if (labels || selected.is_some())
            && let Some(key) = selected.as_ref()
            && let Some(residue) = molecule.residue(key)
            && chains.get(residue.chain).copied().unwrap_or(false)
        {
            let point = camera
                .project(molecule.atoms[residue.anchor].p, draw, molecule.center)
                .0;
            if draw.contains(point) {
                let note = point + Vec2::new(26., -28.);
                painter.circle_stroke(point, 6., Stroke::new(1., Color32::from_rgb(245, 208, 84)));
                painter.line_segment(
                    [point + Vec2::new(5., -5.), note],
                    Stroke::new(1., Color32::from_gray(140)),
                );
                painter.text(
                    note,
                    Align2::LEFT_BOTTOM,
                    format!("{key}  [selection]"),
                    FontId::monospace(11.),
                    Color32::from_rgb(230, 218, 153),
                );
            }
        }
    }
    let mut title = egui::text::LayoutJob::simple(
        format!(
            "{}  {} / {}",
            index + 1,
            molecule.name,
            representation.name()
        ),
        FontId::monospace(12.),
        Color32::from_gray(225),
        (rect.width() - 92.).max(1.),
    );
    title.wrap.max_rows = 1;
    title.wrap.break_anywhere = true;
    painter.galley(
        rect.min + Vec2::new(12., 11.),
        painter.layout_job(title),
        Color32::from_gray(225),
    );
    painter.text(
        rect.min + Vec2::new(12., 28.),
        Align2::LEFT_TOP,
        format!(
            "{} / {} chains / {} residues",
            molecule.format.to_ascii_uppercase(),
            molecule.chains.len(),
            molecule.residues.len()
        ),
        FontId::monospace(9.),
        Color32::from_gray(120),
    );
    painter.text(
        rect.right_top() + Vec2::new(-12., 12.),
        Align2::RIGHT_TOP,
        "STUDIO",
        FontId::monospace(9.),
        Color32::from_rgb(123, 158, 165),
    );
    painter.text(
        rect.left_bottom() + Vec2::new(12., -12.),
        Align2::LEFT_BOTTOM,
        format!(
            "{} atoms   /   {}",
            molecule.atoms.len(),
            if representation == Representation::Cartoon {
                if molecule.secondary_source.starts_with("File annotations") {
                    "file annotations + backbone approximation"
                } else {
                    "backbone approximation (not DSSP)"
                }
            } else if molecule.warnings.is_empty() {
                "source coordinates"
            } else {
                "see structure notes"
            }
        ),
        FontId::monospace(10.),
        Color32::from_gray(135),
    );
    if axes {
        let base = rect.right_bottom() - Vec2::splat(38.);
        for (v, label, color) in [
            (V3(1., 0., 0.), "X", Color32::from_rgb(201, 97, 86)),
            (V3(0., 1., 0.), "Y", Color32::from_rgb(119, 185, 121)),
            (V3(0., 0., 1.), "Z", Color32::from_rgb(114, 151, 213)),
        ] {
            let r = camera.rotate(v);
            let end = base + Vec2::new(r.0, -r.1) * 23.;
            painter.line_segment([base, end], Stroke::new(1.5, color));
            painter.text(
                end,
                Align2::CENTER_CENTER,
                label,
                FontId::monospace(9.),
                color,
            );
        }
    }
    changed
}
