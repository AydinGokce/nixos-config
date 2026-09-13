//! Named protein colors bound to the exact structure and parsed residue order.
use crate::{scene, ui_state};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Span {
    pub start: usize,
    pub end: usize,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Layer {
    pub id: String,
    pub label: String,
    pub color: [u8; 3],
    pub enabled: bool,
    /// Parsed residue indices, protected by both hashes on Data.
    pub spans: Vec<Span>,
    pub origin: Value,
}

#[derive(Clone, Default, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Data {
    pub structure_sha256: String,
    pub residue_map_sha256: String,
    pub layers: Vec<Layer>,
}

#[derive(Clone, Default)]
pub struct Editor {
    pub data: Data,
    pub undo: Vec<Data>,
    pub redo: Vec<Data>,
    pub open: bool,
    pub editing: Option<String>,
    pub label: String,
    pub ranges: String,
    pub color: [u8; 3],
    pub chain: usize,
    pub error: String,
}

pub fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn residue_signature(molecule: &scene::Molecule) -> String {
    digest(
        &serde_json::to_vec(&molecule.residues.iter().map(|r| &r.key).collect::<Vec<_>>()).unwrap(),
    )
}
pub fn palette(index: usize) -> [u8; 3] {
    const COLORS: [[u8; 3]; 10] = [
        [230, 156, 78],
        [180, 131, 218],
        [105, 192, 128],
        [227, 117, 137],
        [113, 155, 232],
        [210, 195, 95],
        [93, 202, 169],
        [218, 141, 199],
        [178, 188, 206],
        [87, 190, 209],
    ];
    COLORS[index % COLORS.len()]
}

impl Editor {
    pub fn new(molecule: &scene::Molecule, structure_sha256: &str) -> Self {
        Self {
            data: Data {
                structure_sha256: structure_sha256.into(),
                residue_map_sha256: residue_signature(molecule),
                layers: vec![],
            },
            chain: molecule
                .chains
                .iter()
                .position(|c| c.kind == scene::MoleculeKind::Protein)
                .unwrap_or(0),
            color: palette(0),
            ..Self::default()
        }
    }
    pub fn restore(value: &Value, molecule: &scene::Molecule, sha: &str) -> Self {
        let mut editor = Self::new(molecule, sha);
        if value.is_null() {
            return editor;
        }
        match serde_json::from_value::<Data>(value.clone()) {
            Ok(data) => match validate(&data, molecule, sha) {
                Ok(()) => editor.data = data,
                Err(error) => editor.error = error,
            },
            Err(_) => editor.error = "Saved domain colors could not be read.".into(),
        }
        editor
    }
    pub fn replace(&mut self, data: Data, molecule: &scene::Molecule) -> Result<(), String> {
        validate(&data, molecule, &self.data.structure_sha256)?;
        if self.data != data {
            self.undo.push(std::mem::replace(&mut self.data, data));
            if self.undo.len() > 30 {
                self.undo.remove(0);
            }
            self.redo.clear();
        }
        self.error.clear();
        Ok(())
    }
    pub fn undo(&mut self) {
        if let Some(data) = self.undo.pop() {
            self.redo.push(std::mem::replace(&mut self.data, data));
        }
    }
    pub fn redo(&mut self) {
        if let Some(data) = self.redo.pop() {
            self.undo.push(std::mem::replace(&mut self.data, data));
        }
    }
    pub fn colors(&self, molecule: &scene::Molecule) -> BTreeMap<scene::ResidueKey, [u8; 3]> {
        let mut colors = BTreeMap::new();
        for layer in self.data.layers.iter().filter(|l| l.enabled) {
            for span in &layer.spans {
                for residue in molecule
                    .residues
                    .get(span.start..span.end)
                    .unwrap_or_default()
                {
                    colors.insert(residue.key.clone(), layer.color);
                }
            }
        }
        colors
    }
}

fn validate(data: &Data, molecule: &scene::Molecule, sha: &str) -> Result<(), String> {
    if data.structure_sha256 != sha || data.residue_map_sha256 != residue_signature(molecule) {
        return Err("Saved domains belong to different structure bytes or residue identities; import them again for this structure.".into());
    }
    if data.layers.len() > 128 {
        return Err("Keep at most 128 domain layers per structure tab.".into());
    }
    let mut count = 0usize;
    let mut ids = std::collections::BTreeSet::new();
    for layer in &data.layers {
        if layer.id.is_empty()
            || layer.id.len() > 128
            || !ids.insert(&layer.id)
            || layer.label.trim().is_empty()
            || layer.label.len() > 512
            || layer.spans.is_empty()
            || layer.spans.len() > 4096
            || serde_json::to_vec(&layer.origin).map_or(true, |v| v.len() > 32768)
        {
            return Err("Domain names, identities or provenance exceed supported bounds.".into());
        }
        let mut previous = 0;
        for span in &layer.spans {
            if span.start >= span.end
                || span.end > molecule.residues.len()
                || span.start < previous
                || molecule.residues[span.start..span.end]
                    .iter()
                    .any(|r| r.kind != scene::MoleculeKind::Protein)
            {
                return Err("Domain ranges must identify existing protein residues.".into());
            }
            count += span.end - span.start;
            previous = span.end;
        }
    }
    if count > 100_000 {
        return Err(
            "Domain layers exceed 100,000 residue references; remove unused layers.".into(),
        );
    }
    Ok(())
}

