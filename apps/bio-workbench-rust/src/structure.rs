//! Bounded coordinate readers and molecular identifiers. No network or file IO.
use super::V3;
use eframe::egui::Color32;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;

pub const MAX_STRUCTURE_BYTES: usize = 32 * 1024 * 1024;
pub const MAX_ATOMS: usize = 100_000;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MoleculeKind {
    Protein,
    Rna,
    Dna,
    Ligand,
    Water,
    Unknown,
}
impl MoleculeKind {
    pub fn name(self) -> &'static str {
        match self {
            Self::Protein => "Protein",
            Self::Rna => "RNA",
            Self::Dna => "DNA",
            Self::Ligand => "Ligand / ion",
            Self::Water => "Water",
            Self::Unknown => "Other",
        }
    }
    pub fn polymer(self) -> bool {
        matches!(self, Self::Protein | Self::Rna | Self::Dna)
    }
}
impl fmt::Display for MoleculeKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ResidueKey {
    pub model: String,
    pub chain: String,
    pub label_chain: String,
    pub sequence: String,
    pub label_sequence: String,
    pub insertion: String,
    pub component: String,
    pub segment: usize,
}
impl ResidueKey {
    fn position_key(&self) -> Self {
        let mut key = self.clone();
        key.component.clear();
        key
    }
}
impl fmt::Display for ResidueKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}/{}{} {}",
            if self.chain.is_empty() {
                "(blank)"
            } else {
                &self.chain
            },
            self.sequence,
            self.insertion,
            self.component
        )?;
        if !self.label_chain.is_empty() && self.label_chain != self.chain {
            write!(f, " [label {}]", self.label_chain)?;
        }
        if self.segment > 0 {
            write!(f, " [segment {}]", self.segment + 1)?;
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Secondary {
    Coil,
    Helix,
    Sheet,
}
#[derive(Clone)]
pub struct Atom {
    pub p: V3,
    pub name: String,
    pub element: String,
    pub chain: usize,
    pub residue: usize,
    pub serial: String,
    pub altloc: String,
    pub occupancy: f32,
    pub b_factor: Option<f32>,
    pub hetero: bool,
}
#[derive(Clone)]
pub struct Residue {
    pub key: ResidueKey,
    pub name: String,
    pub chain: usize,
    pub kind: MoleculeKind,
    pub letter: char,
    pub anchor: usize,
    pub atoms: Vec<usize>,
    pub secondary: Secondary,
    pub connected_to_previous: bool,
}
#[derive(Clone)]
pub struct Chain {
    pub id: String,
    pub label_id: String,
    pub kind: MoleculeKind,
    pub residues: Vec<usize>,
    pub atom_count: usize,
    pub color: Color32,
}
#[derive(Clone)]
pub struct Molecule {
    pub name: String,
    pub format: String,
    pub atoms: Vec<Atom>,
    pub residues: Vec<Residue>,
    pub chains: Vec<Chain>,
    pub bonds: Vec<(usize, usize)>,
    pub center: V3,
    pub radius: f32,
    pub warnings: Vec<String>,
    pub secondary_source: String,
}
#[derive(Clone)]
struct RawAtom {
    key: ResidueKey,
    p: V3,
    name: String,
    element: String,
    serial: String,
    altloc: String,
    occupancy: f32,
    b_factor: Option<f32>,
    hetero: bool,
    entity: String,
}
struct Range {
    chain: String,
    label_chain: String,
    label_sequence: bool,
    start: String,
    end: String,
    start_ins: String,
    end_ins: String,
    kind: Secondary,
}
#[derive(Default)]
struct Builder {
    atoms: Vec<RawAtom>,
    ranges: Vec<Range>,
    serial_bonds: Vec<(String, String)>,
    connections: Vec<(AtomRef, AtomRef)>,
    entities: HashMap<String, MoleculeKind>,
    asym: HashMap<String, String>,
}
#[derive(Clone)]
struct AtomRef {
    chain: String,
    label_chain: String,
    sequence: String,
    label_sequence: String,
    insertion: String,
    name: String,
    component: String,
    altloc: String,
}
fn field(line: &str, a: usize, b: usize) -> &str {
    line.get(a..b.min(line.len())).unwrap_or("").trim()
}
fn missing(s: &str) -> bool {
    s.is_empty() || s == "." || s == "?"
}
fn id(s: &str) -> Result<String, String> {
    if s.len() > 256 || s.chars().any(char::is_control) {
        return Err("Coordinate identifier is too long or contains control characters".into());
    }
    Ok(if missing(s) {
        String::new()
    } else {
        s.to_owned()
    })
}
fn number(s: &str, label: &str) -> Result<f32, String> {
    let n = s
        .parse::<f32>()
        .map_err(|_| format!("Invalid {label}: {s:?}"))?;
    if !n.is_finite() || n.abs() > 1_000_000. {
        return Err(format!("Nonfinite or out-of-range {label}"));
    }
    Ok(n)
}
fn optional_number(s: &str, label: &str) -> Result<Option<f32>, String> {
    if missing(s) {
        Ok(None)
    } else {
        number(s, label).map(Some)
    }
}
fn element(raw: &str, name: &str, hetero: bool) -> String {
    if !missing(raw) {
        return raw.trim().to_ascii_uppercase();
    }
    let letters: String = name.chars().filter(char::is_ascii_alphabetic).collect();
    let upper = letters.to_ascii_uppercase();
    if hetero
        && matches!(
            upper.as_str(),
            "CL" | "BR" | "FE" | "ZN" | "MG" | "MN" | "CU" | "NA" | "CA" | "CO" | "NI" | "SE"
        )
    {
        upper
    } else {
        upper
            .chars()
            .next()
            .map(|c| c.to_string())
            .unwrap_or_else(|| "C".into())
    }
}
impl Molecule {
    pub fn parse(bytes: &[u8], format: &str, name: &str) -> Result<Self, String> {
        if bytes.len() > MAX_STRUCTURE_BYTES {
            return Err("Structure exceeds the 32 MiB viewer input limit".into());
        }
        let text =
            std::str::from_utf8(bytes).map_err(|_| "Coordinates must be UTF-8/ASCII text")?;
        if text.contains('\0') {
            return Err("Coordinate file contains NUL bytes".into());
        }
        let format = match format.to_ascii_lowercase().as_str() {
            "pdb" => "pdb",
            "cif" | "mmcif" => "mmcif",
            _ => return Err("Viewer supports PDB and mmCIF coordinate files".into()),
        };
        let mut builder = Builder::default();
        if format == "pdb" {
            builder.pdb(text)?;
        } else {
            builder.cif(text)?;
        }
        builder.finish(name, format)
    }
    pub fn reference_bytes() -> Vec<u8> {
        let mut text = String::new();
        for line in include_str!("../fixtures/4oo8.pdb").lines() {
            if line.starts_with("ATOM ")
                && matches!(line.as_bytes().get(21), Some(b'A' | b'B' | b'C'))
                || line.starts_with("HELIX ")
                || line.starts_with("SHEET ")
            {
                text.push_str(line);
                text.push('\n');
            }
        }
        text.push_str("END\n");
        text.into_bytes()
    }
    pub fn reference() -> Self {
        Self::parse(
            &Self::reference_bytes(),
            "pdb",
            "Experimental Cas9 / 4OO8 chains A/B/C",
        )
        .expect("embedded reference is valid")
    }
    pub fn residue(&self, key: &ResidueKey) -> Option<&Residue> {
        self.residues.iter().find(|r| &r.key == key)
    }
    pub fn residue_position(&self, key: &ResidueKey) -> Option<V3> {
        self.residue(key).map(|r| self.atoms[r.anchor].p)
    }
    pub fn distance(&self, a: &ResidueKey, b: &ResidueKey) -> Result<f32, String> {
        let a = self
            .residue_position(a)
            .ok_or("First residue is absent from this structure")?;
        let b = self
            .residue_position(b)
            .ok_or("Second residue is absent from this structure")?;
        Ok(a.sub(b).length())
    }
}
impl Builder {
    fn push(&mut self, atom: RawAtom) -> Result<(), String> {
        if self.atoms.len() >= MAX_ATOMS * 4 {
            return Err("Structure exceeds the atom/conformer limit".into());
        }
        if atom.element.len() > 3 || !atom.element.bytes().all(|b| b.is_ascii_alphabetic()) {
            return Err("Invalid chemical element field".into());
        }
        if atom.name.is_empty() || atom.key.component.is_empty() {
            return Err("Atom name and residue name are required".into());
        }
        self.atoms.push(atom);
        Ok(())
    }
    fn pdb(&mut self, text: &str) -> Result<(), String> {
        let mut model = "1".to_string();
        let mut segment = 0;
        for (line_no, line) in text.lines().enumerate() {
            let record = field(line, 0, 6);
            if matches!(record, "ATOM" | "HETATM") {
                if !line.is_ascii() || line.len() < 54 {
                    return Err(format!(
                        "Truncated/non-ASCII PDB atom at line {}",
                        line_no + 1
                    ));
                }
                let hetero = record == "HETATM";
                let name = id(field(line, 12, 16))?;
                self.push(RawAtom {
                    key: ResidueKey {
                        model: model.clone(),
                        chain: id(field(line, 21, 22))?,
                        label_chain: String::new(),
                        sequence: id(field(line, 22, 26))?,
                        label_sequence: String::new(),
                        insertion: id(field(line, 26, 27))?,
                        component: id(field(line, 17, 20))?,
                        segment,
                    },
                    p: V3(
                        number(field(line, 30, 38), "x coordinate")?,
                        number(field(line, 38, 46), "y coordinate")?,
                        number(field(line, 46, 54), "z coordinate")?,
                    ),
                    element: element(
                        field(line, 76, 78),
                        &name,
                        hetero && line.as_bytes().get(12) != Some(&b' '),
                    ),
                    name,
                    serial: id(field(line, 6, 11))?,
                    altloc: id(field(line, 16, 17))?,
                    occupancy: optional_number(field(line, 54, 60), "occupancy")?.unwrap_or(1.),
                    b_factor: optional_number(field(line, 60, 66), "B/temperature factor")?,
                    hetero,
                    entity: String::new(),
                })?;
            } else if record == "MODEL" {
                model = id(field(line, 10, 14))?;
                segment = 0;
            } else if record == "TER" {
                segment += 1;
            } else if record == "HELIX" && line.len() >= 38 {
                self.ranges.push(Range {
                    chain: id(field(line, 19, 20))?,
                    label_chain: String::new(),
                    label_sequence: false,
                    start: id(field(line, 21, 25))?,
                    start_ins: id(field(line, 25, 26))?,
                    end: id(field(line, 33, 37))?,
                    end_ins: id(field(line, 37, 38))?,
                    kind: Secondary::Helix,
                });
            } else if record == "SHEET" && line.len() >= 38 {
                self.ranges.push(Range {
                    chain: id(field(line, 21, 22))?,
                    label_chain: String::new(),
                    label_sequence: false,
                    start: id(field(line, 22, 26))?,
                    start_ins: id(field(line, 26, 27))?,
                    end: id(field(line, 33, 37))?,
                    end_ins: id(field(line, 37, 38))?,
                    kind: Secondary::Sheet,
                });
            } else if record == "CONECT" {
                let from = id(field(line, 6, 11))?;
                for offset in (11..line.len()).step_by(5) {
                    let to = id(field(line, offset, offset + 5))?;
                    if !from.is_empty() && !to.is_empty() {
                        self.serial_bonds.push((from.clone(), to));
                    }
                }
            }
        }
        Ok(())
    }
    fn finish(self, name: &str, format: &str) -> Result<Molecule, String> {
        if self.ranges.len() > 4096 {
            return Err("Structure exceeds the 4,096 secondary-structure annotation limit".into());
        }
        let first = self
            .atoms
            .first()
            .ok_or("No ATOM/HETATM coordinates found")?
            .key
            .model
            .clone();
        let models: HashSet<_> = self.atoms.iter().map(|a| a.key.model.clone()).collect();
        let mut warnings = Vec::new();
        if models.len() > 1 {
            warnings.push(format!("Showing coordinate model {first}; file contains {} models/states. Original file export retains all states.",models.len()));
        }
        let mut alternate: HashMap<ResidueKey, BTreeMap<String, (f32, usize)>> = HashMap::new();
        for atom in self
            .atoms
            .iter()
            .filter(|a| a.key.model == first && !a.altloc.is_empty())
        {
            let entry = alternate
                .entry(atom.key.position_key())
                .or_default()
                .entry(atom.altloc.clone())
                .or_default();
            entry.0 += atom.occupancy;
            entry.1 += 1;
        }
        let chosen: HashMap<_, _> = alternate
            .iter()
            .map(|(key, alts)| {
                let alt = alts
                    .iter()
                    .max_by(|(an, (asum, ac)), (bn, (bsum, bc))| {
                        (asum / *ac as f32)
                            .total_cmp(&(bsum / *bc as f32))
                            .then_with(|| bn.cmp(an))
                    })
                    .unwrap()
                    .0
                    .clone();
                (key.clone(), alt)
            })
            .collect();
        if !chosen.is_empty() {
            warnings.push(format!("Selected one alternate conformer per residue by mean occupancy ({} residues); original bytes retain all alternates.",chosen.len()));
        }
        let mut atoms = Vec::new();
        let mut residues: Vec<Residue> = Vec::new();
        let mut chains: Vec<Chain> = Vec::new();
        let mut residue_ids = HashMap::new();
        let mut chain_ids = HashMap::new();
        let mut seen_atoms = HashSet::new();
        let mut residue_entities = Vec::new();
        for raw in self.atoms {
            if raw.key.model != first
                || !raw.altloc.is_empty()
                    && chosen.get(&raw.key.position_key()) != Some(&raw.altloc)
            {
                continue;
            }
            let atom_key = (raw.key.clone(), raw.name.clone());
            if !seen_atoms.insert(atom_key) {
                return Err(format!("Duplicate atom name {} at {}", raw.name, raw.key));
            }
            if atoms.len() >= MAX_ATOMS {
                return Err("Structure exceeds the 100,000 displayed atom limit".into());
            }
            let chain_key = (
                raw.key.chain.clone(),
                raw.key.label_chain.clone(),
                raw.key.segment,
            );
            if !chain_ids.contains_key(&chain_key) && chains.len() >= 256 {
                return Err("Structure exceeds the 256 chain/segment viewer limit".into());
            }
            let chain = *chain_ids.entry(chain_key).or_insert_with(|| {
                let n = chains.len();
                chains.push(Chain {
                    id: raw.key.chain.clone(),
                    label_id: raw.key.label_chain.clone(),
                    kind: MoleculeKind::Unknown,
                    residues: Vec::new(),
                    atom_count: 0,
                    color: Color32::WHITE,
                });
                n
            });
            let residue = *residue_ids.entry(raw.key.clone()).or_insert_with(|| {
                let n = residues.len();
                residues.push(Residue {
                    key: raw.key.clone(),
                    name: raw.key.component.clone(),
                    chain,
                    kind: MoleculeKind::Unknown,
                    letter: 'X',
                    anchor: atoms.len(),
                    atoms: Vec::new(),
                    secondary: Secondary::Coil,
                    connected_to_previous: false,
                });
                residue_entities.push(raw.entity.clone());
                chains[chain].residues.push(n);
                n
            });
            if residues[residue].atoms.len() >= 4096 {
                return Err("A residue exceeds the 4,096 atom viewer limit".into());
            }
            residues[residue].atoms.push(atoms.len());
            chains[chain].atom_count += 1;
            atoms.push(Atom {
                p: raw.p,
                name: raw.name,
                element: raw.element,
                chain,
                residue,
                serial: raw.serial,
                altloc: raw.altloc,
                occupancy: raw.occupancy,
                b_factor: raw.b_factor,
                hetero: raw.hetero,
            });
        }
        for (i, residue) in residues.iter_mut().enumerate() {
            let entity = if residue_entities[i].is_empty() {
                self.asym.get(&residue.key.label_chain)
            } else {
                Some(&residue_entities[i])
            };
            let hint = entity.and_then(|e| self.entities.get(e)).copied();
            let names: HashSet<_> = residue
                .atoms
                .iter()
                .map(|&a| atoms[a].name.as_str())
                .collect();
            residue.kind = classify(&residue.name, &names, hint);
            residue.letter = residue_letter(&residue.name);
            let wanted = match residue.kind {
                MoleculeKind::Protein => &["CA"][..],
                MoleculeKind::Rna | MoleculeKind::Dna => &["C4'", "C4*", "P"][..],
                _ => &[][..],
            };
            residue.anchor = wanted
                .iter()
                .find_map(|n| residue.atoms.iter().copied().find(|&a| atoms[a].name == *n))
                .or_else(|| {
                    residue
                        .atoms
                        .iter()
                        .copied()
                        .find(|&a| atoms[a].element != "H")
                })
                .unwrap_or(residue.atoms[0]);
        }
        let mut protein_index = 0;
        for (index, chain) in chains.iter_mut().enumerate() {
            chain.kind = chain
                .residues
                .iter()
                .map(|&r| residues[r].kind)
                .find(|k| k.polymer())
                .unwrap_or(residues[chain.residues[0]].kind);
            chain.color = palette(
                if chain.kind == MoleculeKind::Protein {
                    let n = protein_index;
                    protein_index += 1;
                    n
                } else {
                    index
                },
                chain.kind,
            );
            for pair in chain.residues.windows(2) {
                let (a, b) = (pair[0], pair[1]);
                residues[b].connected_to_previous = continuous(&residues[a], &residues[b], &atoms);
            }
        }
        let mut explicit = HashSet::new();
        let mut serials: HashMap<&str, Option<usize>> = HashMap::new();
        for (i, atom) in atoms
            .iter()
            .enumerate()
            .filter(|(_, a)| !a.serial.is_empty())
        {
            serials
                .entry(&atom.serial)
                .and_modify(|v| *v = None)
                .or_insert(Some(i));
        }
        for (a, b) in self.serial_bonds {
            if let Some((Some(&a), Some(&b))) = serials
                .get(a.as_str())
                .map(Option::as_ref)
                .zip(serials.get(b.as_str()).map(Option::as_ref))
                && a != b
            {
                explicit.insert((a.min(b), a.max(b)));
            }
        }
        let mut atoms_by_name: HashMap<&str, Vec<usize>> = HashMap::new();
        for (i, atom) in atoms.iter().enumerate() {
            atoms_by_name.entry(&atom.name).or_default().push(i);
        }
        for (a, b) in self.connections {
            let find = |key: &AtomRef| {
                let candidates = atoms_by_name
                    .get(key.name.as_str())
                    .map_or(&[][..], Vec::as_slice);
                let mut matches = candidates
                    .iter()
                    .map(|&i| (i, &atoms[i]))
                    .filter(|(_, atom)| {
                        let r = &residues[atom.residue].key;
                        atom.name == key.name
                            && (key.chain.is_empty() || r.chain == key.chain)
                            && (key.label_chain.is_empty() || r.label_chain == key.label_chain)
                            && (key.sequence.is_empty() || r.sequence == key.sequence)
                            && (key.label_sequence.is_empty()
                                || r.label_sequence == key.label_sequence)
                            && (key.component.is_empty() || r.component == key.component)
                            && (key.altloc.is_empty() || atom.altloc == key.altloc)
                            && r.insertion == key.insertion
                    });
                let candidate = matches.next().map(|(i, _)| i);
                if matches.next().is_some() {
                    None
                } else {
                    candidate
                }
            };
            if let Some((a, b)) = find(&a).zip(find(&b))
                && a != b
            {
                explicit.insert((a.min(b), a.max(b)));
            }
        }
        let bonds = infer_bonds(&atoms, &residues, explicit)?;
        let center = atoms.iter().fold([0.0_f64; 3], |mut s, a| {
            s[0] += a.p.0 as f64;
            s[1] += a.p.1 as f64;
            s[2] += a.p.2 as f64;
            s
        });
        let center = V3(
            (center[0] / atoms.len() as f64) as f32,
            (center[1] / atoms.len() as f64) as f32,
            (center[2] / atoms.len() as f64) as f32,
        );
        let radius = atoms
            .iter()
            .map(|a| a.p.sub(center).length())
            .fold(1., f32::max);
        let mut molecule = Molecule {
            name: name.to_owned(),
            format: format.to_owned(),
            atoms,
            residues,
            chains,
            bonds,
            center,
            radius,
            warnings,
            secondary_source: String::new(),
        };
        assign_secondary(&mut molecule, &self.ranges);
        Ok(molecule)
    }
}
fn palette(index: usize, kind: MoleculeKind) -> Color32 {
    match kind {
        MoleculeKind::Rna => Color32::from_rgb(231, 163, 62),
        MoleculeKind::Dna => Color32::from_rgb(199, 114, 207),
        MoleculeKind::Ligand => Color32::from_rgb(172, 198, 110),
        MoleculeKind::Water => Color32::from_rgb(130, 170, 215),
        _ => [
            Color32::from_rgb(89, 176, 162),
            Color32::from_rgb(104, 161, 204),
            Color32::from_rgb(151, 185, 130),
            Color32::from_rgb(188, 139, 168),
        ][index % 4],
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
        "MET" | "MSE" => 'M',
        "PHE" => 'F',
        "PRO" => 'P',
        "SER" => 'S',
        "THR" => 'T',
        "TRP" => 'W',
        "TYR" => 'Y',
        "VAL" => 'V',
        "A" | "DA" => 'A',
        "C" | "DC" => 'C',
        "G" | "DG" => 'G',
        "U" | "DU" => 'U',
        "T" | "DT" => 'T',
        "I" | "DI" => 'I',
        _ => 'X',
    }
}
fn classify(name: &str, atoms: &HashSet<&str>, hint: Option<MoleculeKind>) -> MoleculeKind {
    if let Some(kind) = hint {
        return kind;
    }
    if matches!(name, "HOH" | "WAT" | "DOD") {
        MoleculeKind::Water
    } else if atoms.contains("N") && atoms.contains("CA") && atoms.contains("C")
        || matches!(
            name,
            "ALA"
                | "ARG"
                | "ASN"
                | "ASP"
                | "CYS"
                | "GLN"
                | "GLU"
                | "GLY"
                | "HIS"
                | "ILE"
                | "LEU"
                | "LYS"
                | "MET"
                | "MSE"
                | "PHE"
                | "PRO"
                | "SER"
                | "THR"
                | "TRP"
                | "TYR"
                | "VAL"
        )
    {
        MoleculeKind::Protein
    } else if matches!(name, "DA" | "DC" | "DG" | "DT" | "DI" | "DU") {
        MoleculeKind::Dna
    } else if matches!(name, "A" | "C" | "G" | "U" | "I")
        || atoms.contains("O2'") && atoms.contains("C1'")
    {
        MoleculeKind::Rna
    } else if atoms.contains("C1'") && (atoms.contains("O3'") || atoms.contains("P")) {
        MoleculeKind::Dna
    } else {
        MoleculeKind::Ligand
    }
}
fn atom_named<'a>(r: &Residue, atoms: &'a [Atom], name: &str) -> Option<&'a Atom> {
    r.atoms.iter().map(|&i| &atoms[i]).find(|a| a.name == name)
}
fn sequential(a: &ResidueKey, b: &ResidueKey) -> bool {
    if a.chain != b.chain || a.label_chain != b.label_chain || a.segment != b.segment {
        return false;
    }
    if let (Ok(x), Ok(y)) = (
        a.label_sequence.parse::<i64>(),
        b.label_sequence.parse::<i64>(),
    ) {
        return y == x + 1;
    }
    match (a.sequence.parse::<i64>(), b.sequence.parse::<i64>()) {
        (Ok(x), Ok(y)) => {
            y == x + 1 && b.insertion.is_empty()
                || y == x
                    && (!b.insertion.is_empty())
                    && (a.insertion.is_empty()
                        || a.insertion.len() == 1
                            && b.insertion.len() == 1
                            && b.insertion.as_bytes()[0] == a.insertion.as_bytes()[0] + 1)
        }
        _ => false,
    }
}
fn continuous(a: &Residue, b: &Residue, atoms: &[Atom]) -> bool {
    if a.kind != b.kind || !a.kind.polymer() || !sequential(&a.key, &b.key) {
        return false;
    }
    let bond = if a.kind == MoleculeKind::Protein {
        atom_named(a, atoms, "C").zip(atom_named(b, atoms, "N"))
    } else {
        atom_named(a, atoms, "O3'").zip(atom_named(b, atoms, "P"))
    };
    if let Some((x, y)) = bond {
        return (0.6..2.2).contains(&x.p.sub(y.p).length());
    }
    let distance = atoms[a.anchor].p.sub(atoms[b.anchor].p).length();
    distance > 0.5
        && distance
            < if a.kind == MoleculeKind::Protein {
                4.8
            } else {
                9.
            }
}
fn covalent(element: &str) -> f32 {
    match element {
        "H" | "D" => 0.31,
        "C" => 0.76,
        "N" => 0.71,
        "O" => 0.66,
        "F" => 0.57,
        "P" => 1.07,
        "S" => 1.05,
        "CL" => 1.02,
        "BR" => 1.20,
        "I" => 1.39,
        "SE" => 1.20,
        _ => 0.,
    }
}
fn infer_bonds(
    atoms: &[Atom],
    residues: &[Residue],
    mut bonds: HashSet<(usize, usize)>,
) -> Result<Vec<(usize, usize)>, String> {
    let mut candidates = 0_usize;
    // Infer only within one residue. Across residues, add only the known peptide
    // or phosphodiester connection between verified consecutive polymer units.
    for residue in residues {
        if residue.kind == MoleculeKind::Water {
            continue;
        }
        let mut grid: HashMap<(i32, i32, i32), Vec<usize>> = HashMap::new();
        for &i in &residue.atoms {
            let a = &atoms[i];
            let ra = covalent(&a.element);
            if ra == 0. {
                continue;
            }
            let cell = (
                (a.p.0 / 3.).floor() as i32,
                (a.p.1 / 3.).floor() as i32,
                (a.p.2 / 3.).floor() as i32,
            );
            for x in -1..=1 {
                for y in -1..=1 {
                    for z in -1..=1 {
                        if let Some(neighbors) = grid.get(&(cell.0 + x, cell.1 + y, cell.2 + z)) {
                            for &j in neighbors {
                                candidates += 1;
                                if candidates > 20_000_000 {
                                    return Err("Coordinate density exceeds the bounded bond inference limit".into());
                                }
                                let b = &atoms[j];
                                let rb = covalent(&b.element);
                                let d = a.p.sub(b.p).length();
                                if rb > 0. && d > 0.45 && d < ra + rb + 0.35 {
                                    bonds.insert((j.min(i), j.max(i)));
                                }
                            }
                        }
                    }
                }
            }
            grid.entry(cell).or_default().push(i);
        }
    }
    let mut previous: HashMap<usize, &Residue> = HashMap::new();
    for b in residues {
        if b.connected_to_previous
            && let Some(a) = previous.get(&b.chain)
        {
            let names = if b.kind == MoleculeKind::Protein {
                ("C", "N")
            } else {
                ("O3'", "P")
            };
            if let Some((a, b)) = a
                .atoms
                .iter()
                .copied()
                .find(|&i| atoms[i].name == names.0)
                .zip(b.atoms.iter().copied().find(|&i| atoms[i].name == names.1))
            {
                bonds.insert((a.min(b), a.max(b)));
            }
        }
        previous.insert(b.chain, b);
    }
    let mut result: Vec<_> = bonds.into_iter().collect();
    result.sort_unstable();
    Ok(result)
}

