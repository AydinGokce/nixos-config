//! Static, coordinate-derived meshes. Constructed once, then retained on the GPU.
use super::{Atom, Molecule, MoleculeKind, Secondary, V3};
use std::collections::BTreeMap;
use std::f32::consts::TAU;

#[derive(Default)]
pub(super) struct Mesh {
    // position, normal, linear material color, residue ID (protein only)
    pub vertices: Vec<[f32; 10]>,
    pub indices: Vec<u32>,
}
pub(super) struct Chain {
    pub cartoon: Mesh,
    pub trace: Mesh,
    pub sticks: Mesh,
    pub atoms: Vec<[f32; 8]>,
    pub details: Vec<[f32; 8]>,
}
fn color(molecule: &Molecule, chain: usize) -> V3 {
    let c = molecule.chains[chain].color;
    V3(
        (c.r() as f32 / 255.).powf(2.2),
        (c.g() as f32 / 255.).powf(2.2),
        (c.b() as f32 / 255.).powf(2.2),
    )
}
fn atom_color(molecule: &Molecule, atom: &Atom) -> V3 {
    if matches!(
        molecule.residues[atom.residue].kind,
        MoleculeKind::Rna | MoleculeKind::Dna
    ) {
        return color(molecule, atom.chain);
    }
    match atom.element.as_str() {
        "N" => V3(0.14, 0.28, 0.78),
        "O" => V3(0.83, 0.12, 0.085),
        "S" | "P" => V3(0.88, 0.56, 0.06),
        "H" | "D" => V3(0.65, 0.65, 0.65),
        "CL" | "F" => V3(0.18, 0.72, 0.16),
        _ => color(molecule, atom.chain),
    }
}
impl Mesh {
    fn vertex(&mut self, p: V3, normal: V3, color: V3, residue: f32) -> u32 {
        let index = self.vertices.len() as u32;
        self.vertices.push([
            p.0, p.1, p.2, normal.0, normal.1, normal.2, color.0, color.1, color.2, residue,
        ]);
        index
    }
    fn triangle(&mut self, a: u32, b: u32, c: u32) {
        self.indices.extend_from_slice(&[a, b, c]);
    }
    fn cylinder(&mut self, a: V3, b: V3, radius: f32, color: V3, residue: f32) {
        if b.sub(a).length() < 0.001 {
            return;
        }
        let tangent = b.sub(a).unit();
        let axis = if tangent.0.abs() < 0.8 {
            V3(1., 0., 0.)
        } else {
            V3(0., 1., 0.)
        };
        let wide = tangent.cross(axis).unit();
        let high = tangent.cross(wide).unit();
        let base = self.vertices.len() as u32;
        const SIDES: u32 = 10;
        for point in [a, b] {
            for i in 0..SIDES {
                let angle = i as f32 * TAU / SIDES as f32;
                let n = wide.mul(angle.cos()).add(high.mul(angle.sin()));
                self.vertex(point.add(n.mul(radius)), n, color, residue);
            }
        }
        for i in 0..SIDES {
            let next = (i + 1) % SIDES;
            self.triangle(base + i, base + next, base + SIDES + i);
            self.triangle(base + next, base + SIDES + next, base + SIDES + i);
        }
    }
    fn smooth_normals(&mut self, start: usize, triangles: usize) {
        for v in &mut self.vertices[start..] {
            v[3..6].fill(0.);
        }
        for face in self.indices[triangles..].chunks_exact(3) {
            let point = |i: u32| {
                let p = self.vertices[i as usize];
                V3(p[0], p[1], p[2])
            };
            let n = point(face[1])
                .sub(point(face[0]))
                .cross(point(face[2]).sub(point(face[0])));
            for &index in face {
                let v = &mut self.vertices[index as usize];
                v[3] += n.0;
                v[4] += n.1;
                v[5] += n.2;
            }
        }
        for v in &mut self.vertices[start..] {
            let n = V3(v[3], v[4], v[5]).unit();
            let n = if n.length() < 0.5 { V3(0., 1., 0.) } else { n };
            v[3] = n.0;
            v[4] = n.1;
            v[5] = n.2;
        }
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
#[derive(Clone, Copy)]
struct Guide {
    p: V3,
    wide: V3,
    width: f32,
    height: f32,
    residue: f32,
}

/// Shared rings, continuously transported frames, and area-weighted normals.
/// No independent per-segment panels or camera-dependent geometry rebuilding.
fn sweep(mesh: &mut Mesh, guides: &[Guide], color: V3) {
    if guides.len() < 2 {
        return;
    }
    let start = mesh.vertices.len();
    let triangles = mesh.indices.len();
    const STEPS: usize = 12;
    const SIDES: usize = 16;
    let mut prior = V3::default();
    let mut samples = Vec::with_capacity(guides.len() * STEPS);
    for i in 0..guides.len() - 1 {
        let a = guides[i.saturating_sub(1)];
        let b = guides[i];
        let c = guides[i + 1];
        let d = guides[(i + 2).min(guides.len() - 1)];
        for step in 0..STEPS {
            let t = step as f32 / STEPS as f32;
            let ease = t * t * (3. - 2. * t);
            let p = spline(a.p, b.p, c.p, d.p, t);
            let q = spline(a.p, b.p, c.p, d.p, (t + 0.002).min(1.));
            let tangent = q.sub(p).unit();
            let preferred = b.wide.mul(1. - t).add(c.wide.mul(t));
            let mut guide = preferred.sub(tangent.mul(preferred.dot(tangent))).unit();
            let transported = prior.sub(tangent.mul(prior.dot(tangent))).unit();
            if guide.length() < 0.1 {
                let axis = if tangent.0.abs() < 0.8 {
                    V3(1., 0., 0.)
                } else {
                    V3(0., 1., 0.)
                };
                guide = tangent.cross(axis).unit();
            }
            if guide.dot(transported) < 0. {
                guide = guide.mul(-1.);
            }
            let wide = if transported.length() > 0.1 {
                transported.mul(0.55).add(guide.mul(0.45)).unit()
            } else {
                guide
            };
            prior = wide;
            let high = tangent.cross(wide).unit();
            samples.push((
                p,
                wide,
                high,
                b.width * (1. - ease) + c.width * ease,
                b.height * (1. - ease) + c.height * ease,
                if t < 0.5 { b.residue } else { c.residue },
            ));
        }
    }
    let last = guides[guides.len() - 1];
    let tangent = last.p.sub(guides[guides.len() - 2].p).unit();
    let wide = prior.sub(tangent.mul(prior.dot(tangent))).unit();
    samples.push((
        last.p,
        wide,
        tangent.cross(wide).unit(),
        last.width,
        last.height,
        last.residue,
    ));
    for &(p, wide, high, width, height, residue) in &samples {
        for side in 0..SIDES {
            let angle = side as f32 * TAU / SIDES as f32;
            let point = p
                .add(wide.mul(width * angle.cos()))
                .add(high.mul(height * angle.sin()));
            mesh.vertex(point, V3::default(), color, residue);
        }
    }
    for row in 0..samples.len() - 1 {
        for side in 0..SIDES {
            let a = (start + row * SIDES + side) as u32;
            let b = (start + row * SIDES + (side + 1) % SIDES) as u32;
            let c = a + SIDES as u32;
            let d = b + SIDES as u32;
            // Outward winding for the parallel-transported ring basis.
            mesh.triangle(a, b, c);
            mesh.triangle(b, d, c);
        }
    }
    for (sample, row, reverse) in [
        (samples[0], 0, true),
        (samples[samples.len() - 1], samples.len() - 1, false),
    ] {
        let center = mesh.vertex(sample.0, V3::default(), color, sample.5);
        for side in 0..SIDES {
            let a = (start + row * SIDES + side) as u32;
            let b = (start + row * SIDES + (side + 1) % SIDES) as u32;
            if reverse {
                mesh.triangle(center, b, a);
            } else {
                mesh.triangle(center, a, b);
            }
        }
    }
    mesh.smooth_normals(start, triangles);
}
fn atom_named(molecule: &Molecule, residue: usize, name: &str) -> Option<V3> {
    molecule.residues[residue]
        .atoms
        .iter()
        .find_map(|&i| (molecule.atoms[i].name == name).then_some(molecule.atoms[i].p))
}
fn protein(molecule: &Molecule, chain: usize, cartoon: bool) -> Mesh {
    let mut mesh = Mesh::default();
    let mut guide = Vec::new();
    let mut previous = V3::default();
    let ids = &molecule.chains[chain].residues;
    for (i, &id) in ids.iter().enumerate() {
        let residue = &molecule.residues[id];
        let point =
            atom_named(molecule, id, "CA").filter(|_| residue.kind == MoleculeKind::Protein);
        if !residue.connected_to_previous || point.is_none() {
            sweep(&mut mesh, &guide, color(molecule, chain));
            guide.clear();
            previous = V3::default();
        }
        let Some(point) = point else {
            continue;
        };
        let next = ids
            .get(i + 1)
            .map(|&r| &molecule.residues[r])
            .filter(|r| r.connected_to_previous);
        let after = ids
            .get(i + 2)
            .map(|&r| &molecule.residues[r])
            .filter(|r| r.connected_to_previous);
        let (mut width, height) = if !cartoon {
            (0.24, 0.24)
        } else {
            match residue.secondary {
                Secondary::Helix => (1.18, 0.25),
                Secondary::Sheet => (1.35, 0.24),
                Secondary::Coil => (0.26, 0.26),
            }
        };
        if cartoon && residue.secondary == Secondary::Sheet {
            if next.is_none_or(|r| r.secondary != Secondary::Sheet) {
                width = 0.22;
            } else if after.is_none_or(|r| r.secondary != Secondary::Sheet) {
                width = 1.85;
            }
        }
        let mut wide = atom_named(molecule, id, "O")
            .zip(atom_named(molecule, id, "C"))
            .map_or(V3(0., 1., 0.), |(o, c)| o.sub(c).unit());
        if wide.dot(previous) < 0. {
            wide = wide.mul(-1.);
        }
        previous = wide;
        let mut p = point;
        if cartoon
            && residue.secondary == Secondary::Sheet
            && residue.connected_to_previous
            && i > 0
            && next.is_some()
            && let Some((a, b)) =
                atom_named(molecule, ids[i - 1], "CA").zip(atom_named(molecule, ids[i + 1], "CA"))
        {
            p = point.mul(0.6).add(a.add(b).mul(0.2));
        }
        guide.push(Guide {
            p: p.sub(molecule.center),
            wide,
            width,
            height,
            residue: (id + 1) as f32,
        });
    }
    sweep(&mut mesh, &guide, color(molecule, chain));
    mesh
}
fn nucleic(molecule: &Molecule, chain: usize, bases: bool) -> Mesh {
    let mut mesh = Mesh::default();
    let mut guide: Vec<Guide> = Vec::new();
    for &id in &molecule.chains[chain].residues {
        let residue = &molecule.residues[id];
        if !residue.connected_to_previous
            || !matches!(residue.kind, MoleculeKind::Rna | MoleculeKind::Dna)
        {
            sweep(&mut mesh, &guide, color(molecule, chain));
            guide.clear();
        }
        if !matches!(residue.kind, MoleculeKind::Rna | MoleculeKind::Dna) {
            continue;
        }
        let p = molecule.atoms[residue.anchor].p.sub(molecule.center);
        guide.push(Guide {
            p,
            wide: V3(0., 1., 0.),
            width: 0.48,
            height: 0.48,
            residue: (id + 1) as f32,
        });
    }
    sweep(&mut mesh, &guide, color(molecule, chain));
    if !bases {
        return mesh;
    }
    for &id in &molecule.chains[chain].residues {
        let r = &molecule.residues[id];
        if !matches!(r.kind, MoleculeKind::Rna | MoleculeKind::Dna) {
            continue;
        }
        let residue: BTreeMap<_, _> = r
            .atoms
            .iter()
            .map(|&i| {
                (
                    &*molecule.atoms[i].name,
                    molecule.atoms[i].p.sub(molecule.center),
                )
            })
            .collect();
        let selected = (id + 1) as f32;
        let purine = residue.contains_key("N9");
        let ring: &[&str] = if purine {
            &["N9", "C8", "N7", "C5", "C6", "N1", "C2", "N3", "C4"]
        } else {
            &["N1", "C2", "N3", "C4", "C5", "C6"]
        };
        let points: Vec<_> = ring
            .iter()
            .filter_map(|name| residue.get(name).copied())
            .collect();
        if points.len() != ring.len() {
            continue;
        }
        let n = points[1]
            .sub(points[0])
            .cross(points[2].sub(points[0]))
            .unit();
        if n.length() < 0.9 {
            continue;
        }
        let center = points
            .iter()
            .fold(V3::default(), |s, &p| s.add(p))
            .mul(1. / points.len() as f32);
        let c = color(molecule, chain).mul(0.82);
        for sign in [-1., 1.] {
            let normal = n.mul(sign);
            let middle = mesh.vertex(center.add(normal.mul(0.11)), normal, c, selected);
            let first = mesh.vertices.len() as u32;
            for &point in &points {
                mesh.vertex(point.add(normal.mul(0.11)), normal, c, selected);
            }
            for i in 0..points.len() {
                let a = first + i as u32;
                let b = first + ((i + 1) % points.len()) as u32;
                if sign > 0. {
                    mesh.triangle(middle, a, b);
                } else {
                    mesh.triangle(middle, b, a);
                }
                if sign > 0. {
                    mesh.cylinder(points[i], points[(i + 1) % points.len()], 0.12, c, selected);
                }
            }
        }
        for (a, b) in [
            ("P", "C4'"),
            ("C4'", "C1'"),
            ("C1'", if purine { "N9" } else { "N1" }),
        ] {
            if let Some((&a, &b)) = residue.get(a).zip(residue.get(b)) {
                mesh.cylinder(a, b, 0.20, color(molecule, chain), selected);
            }
        }
    }
    mesh
}
impl Mesh {
    fn append(&mut self, other: Self) {
        let offset = self.vertices.len() as u32;
        self.vertices.extend(other.vertices);
        self.indices
            .extend(other.indices.into_iter().map(|i| i + offset));
    }
}
pub(super) fn build(molecule: &Molecule) -> Vec<Chain> {
    // All nonpolymers and incomplete/single-residue polymers remain visible in cartoon/trace.
    let mut detail = vec![true; molecule.residues.len()];
    for chain in &molecule.chains {
        for pair in chain.residues.windows(2) {
            let (a, b) = (pair[0], pair[1]);
            if molecule.residues[b].connected_to_previous {
                let valid = |r: usize| {
                    molecule.residues[r].kind != MoleculeKind::Protein
                        || atom_named(molecule, r, "CA").is_some()
                };
                if valid(a) && valid(b) {
                    detail[a] = false;
                    detail[b] = false;
                }
            }
        }
    }
    molecule
        .chains
        .iter()
        .enumerate()
        .map(|(chain, _)| {
            let mut cartoon = protein(molecule, chain, true);
            cartoon.append(nucleic(molecule, chain, true));
            let mut trace = protein(molecule, chain, false);
            trace.append(nucleic(molecule, chain, false));
            let mut sticks = Mesh::default();
            for &(ai, bi) in &molecule.bonds {
                let (a, b) = (&molecule.atoms[ai], &molecule.atoms[bi]);
                let (pa, pb) = (a.p.sub(molecule.center), b.p.sub(molecule.center));
                let mid = pa.add(pb).mul(0.5);
                // A cross-chain explicit bond is split so each half follows its chain toggle.
                for (atom, p) in [(a, pa), (b, pb)] {
                    if atom.chain != chain {
                        continue;
                    }
                    let c = atom_color(molecule, atom);
                    let residue = (atom.residue + 1) as f32;
                    sticks.cylinder(p, mid, 0.135, c, residue);
                    if detail[atom.residue] {
                        cartoon.cylinder(p, mid, 0.16, c, residue);
                        trace.cylinder(p, mid, 0.16, c, residue);
                    }
                }
            }
            let mut atoms = Vec::new();
            let mut details = Vec::new();
            for atom in molecule.atoms.iter().filter(|a| a.chain == chain) {
                let p = atom.p.sub(molecule.center);
                let c = atom_color(molecule, atom);
                let radius = match atom.element.as_str() {
                    "H" | "D" => 1.20,
                    "O" => 1.52,
                    "N" => 1.55,
                    "S" | "P" => 1.80,
                    "CL" => 1.75,
                    "F" => 1.47,
                    _ => 1.70,
                };
                let data = [
                    p.0,
                    p.1,
                    p.2,
                    radius,
                    c.0,
                    c.1,
                    c.2,
                    (atom.residue + 1) as f32,
                ];
                atoms.push(data);
                if detail[atom.residue] {
                    details.push(data);
                }
            }
            Chain {
                cartoon,
                trace,
                sticks,
                atoms,
                details,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ligands_and_isolated_polymer_residues_stay_visible_without_gap_ribbons() {
        let bytes=b"data_test\nloop_\n_atom_site.group_PDB\n_atom_site.id\n_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_comp_id\n_atom_site.label_asym_id\n_atom_site.label_seq_id\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\nATOM 1 C CA ALA protein_long 1 0 0 0\nATOM 2 C CA ALA protein_long 3 3.8 0 0\nHETATM 3 C C1 X12 ligand . 10 0 0\nHETATM 4 N N1 X12 ligand . 11.4 0 0\nHETATM 5 O O HOH water . 15 0 0\nATOM 6 C CA GLY fourth 1 20 0 0\n";
        let molecule = Molecule::parse(bytes, "cif", "mixed").unwrap();
        let chains = build(&molecule);
        assert_eq!(chains.len(), 4);
        assert!(chains[0].cartoon.indices.is_empty());
        assert_eq!(chains[0].details.len(), 2);
        assert!(!chains[1].cartoon.indices.is_empty());
        assert_eq!(chains[1].details.len(), 2);
        assert_eq!(chains[2].details.len(), 1);
        assert_eq!(chains[3].details.len(), 1);
        assert!(chains[1].cartoon.vertices.iter().all(|v| v[9] == 3.));
    }
    #[test]
    fn cas9_meshes_have_finite_unit_normals_and_valid_indices() {
        let molecule = Molecule::reference();
        let chains = build(&molecule);
        assert_eq!(chains.iter().map(|c| c.atoms.len()).sum::<usize>(), 12485);
        for chain in &chains {
            for mesh in [&chain.cartoon, &chain.trace, &chain.sticks] {
                assert!(!mesh.indices.is_empty());
                assert!(
                    mesh.indices
                        .iter()
                        .all(|&i| (i as usize) < mesh.vertices.len())
                );
                for v in &mesh.vertices {
                    assert!(v.iter().all(|n| n.is_finite()));
                    let length = V3(v[3], v[4], v[5]).length();
                    assert!((length - 1.).abs() < 0.001, "normal length {length}");
                }
            }
        }
    }
}