pub fn protein_chain(molecule: &scene::Molecule, chain: usize) -> Vec<usize> {
    molecule
        .chains
        .get(chain)
        .map(|c| {
            c.residues
                .iter()
                .copied()
                .filter(|&i| molecule.residues[i].kind == scene::MoleculeKind::Protein)
                .collect()
        })
        .unwrap_or_default()
}
pub fn spans(mut indices: Vec<usize>) -> Vec<Span> {
    indices.sort_unstable();
    indices.dedup();
    let mut output: Vec<Span> = Vec::new();
    for index in indices {
        if let Some(last) = output.last_mut().filter(|last| last.end == index) {
            last.end += 1;
        } else {
            output.push(Span {
                start: index,
                end: index + 1,
            });
        }
    }
    output
}
pub fn parse_ranges(input: &str, length: usize) -> Result<Vec<Span>, String> {
    if input.len() > 4096 {
        return Err("Protein ranges exceed the input limit.".into());
    }
    let mut indices = Vec::new();
    for part in input
        .split([',', ';', '\n'])
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        let (a, b) = part.split_once('-').unwrap_or((part, part));
        let (Ok(start), Ok(end)) = (a.trim().parse::<usize>(), b.trim().parse::<usize>()) else {
            return Err("Use protein positions such as 1-150, 320-400.".into());
        };
        if start == 0 || start > end || end > length {
            return Err(format!("Protein positions must be between 1 and {length}."));
        }
        if indices.len() + end - start + 1 > 100_000 {
            return Err("Too many protein positions.".into());
        }
        indices.extend(start - 1..end);
    }
    if indices.is_empty() {
        return Err("Enter at least one protein position or range.".into());
    }
    Ok(spans(indices))
}
pub fn ranges_label(ranges: &[Span]) -> String {
    ranges
        .iter()
        .map(|s| {
            if s.end == s.start + 1 {
                (s.start + 1).to_string()
            } else {
                format!("{}-{}", s.start + 1, s.end)
            }
        })
        .collect::<Vec<_>>()
        .join(", ")
}
pub fn manual_layer(
    molecule: &scene::Molecule,
    chain: usize,
    input: &str,
    name: &str,
    color: [u8; 3],
) -> Result<Layer, String> {
    let residues = protein_chain(molecule, chain);
    let ranges = parse_ranges(input, residues.len())?;
    let indices = ranges
        .iter()
        .flat_map(|s| residues[s.start..s.end].iter().copied())
        .collect();
    Ok(Layer {
        id: ui_state::uid(),
        label: name.trim().into(),
        color,
        enabled: true,
        spans: spans(indices),
        origin: json!({"kind":"manual","chain_index":chain,"chain":molecule.chains[chain].id,
            "coordinate_system":"chain_protein_0based_half_open","protein_segments":ranges}),
    })
}

