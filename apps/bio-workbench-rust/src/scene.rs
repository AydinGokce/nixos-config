use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Vec2};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
#[path = "scene_geometry.rs"]
mod geometry;
#[path = "scene_gpu.rs"]
mod gpu;
#[path = "structure.rs"]
mod structure;
#[path = "scene_surface.rs"]
mod surface;
pub use structure::{Atom, Molecule, MoleculeKind, ResidueKey, Secondary};

/// Optional sRGB material colors keyed by exact source residue identities.
/// Empty maps leave the normal chain/element palette unchanged.
pub type ResidueColors = BTreeMap<ResidueKey, [u8; 3]>;

/// A design selection is separate from the ordinary single-residue inspector.
/// Exact source residue identities survive view restoration; array offsets do not.
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Hotspots {
    pub enabled: bool,
    pub residues: BTreeSet<ResidueKey>,
    pub anchor: Option<ResidueKey>,
    #[serde(skip)]
    pending: Option<PickGesture>,
}
#[derive(Clone, Copy)]
struct PickGesture {
    toggle: bool,
    range: bool,
}
impl Hotspots {
    pub fn retain_existing(&mut self, molecule: &Molecule) {
        self.residues.retain(|key| {
            molecule
                .residue(key)
                .is_some_and(|residue| residue.kind == MoleculeKind::Protein)
        });
        if self
            .anchor
            .as_ref()
            .is_some_and(|key| molecule.residue(key).is_none())
        {
            self.anchor = None;
        }
    }
    pub fn pick(&mut self, molecule: &Molecule, key: &ResidueKey, toggle: bool, range: bool) {
        let Some(residue) = molecule.residue(key) else {
            return;
        };
        // BindCraft's input contract is a protein target. Nucleotides and ligands
        // can still be inspected, but never become silently accepted hotspots.
        if residue.kind != MoleculeKind::Protein {
            return;
        }
        if range
            && let Some(anchor) = self.anchor.as_ref().and_then(|key| molecule.residue(key))
            && anchor.chain == residue.chain
        {
            let chain = &molecule.chains[residue.chain];
            let positions = chain
                .residues
                .iter()
                .enumerate()
                .filter_map(|(index, &id)| {
                    let candidate = &molecule.residues[id];
                    (candidate.key == *key || candidate.key == anchor.key).then_some(index)
                })
                .collect::<Vec<_>>();
            if let (Some(&start), Some(&end)) = (positions.first(), positions.last()) {
                for &id in &chain.residues[start..=end] {
                    let candidate = &molecule.residues[id];
                    if candidate.kind == MoleculeKind::Protein {
                        self.residues.insert(candidate.key.clone());
                    }
                }
            }
        } else if toggle || self.enabled {
            if !self.residues.remove(key) {
                self.residues.insert(key.clone());
            }
        } else {
            return;
        }
        self.anchor = Some(key.clone());
    }
}

