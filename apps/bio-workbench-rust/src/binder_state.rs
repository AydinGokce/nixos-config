//! Binder design drafts and immutable submission/selection identities.
use crate::ui_state::{rows, text, uid};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Residue {
    pub chain: String,
    pub number: i64,
    #[serde(default)]
    pub insertion_code: String,
}
impl Residue {
    pub fn label(&self) -> String {
        format!(
            "{}:{}{}",
            if self.chain.is_empty() {
                "∅"
            } else {
                &self.chain
            },
            self.number,
            self.insertion_code
        )
    }
}

pub fn inspected_residues(inspection: &Value) -> Vec<Residue> {
    rows(inspection, "chains")
        .iter()
        .flat_map(|chain| rows(chain, "residues"))
        .filter_map(|residue| serde_json::from_value(residue.clone()).ok())
        .collect()
}

/// Resolve text against actual author identifiers, never positional offsets.
pub fn parse_selection(input: &str, available: &[Residue]) -> Result<BTreeSet<Residue>, String> {
    let mut selected = BTreeSet::new();
    for token in input
        .split([',', ';', '\n'])
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        if let Some(residue) = available.iter().find(|r| r.label() == token) {
            selected.insert(residue.clone());
            continue;
        }
        let Some((chain, range)) = token.rsplit_once(':') else {
            return Err(format!(
                "Use chain-qualified positions, for example A:56 or A:56-60: {token}"
            ));
        };
        let chain = if chain == "∅" { "" } else { chain };
        let split = range
            .char_indices()
            .skip(1)
            .find(|(_, ch)| *ch == '-')
            .map(|(i, _)| i);
        let bounds = split.and_then(|i| {
            Some((
                range[..i].parse::<i64>().ok()?,
                range[i + 1..].parse::<i64>().ok()?,
            ))
        });
        let Some((start, end)) = bounds.filter(|(start, end)| start <= end) else {
            return Err(format!(
                "Residue is absent or the range is invalid: {token}"
            ));
        };
        if !available
            .iter()
            .any(|r| r.chain == chain && r.number == start && r.insertion_code.is_empty())
            || !available
                .iter()
                .any(|r| r.chain == chain && r.number == end && r.insertion_code.is_empty())
        {
            return Err(format!(
                "Both range endpoints must exist in the structure: {token}"
            ));
        }
        selected.extend(
            available
                .iter()
                .filter(|r| r.chain == chain && r.number >= start && r.number <= end)
                .cloned(),
        );
    }
    Ok(selected)
}

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Patch {
    pub name: String,
    pub sha256: String,
    pub chains: BTreeSet<String>,
    pub crop: String,
    pub hotspots: BTreeSet<Residue>,
}

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Intent {
    pub id: String,
    pub endpoint: String,
    pub request: Value,
    pub operation: String,
    pub batch_id: String,
    pub error: String,
    pub uncertain: bool,
}
impl Intent {
    /// The UI intent is saved before the transport journal is enqueued. An
    /// empty operation ID can therefore still represent a delivered request.
    pub fn unresolved(&self) -> bool {
        self.batch_id.is_empty() && (self.error.is_empty() || self.uncertain)
    }