/// Exact sequence correspondence. Author residue numbers are never offsets.
pub fn sequence_mapping(
    protein: &str,
    molecule: &scene::Molecule,
    chain: usize,
) -> Result<Vec<Option<usize>>, String> {
    let indices = protein_chain(molecule, chain);
    let sequence: String = indices
        .iter()
        .map(|&i| molecule.residues[i].letter)
        .collect();
    if protein.is_empty()
        || sequence.is_empty()
        || !protein.is_ascii()
        || !sequence.is_ascii()
        || !protein
            .bytes()
            .all(|b| b"ACDEFGHIKLMNPQRSTVWY".contains(&b))
        || !sequence
            .bytes()
            .all(|b| b"ACDEFGHIKLMNPQRSTVWY".contains(&b))
    {
        return Err(
            "An exact canonical protein sequence is required for annotation mapping.".into(),
        );
    }
    let mut mapping = vec![None; protein.len()];
    if protein == sequence {
        for (dest, index) in mapping.iter_mut().zip(indices) {
            *dest = Some(index);
        }
        return Ok(mapping);
    }
    let (haystack, needle, chain_is_shorter) = if protein.len() >= sequence.len() {
        (protein, sequence.as_str(), true)
    } else {
        (sequence.as_str(), protein, false)
    };
    let matches: Vec<_> = haystack
        .as_bytes()
        .windows(needle.len())
        .enumerate()
        .filter(|(_, part)| *part == needle.as_bytes())
        .map(|(i, _)| i)
        .take(2)
        .collect();
    if matches.len() == 1 {
        let start = matches[0];
        if chain_is_shorter {
            for (offset, index) in indices.iter().enumerate() {
                mapping[start + offset] = Some(*index);
            }
        } else {
            for (offset, dest) in mapping.iter_mut().enumerate() {
                *dest = Some(indices[start + offset]);
            }
        }
        return Ok(mapping);
    }
    if matches.len() > 1 || !chain_is_shorter {
        return Err("The selected chain has no unique exact match to this protein.".into());
    }
    // Missing coordinates are supported only when their sequence embedding is unique.
    let mut early = Vec::new();
    let mut cursor = 0;
    for letter in sequence.bytes() {
        let Some(offset) = protein.as_bytes()[cursor..]
            .iter()
            .position(|&b| b == letter)
        else {
            return Err("The selected chain differs from this protein sequence.".into());
        };
        cursor += offset;
        early.push(cursor);
        cursor += 1;
    }
    let mut late = Vec::new();
    cursor = protein.len();
    for letter in sequence.bytes().rev() {
        let Some(position) = protein.as_bytes()[..cursor]
            .iter()
            .rposition(|&b| b == letter)
        else {
            unreachable!()
        };
        late.push(position);
        cursor = position;
    }
    late.reverse();
    if early != late {
        return Err("Missing residues make the sequence correspondence ambiguous; choose a complete matching structure.".into());
    }
    for (position, index) in early.into_iter().zip(indices) {
        mapping[position] = Some(index);
    }
    Ok(mapping)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn protein(names: &[&str]) -> scene::Molecule {
        let raw = names
            .iter()
            .enumerate()
            .map(|(i, name)| {
                format!(
                    "ATOM  {:5}  CA  {:3} A{:4}    {:8.3}{:8.3}{:8.3}  1.00 20.00           C  \n",
                    i + 1,
                    name,
                    i + 101,
                    i as f32 * 3.8,
                    0.,
                    0.
                )
            })
            .collect::<String>();
        scene::Molecule::parse(raw.as_bytes(), "pdb", "test").unwrap()
    }
    #[test]
    fn ranges_are_protein_indices_not_author_numbers() {
        let molecule = protein(&["ALA", "CYS", "ASP", "GLU"]);
        let layer = manual_layer(&molecule, 0, "2-3", "domain", palette(0)).unwrap();
        assert_eq!(layer.spans, vec![Span { start: 1, end: 3 }]);
        assert_eq!(molecule.residues[1].key.sequence, "102");
        assert!(parse_ranges("0-2", 4).is_err());
        assert!(parse_ranges("2-5", 4).is_err());
        assert_eq!(
            parse_ranges("2-3,1,3", 4).unwrap(),
            vec![Span { start: 0, end: 3 }]
        );
    }
    #[test]
    fn exact_mapping_handles_offsets_missing_residues_and_rejects_ambiguity() {
        let molecule = protein(&["ALA", "CYS", "GLU"]);
        assert_eq!(
            sequence_mapping("ACDE", &molecule, 0).unwrap(),
            vec![Some(0), Some(1), None, Some(2)]
        );
        assert!(sequence_mapping("ACDDE", &molecule, 0).is_ok());
        assert!(sequence_mapping("AACDE", &molecule, 0).is_err());
        assert!(sequence_mapping("ACDF", &molecule, 0).is_err());
        assert!(sequence_mapping("ACXDE", &molecule, 0).is_err());
        assert_eq!(
            sequence_mapping("TTACETT", &molecule, 0).unwrap()[2..5],
            [Some(0), Some(1), Some(2)]
        );
        assert!(sequence_mapping("ACEACE", &molecule, 0).is_err());
    }
    #[test]
    fn domain_restore_is_bound_to_bytes_and_residue_order_with_reversible_edits() {
        let molecule = protein(&["ALA", "CYS", "ASP", "GLU"]);
        let mut editor = Editor::new(&molecule, "bytes");
        let mut data = editor.data.clone();
        data.layers
            .push(manual_layer(&molecule, 0, "1-2", "first", palette(0)).unwrap());
        editor.replace(data, &molecule).unwrap();
        editor.undo();
        assert!(editor.data.layers.is_empty());
        editor.redo();
        let saved = json!(editor.data);
        let restored = Editor::restore(&saved, &molecule, "bytes");
        assert_eq!(restored.data, editor.data);
        assert_eq!(restored.colors(&molecule).len(), 2);
        assert!(
            Editor::restore(&saved, &molecule, "other")
                .data
                .layers
                .is_empty()
        );
        let mut reordered = molecule.clone();
        reordered.residues.swap(0, 1);
        assert!(
            Editor::restore(&saved, &reordered, "bytes")
                .data
                .layers
                .is_empty()
        );
    }
}
