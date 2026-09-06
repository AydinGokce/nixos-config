use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Vec2};
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
}
impl Default for Camera {
    fn default() -> Self {
        Self {
            yaw: -0.35,
            pitch: 0.15,
            zoom: 1.1,
            pan: Vec2::ZERO,
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
        let perspective = 320. / (320. - p.2);
        (
            rect.center() + self.pan + Vec2::new(p.0, -p.1) * scale * perspective,
            p.2,
        )
    }
}
fn tint(c: Color32, s: f32) -> Color32 {
    Color32::from_rgb(
        (c.r() as f32 * s).min(255.) as u8,
        (c.g() as f32 * s).min(255.) as u8,
        (c.b() as f32 * s).min(255.) as u8,
    )
}
fn residue_color(i: f32) -> Color32 {
    tint(chain_color('A'), 0.86 + i / 1368. * 0.20)
}
fn atom_color(a: &Atom) -> Color32 {
    if a.chain != 'A' {
        return chain_color(a.chain);
    }
    match a.element.as_str() {
        "N" => Color32::from_rgb(97, 132, 242),
        "O" => Color32::from_rgb(234, 88, 80),
        "S" => Color32::from_rgb(233, 205, 80),
        _ => Color32::from_rgb(80, 183, 152),
    }
}
fn spline(a: V3, b: V3, c: V3, d: V3, t: f32) -> V3 {
    b.mul(2.)
        .add(c.sub(a).mul(t))
        .add(a.mul(2.).sub(b.mul(5.)).add(c.mul(4.)).sub(d).mul(t * t))
        .add(
            a.mul(-1.)
                .add(b.mul(3.))
                .sub(c.mul(3.))
                .add(d)
                .mul(t * t * t),
        )
        .mul(0.5)
}
#[allow(clippy::too_many_arguments)]
pub fn viewport(
    ui: &mut egui::Ui,
    molecule: &Molecule,
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
        let d = ui.input(|i| i.pointer.delta());
        if response.dragged_by(egui::PointerButton::Secondary) || ui.input(|i| i.modifiers.shift) {
            camera.pan += d;
        } else {
            camera.yaw += d.x * 0.008;
            camera.pitch += d.y * 0.008;
        }
        changed = true;
    }
    if response.hovered() {
        let s = ui.input(|i| i.smooth_scroll_delta.y);
        if s != 0. {
            camera.zoom = (camera.zoom * (s * 0.002).exp()).clamp(0.25, 4.);
            changed = true;
        }
    }
    if response.double_clicked() {
        *camera = Camera::default();
        changed = true;
    }
    let draw = Rect::from_min_max(rect.min + Vec2::new(8., 43.), rect.max - Vec2::new(8., 32.));
    if visible {
        match representation {
            Representation::Cartoon => {
                let mut faces = Vec::new();
                for i in 0..molecule.ca.len() - 1 {
                    if !chains[0] {
                        continue;
                    }
                    let a = molecule.ca[i.saturating_sub(1)];
                    let b = molecule.ca[i];
                    let c = molecule.ca[i + 1];
                    let d = molecule.ca[(i + 2).min(molecule.ca.len() - 1)];
                    if b.sub(c).length() > 5.0 {
                        continue;
                    }
                    for step in 0..7 {
                        let t = step as f32 / 7.;
                        let u = (step + 1) as f32 / 7.;
                        let p = spline(a, b, c, d, t);
                        let q = spline(a, b, c, d, u);
                        let tangent = q.sub(p).unit();
                        let normal = tangent.cross(V3(0.2, 0.7, 1.)).unit();
                        let residue = molecule.ca_ids[i];
                        let sheet = molecule
                            .sheets
                            .iter()
                            .any(|(a, b)| (*a..=*b).contains(&residue));
                        let width = if sheet {
                            1.0
                        } else if molecule
                            .helices
                            .iter()
                            .any(|(a, b)| (*a..=*b).contains(&residue))
                        {
                            0.85
                        } else {
                            0.22
                        };
                        let points = [
                            p.add(normal.mul(width)),
                            q.add(normal.mul(width)),
                            q.sub(normal.mul(width)),
                            p.sub(normal.mul(width)),
                        ];
                        let projected: Vec<_> = points
                            .iter()
                            .map(|p| camera.project(*p, draw, molecule.center))
                            .collect();
                        let z = projected.iter().map(|p| p.1).sum::<f32>() / 4.;
                        let nv = camera.rotate(normal.cross(tangent));
                        let shade = (0.52 + nv.2.abs() * 0.35 + (z + 50.) / 450.).clamp(0.32, 1.10);
                        faces.push((
                            z,
                            projected.into_iter().map(|p| p.0).collect::<Vec<_>>(),
                            tint(residue_color(residue as f32), shade),
                        ));
                    }
                }
                faces.sort_by(|a, b| a.0.total_cmp(&b.0));
                // Explicit triangles avoid antialiasing miter artifacts in
                // very narrow projected ribbons. Depth ordering remains local.
                let mut mesh = egui::Mesh::default();
                for (_, points, color) in faces {
                    let start = mesh.vertices.len() as u32;
                    for pos in points {
                        mesh.vertices.push(egui::epaint::Vertex {
                            pos,
                            uv: egui::epaint::WHITE_UV,
                            color,
                        });
                    }
                    mesh.indices.extend_from_slice(&[
                        start,
                        start + 1,
                        start + 2,
                        start,
                        start + 2,
                        start + 3,
                    ]);
                }
                painter.add(egui::Shape::mesh(mesh));
            }
            Representation::Trace => {
                let mut segments: Vec<_> = molecule
                    .ca
                    .windows(2)
                    .enumerate()
                    .filter(|(_, w)| chains[0] && w[0].sub(w[1]).length() < 5.)
                    .map(|(i, w)| {
                        let (a, z) = camera.project(w[0], draw, molecule.center);
                        let (b, v) = camera.project(w[1], draw, molecule.center);
                        (z + v, [a, b], residue_color(i as f32))
                    })
                    .collect();
                segments.sort_by(|a, b| a.0.total_cmp(&b.0));
                for (_, line, color) in segments {
                    painter.line_segment(line, Stroke::new(3.5 * camera.zoom, color));
                }
            }
            Representation::Sticks | Representation::Spheres => {
                let projected: Vec<_> = molecule
                    .atoms
                    .iter()
                    .map(|a| camera.project(a.p, draw, molecule.center))
                    .collect();
                if representation == Representation::Sticks {
                    let mut bonds = molecule.bonds.clone();
                    bonds.sort_by(|(a, b), (c, d)| {
                        (projected[*a].1 + projected[*b].1)
                            .total_cmp(&(projected[*c].1 + projected[*d].1))
                    });
                    for (a, b) in bonds {
                        if !chains[(molecule.atoms[a].chain as u8 - b'A') as usize] {
                            continue;
                        }
                        let mid = projected[a].0.lerp(projected[b].0, 0.5);
                        for (atom, line) in [(a, [projected[a].0, mid]), (b, [mid, projected[b].0])]
                        {
                            let shade = ((projected[atom].1 + 100.) / 150.).clamp(0.35, 1.);
                            painter.line_segment(
                                line,
                                Stroke::new(
                                    (0.9 * camera.zoom).max(0.5),
                                    tint(atom_color(&molecule.atoms[atom]), shade),
                                ),
                            );
                        }
                    }
                }
                let mut atoms: Vec<_> = (0..molecule.atoms.len())
                    .filter(|i| chains[(molecule.atoms[*i].chain as u8 - b'A') as usize])
                    .collect();
                atoms.sort_by(|a, b| projected[*a].1.total_cmp(&projected[*b].1));
                for atom in atoms {
                    let (p, z) = projected[atom];
                    let r = if representation == Representation::Spheres {
                        2.7 * camera.zoom
                    } else {
                        0.45 * camera.zoom
                    };
                    let c = tint(
                        atom_color(&molecule.atoms[atom]),
                        ((z + 100.) / 150.).clamp(0.35, 1.),
                    );
                    painter.circle_filled(p, r, c);
                    if representation == Representation::Spheres {
                        painter.circle_filled(p - Vec2::splat(r * 0.25), r * 0.42, tint(c, 1.3));
                    }
                }
            }
        }
        if matches!(
            representation,
            Representation::Cartoon | Representation::Trace
        ) {
            for chain in ['B', 'C'] {
                if !chains[(chain as u8 - b'A') as usize] {
                    continue;
                }
                let points: Vec<_> = molecule
                    .atoms
                    .iter()
                    .filter(|a| a.chain == chain && a.name == "P")
                    .collect();
                for segment in points.windows(2) {
                    if segment[0].p.sub(segment[1].p).length() > 9. {
                        continue;
                    }
                    let (a, z) = camera.project(segment[0].p, draw, molecule.center);
                    let (b, _) = camera.project(segment[1].p, draw, molecule.center);
                    let color = tint(chain_color(chain), ((z + 120.) / 170.).clamp(0.55, 1.));
                    painter.line_segment([a, b], Stroke::new(3.0 * camera.zoom, color));
                    painter.circle_filled(a, 1.8 * camera.zoom, color);
                }
            }
        }
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
            if let Some((i, d)) = nearest
                && d < 24.
            {
                *selected = molecule.ca_ids[i];
            }
        }
        if chains[0] && (labels || *selected > 0) {
            let residue = if *selected > 0 { *selected } else { 840 };
            if let Some(p) = molecule
                .ca_ids
                .iter()
                .position(|id| *id == residue)
                .map(|i| &molecule.ca[i])
            {
                let (point, _) = camera.project(*p, draw, molecule.center);
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
