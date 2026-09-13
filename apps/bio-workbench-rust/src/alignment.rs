//! Display-only rigid fitting. Input files, chemistry, and residue identities stay unchanged.
//!
//! Horn's unit-quaternion least-squares rotation, without estimating scale:
//! https://people.csail.mit.edu/bkph/papers/Absolute_Orientation_Scanned.pdf
//! Correspondence is deliberately conservative: unique, exactly equal ordered
//! polymer component sequences. We never invent homomer chain correspondences.
use crate::scene::{Molecule, MoleculeKind, ResidueKey, V3};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

type Point = [f64; 3];
type Rotation = [[f64; 3]; 3];
const IDENTITY: Rotation = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]];
// Source parsers already limit coordinates to 10^6 Å. Leave room for rigid
// transformations between their coordinate frames without accepting arbitrary
// finite values that overflow GPU calculations or destroy camera precision.
const MAX_DISPLAY_COORDINATE: f64 = 10_000_000.;

/// Restore a display fit onto freshly parsed, unchanged source coordinates.
/// The caller verifies source/reference identities separately. Reject scaling,
/// reflection, shear, non-finite values and unsafe bounds before any mutation.
pub fn apply_display_transform(
    molecule: &mut Molecule,
    rotation: [[f64; 3]; 3],
    translation: [f64; 3],
    display_center: [f64; 3],
) -> Result<(), String> {
    if rotation.iter().flatten().any(|v| !v.is_finite()) {
        return Err("Saved alignment rotation contains a non-finite value".into());
    }
    for i in 0..3 {
        for j in 0..3 {
            let dot: f64 = (0..3).map(|k| rotation[i][k] * rotation[j][k]).sum();
            if (dot - if i == j { 1. } else { 0. }).abs() > 1e-6 {
                return Err("Saved alignment rotation is not orthonormal (scale or shear)".into());
            }
        }
    }
    let [[a, b, c], [d, e, f], [g, h, i]] = rotation;
    let determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
    if (determinant - 1.).abs() > 1e-6 {
        return Err("Saved alignment rotation is not a proper rotation (reflection)".into());
    }
    let bounded = |values: &[f64]| {
        values
            .iter()
            .all(|v| v.is_finite() && v.abs() <= MAX_DISPLAY_COORDINATE)
    };
    if !bounded(&translation) || !bounded(&display_center) {
        return Err(
            "Saved alignment translation or display center is outside finite bounds".into(),
        );
    }
    if molecule.atoms.is_empty() {
        return Err("Saved alignment cannot be applied to an empty structure".into());
    }
    let positions = molecule
        .atoms
        .iter()
        .map(|atom| {
            let p = add(multiply(rotation, xyz(atom.p)), translation);
            if bounded(&p) {
                Ok(V3(p[0] as f32, p[1] as f32, p[2] as f32))
            } else {
                Err("Saved alignment produces coordinates outside finite bounds".to_owned())
            }
        })
        .collect::<Result<Vec<_>, _>>()?;
    let center = V3(
        display_center[0] as f32,
        display_center[1] as f32,
        display_center[2] as f32,
    );
    let radius = positions
        .iter()
        .map(|&p| norm2(sub(xyz(p), xyz(center))).sqrt())
        .fold(1., f64::max) as f32;
    for (atom, position) in molecule.atoms.iter_mut().zip(positions) {
        atom.p = position;
    }
    molecule.center = center;
    molecule.radius = radius;
    Ok(())
}

