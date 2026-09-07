use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Vec2};
use std::sync::Arc;
#[path = "scene_geometry.rs"]
mod geometry;
#[path = "scene_gpu.rs"]
mod gpu;

pub struct Renderer(Arc<egui::mutex::Mutex<gpu::Renderer>>);
impl Renderer {
    pub fn new(gl: &eframe::glow::Context, molecule: &Molecule) -> Result<Self, String> {
        gpu::Renderer::new(gl, molecule)
            .map(|renderer| Self(Arc::new(egui::mutex::Mutex::new(renderer))))
    }
    pub fn destroy(&self, gl: &eframe::glow::Context) {
        self.0.lock().destroy(gl);
    }
}
#[derive(Clone, Copy, Default)]
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
pub struct Atom {
    pub p: V3,
    pub name: String,
    pub element: String,
    pub chain: char,
    pub residue: usize,
}
pub struct Molecule {
    pub atoms: Vec<Atom>,
    pub ca: Vec<V3>,
    pub ca_ids: Vec<usize>,
    pub sequence: Vec<char>,
    pub bonds: Vec<(usize, usize)>,
    pub center: V3,
    pub helices: Vec<(usize, usize)>,
    pub sheets: Vec<(usize, usize)>,
}
pub fn chain_color(chain: char) -> Color32 {
    match chain {
        'B' => Color32::from_rgb(231, 163, 62),
        'C' => Color32::from_rgb(199, 114, 207),
        _ => Color32::from_rgb(89, 176, 162),
    }
}
fn residue_letter(name: &str) -> char {
    match name {
        "ALA" => 'A',
        "ARG" => 'R',
        "ASN" => 'N',
        "ASP" => 'D',
        "CYS" => 'C',
        "GLN" => 'Q',
        "GLU" => 'E',
        "GLY" => 'G',
        "HIS" => 'H',
        "ILE" => 'I',
        "LEU" => 'L',
        "LYS" => 'K',
        "MET" => 'M',
        "PHE" => 'F',
        "PRO" => 'P',
        "SER" => 'S',
        "THR" => 'T',
        "TRP" => 'W',
        "TYR" => 'Y',
        "VAL" => 'V',
        _ => 'X',
    }
}
impl Molecule {
    pub fn reference() -> Self {
        // Fixed experimental 4OO8 reference, first complex only (chains A/B/C).
        // This is not a general PDB importer or validated chemical bond parser.
        let source = include_str!("../fixtures/4oo8.pdb");
        let mut atoms = Vec::new();
        let mut ca = Vec::new();
        let mut ca_ids = Vec::new();
        let mut sequence = Vec::new();
        let mut helices = Vec::new();
        let mut sheets = Vec::new();
        for line in source.lines() {
            if line.starts_with("HELIX ")
                && &line[19..20] == "A"
                && let (Ok(a), Ok(b)) = (line[21..25].trim().parse(), line[33..37].trim().parse())
            {
                helices.push((a, b));
            }
            if line.starts_with("SHEET ")
                && &line[21..22] == "A"
                && let (Ok(a), Ok(b)) = (line[22..26].trim().parse(), line[33..37].trim().parse())
            {
                sheets.push((a, b));
            }
            if !line.starts_with("ATOM ") || line.len() < 78 {
                continue;
            }
            let chain = line.as_bytes()[21] as char;
            if !['A', 'B', 'C'].contains(&chain) {
                continue;
            }
            if let (Ok(x), Ok(y), Ok(z), Ok(residue)) = (
                line[30..38].trim().parse(),
                line[38..46].trim().parse(),
                line[46..54].trim().parse(),
                line[22..26].trim().parse(),
            ) {
                let atom = Atom {
                    p: V3(x, y, z),
                    name: line[12..16].trim().into(),
                    element: line[76..78].trim().into(),
                    chain,
                    residue,
                };
                if chain == 'A' && atom.name == "CA" {
                    ca.push(atom.p);
                    ca_ids.push(residue);
                    sequence.push(residue_letter(line[17..20].trim()));
                }
                atoms.push(atom);
            }
        }
        let center = atoms
            .iter()
            .fold(V3::default(), |s, a| s.add(a.p))
            .mul(1. / atoms.len() as f32);
        // A spatial grid bounds the one-time illustrative bond construction.
        let mut grid: std::collections::HashMap<(i32, i32, i32), Vec<usize>> =
            std::collections::HashMap::new();
        let mut bonds = Vec::new();
        for (i, atom) in atoms.iter().enumerate() {
            let cell = (
                (atom.p.0 / 2.).floor() as i32,
                (atom.p.1 / 2.).floor() as i32,
                (atom.p.2 / 2.).floor() as i32,
            );
            for dx in -1..=1 {
                for dy in -1..=1 {
                    for dz in -1..=1 {
                        if let Some(neighbors) = grid.get(&(cell.0 + dx, cell.1 + dy, cell.2 + dz))
                        {
                            for &j in neighbors {
                                if atom.chain != atoms[j].chain {
                                    continue;
                                }
                                let d = atom.p.sub(atoms[j].p).length();
                                let limit = if atom.element == "S" || atoms[j].element == "S" {
                                    1.95
                                } else {
                                    1.8
                                };
                                if d > 0.5 && d < limit {
                                    bonds.push((j, i));
                                }
                            }
                        }
                    }
                }
            }
            grid.entry(cell).or_default().push(i);
        }
        Self {
            atoms,
            ca,
            ca_ids,
            sequence,
            bonds,
            center,
            helices,
            sheets,
        }
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
            Self::Trace => "C-alpha trace",
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
        }
    }
}
impl Camera {
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
        let scale = rect.width().min(rect.height()) / 135. * self.zoom;
        let perspective = 210. / (210. - p.2);
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
    selected: &mut usize,
    visible: bool,
    labels: bool,
    axes: bool,
    chains: [bool; 3],
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
        *camera = Camera::default();
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
            *camera = Camera::default();
            changed = true;
            ui.close();
        }
    });
    let draw = Rect::from_min_max(rect.min + Vec2::new(8., 43.), rect.max - Vec2::new(8., 32.));
    if draw.is_positive() && visible {
        let gpu = renderer.0.clone();
        let camera = *camera;
        let selected = *selected;
        let callback = eframe::egui_glow::CallbackFn::new(move |info, painter| {
            gpu.lock().paint(
                painter.gl(),
                info,
                painter.intermediate_fbo(),
                camera,
                representation,
                index,
                selected,
                chains,
            );
        });
        painter.add(egui::PaintCallback {
            rect: draw,
            callback: Arc::new(callback),
        });
    }
    if visible && chains[0] {
        if response.clicked()
            && let Some(pointer) = response.interact_pointer_pos()
        {
            let nearest = molecule
                .ca
                .iter()
                .enumerate()
                .map(|(i, p)| {
                    (
                        i,
                        camera
                            .project(*p, draw, molecule.center)
                            .0
                            .distance(pointer),
                    )
                })
                .min_by(|a, b| a.1.total_cmp(&b.1));
            if let Some((i, distance)) = nearest
                && distance < 24.
            {
                *selected = molecule.ca_ids[i];
            }
        }
        if labels || *selected > 0 {
            let residue = if *selected > 0 { *selected } else { 840 };
            if let Some(point) = molecule
                .ca_ids
                .iter()
                .position(|id| *id == residue)
                .map(|i| camera.project(molecule.ca[i], draw, molecule.center).0)
                && draw.contains(point)
            {
                let note = point + Vec2::new(26., -28.);
                painter.circle_stroke(point, 6., Stroke::new(1., Color32::from_rgb(245, 208, 84)));
                painter.line_segment(
                    [point + Vec2::new(5., -5.), note],
                    Stroke::new(1., Color32::from_gray(140)),
                );
                painter.text(
                    note,
                    Align2::LEFT_BOTTOM,
                    format!("A/{residue}  [selection]"),
                    FontId::monospace(11.),
                    Color32::from_rgb(230, 218, 153),
                );
            }
        }
    }
    painter.text(
        rect.min + Vec2::new(12., 11.),
        Align2::LEFT_TOP,
        format!(
            "{}  4OO8 / {}",
            if index == 0 { "A" } else { "B" },
            representation.name()
        ),
        FontId::monospace(12.),
        Color32::from_gray(225),
    );
    painter.text(
        rect.min + Vec2::new(12., 28.),
        Align2::LEFT_TOP,
        "REFERENCE FIXTURE  /  NOT A PREDICTION",
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
        format!("Cas9 + sgRNA + DNA   /   {} atoms", molecule.atoms.len()),
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