fn dihedral(a: V3, b: V3, c: V3, d: V3) -> Option<f32> {
    let axis = c.sub(b).unit();
    let first = a.sub(b);
    let last = d.sub(c);
    let v = first.sub(axis.mul(first.dot(axis)));
    let w = last.sub(axis.mul(last.dot(axis)));
    if v.length() < 1e-5 || w.length() < 1e-5 {
        return None;
    }
    Some(axis.cross(v).dot(w).atan2(v.dot(w)).to_degrees())
}
fn assign_secondary(m: &mut Molecule, ranges: &[Range]) {
    let mut source = vec![false; m.residues.len()];
    let mut estimated = vec![Secondary::Coil; m.residues.len()];
    for (i, r) in m.residues.iter_mut().enumerate() {
        if r.kind != MoleculeKind::Protein {
            continue;
        }
        for range in ranges {
            let sequence = if range.label_sequence {
                &r.key.label_sequence
            } else {
                &r.key.sequence
            };
            let chain = if range.label_chain.is_empty() {
                r.key.chain == range.chain
            } else {
                r.key.label_chain == range.label_chain
            };
            if chain
                && let (Ok(value), Ok(start), Ok(end)) = (
                    sequence.parse::<i64>(),
                    range.start.parse::<i64>(),
                    range.end.parse::<i64>(),
                )
                && (value > start || value == start && r.key.insertion >= range.start_ins)
                && (value < end || value == end && r.key.insertion <= range.end_ins)
            {
                r.secondary = range.kind;
                source[i] = true;
                break;
            }
        }
    }
    for chain in &m.chains {
        for triple in chain.residues.windows(3) {
            let (a, b, c) = (
                &m.residues[triple[0]],
                &m.residues[triple[1]],
                &m.residues[triple[2]],
            );
            if b.kind != MoleculeKind::Protein
                || !b.connected_to_previous
                || !c.connected_to_previous
            {
                continue;
            }
            let p = [
                atom_named(a, &m.atoms, "C"),
                atom_named(b, &m.atoms, "N"),
                atom_named(b, &m.atoms, "CA"),
                atom_named(b, &m.atoms, "C"),
                atom_named(c, &m.atoms, "N"),
            ];
            if let [Some(a), Some(b), Some(c), Some(d), Some(e)] = p
                && let Some((phi, psi)) =
                    dihedral(a.p, b.p, c.p, d.p).zip(dihedral(b.p, c.p, d.p, e.p))
            {
                estimated[triple[1]] =
                    if (-100.0..=-30.0).contains(&phi) && (-80.0..=-5.0).contains(&psi) {
                        Secondary::Helix
                    } else if (-180.0..=-65.0).contains(&phi)
                        && ((65.0..=180.0).contains(&psi) || (-180.0..=-130.0).contains(&psi))
                    {
                        Secondary::Sheet
                    } else {
                        Secondary::Coil
                    };
            }
        }
        let mut position = 0;
        while position < chain.residues.len() {
            let kind = estimated[chain.residues[position]];
            let mut end = position + 1;
            while end < chain.residues.len() && estimated[chain.residues[end]] == kind {
                end += 1;
            }
            if kind != Secondary::Coil
                && end - position >= if kind == Secondary::Helix { 3 } else { 2 }
            {
                for &r in &chain.residues[position..end] {
                    if !source[r] {
                        m.residues[r].secondary = kind;
                    }
                }
            }
            position = end;
        }
    }
    m.secondary_source=if source.iter().any(|s|*s){"File annotations; unassigned regions use a backbone-geometry display approximation (not DSSP)."}else{"Backbone-geometry display approximation from phi/psi angles (not DSSP or experimental secondary-structure annotation)."}.into();
}

