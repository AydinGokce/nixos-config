//! Static, coordinate-derived meshes. Constructed once, then retained on the GPU.
use super::{Atom, Molecule, V3, chain_color};
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
}
fn color(chain: char) -> V3 {
    let c = chain_color(chain);
    // Materials are lit in linear space, then tone mapped in the postprocess.
    V3(
        (c.r() as f32 / 255.).powf(2.2),
        (c.g() as f32 / 255.).powf(2.2),
        (c.b() as f32 / 255.).powf(2.2),
    )
}
fn atom_color(atom: &Atom) -> V3 {
    if atom.chain != 'A' {
        return color(atom.chain);
    }
    match atom.element.as_str() {
        "N" => V3(0.14, 0.28, 0.78),
        "O" => V3(0.83, 0.12, 0.085),
        "S" | "P" => V3(0.88, 0.56, 0.06),
        _ => color('A'),
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
                b.residue * (1. - t) + c.residue * t,
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
fn protein(molecule: &Molecule, cartoon: bool) -> Mesh {
    let mut mesh = Mesh::default();
    let mut guide = Vec::new();
    let oxygens: BTreeMap<_, _> = molecule
        .atoms
        .iter()
        .filter(|a| a.chain == 'A' && a.name == "O")
        .map(|a| (a.residue, a.p))
        .collect();
    let carbons: BTreeMap<_, _> = molecule
        .atoms
        .iter()
        .filter(|a| a.chain == 'A' && a.name == "C")
        .map(|a| (a.residue, a.p))
        .collect();
    let mut previous = V3::default();
    for (i, (&point, &id)) in molecule.ca.iter().zip(&molecule.ca_ids).enumerate() {
        if i > 0
            && (point.sub(molecule.ca[i - 1]).length() > 4.8 || id != molecule.ca_ids[i - 1] + 1)
        {
            sweep(&mut mesh, &guide, color('A'));
            guide.clear();
            previous = V3::default();
        }
        let helix = molecule.helices.iter().any(|&(a, b)| (a..=b).contains(&id));
        let sheet = molecule
            .sheets
            .iter()
            .find(|&&(a, b)| (a..=b).contains(&id));
        let (mut width, height) = if !cartoon {
            (0.24, 0.24)
        } else if helix {
            (1.18, 0.25)
        } else if sheet.is_some() {
            (1.35, 0.24)
        } else {
            (0.26, 0.26)
        };
        if cartoon && let Some(&(_, end)) = sheet {
            if id + 1 == end {
                width = 1.85;
            } else if id == end {
                width = 0.22;
            }
        }
        let mut wide = oxygens
            .get(&id)
            .zip(carbons.get(&id))
            .map_or(V3(0., 1., 0.), |(o, c)| o.sub(*c).unit());
        if wide.dot(previous) < 0. {
            wide = wide.mul(-1.);
        }
        previous = wide;
        // Suppress beta-strand zigzag while preserving actual coordinate origin.
        let p = if cartoon
            && sheet.is_some()
            && i > 0
            && i + 1 < molecule.ca.len()
            && molecule.ca[i - 1].sub(point).length() < 4.8
            && molecule.ca[i + 1].sub(point).length() < 4.8
        {
            point
                .mul(0.6)
                .add(molecule.ca[i - 1].add(molecule.ca[i + 1]).mul(0.2))
        } else {
            point
        };
        guide.push(Guide {
            p: p.sub(molecule.center),
            wide,
            width,
            height,
            residue: id as f32,
        });
    }
    sweep(&mut mesh, &guide, color('A'));
    mesh
}
fn nucleic(molecule: &Molecule, chain: char, bases: bool) -> Mesh {
    let mut mesh = Mesh::default();
    let mut guide: Vec<Guide> = Vec::new();
    for atom in molecule
        .atoms
        .iter()
        .filter(|a| a.chain == chain && a.name == "P")
    {
        let p = atom.p.sub(molecule.center);
        if let Some(last) = guide.last()
            && last.p.sub(p).length() > 9.
        {
            sweep(&mut mesh, &guide, color(chain));
            guide.clear();
        }
        guide.push(Guide {
            p,
            wide: V3(0., 1., 0.),
            width: 0.48,
            height: 0.48,
            residue: -1.,
        });
    }
    sweep(&mut mesh, &guide, color(chain));
    if !bases {
        return mesh;
    }
    let mut residues: BTreeMap<usize, BTreeMap<&str, V3>> = BTreeMap::new();
    for atom in molecule.atoms.iter().filter(|a| a.chain == chain) {
        residues
            .entry(atom.residue)
            .or_default()
            .insert(&atom.name, atom.p.sub(molecule.center));
    }
    for residue in residues.values() {
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
        let center = points
            .iter()
            .fold(V3::default(), |s, &p| s.add(p))
            .mul(1. / points.len() as f32);
        let c = color(chain).mul(0.82);
        for sign in [-1., 1.] {
            let normal = n.mul(sign);
            let middle = mesh.vertex(center.add(normal.mul(0.11)), normal, c, -1.);
            let first = mesh.vertices.len() as u32;
            for &point in &points {
                mesh.vertex(point.add(normal.mul(0.11)), normal, c, -1.);
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
                    mesh.cylinder(points[i], points[(i + 1) % points.len()], 0.12, c, -1.);
                }
            }
        }
        for (a, b) in [
            ("P", "C4'"),
            ("C4'", "C1'"),
            ("C1'", if purine { "N9" } else { "N1" }),
        ] {
            if let Some((&a, &b)) = residue.get(a).zip(residue.get(b)) {
                mesh.cylinder(a, b, 0.20, color(chain), -1.);
            }
        }
    }
    mesh
}
pub(super) fn build(molecule: &Molecule) -> [Chain; 3] {
    std::array::from_fn(|index| {
        let chain = (b'A' + index as u8) as char;
        let cartoon = if chain == 'A' {
            protein(molecule, true)
        } else {
            nucleic(molecule, chain, true)
        };
        let trace = if chain == 'A' {
            protein(molecule, false)
        } else {
            nucleic(molecule, chain, false)
        };
        let mut sticks = Mesh::default();
        for &(a, b) in &molecule.bonds {
            let a = &molecule.atoms[a];
            let b = &molecule.atoms[b];
            if a.chain != chain {
                continue;
            }
            let pa = a.p.sub(molecule.center);
            let pb = b.p.sub(molecule.center);
            let mid = pa.add(pb).mul(0.5);
            for (atom, p) in [(a, pa), (b, pb)] {
                sticks.cylinder(
                    p,
                    mid,
                    0.135,
                    atom_color(atom),
                    if chain == 'A' {
                        atom.residue as f32
                    } else {
                        -1.
                    },
                );
            }
        }
        let atoms = molecule
            .atoms
            .iter()
            .filter(|a| a.chain == chain)
            .map(|atom| {
                let p = atom.p.sub(molecule.center);
                let c = atom_color(atom);
                let radius = match atom.element.as_str() {
                    "O" => 1.52,
                    "N" => 1.55,
                    "S" | "P" => 1.80,
                    _ => 1.70,
                };
                [
                    p.0,
                    p.1,
                    p.2,
                    radius,
                    c.0,
                    c.1,
                    c.2,
                    if chain == 'A' {
                        atom.residue as f32
                    } else {
                        -1.
                    },
                ]
            })
            .collect();
        Chain {
            cartoon,
            trace,
            sticks,
            atoms,
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
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