    pub fn matches_batch(&self, endpoint: &str, batch_id: &str) -> bool {
        !endpoint.is_empty()
            && self.endpoint == endpoint
            && !batch_id.is_empty()
            && self.batch_id == batch_id
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Draft {
    pub enabled: bool,
    pub name: String,
    pub endpoint: String,
    pub target: Value,
    pub target_name: String,
    pub target_slot: Option<usize>,
    pub inspection: Value,
    pub chains: BTreeSet<String>,
    pub hotspots: BTreeSet<Residue>,
    pub crop: String,
    pub crop_enabled: bool,
    pub lengths: [u32; 2],
    pub designs: u32,
    pub timeout_minutes: u32,
    pub max_cost_usd: f64,
    pub seed: String,
    pub patches: Vec<Patch>,
    pub intent: Option<Intent>,
    pub save_intent: Option<Intent>,
    pub results_job: String,
    pub source_ref: String,
    pub project_ref: String,
}
impl Default for Draft {
    fn default() -> Self {
        Self {
            enabled: false,
            name: "Binder design".into(),
            endpoint: String::new(),
            target: Value::Null,
            target_name: String::new(),
            target_slot: None,
            inspection: Value::Null,
            chains: BTreeSet::new(),
            hotspots: BTreeSet::new(),
            crop: String::new(),
            crop_enabled: false,
            lengths: [65, 100],
            designs: 1,
            timeout_minutes: 60,
            max_cost_usd: 10.,
            seed: String::new(),
            patches: Vec::new(),
            intent: None,
            save_intent: None,
            results_job: String::new(),
            source_ref: String::new(),
            project_ref: String::new(),
        }
    }
}
impl Draft {
    /// Remove only the editable target, keeping exact submitted requests and
    /// saved, structure-bound patches available for recovery and later reuse.
    pub fn clear_target(&mut self) {
        self.name = Self::default().name;
        self.endpoint.clear();
        self.target = Value::Null;
        self.target_name.clear();
        self.target_slot = None;
        self.inspection = Value::Null;
        self.chains.clear();
        self.hotspots.clear();
        self.crop.clear();
        self.crop_enabled = false;
        self.source_ref.clear();
        self.project_ref.clear();
    }

    pub fn request(&self, endpoint: &str) -> Result<Value, String> {
        if self.endpoint != endpoint || endpoint.is_empty() {
            return Err("Select and inspect a target on this head connection.".into());
        }
        if text(&self.target, "id").is_empty()
            || self.inspection["target"]["sha256"] != self.target["sha256"]
        {
            return Err("Wait for the target structure to finish uploading and inspecting.".into());
        }
        let available = inspected_residues(&self.inspection);
        if self.chains.is_empty()
            || self
                .chains
                .iter()
                .any(|chain| !available.iter().any(|r| &r.chain == chain))
        {
            return Err("Select at least one supported protein chain.".into());
        }
        if self.lengths[0] < 5 || self.lengths[1] < self.lengths[0] || self.lengths[1] > 1000 {
            return Err(
                "Binder lengths must be an ordered range between 5 and 1,000 residues.".into(),
            );
        }
        if self.designs == 0
            || self.designs > 10_000
            || self.timeout_minutes == 0
            || self.timeout_minutes > 1425
            || !self.max_cost_usd.is_finite()
            || self.max_cost_usd <= 0.
            || self.max_cost_usd > 750.
        {
            return Err("Set positive design, runtime, and spending limits.".into());
        }
        let crop = if self.crop_enabled {
            let crop = parse_selection(&self.crop, &available)?;
            if crop.is_empty() {
                return Err("Select residues for the crop or disable cropping.".into());
            }
            Some(crop)
        } else {
            None
        };
        for residue in self
            .hotspots
            .iter()
            .chain(crop.iter().flat_map(|r| r.iter()))
        {
            if !available.contains(residue) || !self.chains.contains(&residue.chain) {
                return Err(format!(
                    "{} is outside the selected target chains.",
                    residue.label()
                ));
            }
        }
        if let Some(crop) = &crop
            && !self.hotspots.is_subset(crop)
        {
            return Err("Every hotspot must be included in the submitted crop.".into());
        }
        let mut target = self.target.clone();
        if !self.source_ref.trim().is_empty() {
            target["source_ref"] = json!(self.source_ref.trim());
        }
        if !self.project_ref.trim().is_empty() {
            target["project_ref"] = json!(self.project_ref.trim());
        }
        let mut value = json!({"request_key":uid(),"name":self.name,"target":target,"chains":self.chains,
            "hotspots":self.hotspots,"lengths":self.lengths,"designs":self.designs,
            "timeout_seconds":u64::from(self.timeout_minutes)*60,"max_cost_usd":self.max_cost_usd});
        if let Some(crop) = crop {
            value["crop"] = json!(crop);
        }
        if !self.seed.trim().is_empty() {
            let seed = self
                .seed
                .trim()
                .parse::<u32>()
                .map_err(|_| "Seed must be between 0 and 2,147,483,647.")?;
            if seed > 2_147_483_647 {
                return Err("Seed must be between 0 and 2,147,483,647.".into());
            }
            value["seed"] = json!(seed);
        }
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn available() -> Vec<Residue> {
        vec![
            ("A", -2, ""),
            ("A", 1, ""),
            ("A", 2, ""),
            ("A", 2, "A"),
            ("A", 4, ""),
            ("B", 2, ""),
        ]
        .into_iter()
        .map(|(chain, number, ins)| Residue {
            chain: chain.into(),
            number,
            insertion_code: ins.into(),
        })
        .collect()
    }
    #[test]
    fn selection_preserves_chain_author_numbers_insertions_and_gaps() {
        let residues = available();
        let selection = parse_selection("A:1-4, B:2, A:-2", &residues).unwrap();
        assert_eq!(selection.len(), 6);
        assert!(selection.contains(&residues[3]));
        assert!(parse_selection("A:3", &residues).is_err());
        assert!(parse_selection("A:1-9", &residues).is_err());
        assert!(parse_selection("2", &residues).is_err());
        assert_eq!(parse_selection("A:2A", &residues).unwrap().len(), 1);
    }
    #[test]
    fn submission_rejects_stale_head_outside_hotspot_and_empty_crop() {
        let residues = available();
        let target = json!({"kind":"artifact","id":"artifact","sha256":"exact"});
        let mut draft = Draft {
            endpoint: "head".into(),
            target: target.clone(),
            inspection: json!({"target":target,"chains":[{"residues":residues}]}),
            chains: BTreeSet::from(["A".into()]),
            ..Default::default()
        };
        assert!(draft.request("other-head").is_err());
        assert!(draft.request("head").is_ok());
        draft.hotspots.insert(residues[5].clone());
        assert!(draft.request("head").is_err());
        draft.hotspots = BTreeSet::from([residues[1].clone()]);
        draft.crop_enabled = true;
        assert!(draft.request("head").is_err());
        draft.crop = "A:2".into();
        assert!(draft.request("head").is_err());
        draft.crop = "A:1-4".into();
        assert_eq!(
            draft.request("head").unwrap()["crop"]
                .as_array()
                .unwrap()
                .len(),
            4
        );
    }
    #[test]
    fn draft_roundtrip_preserves_pending_exact_request_and_patch_identity() {
        let draft = Draft {
            intent: Some(Intent {
                id: "intent".into(),
                operation: "op".into(),
                request: json!({"request_key":"original","seed":42}),
                uncertain: true,
                ..Default::default()
            }),
            patches: vec![Patch {
                name: "patch".into(),
                sha256: "original-structure".into(),
                hotspots: BTreeSet::from([available()[3].clone()]),
                ..Default::default()
            }],
            ..Default::default()
        };
        let restored: Draft = serde_json::from_value(json!(draft)).unwrap();
        assert_eq!(restored.intent.unwrap().request["request_key"], "original");
        assert_eq!(
            restored.patches[0]
                .hotspots
                .iter()
                .next()
                .unwrap()
                .insertion_code,
            "A"
        );
        assert_eq!(restored.patches[0].sha256, "original-structure");
    }
    #[test]
    fn intent_without_ui_operation_receipt_still_requires_exact_reconciliation() {
        let mut intent = Intent {
            request: json!({"request_key":"captured-before-enqueue"}),
            ..Default::default()
        };
        assert!(intent.unresolved());
        intent.error = "Request was rejected before allocation".into();
        assert!(!intent.unresolved());
        intent.uncertain = true;
        assert!(intent.unresolved());
        intent.batch_id = "accepted-batch".into();
        assert!(!intent.unresolved());
    }

    #[test]
    fn progress_rejects_prior_batch_replies_and_other_heads() {
        let mut intent = Intent {
            endpoint: "head-one".into(),
            batch_id: "first-run".into(),
            ..Default::default()
        };
        assert!(intent.matches_batch("head-one", "first-run"));
        intent.batch_id.clear();
        assert!(!intent.matches_batch("head-one", "first-run"));
        assert!(!intent.matches_batch("head-one", ""));
        intent.batch_id = "second-run".into();
        assert!(!intent.matches_batch("head-one", "first-run"));
        assert!(intent.matches_batch("head-one", "second-run"));
        assert!(!intent.matches_batch("head-two", "second-run"));
    }

    #[test]
    fn clearing_target_persists_without_changing_submitted_or_recoverable_work() {
        let mut draft = Draft {
            enabled: true,
            name: "target binders".into(),
            endpoint: "head".into(),
            target: json!({"kind":"upload","id":"upload","sha256":"original"}),
            target_name: "target".into(),
            target_slot: Some(4),
            inspection: json!({"target":{"sha256":"original"}}),
            chains: BTreeSet::from(["A".into()]),
            hotspots: BTreeSet::from([available()[3].clone()]),
            crop: "A:1-4".into(),
            crop_enabled: true,
            source_ref: "construct:source@1".into(),
            project_ref: "project:project@2".into(),
            lengths: [40, 80],
            designs: 5,
            seed: "42".into(),
            results_job: "existing-results".into(),
            patches: vec![Patch {
                name: "saved patch".into(),
                sha256: "original".into(),
                hotspots: BTreeSet::from([available()[3].clone()]),
                ..Default::default()
            }],
            intent: Some(Intent {
                endpoint: "head".into(),
                request: json!({"target":{"id":"original"},"request_key":"never-resubmit"}),
                uncertain: true,
                ..Default::default()
            }),
            save_intent: Some(Intent {
                endpoint: "head".into(),
                request: json!({"candidate":"existing","request_key":"never-save-twice"}),
                uncertain: true,
                ..Default::default()
            }),
            ..Default::default()
        };
        let before = json!(draft);
        draft.clear_target();
        let restored: Draft = serde_json::from_value(json!(draft)).unwrap();
        assert!(restored.enabled);
        assert!(restored.target.is_null() && restored.inspection.is_null());
        assert!(restored.target_slot.is_none());
        assert!(restored.endpoint.is_empty() && restored.target_name.is_empty());
        assert!(restored.chains.is_empty() && restored.hotspots.is_empty());
        assert!(restored.crop.is_empty() && !restored.crop_enabled);
        assert!(restored.source_ref.is_empty() && restored.project_ref.is_empty());
        assert!(restored.request("head").is_err());
        for field in [
            "intent",
            "save_intent",
            "patches",
            "results_job",
            "lengths",
            "designs",
            "seed",
            "timeout_minutes",
            "max_cost_usd",
        ] {
            assert_eq!(json!(restored)[field], before[field], "changed {field}");
        }
    }
}