#[derive(Clone, Copy)]
struct Token<'a> {
    text: &'a str,
    quoted: bool,
}
struct Lexer<'a> {
    text: &'a str,
    pos: usize,
    peeked: Option<Token<'a>>,
}
impl<'a> Lexer<'a> {
    fn new(text: &'a str) -> Self {
        Self {
            text: text.trim_start_matches('\u{feff}'),
            pos: 0,
            peeked: None,
        }
    }
    fn peek(&mut self) -> Result<Option<Token<'a>>, String> {
        if self.peeked.is_none() {
            self.peeked = self.read()?;
        }
        Ok(self.peeked)
    }
    fn next(&mut self) -> Result<Option<Token<'a>>, String> {
        if let Some(token) = self.peeked.take() {
            Ok(Some(token))
        } else {
            self.read()
        }
    }
    fn read(&mut self) -> Result<Option<Token<'a>>, String> {
        let bytes = self.text.as_bytes();
        loop {
            while self.pos < bytes.len() && bytes[self.pos].is_ascii_whitespace() {
                self.pos += 1;
            }
            if bytes.get(self.pos) == Some(&b'#') {
                while self.pos < bytes.len() && bytes[self.pos] != b'\n' {
                    self.pos += 1;
                }
            } else {
                break;
            }
        }
        if self.pos == bytes.len() {
            return Ok(None);
        }
        let start = self.pos;
        if bytes[start] == b';' && (start == 0 || bytes[start - 1] == b'\n') {
            self.pos += 1;
            let content = self.pos;
            while self.pos < bytes.len() {
                if bytes[self.pos] == b';' && self.pos > 0 && bytes[self.pos - 1] == b'\n' {
                    let end = self.pos;
                    self.pos += 1;
                    if self.pos < bytes.len()
                        && !bytes[self.pos].is_ascii_whitespace()
                        && bytes[self.pos] != b'#'
                    {
                        return Err("Malformed mmCIF multiline delimiter".into());
                    }
                    return Ok(Some(Token {
                        text: &self.text[content..end],
                        quoted: true,
                    }));
                }
                self.pos += 1;
            }
            return Err("Unterminated mmCIF multiline value".into());
        }
        if matches!(bytes[start], b'\'' | b'"') {
            let quote = bytes[start];
            self.pos += 1;
            let content = self.pos;
            while self.pos < bytes.len() {
                if bytes[self.pos] == quote
                    && (self.pos + 1 == bytes.len()
                        || bytes[self.pos + 1].is_ascii_whitespace()
                        || bytes[self.pos + 1] == b'#')
                {
                    let end = self.pos;
                    self.pos += 1;
                    return Ok(Some(Token {
                        text: &self.text[content..end],
                        quoted: true,
                    }));
                }
                self.pos += 1;
            }
            return Err("Unterminated mmCIF quoted value".into());
        }
        while self.pos < bytes.len() && !bytes[self.pos].is_ascii_whitespace() {
            self.pos += 1;
        }
        Ok(Some(Token {
            text: &self.text[start..self.pos],
            quoted: false,
        }))
    }
}
fn control(token: Token<'_>) -> bool {
    !token.quoted
        && (token.text.starts_with('_')
            || token.text.eq_ignore_ascii_case("loop_")
            || token.text.eq_ignore_ascii_case("stop_")
            || token.text.eq_ignore_ascii_case("global_")
            || token.text.to_ascii_lowercase().starts_with("data_")
            || token.text.to_ascii_lowercase().starts_with("save_"))
}
struct Row<'a> {
    columns: &'a HashMap<String, usize>,
    values: &'a [&'a str],
    prefix: &'a str,
}
impl Row<'_> {
    fn get(&self, name: &str) -> &str {
        self.columns
            .get(&format!("{}.{name}", self.prefix))
            .and_then(|&i| self.values.get(i))
            .copied()
            .unwrap_or("")
    }
    fn preferred(&self, a: &str, b: &str) -> &str {
        let value = self.get(a);
        if missing(value) { self.get(b) } else { value }
    }
}
impl Builder {
    fn cif(&mut self, text: &str) -> Result<(), String> {
        let mut lexer = Lexer::new(text);
        let mut scalar: HashMap<String, String> = HashMap::new();
        let mut block = String::new();
        let mut atom_block = None;
        while let Some(token) = lexer.next()? {
            let lower = token.text.to_ascii_lowercase();
            if !token.quoted && lower.starts_with("data_") {
                block = token.text.to_owned();
                continue;
            }
            if !token.quoted && lower == "loop_" {
                let mut columns = HashMap::new();
                let mut tags = Vec::new();
                while let Some(tag) = lexer.peek()? {
                    if tag.quoted || !tag.text.starts_with('_') {
                        break;
                    }
                    let tag = lexer.next()?.unwrap().text.to_ascii_lowercase();
                    if columns.insert(tag.clone(), tags.len()).is_some() {
                        return Err("Duplicate mmCIF loop column".into());
                    }
                    tags.push(tag);
                    if tags.len() > 512 {
                        return Err("Too many mmCIF loop columns".into());
                    }
                }
                if tags.is_empty() {
                    return Err("mmCIF loop has no columns".into());
                }
                let prefix = tags[0].split('.').next().unwrap_or("").to_owned();
                if tags
                    .iter()
                    .any(|t| t.split('.').next() != Some(prefix.as_str()))
                {
                    return Err("Mixed categories in mmCIF loop".into());
                }
                while let Some(next) = lexer.peek()? {
                    if control(next) {
                        break;
                    }
                    let mut values = Vec::with_capacity(tags.len());
                    for _ in &tags {
                        let next = lexer.next()?.ok_or("Truncated mmCIF loop row")?;
                        if control(next) {
                            return Err("Incomplete mmCIF loop row".into());
                        }
                        values.push(next.text);
                    }
                    if prefix == "_atom_site" {
                        if atom_block.as_ref().is_some_and(|old| old != &block) {
                            return Err("Multiple coordinate data blocks in one mmCIF are unsupported; load each structure separately".into());
                        }
                        atom_block = Some(block.clone());
                    }
                    self.cif_row(&Row {
                        columns: &columns,
                        values: &values,
                        prefix: &prefix,
                    })?;
                }
            } else if !token.quoted && token.text.starts_with('_') {
                let value = lexer.next()?.ok_or("mmCIF tag has no value")?;
                if control(value) {
                    return Err("mmCIF tag has no value".into());
                }
                scalar.insert(lower, value.text.to_owned());
            } else if !token.quoted
                && (lower == "stop_" || lower.starts_with("save_") || lower == "global_")
            {
            } else {
                return Err(format!(
                    "Unexpected mmCIF token: {:?}",
                    token.text.chars().take(60).collect::<String>()
                ));
            }
        }
        let categories: HashSet<_> = scalar
            .keys()
            .filter_map(|k| k.split('.').next())
            .map(str::to_owned)
            .collect();
        for category in categories {
            let mut tags: Vec<_> = scalar
                .keys()
                .filter(|k| k.starts_with(&format!("{category}.")))
                .cloned()
                .collect();
            tags.sort();
            let columns: HashMap<_, _> = tags
                .iter()
                .enumerate()
                .map(|(i, t)| (t.clone(), i))
                .collect();
            let values: Vec<_> = tags.iter().map(|t| scalar[t].as_str()).collect();
            self.cif_row(&Row {
                columns: &columns,
                values: &values,
                prefix: &category,
            })?;
        }
        Ok(())
    }
    fn cif_row(&mut self, row: &Row<'_>) -> Result<(), String> {
        match row.prefix {
            "_atom_site" => {
                let group = row.get("group_pdb");
                if !missing(group) && group != "ATOM" && group != "HETATM" {
                    return Err("Unknown mmCIF atom record type".into());
                }
                let name = id(row.preferred("auth_atom_id", "label_atom_id"))?;
                let hetero = group == "HETATM";
                let model = row.get("pdbx_pdb_model_num");
                self.push(RawAtom {
                    key: ResidueKey {
                        model: if missing(model) {
                            "1".into()
                        } else {
                            id(model)?
                        },
                        chain: id(row.preferred("auth_asym_id", "label_asym_id"))?,
                        label_chain: id(row.get("label_asym_id"))?,
                        sequence: id(row.preferred("auth_seq_id", "label_seq_id"))?,
                        label_sequence: id(row.get("label_seq_id"))?,
                        insertion: id(row.get("pdbx_pdb_ins_code"))?,
                        component: id(row.preferred("auth_comp_id", "label_comp_id"))?,
                        segment: 0,
                    },
                    p: V3(
                        number(row.get("cartn_x"), "x coordinate")?,
                        number(row.get("cartn_y"), "y coordinate")?,
                        number(row.get("cartn_z"), "z coordinate")?,
                    ),
                    element: element(row.get("type_symbol"), &name, hetero),
                    name,
                    serial: id(row.get("id"))?,
                    altloc: id(row.get("label_alt_id"))?,
                    occupancy: optional_number(row.get("occupancy"), "occupancy")?.unwrap_or(1.),
                    b_factor: optional_number(row.get("b_iso_or_equiv"), "B/temperature factor")?,
                    hetero,
                    entity: id(row.get("label_entity_id"))?,
                })?;
            }
            "_entity_poly" => {
                let kind = match row.get("type").to_ascii_lowercase().as_str() {
                    "polyribonucleotide" => MoleculeKind::Rna,
                    "polydeoxyribonucleotide" => MoleculeKind::Dna,
                    t if t.starts_with("polypeptide") => MoleculeKind::Protein,
                    _ => return Ok(()),
                };
                self.entities.insert(id(row.get("entity_id"))?, kind);
            }
            "_struct_asym" => {
                self.asym
                    .insert(id(row.get("id"))?, id(row.get("entity_id"))?);
            }
            "_struct_conf" | "_struct_sheet_range" => {
                let kind = if row.prefix == "_struct_sheet_range" {
                    Secondary::Sheet
                } else if row
                    .get("conf_type_id")
                    .to_ascii_uppercase()
                    .starts_with("HELX")
                {
                    Secondary::Helix
                } else {
                    return Ok(());
                };
                let authored =
                    !missing(row.get("beg_auth_seq_id")) && !missing(row.get("end_auth_seq_id"));
                self.ranges.push(Range {
                    chain: id(row.get("beg_auth_asym_id"))?,
                    label_chain: if !missing(row.get("beg_auth_asym_id")) {
                        String::new()
                    } else {
                        id(row.get("beg_label_asym_id"))?
                    },
                    label_sequence: !authored,
                    start: id(row.preferred("beg_auth_seq_id", "beg_label_seq_id"))?,
                    end: id(row.preferred("end_auth_seq_id", "end_label_seq_id"))?,
                    start_ins: id(row.get("pdbx_beg_pdb_ins_code"))?,
                    end_ins: id(row.get("pdbx_end_pdb_ins_code"))?,
                    kind,
                });
            }
            "_struct_conn" => {
                if !matches!(row.get("conn_type_id"), "covale" | "disulf" | "metalc") {
                    return Ok(());
                }
                let partner = |n: u8| -> Result<AtomRef, String> {
                    Ok(AtomRef {
                        chain: id(row.get(&format!("ptnr{n}_auth_asym_id")))?,
                        label_chain: id(row.get(&format!("ptnr{n}_label_asym_id")))?,
                        sequence: id(row.get(&format!("ptnr{n}_auth_seq_id")))?,
                        label_sequence: id(row.get(&format!("ptnr{n}_label_seq_id")))?,
                        insertion: id(row.get(&format!("pdbx_ptnr{n}_pdb_ins_code")))?,
                        name: id(row.preferred(
                            &format!("ptnr{n}_auth_atom_id"),
                            &format!("ptnr{n}_label_atom_id"),
                        ))?,
                        component: id(row.preferred(
                            &format!("ptnr{n}_auth_comp_id"),
                            &format!("ptnr{n}_label_comp_id"),
                        ))?,
                        altloc: id(row.get(&format!("pdbx_ptnr{n}_label_alt_id")))?,
                    })
                };
                self.connections.push((partner(1)?, partner(2)?));
            }
            _ => {}
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cif(rows: &str) -> Vec<u8> {
        format!("data_test\nloop_\n_atom_site.group_PDB\n_atom_site.id\n_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_alt_id\n_atom_site.label_comp_id\n_atom_site.label_asym_id\n_atom_site.label_entity_id\n_atom_site.label_seq_id\n_atom_site.auth_asym_id\n_atom_site.auth_seq_id\n_atom_site.pdbx_PDB_ins_code\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n_atom_site.occupancy\n_atom_site.B_iso_or_equiv\n_atom_site.pdbx_PDB_model_num\n{rows}\n#\n").into_bytes()
    }
    fn parse(rows: &str) -> Molecule {
        Molecule::parse(&cif(rows), "mmcif", "test").unwrap()
    }
    fn pdb_atom(
        serial: usize,
        name: &str,
        residue: &str,
        chain: char,
        sequence: i32,
        insertion: char,
        x: f32,
    ) -> String {
        format!(
            "ATOM  {serial:>5} {name:^4} {residue:>3} {chain}{sequence:>4}{insertion}   {x:>8.3}{:>8.3}{:>8.3}{:>6.2}{:>6.2}          {:>2}\n",
            0., 0., 1., 42., "C"
        )
    }
    #[test]
    fn author_label_insertion_nonstandard_ligand_and_chain_ids_survive() {
        let m = parse(
            "ATOM 1 C CA . MSE AA 1 1 long_chain 42 A 0 0 0 1 88 1\nATOM 2 C CA . ALA AB 2 1 long_chain 42 A 1 0 0 1 77 1\nHETATM 3 SE SE . XSE LC 3 . ligand 17 ? 3 0 0 1 66 1",
        );
        assert_eq!(m.chains.len(), 3);
        assert_eq!(m.residues.len(), 3);
        let r = &m.residues[0];
        assert_eq!(r.key.chain, "long_chain");
        assert_eq!(r.key.label_chain, "AA");
        assert_eq!(r.key.sequence, "42");
        assert_eq!(r.key.label_sequence, "1");
        assert_eq!(r.key.insertion, "A");
        assert_eq!(r.kind, MoleculeKind::Protein);
        assert!(m.atoms[2].hetero);
        assert_eq!(m.atoms[2].element, "SE");
        assert_eq!(m.residues[2].kind, MoleculeKind::Ligand);
        assert!(m.bonds.is_empty());
        let saved = serde_json::to_string(&r.key).unwrap();
        let restored: ResidueKey = serde_json::from_str(&saved).unwrap();
        assert_eq!(restored, r.key);
        assert_eq!(
            m.distance(&m.residues[0].key, &m.residues[1].key).unwrap(),
            1.
        );
    }
    #[test]
    fn alternate_selection_is_per_residue_and_first_model_is_explicit() {
        let m = parse(
            "ATOM 1 N N . ALA A 1 1 A 1 ? 0 0 0 1 10 1\nATOM 2 C CA A ALA A 1 1 A 1 ? 1 0 0 .4 20 1\nATOM 3 C CA B ALA A 1 1 A 1 ? 2 0 0 .6 30 1\nATOM 4 C CA . ALA A 1 1 A 1 ? 9 0 0 1 40 2",
        );
        assert_eq!(m.atoms.len(), 2);
        assert_eq!(m.atoms[1].altloc, "B");
        assert_eq!(m.atoms[1].p.0, 2.);
        assert_eq!(m.warnings.len(), 2);
        assert_eq!(m.atoms[1].b_factor, Some(30.));
    }
    #[test]
    fn alternative_residue_chemistries_choose_one_physical_conformer() {
        let m = parse(
            "ATOM 1 C CA A ALA A 1 1 A 1 ? 0 0 0 .4 30 1\nATOM 2 C CA B SER A 1 1 A 1 ? 1 0 0 .6 30 1",
        );
        assert_eq!(m.residues.len(), 1);
        assert_eq!(m.residues[0].name, "SER");
        assert_eq!(m.atoms[0].altloc, "B");
    }
    #[test]
    fn missing_secondary_records_use_a_labeled_backbone_approximation() {
        let m = parse("ATOM 1 C CA . ALA A 1 1 A 1 ? 0 0 0 1 90 1");
        assert!(m.secondary_source.contains("approximation"));
        assert!(m.secondary_source.contains("not DSSP"));
        assert_eq!(m.residues[0].secondary, Secondary::Coil);
        // Standard signed torsion: this known geometry is +90 degrees.
        assert!(
            (dihedral(
                V3(1., 0., 0.),
                V3(0., 0., 0.),
                V3(0., 1., 0.),
                V3(0., 1., -1.)
            )
            .unwrap()
                - 90.)
                .abs()
                < 0.001
        );
    }
    #[test]
    fn discontinuous_numbering_ter_and_other_chains_do_not_connect() {
        let text = [
            pdb_atom(1, "CA", "ALA", 'A', 1, ' ', 0.),
            pdb_atom(2, "CA", "GLY", 'A', 3, ' ', 3.8),
            "TER\n".into(),
            pdb_atom(3, "CA", "GLY", 'A', 4, ' ', 5.),
            pdb_atom(4, "CA", "GLY", 'B', 5, ' ', 6.),
        ]
        .concat();
        let m = Molecule::parse(text.as_bytes(), "pdb", "gaps").unwrap();
        assert_eq!(m.chains.len(), 3);
        assert!(m.residues.iter().all(|r| !r.connected_to_previous));
        assert!(m.bonds.is_empty());
        let text = [
            pdb_atom(1, "CA", "ALA", 'A', 42, ' ', 0.),
            pdb_atom(2, "CA", "ALA", 'A', 42, 'A', 3.8),
            pdb_atom(3, "CA", "GLY", 'A', 43, ' ', 7.6),
        ]
        .concat();
        let m = Molecule::parse(text.as_bytes(), "pdb", "insertions").unwrap();
        assert!(m.residues[1].connected_to_previous);
        assert!(m.residues[2].connected_to_previous);
        assert_eq!(m.residues[1].key.insertion, "A");
    }
    #[test]
    fn nucleic_entities_and_quoted_atom_names_are_preserved() {
        let mut bytes = cif(
            "ATOM 1 C \"C4'\" . SYN R 9 1 RNA 1 ? 0 0 0 1 30 1\nATOM 2 O \"O2'\" . SYN R 9 1 RNA 1 ? 1 0 0 1 30 1\nATOM 3 C \"C4'\" . SYN D 10 1 DNA 1 ? 9 0 0 1 30 1",
        );
        bytes.extend_from_slice(b"loop_\n_entity_poly.entity_id\n_entity_poly.type\n9 polyribonucleotide\n10 polydeoxyribonucleotide\n_struct.title\n;Title with spaces\n and a semicolon ; in its content\n;\n");
        let m = Molecule::parse(&bytes, "cif", "synthetic").unwrap();
        assert_eq!(m.residues[0].kind, MoleculeKind::Rna);
        assert_eq!(m.residues[1].kind, MoleculeKind::Dna);
        assert_eq!(m.atoms[0].name, "C4'");
    }
    #[test]
    fn explicit_cross_chain_bonds_are_retained_without_proximity_guessing() {
        let mut bytes = cif(
            "HETATM 1 C C1 . LIG A 1 . A 1 ? 0 0 0 1 10 1\nHETATM 2 N N1 . LIG B 2 . B 1 ? 1.4 0 0 1 10 1\nHETATM 3 O O1 . LIG C 3 . C 1 ? 2 0 0 1 10 1",
        );
        assert!(
            Molecule::parse(&bytes, "cif", "none")
                .unwrap()
                .bonds
                .is_empty()
        );
        bytes.extend_from_slice(b"loop_\n_struct_conn.conn_type_id\n_struct_conn.ptnr1_label_asym_id\n_struct_conn.ptnr1_label_atom_id\n_struct_conn.ptnr2_label_asym_id\n_struct_conn.ptnr2_label_atom_id\ncovale A C1 B N1\n");
        assert_eq!(
            Molecule::parse(&bytes, "cif", "linked").unwrap().bonds,
            vec![(0, 1)]
        );
    }
    #[test]
    fn truncated_nonfinite_binary_and_unknown_formats_fail_clearly() {
        for xyz in ["NaN", "inf", "1e99"] {
            let rows = format!("ATOM 1 C CA . ALA A 1 1 A 1 ? {xyz} 0 0 1 90 1");
            assert!(Molecule::parse(&cif(&rows), "cif", "bad").is_err());
        }
        assert!(Molecule::parse(&cif("ATOM 1 C"), "cif", "truncated").is_err());
        assert!(Molecule::parse(b"ATOM  1\n", "pdb", "truncated").is_err());
        assert!(Molecule::parse(b"\0", "pdb", "binary").is_err());
        assert!(Molecule::parse(b"END\n", "xyz", "unknown").is_err());
        assert!(
            Molecule::parse(&vec![b' '; MAX_STRUCTURE_BYTES + 1], "pdb", "large")
                .err()
                .unwrap()
                .contains("32 MiB")
        );
    }
}