pub struct Renderer(Arc<egui::mutex::Mutex<gpu::Renderer>>);
impl Renderer {
    pub fn new(gl: &eframe::glow::Context, molecule: &Molecule) -> Result<Self, String> {
        gpu::Renderer::new(gl, molecule)
            .map(|renderer| Self(Arc::new(egui::mutex::Mutex::new(renderer))))
    }
    pub fn error(&self) -> Option<String> {
        self.0.lock().error.clone()
    }
    pub fn surface_ready(&self) -> bool {
        self.0.lock().surface_ready()
    }
    pub fn surface_error(&self) -> Option<String> {
        self.0.lock().surface_error().map(str::to_owned)
    }
    pub fn poll_surface(&self, gl: &eframe::glow::Context) {
        self.0.lock().accept_surface(gl);
    }
    /// Set colors for this renderer's molecule. Geometry remains unchanged;
    /// only a bounded lookup texture is updated at the next draw, if needed.
    /// Unmatched identities are ignored. Selection and hotspots remain visible.
    pub fn set_residue_colors(&self, molecule: &Molecule, colors: &ResidueColors) {
        self.0.lock().set_residue_colors(molecule, colors);
    }
    /// Read the exact mapped color for a zero-based index in this molecule's
    /// residue array, so the sequence strip shares the 3D material palette.
    pub fn residue_color(&self, residue_index: usize) -> Option<Color32> {
        self.0
            .lock()
            .residue_color(residue_index)
            .map(|[r, g, b]| Color32::from_rgb(r, g, b))
    }
    /// Resolve the previous GPU frame's click before a caller captures a design
    /// intent. The input panel is drawn before the viewer, so consuming only in
    /// viewport() would otherwise omit a just-clicked hotspot from Run.
    pub fn consume_pick(
        &self,
        molecule: &Molecule,
        selected: &mut Option<ResidueKey>,
        hotspots: &mut Hotspots,
    ) -> bool {
        if let Some(picked) = self.0.lock().take_pick()
            && let Some(gesture) = hotspots.pending.take()
            && let Some(residue) = picked.and_then(|id| molecule.residues.get(id))
        {
            *selected = Some(residue.key.clone());
            hotspots.pick(molecule, &residue.key, gesture.toggle, gesture.range);
            return true;
        }
        false
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
    Surface,
}
impl Representation {
    pub fn name(self) -> &'static str {
        match self {
            Self::Cartoon => "cartoon",
            Self::Sticks => "sticks",
            Self::Spheres => "spheres",
            Self::Trace => "backbone trace",
            Self::Surface => "surface",
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
    hotspots: &mut Hotspots,
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
    if representation == Representation::Surface {
        renderer.0.lock().request_surface(molecule);
        if !renderer.surface_ready() {
            ui.ctx()
                .request_repaint_after(std::time::Duration::from_millis(50));
        }
    }
    if renderer.consume_pick(molecule, selected, hotspots) {
        ui.ctx().request_repaint();
    }
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
    if response.double_clicked()
        && !hotspots.enabled
        && !ui.input(|input| input.modifiers.ctrl || input.modifiers.command)
    {
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
    if representation == Representation::Surface && !renderer.surface_ready() {
        painter.text(
            draw.center(),
            Align2::CENTER_CENTER,
            renderer.0.lock().surface_message(),
            FontId::proportional(13.),
            Color32::from_gray(180),
        );
    }
    if draw.is_positive() && visible {
        let gpu = renderer.0.clone();
        let camera = *camera;
        let selected = selected
            .as_ref()
            .and_then(|key| molecule.residues.iter().position(|r| &r.key == key))
            .map_or(0, |index| index + 1);
        let chains = chains.to_vec();
        let hotspot_ids: Vec<_> = molecule
            .residues
            .iter()
            .enumerate()
            .filter_map(|(id, residue)| hotspots.residues.contains(&residue.key).then_some(id + 1))
            .collect();
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
                &hotspot_ids,
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
            && draw.contains(pointer)
        {
            let modifiers = ui.input(|input| input.modifiers);
            hotspots.pending = Some(PickGesture {
                toggle: modifiers.ctrl || modifiers.command || hotspots.enabled,
                range: modifiers.shift && hotspots.enabled,
            });
            renderer.0.lock().request_pick([
                (pointer.x - draw.left()) / draw.width(),
                (pointer.y - draw.top()) / draw.height(),
            ]);
            ui.ctx().request_repaint();
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
        if representation == Representation::Surface {
            renderer.0.lock().surface_message().to_owned()
        } else {
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
            )
        },
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

#[cfg(test)]
mod hotspot_tests {
    use super::*;
    #[test]
    fn hotspot_toggle_range_and_restore_preserve_exact_identity() {
        let molecule = Molecule::reference();
        let protein = &molecule
            .chains
            .iter()
            .find(|chain| chain.kind == MoleculeKind::Protein)
            .unwrap()
            .residues;
        let a = molecule.residues[protein[10]].key.clone();
        let b = molecule.residues[protein[15]].key.clone();
        let mut selection = Hotspots::default();
        selection.pick(&molecule, &a, false, false);
        assert!(selection.residues.is_empty());
        selection.pick(&molecule, &a, true, false);
        selection.pick(&molecule, &b, true, true);
        assert_eq!(selection.residues.len(), 6);
        selection.pick(&molecule, &a, true, false);
        assert_eq!(selection.residues.len(), 5);
        let mut restored: Hotspots =
            serde_json::from_value(serde_json::to_value(&selection).unwrap()).unwrap();
        let mut missing = a.clone();
        missing.sequence = "999999".into();
        restored.residues.insert(missing);
        restored.retain_existing(&molecule);
        assert_eq!(restored.residues, selection.residues);
    }
    #[test]
    fn nucleotide_clicks_never_become_protein_hotspots() {
        let molecule = Molecule::reference();
        let nucleotide = molecule
            .residues
            .iter()
            .find(|residue| matches!(residue.kind, MoleculeKind::Rna | MoleculeKind::Dna))
            .unwrap();
        let mut selection = Hotspots {
            enabled: true,
            ..Default::default()
        };
        selection.pick(&molecule, &nucleotide.key, true, false);
        assert!(selection.residues.is_empty());
        selection.residues.insert(nucleotide.key.clone());
        selection.retain_existing(&molecule);
        assert!(selection.residues.is_empty());
    }
}