#[derive(Clone, Debug, Serialize)]
pub struct ChainMatch {
    pub reference_chain_index: usize,
    pub moving_chain_index: usize,
    pub reference_chain: String,
    pub moving_chain: String,
    pub residues: usize,
    pub matched_anchors: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct AlignmentReceipt {
    pub status: &'static str,
    pub reference_index: usize,
    pub method: &'static str,
    pub correspondence: &'static str,
    pub matched_anchors: usize,
    pub chain_matches: Vec<ChainMatch>,
    pub unmatched_reference_polymer_chains: usize,
    pub unmatched_moving_polymer_chains: usize,
    pub skipped_missing_anchors: usize,
    pub ambiguous_chain_sequences: usize,
    pub anchor_atoms: BTreeMap<String, usize>,
    pub rmsd_angstrom: Option<f64>,
    /// Row-major matrix: reference_xyz = rotation * source_xyz + translation_angstrom.
    pub rotation: Option<Rotation>,
    pub translation_angstrom: Option<Point>,
    pub reason: Option<String>,
}
impl AlignmentReceipt {
    pub fn reference() -> Self {
        Self {
            status: "reference",
            reference_index: 0,
            method: "identity_reference",
            correspondence: "not_applicable",
            matched_anchors: 0,
            chain_matches: Vec::new(),
            unmatched_reference_polymer_chains: 0,
            unmatched_moving_polymer_chains: 0,
            skipped_missing_anchors: 0,
            ambiguous_chain_sequences: 0,
            anchor_atoms: BTreeMap::new(),
            rmsd_angstrom: None,
            rotation: Some(IDENTITY),
            translation_angstrom: Some([0.; 3]),
            reason: None,
        }
    }
    pub fn unavailable(reason: String) -> Self {
        Self {
            status: "unavailable",
            method: "not_fitted",
            correspondence: "unique_exact_polymer_component_sequence",
            rotation: None,
            translation_angstrom: None,
            reason: Some(reason),
            ..Self::reference()
        }
    }
}

struct PolymerChain {
    index: usize,
    residues: Vec<usize>,
}
type Sequence = Vec<(&'static str, String)>;
fn polymer_chains(molecule: &Molecule) -> BTreeMap<Sequence, Vec<PolymerChain>> {
    let mut groups: BTreeMap<Sequence, Vec<PolymerChain>> = BTreeMap::new();
    for (index, chain) in molecule.chains.iter().enumerate() {
        let residues: Vec<_> = chain
            .residues
            .iter()
            .copied()
            .filter(|&r| molecule.residues[r].kind.polymer())
            .collect();
        if residues.is_empty() {
            continue;
        }
        let sequence = residues
            .iter()
            .map(|&r| {
                let residue = &molecule.residues[r];
                (residue.kind.name(), residue.name.clone())
            })
            .collect();
        groups
            .entry(sequence)
            .or_default()
            .push(PolymerChain { index, residues });
    }
    groups
}
fn named_atom(molecule: &Molecule, residue: usize, names: &[&str]) -> Option<usize> {
    names.iter().find_map(|name| {
        molecule.residues[residue]
            .atoms
            .iter()
            .copied()
            .find(|&index| molecule.atoms[index].name == *name)
    })
}
fn paired_anchor(
    reference: &Molecule,
    r: usize,
    moving: &Molecule,
    m: usize,
) -> Option<(usize, usize, &'static str)> {
    let kinds: &[(&[&str], &str)] = match reference.residues[r].kind {
        MoleculeKind::Protein => &[(&["CA"], "CA")],
        MoleculeKind::Dna | MoleculeKind::Rna => &[(&["C4'", "C4*"], "C4'"), (&["P"], "P")],
        _ => return None,
    };
    kinds.iter().find_map(|(names, canonical)| {
        Some((
            named_atom(reference, r, names)?,
            named_atom(moving, m, names)?,
            *canonical,
        ))
    })
}

pub fn align_to_reference(
    reference: &Molecule,
    moving: &mut Molecule,
) -> Result<AlignmentReceipt, String> {
    let reference_chains = polymer_chains(reference);
    let moving_chains = polymer_chains(moving);
    let mut pairs = Vec::new();
    let mut receipt = AlignmentReceipt {
        status: "aligned",
        method: "horn_quaternion_rigid_least_squares",
        correspondence: "unique_exact_polymer_component_sequence",
        unmatched_reference_polymer_chains: reference_chains.values().map(Vec::len).sum(),
        unmatched_moving_polymer_chains: moving_chains.values().map(Vec::len).sum(),
        ..AlignmentReceipt::reference()
    };
    let mut ambiguous = false;
    for (sequence, reference_group) in &reference_chains {
        let Some(moving_group) = moving_chains.get(sequence) else {
            continue;
        };
        if reference_group.len() != 1 || moving_group.len() != 1 {
            ambiguous = true;
            receipt.ambiguous_chain_sequences += 1;
            continue;
        }
        let (r, m) = (&reference_group[0], &moving_group[0]);
        let before = pairs.len();
        for (&ri, &mi) in r.residues.iter().zip(&m.residues) {
            if let Some((ra, ma, name)) = paired_anchor(reference, ri, moving, mi) {
                pairs.push((ra, ma));
                *receipt.anchor_atoms.entry(name.into()).or_default() += 1;
            } else {
                receipt.skipped_missing_anchors += 1;
            }
        }
        receipt.chain_matches.push(ChainMatch {
            reference_chain_index: r.index,
            moving_chain_index: m.index,
            reference_chain: reference.chains[r.index].id.clone(),
            moving_chain: moving.chains[m.index].id.clone(),
            residues: r.residues.len(),
            matched_anchors: pairs.len() - before,
        });
        receipt.unmatched_reference_polymer_chains -= 1;
        receipt.unmatched_moving_polymer_chains -= 1;
    }
    if pairs.len() < 3 {
        return Err(format!(
            "Only {} matching polymer anchors; at least three are required. {}",
            pairs.len(),
            if ambiguous {
                "Repeated identical chain sequences make chain correspondence ambiguous."
            } else {
                "Use uniquely corresponding chains with identical ordered polymer component sequences and actual CA or C4'/P atoms."
            }
        ));
    }
    fit_pairs(reference, moving, &pairs, receipt)
}

/// Fit a cropped target using an externally verified source-to-output residue
/// map. Neither residue numbering nor a similar sequence invents correspondence.
/// All mapped residues must exist, occur once, and retain the same chemistry.
/// Caller retains this map with the exact source/output artifact hashes.
pub fn align_with_residue_pairs(
    reference: &Molecule,
    moving: &mut Molecule,
    residue_pairs: &[(ResidueKey, ResidueKey)],
) -> Result<AlignmentReceipt, String> {
    if residue_pairs.len() < 3
        || residue_pairs.len() > reference.residues.len().min(moving.residues.len())
    {
        return Err("Explicit alignment requires at least three distinct paired residues within both structures".into());
    }
    let reference_ids: BTreeMap<_, _> = reference
        .residues
        .iter()
        .enumerate()
        .map(|(id, residue)| (&residue.key, id))
        .collect();
    let moving_ids: BTreeMap<_, _> = moving
        .residues
        .iter()
        .enumerate()
        .map(|(id, residue)| (&residue.key, id))
        .collect();
    let mut used_reference = BTreeSet::new();
    let mut used_moving = BTreeSet::new();
    let mut groups: BTreeMap<(usize, usize), (usize, usize)> = BTreeMap::new();
    let mut pairs = Vec::with_capacity(residue_pairs.len());
    let mut receipt = AlignmentReceipt {
        status: "aligned",
        method: "horn_quaternion_rigid_least_squares",
        correspondence: "explicit_exact_residue_identity_pairs",
        ..AlignmentReceipt::reference()
    };
    for (rkey, mkey) in residue_pairs {
        let &r = reference_ids
            .get(rkey)
            .ok_or_else(|| format!("Explicit reference residue does not exist: {rkey}"))?;
        let &m = moving_ids
            .get(mkey)
            .ok_or_else(|| format!("Explicit moving residue does not exist: {mkey}"))?;
        if !used_reference.insert(r) || !used_moving.insert(m) {
            return Err(
                "Explicit alignment map must contain one-to-one residue pairs without duplicates"
                    .into(),
            );
        }
        let (fixed, source) = (&reference.residues[r], &moving.residues[m]);
        if !fixed.kind.polymer() || fixed.kind != source.kind || fixed.name != source.name {
            return Err(format!(
                "Explicit residue pair has different polymer chemistry: {rkey} / {mkey}"
            ));
        }
        let group = groups.entry((fixed.chain, source.chain)).or_default();
        group.0 += 1;
        if let Some((ra, ma, name)) = paired_anchor(reference, r, moving, m) {
            pairs.push((ra, ma));
            group.1 += 1;
            *receipt.anchor_atoms.entry(name.into()).or_default() += 1;
        } else {
            receipt.skipped_missing_anchors += 1;
        }
    }
    let reference_chains: BTreeSet<_> = groups.keys().map(|&(r, _)| r).collect();
    let moving_chains: BTreeSet<_> = groups.keys().map(|&(_, m)| m).collect();
    let polymer_count = |molecule: &Molecule| {
        molecule
            .chains
            .iter()
            .filter(|chain| {
                chain
                    .residues
                    .iter()
                    .any(|&r| molecule.residues[r].kind.polymer())
            })
            .count()
    };
    receipt.unmatched_reference_polymer_chains = polymer_count(reference) - reference_chains.len();
    receipt.unmatched_moving_polymer_chains = polymer_count(moving) - moving_chains.len();
    receipt.chain_matches = groups
        .into_iter()
        .map(|((r, m), (residues, matched_anchors))| ChainMatch {
            reference_chain_index: r,
            moving_chain_index: m,
            reference_chain: reference.chains[r].id.clone(),
            moving_chain: moving.chains[m].id.clone(),
            residues,
            matched_anchors,
        })
        .collect();
    fit_pairs(reference, moving, &pairs, receipt)
}

fn fit_pairs(
    reference: &Molecule,
    moving: &mut Molecule,
    pairs: &[(usize, usize)],
    mut receipt: AlignmentReceipt,
) -> Result<AlignmentReceipt, String> {
    let fixed: Vec<_> = pairs
        .iter()
        .map(|&(r, _)| xyz(reference.atoms[r].p))
        .collect();
    let source: Vec<_> = pairs.iter().map(|&(_, m)| xyz(moving.atoms[m].p)).collect();
    let (rotation, translation) = rigid_fit(&source, &fixed)?;
    // Prepare all transformed positions before mutation, so a failed fit cannot
    // leave a partly transformed molecule. Ligands/ions move with the polymer.
    let positions: Vec<_> = moving
        .atoms
        .iter()
        .map(|atom| {
            let p = add(multiply(rotation, xyz(atom.p)), translation);
            let p = V3(p[0] as f32, p[1] as f32, p[2] as f32);
            if xyz(p).iter().all(|v| v.is_finite()) {
                Ok(p)
            } else {
                Err("Alignment produced non-finite coordinates".to_string())
            }
        })
        .collect::<Result<_, _>>()?;
    let center = mean(&positions.iter().copied().map(xyz).collect::<Vec<_>>());
    let center = V3(center[0] as f32, center[1] as f32, center[2] as f32);
    let radius = positions
        .iter()
        .map(|&p| norm2(sub(xyz(p), xyz(center))).sqrt())
        .fold(1., f64::max) as f32;
    if !radius.is_finite() {
        return Err("Alignment bounds are not finite".into());
    }
    receipt.matched_anchors = pairs.len();
    receipt.rmsd_angstrom = Some(
        (pairs
            .iter()
            .map(|&(r, m)| norm2(sub(xyz(positions[m]), xyz(reference.atoms[r].p))))
            .sum::<f64>()
            / pairs.len() as f64)
            .sqrt(),
    );
    receipt.rotation = Some(rotation);
    receipt.translation_angstrom = Some(translation);
    for (atom, position) in moving.atoms.iter_mut().zip(positions) {
        atom.p = position;
    }
    moving.center = center;
    moving.radius = radius;
    Ok(receipt)
}

fn xyz(p: V3) -> Point {
    [p.0 as f64, p.1 as f64, p.2 as f64]
}
fn add(a: Point, b: Point) -> Point {
    std::array::from_fn(|i| a[i] + b[i])
}
fn sub(a: Point, b: Point) -> Point {
    std::array::from_fn(|i| a[i] - b[i])
}
fn norm2(p: Point) -> f64 {
    p.iter().map(|x| x * x).sum()
}
fn multiply(rotation: Rotation, p: Point) -> Point {
    std::array::from_fn(|i| (0..3).map(|j| rotation[i][j] * p[j]).sum())
}
fn mean(points: &[Point]) -> Point {
    std::array::from_fn(|i| points.iter().map(|p| p[i]).sum::<f64>() / points.len() as f64)
}
fn noncollinear(points: &[Point]) -> bool {
    let Some(&axis) = points
        .iter()
        .max_by(|a, b| norm2(**a).total_cmp(&norm2(**b)))
    else {
        return false;
    };
    let spread = norm2(axis);
    spread > 1e-12
        && points.iter().any(|p| {
            let cross = [
                axis[1] * p[2] - axis[2] * p[1],
                axis[2] * p[0] - axis[0] * p[2],
                axis[0] * p[1] - axis[1] * p[0],
            ];
            norm2(cross) > spread * spread * 1e-12
        })
}
fn rigid_fit(source: &[Point], reference: &[Point]) -> Result<(Rotation, Point), String> {
    if source.len() != reference.len() || source.len() < 3 {
        return Err("At least three paired anchors are required".into());
    }
    if source
        .iter()
        .chain(reference)
        .flatten()
        .any(|n| !n.is_finite())
    {
        return Err("Alignment anchors contain non-finite coordinates".into());
    }
    let source_center = mean(source);
    let reference_center = mean(reference);
    let source: Vec<_> = source.iter().map(|&p| sub(p, source_center)).collect();
    let reference: Vec<_> = reference
        .iter()
        .map(|&p| sub(p, reference_center))
        .collect();
    if !noncollinear(&source) || !noncollinear(&reference) {
        return Err("Matching anchors are collinear or coincident; a unique 3D orientation cannot be determined".into());
    }
    let s: Rotation = std::array::from_fn(|i| {
        std::array::from_fn(|j| {
            source
                .iter()
                .zip(&reference)
                .map(|(a, b)| a[i] * b[j])
                .sum()
        })
    });
    let [[xx, xy, xz], [yx, yy, yz], [zx, zy, zz]] = s;
    let matrix = [
        [xx + yy + zz, yz - zy, zx - xz, xy - yx],
        [yz - zy, xx - yy - zz, xy + yx, zx + xz],
        [zx - xz, xy + yx, -xx + yy - zz, yz + zy],
        [xy - yx, zx + xz, yz + zy, -xx - yy + zz],
    ];
    let [w, x, y, z] = largest_eigenvector(matrix)?;
    let rotation = [
        [
            1. - 2. * (y * y + z * z),
            2. * (x * y - w * z),
            2. * (x * z + w * y),
        ],
        [
            2. * (x * y + w * z),
            1. - 2. * (x * x + z * z),
            2. * (y * z - w * x),
        ],
        [
            2. * (x * z - w * y),
            2. * (y * z + w * x),
            1. - 2. * (x * x + y * y),
        ],
    ];
    Ok((
        rotation,
        sub(reference_center, multiply(rotation, source_center)),
    ))
}

fn largest_eigenvector(mut matrix: [[f64; 4]; 4]) -> Result<[f64; 4], String> {
    let scale = matrix.iter().flatten().map(|v| v.abs()).fold(0., f64::max);
    if !scale.is_finite() || scale == 0. {
        return Err("Anchor covariance cannot determine an orientation".into());
    }
    for row in &mut matrix {
        for value in row {
            *value /= scale;
        }
    }
    let mut vectors: [[f64; 4]; 4] =
        std::array::from_fn(|i| std::array::from_fn(|j| if i == j { 1. } else { 0. }));
    // Jacobi diagonalization of a small real symmetric matrix. Selecting its
    // largest algebraic eigenvalue also handles exact 180-degree rotations.
    let mut converged = false;
    for _ in 0..128 {
        let (mut p, mut q) = (0, 1);
        for i in 0..4 {
            for j in i + 1..4 {
                if matrix[i][j].abs() > matrix[p][q].abs() {
                    (p, q) = (i, j);
                }
            }
        }
        if matrix[p][q].abs() < 1e-14 {
            converged = true;
            break;
        }
        let tau = (matrix[q][q] - matrix[p][p]) / (2. * matrix[p][q]);
        let t = tau.signum() / (tau.abs() + (1. + tau * tau).sqrt());
        let c = 1. / (1. + t * t).sqrt();
        let s = t * c;
        let off = matrix[p][q];
        matrix[p][p] -= t * off;
        matrix[q][q] += t * off;
        matrix[p][q] = 0.;
        matrix[q][p] = 0.;
        for i in 0..4 {
            if i != p && i != q {
                let (a, b) = (matrix[i][p], matrix[i][q]);
                matrix[i][p] = c * a - s * b;
                matrix[p][i] = matrix[i][p];
                matrix[i][q] = s * a + c * b;
                matrix[q][i] = matrix[i][q];
            }
            let (a, b) = (vectors[i][p], vectors[i][q]);
            vectors[i][p] = c * a - s * b;
            vectors[i][q] = s * a + c * b;
        }
    }
    if !converged {
        return Err("Rigid-fit eigensolver did not converge".into());
    }
    let mut order = [0, 1, 2, 3];
    order.sort_by(|&a, &b| matrix[b][b].total_cmp(&matrix[a][a]));
    if matrix[order[0]][order[0]] - matrix[order[1]][order[1]] < 1e-10 {
        return Err("Matching anchors do not determine a unique rotation".into());
    }
    let mut result: [f64; 4] = std::array::from_fn(|i| vectors[i][order[0]]);
    let norm = result.iter().map(|v| v * v).sum::<f64>().sqrt();
    for value in &mut result {
        *value /= norm;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    const POINTS: [Point; 6] = [
        [0., 0., 0.],
        [3., 0.5, 0.],
        [3., 3., 1.],
        [0., 3., 2.],
        [-1., 1., 4.],
        [1., -1., 3.],
    ];
    const TURN: Rotation = [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]];
    fn cif(rows: &str) -> Vec<u8> {
        format!("data_alignment\nloop_\n_atom_site.group_PDB\n_atom_site.id\n_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_alt_id\n_atom_site.label_comp_id\n_atom_site.label_asym_id\n_atom_site.label_entity_id\n_atom_site.label_seq_id\n_atom_site.auth_asym_id\n_atom_site.auth_seq_id\n_atom_site.pdbx_PDB_ins_code\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n_atom_site.occupancy\n_atom_site.B_iso_or_equiv\n_atom_site.pdbx_PDB_model_num\n{rows}\n#\n").into_bytes()
    }
    fn protein_rows(
        chain: &str,
        first: usize,
        points: &[Point],
        missing_ca: Option<usize>,
        insertion: bool,
    ) -> String {
        let names = ["MET", "ALA", "GLY", "SER", "THR", "LYS"];
        let mut rows = Vec::new();
        for (i, p) in points.iter().enumerate() {
            let sequence = if insertion && i < 2 { 42 } else { 43 + i };
            let ins = if insertion && i == 1 { "A" } else { "?" };
            for (j, name) in ["N", "CA"].iter().enumerate() {
                if *name == "CA" && missing_ca == Some(i) {
                    continue;
                }
                let p = if *name == "N" {
                    add(*p, [0.7, 0.2, 0.1])
                } else {
                    *p
                };
                rows.push(format!("ATOM {} {} {} . {} {chain} 1 {} {chain} {sequence} {ins} {:.6} {:.6} {:.6} 1 80 1",first+2*i+j,if *name=="N" {"N"} else {"C"},name,names[i%names.len()],i+1,p[0],p[1],p[2]));
            }
        }
        rows.join("\n")
    }
    fn molecule(rows: &str) -> Molecule {
        Molecule::parse(&cif(rows), "cif", "alignment fixture").unwrap()
    }
    fn shifted(points: &[Point], rotation: Rotation, offset: Point) -> Vec<Point> {
        points
            .iter()
            .map(|&p| add(multiply(rotation, p), offset))
            .collect()
    }
    fn determinant(r: Rotation) -> f64 {
        r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
            - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
            + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
    }
    #[test]
    fn known_rigid_transform_including_half_turn_is_recovered() {
        for rotation in [TURN, [[-1., 0., 0.], [0., -1., 0.], [0., 0., 1.]], IDENTITY] {
            let source = shifted(&POINTS, rotation, [11., -7., 4.]);
            let (fit, translation) = rigid_fit(&source, &POINTS).unwrap();
            assert!((determinant(fit) - 1.).abs() < 1e-12);
            for (&p, &expected) in source.iter().zip(&POINTS) {
                assert!(norm2(sub(add(multiply(fit, p), translation), expected)) < 1e-20);
            }
        }
    }
    #[test]
    fn explicit_map_aligns_a_crop_and_moves_unmapped_binder_rigidly() {
        let reference = molecule(&protein_rows("full_author_chain", 1, &POINTS, None, true));
        let moved = shifted(&POINTS, TURN, [11., -7., 4.]);
        let crop = protein_rows("native_target", 1, &moved, None, false)
            .lines()
            .skip(2)
            .take(8)
            .collect::<Vec<_>>()
            .join("\n");
        let binder_points = shifted(&POINTS[..3], IDENTITY, [20., 0., 0.]);
        let binder_moved = shifted(&binder_points, TURN, [11., -7., 4.]);
        let mut moving = molecule(&format!(
            "{crop}\n{}",
            protein_rows("native_binder", 100, &binder_moved, None, false)
        ));
        let original_keys = moving
            .residues
            .iter()
            .map(|residue| residue.key.clone())
            .collect::<Vec<_>>();
        let pairs = reference.residues[1..5]
            .iter()
            .zip(&moving.residues[..4])
            .map(|(r, m)| (r.key.clone(), m.key.clone()))
            .collect::<Vec<_>>();
        assert!(align_to_reference(&reference, &mut moving.clone()).is_err());
        let receipt = align_with_residue_pairs(&reference, &mut moving, &pairs).unwrap();
        assert_eq!(
            receipt.correspondence,
            "explicit_exact_residue_identity_pairs"
        );
        assert_eq!(receipt.matched_anchors, 4);
        assert_eq!(receipt.unmatched_moving_polymer_chains, 1);
        assert!(receipt.rmsd_angstrom.unwrap() < 1e-5);
        for (index, expected) in binder_points.iter().enumerate() {
            let atom = named_atom(&moving, index + 4, &["CA"]).unwrap();
            assert!(norm2(sub(xyz(moving.atoms[atom].p), *expected)) < 1e-8);
        }
        assert_eq!(
            moving
                .residues
                .iter()
                .map(|r| r.key.clone())
                .collect::<Vec<_>>(),
            original_keys
        );
    }
    #[test]
    fn explicit_maps_reject_missing_duplicates_and_different_chemistry_atomically() {
        let reference = molecule(&protein_rows("A", 1, &POINTS, None, true));
        let moved = shifted(&POINTS, TURN, [11., -7., 4.]);
        let moving = molecule(&protein_rows("B", 1, &moved, None, false));
        let pairs = reference
            .residues
            .iter()
            .zip(&moving.residues)
            .map(|(r, m)| (r.key.clone(), m.key.clone()))
            .collect::<Vec<_>>();
        let original = moving
            .atoms
            .iter()
            .map(|atom| xyz(atom.p))
            .collect::<Vec<_>>();
        let mut missing = pairs.clone();
        missing[0].0.chain = "absent".into();
        let mut duplicate = pairs.clone();
        duplicate[1] = duplicate[0].clone();
        let mut chemistry = pairs.clone();
        chemistry.swap(0, 1);
        let original_first = chemistry[1].1.clone();
        chemistry[1].1 = chemistry[0].1.clone();
        chemistry[0].1 = original_first;
        for invalid in [missing, duplicate, chemistry] {
            let mut candidate = moving.clone();
            assert!(align_with_residue_pairs(&reference, &mut candidate, &invalid).is_err());
            assert_eq!(
                candidate
                    .atoms
                    .iter()
                    .map(|atom| xyz(atom.p))
                    .collect::<Vec<_>>(),
                original
            );
        }
    }
    #[test]
    fn explicit_collinear_map_does_not_partly_transform_output() {
        let reference = molecule(&protein_rows("A", 1, &POINTS[..3], None, false));
        let mut moving = molecule(&protein_rows(
            "B",
            1,
            &[[1., 0., 0.], [2., 0., 0.], [3., 0., 0.]],
            None,
            false,
        ));
        let pairs = reference
            .residues
            .iter()
            .zip(&moving.residues)
            .map(|(r, m)| (r.key.clone(), m.key.clone()))
            .collect::<Vec<_>>();
        let original = moving
            .atoms
            .iter()
            .map(|atom| xyz(atom.p))
            .collect::<Vec<_>>();
        assert!(
            align_with_residue_pairs(&reference, &mut moving, &pairs)
                .unwrap_err()
                .contains("collinear")
        );
        assert_eq!(
            moving
                .atoms
                .iter()
                .map(|atom| xyz(atom.p))
                .collect::<Vec<_>>(),
            original
        );
    }
    #[test]
    fn sequence_correspondence_ignores_chain_labels_and_insertion_numbering() {
        let reference = molecule(&protein_rows("author_reference", 1, &POINTS, None, true));
        let moved = shifted(&POINTS, TURN, [11., -7., 4.]);
        let mut moving = molecule(&protein_rows("other_chain", 1, &moved, None, false));
        let residue_keys: Vec<_> = moving.residues.iter().map(|r| r.key.clone()).collect();
        let bonds = moving.bonds.clone();
        let before_secondary: Vec<_> = moving.residues.iter().map(|r| r.secondary).collect();
        let receipt = align_to_reference(&reference, &mut moving).unwrap();
        assert_eq!(receipt.matched_anchors, 6);
        assert!(receipt.rmsd_angstrom.unwrap() < 1e-5);
        assert_eq!(receipt.chain_matches[0].reference_chain, "author_reference");
        assert_eq!(receipt.chain_matches[0].moving_chain, "other_chain");
        assert_eq!(
            moving
                .residues
                .iter()
                .map(|r| r.key.clone())
                .collect::<Vec<_>>(),
            residue_keys
        );
        assert_eq!(moving.bonds, bonds);
        assert_eq!(
            moving
                .residues
                .iter()
                .map(|r| r.secondary)
                .collect::<Vec<_>>(),
            before_secondary
        );
        assert!(moving.atoms.iter().all(
            |a| norm2(sub(xyz(a.p), xyz(moving.center))).sqrt() <= moving.radius as f64 + 1e-5
        ));
    }
    #[test]
    fn missing_ca_is_skipped_instead_of_fitting_the_fallback_nitrogen() {
        let reference = molecule(&protein_rows("A", 1, &POINTS, None, false));
        let moved = shifted(&POINTS, TURN, [5., 8., -2.]);
        let mut moving = molecule(&protein_rows("B", 1, &moved, Some(2), false));
        assert_eq!(moving.atoms[moving.residues[2].anchor].name, "N");
        let receipt = align_to_reference(&reference, &mut moving).unwrap();
        assert_eq!(receipt.matched_anchors, 5);
        assert_eq!(receipt.skipped_missing_anchors, 1);
        assert_eq!(receipt.anchor_atoms["CA"], 5);
        assert!(receipt.rmsd_angstrom.unwrap() < 1e-5);
    }
    #[test]
    fn every_atom_moves_rigidly_without_modifying_chemistry_or_source_bytes() {
        let rows = format!(
            "{}\nHETATM 50 ZN ZN . ZN L 2 . ligand 1 ? 8 9 10 1 42 1",
            protein_rows("A", 1, &POINTS, None, false)
        );
        let bytes = cif(&rows);
        let original = bytes.clone();
        let reference = Molecule::parse(&bytes, "cif", "reference").unwrap();
        let mut moving = reference.clone();
        for atom in &mut moving.atoms {
            let p = add(multiply(TURN, xyz(atom.p)), [10., -3., 6.]);
            atom.p = V3(p[0] as f32, p[1] as f32, p[2] as f32);
        }
        let receipt = align_to_reference(&reference, &mut moving).unwrap();
        assert!(receipt.rmsd_angstrom.unwrap() < 1e-5);
        for (a, b) in moving.atoms.iter().zip(&reference.atoms) {
            assert!(norm2(sub(xyz(a.p), xyz(b.p))) < 1e-8);
            assert_eq!(
                (&a.name, &a.element, a.hetero, a.b_factor),
                (&b.name, &b.element, b.hetero, b.b_factor)
            );
        }
        assert_eq!(bytes, original);
    }
    #[test]
    fn dna_uses_equivalent_sugar_names_or_paired_phosphate_atoms() {
        let fixed = molecule(
            "ATOM 1 C C4' . DA A 1 1 A 1 ? 0 0 0 1 80 1\nATOM 2 C C4' . DT A 1 2 A 2 ? 3 0 0 1 80 1\nATOM 3 P P . DG A 1 3 A 3 ? 0 3 1 1 80 1",
        );
        let mut moving = molecule(
            "ATOM 1 C C4* . DA X 1 1 X 10 ? 10 10 0 1 80 1\nATOM 2 C C4* . DT X 1 2 X 11 ? 10 13 0 1 80 1\nATOM 3 P P . DG X 1 3 X 12 ? 7 10 1 1 80 1",
        );
        let receipt = align_to_reference(&fixed, &mut moving).unwrap();
        assert_eq!(receipt.anchor_atoms["C4'"], 2);
        assert_eq!(receipt.anchor_atoms["P"], 1);
        assert!(receipt.rmsd_angstrom.unwrap() < 1e-5);
    }
    #[test]
    fn repeated_homomer_sequences_are_not_arbitrarily_assigned() {
        let reference = molecule(&format!(
            "{}\n{}",
            protein_rows("A", 1, &POINTS, None, false),
            protein_rows("B", 100, &POINTS, None, false)
        ));
        let mut moving = reference.clone();
        let before: Vec<_> = moving.atoms.iter().map(|a| a.p).collect();
        let error = align_to_reference(&reference, &mut moving).unwrap_err();
        assert!(error.contains("ambiguous"));
        assert_eq!(moving.atoms.iter().map(|a| a.p).collect::<Vec<_>>(), before);
    }
    #[test]
    fn different_sequences_and_degenerate_anchors_are_explicit_errors() {
        let reference = molecule(&protein_rows("A", 1, &POINTS, None, false));
        let mut moving = reference.clone();
        moving.residues[0].name = "VAL".into();
        let before: Vec<_> = moving.atoms.iter().map(|a| a.p).collect();
        assert!(
            align_to_reference(&reference, &mut moving)
                .unwrap_err()
                .contains("Only 0")
        );
        assert_eq!(moving.atoms.iter().map(|a| a.p).collect::<Vec<_>>(), before);
        for points in [
            vec![[0., 0., 0.], [1., 0., 0.], [2., 0., 0.]],
            vec![[1., 1., 1.]; 3],
        ] {
            assert!(
                rigid_fit(&points, &points)
                    .unwrap_err()
                    .contains("collinear")
            );
        }
        assert!(rigid_fit(&POINTS[..2], &POINTS[..2]).is_err());
    }
    #[test]
    fn fitting_never_reflects_or_scales_molecular_coordinates() {
        for source in [
            POINTS.map(|p| [-p[0], p[1], p[2]]),
            POINTS.map(|p| [p[0] * 2., p[1] * 2., p[2] * 2.]),
        ] {
            let (rotation, translation) = rigid_fit(&source, &POINTS).unwrap();
            assert!((determinant(rotation) - 1.).abs() < 1e-12);
            let transformed: Vec<_> = source
                .iter()
                .map(|&p| add(multiply(rotation, p), translation))
                .collect();
            assert!(
                (norm2(sub(transformed[0], transformed[1])) - norm2(sub(source[0], source[1])))
                    .abs()
                    < 1e-10
            );
            let residual = transformed
                .iter()
                .zip(&POINTS)
                .map(|(&a, &b)| norm2(sub(a, b)))
                .sum::<f64>();
            assert!(residual > 0.1);
        }
    }
    #[test]
    fn saved_display_transform_restores_fresh_source_coordinates_and_reference_origin() {
        let reference = molecule(&protein_rows("A", 1, &POINTS, None, true));
        let source = cif(&protein_rows(
            "B",
            1,
            &shifted(&POINTS, TURN, [11., -7., 4.]),
            None,
            false,
        ));
        let mut fitted = Molecule::parse(&source, "cif", "candidate").unwrap();
        let receipt = align_to_reference(&reference, &mut fitted).unwrap();
        let mut reopened = Molecule::parse(&source, "cif", "candidate").unwrap();
        // A cropped target's reference origin need not be its own centroid.
        let origin = [reference.center.0 as f64 + 12., -23., 7.5];
        apply_display_transform(
            &mut reopened,
            receipt.rotation.unwrap(),
            receipt.translation_angstrom.unwrap(),
            origin,
        )
        .unwrap();
        assert_eq!(
            reopened.atoms.iter().map(|a| a.p).collect::<Vec<_>>(),
            fitted.atoms.iter().map(|a| a.p).collect::<Vec<_>>()
        );
        assert_eq!(xyz(reopened.center), origin.map(|v| v as f32 as f64));
        assert!(reopened.atoms.iter().all(|a| {
            norm2(sub(xyz(a.p), xyz(reopened.center))).sqrt() <= reopened.radius as f64 + 1e-5
        }));
        assert_eq!(
            reopened.residues.iter().map(|r| &r.key).collect::<Vec<_>>(),
            fitted.residues.iter().map(|r| &r.key).collect::<Vec<_>>()
        );
    }
    #[test]
    fn corrupted_saved_display_transforms_never_partly_mutate_a_structure() {
        let original = molecule(&protein_rows("A", 1, &POINTS, None, false));
        let invalid = [
            ([[2., 0., 0.], [0., 2., 0.], [0., 0., 2.]], [0.; 3], [0.; 3]),
            (
                [[1., 0.1, 0.], [0., 1., 0.], [0., 0., 1.]],
                [0.; 3],
                [0.; 3],
            ),
            (
                [[-1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
                [0.; 3],
                [0.; 3],
            ),
            (
                [[f64::NAN, 0., 0.], [0., 1., 0.], [0., 0., 1.]],
                [0.; 3],
                [0.; 3],
            ),
            (IDENTITY, [f64::INFINITY, 0., 0.], [0.; 3]),
            (IDENTITY, [0.; 3], [f64::NAN, 0., 0.]),
            (IDENTITY, [0.; 3], [1e30, 0., 0.]),
            // The translation itself is permitted, but later transformed atoms
            // exceed the coordinate ceiling: even earlier atoms stay unchanged.
            (IDENTITY, [MAX_DISPLAY_COORDINATE, 0., 0.], [0.; 3]),
        ];
        for (rotation, translation, center) in invalid {
            let mut candidate = original.clone();
            assert!(
                apply_display_transform(&mut candidate, rotation, translation, center).is_err()
            );
            assert_eq!(
                candidate.atoms.iter().map(|a| a.p).collect::<Vec<_>>(),
                original.atoms.iter().map(|a| a.p).collect::<Vec<_>>()
            );
            assert_eq!(candidate.center, original.center);
            assert_eq!(candidate.radius, original.radius);
        }
    }
}
