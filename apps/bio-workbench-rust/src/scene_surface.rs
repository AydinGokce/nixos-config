//! Bounded, coordinate-derived solvent-accessible molecular envelopes.
//! Marching tetrahedra samples the union of van der Waals balls expanded by a
//! 1.4 Å solvent probe. This is an approximate SAS, not a solvent-excluded surface
//! or an electrostatic/affinity calculation. Each chain retains its own envelope
//! so isolating a chain exposes its interface without rebuilding the geometry.
use super::{Molecule, V3, geometry::Mesh};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

const PROBE: f32 = 1.4;
const GRID_CELLS: usize = 2_000_000;
const MAX_TRIANGLES: usize = 650_000;
const MAX_CHAINS: usize = 256;
const TIME_LIMIT: Duration = Duration::from_secs(25);

#[derive(Clone, Copy)]
struct Ball {
    p: V3,
    radius: f32,
    residue: usize,
}
pub(super) struct Input {
    chains: Vec<(Vec<Ball>, V3)>,
}
pub(super) struct Surface {
    pub chains: Vec<Mesh>,
    pub spacing: f32,
    pub triangles: usize,
}
pub(super) fn radius(element: &str) -> f32 {
    match element {
        "H" | "D" => 1.20,
        "O" => 1.52,
        "N" => 1.55,
        "S" | "P" => 1.80,
        "CL" => 1.75,
        "F" => 1.47,
        _ => 1.70,
    }
}
impl Input {
    pub fn new(molecule: &Molecule) -> Result<Self, String> {
        if molecule.chains.len() > MAX_CHAINS {
            return Err(format!(
                "Surface display supports at most {MAX_CHAINS} chains; isolate a smaller structure."
            ));
        }
        let mut chains: Vec<_> = molecule
            .chains
            .iter()
            .map(|chain| {
                let c = chain.color;
                (
                    Vec::new(),
                    V3(
                        (c.r() as f32 / 255.).powf(2.2),
                        (c.g() as f32 / 255.).powf(2.2),
                        (c.b() as f32 / 255.).powf(2.2),
                    ),
                )
            })
            .collect();
        for atom in &molecule.atoms {
            chains[atom.chain].0.push(Ball {
                p: atom.p.sub(molecule.center),
                radius: radius(&atom.element) + PROBE,
                residue: atom.residue,
            });
        }
        Ok(Self { chains })
    }
}
struct Grid {
    origin: V3,
    step: f32,
    size: [usize; 3],
    field: Vec<f32>,
    owners: Vec<usize>,
}
impl Grid {
    fn bounds(balls: &[Ball], step: f32) -> (V3, [usize; 3]) {
        let mut low = V3(f32::INFINITY, f32::INFINITY, f32::INFINITY);
        let mut high = V3(f32::NEG_INFINITY, f32::NEG_INFINITY, f32::NEG_INFINITY);
        for ball in balls {
            let r = ball.radius + 2. * step;
            low.0 = low.0.min(ball.p.0 - r);
            low.1 = low.1.min(ball.p.1 - r);
            low.2 = low.2.min(ball.p.2 - r);
            high.0 = high.0.max(ball.p.0 + r);
            high.1 = high.1.max(ball.p.1 + r);
            high.2 = high.2.max(ball.p.2 + r);
        }
        let span = high.sub(low);
        let size = [span.0, span.1, span.2]
            .map(|value| ((value / step).ceil() as usize).saturating_add(1).max(3));
        (low, size)
    }
    fn new(balls: &[Ball], step: f32) -> Result<Self, String> {
        let (origin, size) = Self::bounds(balls, step);
        let count = cells(size);
        if count > GRID_CELLS {
            return Err("Surface grid limit exceeded".into());
        }
        Ok(Self {
            origin,
            step,
            size,
            field: vec![2. * step; count],
            owners: vec![usize::MAX; count],
        })
    }
    fn index(&self, x: usize, y: usize, z: usize) -> usize {
        x + self.size[0] * (y + self.size[1] * z)
    }
    fn point(&self, x: usize, y: usize, z: usize) -> V3 {
        self.origin
            .add(V3(x as f32, y as f32, z as f32).mul(self.step))
    }
    fn gradient(&self, x: usize, y: usize, z: usize) -> V3 {
        let axis = |a: [usize; 3], b: [usize; 3]| {
            self.field[self.index(a[0], a[1], a[2])] - self.field[self.index(b[0], b[1], b[2])]
        };
        V3(
            axis(
                [(x + 1).min(self.size[0] - 1), y, z],
                [x.saturating_sub(1), y, z],
            ),
            axis(
                [x, (y + 1).min(self.size[1] - 1), z],
                [x, y.saturating_sub(1), z],
            ),
            axis(
                [x, y, (z + 1).min(self.size[2] - 1)],
                [x, y, z.saturating_sub(1)],
            ),
        )
        .unit()
    }
    fn fill(
        &mut self,
        balls: &[Ball],
        started: Instant,
        cancel: &AtomicBool,
    ) -> Result<(), String> {
        for (number, ball) in balls.iter().enumerate() {
            if number % 256 == 0 {
                check(started, cancel)?;
            }
            let p = ball.p.sub(self.origin).mul(1. / self.step);
            let reach = ball.radius / self.step + 2.;
            let low = [p.0, p.1, p.2].map(|value| (value - reach).floor().max(0.) as usize);
            let high = [p.0, p.1, p.2].map(|value| (value + reach).ceil().max(0.) as usize);
            for z in low[2]..=high[2].min(self.size[2] - 1) {
                for y in low[1]..=high[1].min(self.size[1] - 1) {
                    for x in low[0]..=high[0].min(self.size[0] - 1) {
                        let distance = self.point(x, y, z).sub(ball.p).length() - ball.radius;
                        let index = self.index(x, y, z);
                        if distance < self.field[index] {
                            self.field[index] = distance;
                            self.owners[index] = ball.residue;
                        }
                    }
                }
            }
        }
        Ok(())
    }
}
fn cells(size: [usize; 3]) -> usize {
    size.into_iter().fold(1usize, usize::saturating_mul)
}
fn check(started: Instant, cancel: &AtomicBool) -> Result<(), String> {
    if cancel.load(Ordering::Relaxed) {
        return Err("Surface construction cancelled".into());
    }
    if started.elapsed() > TIME_LIMIT {
        return Err("Surface construction exceeded its 25-second display limit; use a smaller target region.".into());
    }
    Ok(())
}
const CORNERS: [[usize; 3]; 8] = [
    [0, 0, 0],
    [1, 0, 0],
    [1, 1, 0],
    [0, 1, 0],
    [0, 0, 1],
    [1, 0, 1],
    [1, 1, 1],
    [0, 1, 1],
];
const TETS: [[usize; 4]; 6] = [
    [0, 5, 1, 6],
    [0, 1, 2, 6],
    [0, 2, 3, 6],
    [0, 3, 7, 6],
    [0, 7, 4, 6],
    [0, 4, 5, 6],
];
const EDGES: [[usize; 2]; 6] = [[0, 1], [1, 2], [2, 0], [0, 3], [1, 3], [2, 3]];
const FACES: [&[usize]; 16] = [
    &[],
    &[0, 3, 2],
    &[0, 1, 4],
    &[1, 4, 2, 2, 4, 3],
    &[1, 2, 5],
    &[0, 3, 5, 0, 5, 1],
    &[0, 2, 5, 0, 5, 4],
    &[5, 4, 3],
    &[3, 4, 5],
    &[4, 5, 0, 5, 2, 0],
    &[1, 5, 0, 5, 3, 0],
    &[5, 2, 1],
    &[3, 4, 2, 2, 4, 1],
    &[4, 1, 0],
    &[2, 3, 0],
    &[],
];
#[derive(Clone, Copy)]
struct Vertex {
    p: V3,
    n: V3,
    owner: usize,
}
fn triangulate(
    grid: &Grid,
    color: V3,
    remaining: usize,
    started: Instant,
    cancel: &AtomicBool,
) -> Result<Option<Mesh>, String> {
    let mut mesh = Mesh::default();
    for z in 0..grid.size[2] - 1 {
        check(started, cancel)?;
        for y in 0..grid.size[1] - 1 {
            for x in 0..grid.size[0] - 1 {
                let xyz = CORNERS.map(|offset| [x + offset[0], y + offset[1], z + offset[2]]);
                let indices = xyz.map(|p| grid.index(p[0], p[1], p[2]));
                let values = indices.map(|index| grid.field[index]);
                if values.iter().all(|&v| v >= 0.) || values.iter().all(|&v| v < 0.) {
                    continue;
                }
                let points = xyz.map(|p| grid.point(p[0], p[1], p[2]));
                let normals = xyz.map(|p| grid.gradient(p[0], p[1], p[2]));
                for tet in TETS {
                    let mask = tet.iter().enumerate().fold(0, |mask, (i, &corner)| {
                        mask | (usize::from(values[corner] < 0.) << i)
                    });
                    for face in FACES[mask].chunks_exact(3) {
                        if mesh.indices.len() / 3 >= remaining {
                            return Ok(None);
                        }
                        let mut vertices = [Vertex {
                            p: V3::default(),
                            n: V3::default(),
                            owner: 0,
                        }; 3];
                        for (dest, &edge) in vertices.iter_mut().zip(face) {
                            let [a, b] = EDGES[edge].map(|i| tet[i]);
                            let t = (values[a] / (values[a] - values[b])).clamp(0., 1.);
                            let owner = if values[a].abs() < values[b].abs() {
                                indices[a]
                            } else {
                                indices[b]
                            };
                            *dest = Vertex {
                                p: points[a].mul(1. - t).add(points[b].mul(t)),
                                n: normals[a].mul(1. - t).add(normals[b].mul(t)).unit(),
                                owner: grid.owners[owner],
                            };
                        }
                        let normal = vertices[1]
                            .p
                            .sub(vertices[0].p)
                            .cross(vertices[2].p.sub(vertices[0].p));
                        if normal.length() < 1e-7 {
                            continue;
                        }
                        if normal.dot(vertices[0].n.add(vertices[1].n).add(vertices[2].n)) < 0. {
                            vertices.swap(1, 2);
                        }
                        // One exact owner per triangle: interpolation must never invent a
                        // residue ID between unrelated chains/residues in the picking buffer.
                        let owner = if vertices[1].owner == vertices[2].owner {
                            vertices[1].owner
                        } else {
                            vertices[0].owner
                        };
                        if owner == usize::MAX {
                            return Err("Unassigned molecular surface triangle".into());
                        }
                        let first = mesh.vertices.len() as u32;
                        for vertex in vertices {
                            let n = if vertex.n.length() > 0.5 {
                                vertex.n
                            } else {
                                normal.unit()
                            };
                            mesh.vertices.push([
                                vertex.p.0,
                                vertex.p.1,
                                vertex.p.2,
                                n.0,
                                n.1,
                                n.2,
                                color.0,
                                color.1,
                                color.2,
                                (owner + 1) as f32,
                            ]);
                        }
                        mesh.indices
                            .extend_from_slice(&[first, first + 1, first + 2]);
                    }
                }
            }
        }
    }
    Ok(Some(mesh))
}
pub(super) fn build(input: Input, cancel: &AtomicBool) -> Result<Surface, String> {
    let started = Instant::now();
    let mut spacing = 0.72f32;
    for _ in 0..32 {
        check(started, cancel)?;
        let total = input
            .chains
            .iter()
            .filter(|(balls, _)| !balls.is_empty())
            .map(|(balls, _)| cells(Grid::bounds(balls, spacing).1))
            .fold(0usize, usize::saturating_add);
        if total > GRID_CELLS {
            spacing *= 1.15;
            continue;
        }
        let mut output = Vec::new();
        let mut triangles = 0;
        let mut limit = false;
        for (balls, color) in &input.chains {
            if balls.is_empty() {
                output.push(Mesh::default());
                continue;
            }
            let mut grid = Grid::new(balls, spacing)?;
            grid.fill(balls, started, cancel)?;
            let Some(mesh) =
                triangulate(&grid, *color, MAX_TRIANGLES - triangles, started, cancel)?
            else {
                limit = true;
                break;
            };
            if mesh.indices.is_empty() {
                return Err("Target coordinates are too sparse for the bounded molecular surface grid; crop the displayed structure.".into());
            }
            triangles += mesh.indices.len() / 3;
            output.push(mesh);
        }
        if !limit {
            return Ok(Surface {
                chains: output,
                spacing,
                triangles,
            });
        }
        spacing *= 1.25;
    }
    Err("Target exceeds molecular surface display limits; use a smaller target region.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn single_atom_envelope_is_probe_expanded_with_outward_normals() {
        let input = Input {
            chains: vec![(
                vec![Ball {
                    p: V3::default(),
                    radius: 3.1,
                    residue: 7,
                }],
                V3(0.5, 0.3, 0.2),
            )],
        };
        let surface = build(input, &AtomicBool::new(false)).unwrap();
        assert!(surface.triangles > 100);
        for v in &surface.chains[0].vertices {
            let p = V3(v[0], v[1], v[2]);
            let n = V3(v[3], v[4], v[5]);
            assert!((p.length() - 3.1).abs() < 0.12, "radius {}", p.length());
            assert!((n.length() - 1.).abs() < 0.001);
            assert!(n.dot(p.unit()) > 0.98);
            assert_eq!(v[9], 8.);
        }
    }
    #[test]
    fn cancelled_surface_does_not_build() {
        let input = Input::new(&Molecule::reference()).unwrap();
        assert!(
            build(input, &AtomicBool::new(true))
                .err()
                .unwrap()
                .contains("cancelled")
        );
    }
    #[test]
    fn cas9_surface_is_bounded_finite_and_keeps_residue_and_chain_identity() {
        let molecule = Molecule::reference();
        let started = Instant::now();
        let surface = build(Input::new(&molecule).unwrap(), &AtomicBool::new(false)).unwrap();
        eprintln!(
            "Cas9 SAS: {} triangles, {:.3} Å grid, {:?}",
            surface.triangles,
            surface.spacing,
            started.elapsed()
        );
        assert!(surface.triangles <= MAX_TRIANGLES);
        assert_eq!(surface.chains.len(), molecule.chains.len());
        for (index, mesh) in surface.chains.iter().enumerate() {
            assert!(!mesh.indices.is_empty());
            for v in &mesh.vertices {
                assert!(v.iter().all(|n| n.is_finite()));
                assert_eq!(molecule.residues[v[9] as usize - 1].chain, index);
            }
        }
    }
}
