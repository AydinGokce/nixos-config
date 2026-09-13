//! Exact cropped-target correspondence for display against the full input.
use super::*;

fn exact_key(molecule: &scene::Molecule, reference: &Value) -> Result<scene::ResidueKey, String> {
    let requested: binder_state::Residue = serde_json::from_value(reference.clone())
        .map_err(|_| "Incomplete residue correspondence from the head.")?;
    let mut matches = molecule.residues.iter().filter(|r| {
        r.key.chain == requested.chain
            && r.key.sequence.parse::<i64>().ok() == Some(requested.number)
            && r.key.insertion == requested.insertion_code
    });
    let key = matches
        .next()
        .ok_or_else(|| {
            format!(
                "Mapped residue {} is absent from this structure.",
                requested.label()
            )
        })?
        .key
        .clone();
    if matches.next().is_some() {
        return Err(format!(
            "Mapped residue {} is ambiguous in this structure.",
            requested.label()
        ));
    }
    Ok(key)
}

impl Workbench {
    pub(super) fn binder_received_context(
        &mut self,
        job: &str,
        artifact: &str,
        candidate_slot: usize,
        mut value: Value,
        ctx: &egui::Context,
    ) {
        if self.binder.draft.results_job != job
            || self.binder.context_request.as_ref()
                != Some(&(job.into(), artifact.into(), candidate_slot))
        {
            return;
        }
        self.binder.context_request = None;
        if !self.has_view(candidate_slot) {
            return;
        }
        if text(&value, "job_id") != job
            || text(&value, "artifact_id") != artifact
            || text(&value["output_structure_artifact"], "artifact_id") != artifact
            || text(&value["output_structure_artifact"], "sha256").len() != 64
        {
            self.binder.error = "Incomplete or unrelated full-target alignment receipt.".into();
            return;
        }
        if text(&value["output_mapping"], "status") != "available" {
            self.binder.error = format!(
                "Full-target fit unavailable: {}",
                text(&value["output_mapping"], "reason")
            );
            return;
        }
        let original = &value["original_structure_artifact"];
        let id = text(original, "artifact_id").to_owned();
        if id.is_empty()
            || text(original, "sha256").len() != 64
            || original["sha256"] != value["target"]["sha256"]
        {
            self.binder.error = "The full target was not retained with this run.".into();
            return;
        }
        self.artifact_metadata.insert(id.clone(), original.clone());
        self.open_artifact_new_tab(id.clone(), ctx);
        value["source_slot"] = json!(self.state.selected_view);
        value["candidate_slot"] = json!(candidate_slot);
        value["candidate_artifact_id"] = json!(artifact);
        value["source_artifact_id"] = json!(id);
        self.binder.context_pending = Some(value);
    }

    pub(super) fn binder_finish_context(&mut self) {
        let Some(context) = self.binder.context_pending.as_ref() else {
            return;
        };
        let source_slot = context["source_slot"].as_u64().unwrap_or(u64::MAX) as usize;
        let candidate_slot = context["candidate_slot"].as_u64().unwrap_or(u64::MAX) as usize;
        if !self.has_view(source_slot)
            || !self.has_view(candidate_slot)
            || self.view_errors.contains_key(&source_slot)
            || self.view_errors.contains_key(&candidate_slot)
        {
            self.binder.context_pending = None;
            self.binder.error =
                "Full-target fit stopped because a required tab closed or failed to load.".into();
            return;
        }
        if !self.views.contains_key(&source_slot)
            || !self.views.contains_key(&candidate_slot)
            || self.view_loading.contains_key(&source_slot)
            || self.view_loading.contains_key(&candidate_slot)
        {
            return;
        }
        let context = self.binder.context_pending.take().unwrap();
        let result = (|| -> Result<(), String> {
            let source = &self.views[&source_slot];
            let candidate = &self.views[&candidate_slot];
            if source.metadata["sha256"] != context["original_structure_artifact"]["sha256"] {
                return Err("Full target hash differs from the alignment receipt.".into());
            }
            if candidate.metadata["artifact_id"]
                != context["output_structure_artifact"]["artifact_id"]
                || candidate.metadata["sha256"] != context["output_structure_artifact"]["sha256"]
            {
                return Err("Candidate hash differs from the alignment receipt.".into());
            }
            let pairs = rows(&context["output_mapping"], "pairs")
                .iter()
                .map(|pair| {
                    Ok((
                        exact_key(&source.molecule, &pair["original"])?,
                        exact_key(&candidate.molecule, &pair["output"])?,
                    ))
                })
                .collect::<Result<Vec<_>, String>>()?;
            let mut molecule = scene::Molecule::parse(
                &candidate.bytes,
                &candidate.molecule.format,
                &candidate.molecule.name,
            )?;
            let receipt =
                alignment::align_with_residue_pairs(&source.molecule, &mut molecule, &pairs)?;
            molecule.center = source.molecule.center;
            let camera = source.camera;
            let reference_sha = source.metadata["sha256"].clone();
            let source_sha = candidate.metadata["sha256"].clone();
            let center = molecule.center;
            let renderer = scene::Renderer::new(&self.gl, &molecule)?;
            let candidate = self.views.get_mut(&candidate_slot).unwrap();
            candidate.molecule = molecule;
            candidate.camera = camera;
            candidate.metadata["binder_alignment"] = json!(receipt);
            candidate.metadata["binder_alignment_source_sha256"] = source_sha;
            candidate.metadata["binder_alignment_reference_sha256"] = reference_sha;
            candidate.metadata["binder_alignment_center"] = json!([center.0, center.1, center.2]);
            let old = std::mem::replace(&mut candidate.renderer, renderer);
            self.retire_renderer(old);
            self.focus_view(source_slot);
            ui_dock::move_to_split(&mut self.dock, candidate_slot, egui_dock::Split::Right);
            self.focus_view(candidate_slot);
            self.state.link_views = true;
            self.log(format!("Fitted cropped target to full input using {} exact residue correspondences. Full-context molecules were not part of binder optimization.",pairs.len()));
            Ok(())
        })();
        if let Err(error) = result {
            self.binder.error = error;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identity_lookup_uses_author_chain_number_and_insertion() {
        let molecule = scene::Molecule::reference();
        let residue = &molecule.residues[0].key;
        let original = json!({"chain":residue.chain,"number":residue.sequence.parse::<i64>().unwrap(),"insertion_code":residue.insertion});
        assert_eq!(exact_key(&molecule, &original).unwrap(), *residue);
        let mut absent = original;
        absent["insertion_code"] = json!("definitely-absent");
        assert!(exact_key(&molecule, &absent).is_err());
    }
}
