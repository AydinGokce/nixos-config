//! Candidate inspection and project promotion for managed binder jobs.
use super::*;
use ui_binder::Request;

fn metric(candidate: &Value, names: &[&str]) -> Option<f64> {
    names.iter().find_map(|name| {
        let value = &candidate["metrics"][*name];
        value
            .as_f64()
            .or_else(|| value.as_str()?.parse().ok())
            .filter(|n| n.is_finite())
    })
}
fn formatted(value: Option<f64>) -> String {
    value
        .map(|n| format!("{n:.3}"))
        .unwrap_or_else(|| "—".into())
}
fn rank(status: &str) -> u8 {
    match status {
        "accepted" => 0,
        "rejected" => 1,
        _ => 2,
    }
}

fn target_pairs(
    reference: &scene::Molecule,
    moving: &scene::Molecule,
) -> Result<Vec<(scene::ResidueKey, scene::ResidueKey)>, String> {
    let target = |molecule: &scene::Molecule| {
        molecule
            .residues
            .iter()
            .filter(|r| r.key.chain == "A" && r.kind == scene::MoleculeKind::Protein)
            .map(|r| r.key.clone())
            .collect::<Vec<_>>()
    };
    let fixed = target(reference);
    let source = target(moving);
    if fixed.len() < 3
        || fixed.len() != source.len()
        || fixed
            .iter()
            .zip(&source)
            .any(|(a, b)| a.component != b.component)
    {
        return Err(
            "Candidate target chains do not have the same complete ordered chemistry.".into(),
        );
    }
    Ok(fixed.into_iter().zip(source).collect())
}

impl Workbench {
    pub(super) fn binder_open_candidates(&mut self, job: String) {
        if self.binder.draft.results_job != job {
            self.binder.candidates = Value::Null;
            self.binder.selected_candidate.clear();
            self.binder.candidate_cursor = None;
            self.binder.candidate_pages.clear();
            self.binder.context_pending = None;
            self.binder.context_request = None;
        }
        self.binder.draft.results_job = job.clone();
        self.binder.results_open = true;
        self.binder_fetch_candidates();
    }

    pub(super) fn binder_fetch_candidates(&mut self) {
        let job = self.binder.draft.results_job.clone();
        let cursor = self.binder.candidate_cursor.clone();
        let mut params = json!({"job_id":job,"limit":100});
        if let Some(cursor) = &cursor {
            params["cursor"] = json!(cursor);
        }
        self.request(
            "binder.candidates",
            params,
            Purpose::Binder(Request::Candidates(job, cursor)),
        );
    }

    fn binder_open_candidate(
        &mut self,
        candidate: &Value,
        artifact: &Value,
        ctx: &egui::Context,
    ) -> Option<usize> {
        let id = text(artifact, "artifact_id").to_owned();
        if id.is_empty() {
            return None;
        }
        let mut metadata = artifact.clone();
        metadata["binder_job_id"] = json!(self.binder.draft.results_job);
        metadata["binder_candidate_id"] = candidate["candidate_id"].clone();
        metadata["input_name"] = candidate["name"].clone();
        metadata["model"] = json!("BindCraft");
        self.artifact_metadata.insert(id.clone(), metadata);
        self.open_artifact_new_tab(id, ctx);
        Some(self.state.selected_view)
    }

    pub(super) fn binder_results_panel(&mut self, ctx: &egui::Context) {
        if !self.binder.results_open || self.sidebar_tab == 2 {
            return;
        }
        egui::TopBottomPanel::bottom("binder-candidates").resizable(true).default_height(220.).min_height(90.).show(ctx,|ui|{
            ui.horizontal(|ui|{
                ui.strong("BINDER CANDIDATES");
                ui.weak(text(&self.binder.candidates,"state"));
                if ui.button("Refresh").clicked(){self.binder_open_candidates(self.binder.draft.results_job.clone());}
                if ui.button("Align open candidates").clicked(){self.binder_align_candidates();}
                if ui.add_enabled(!self.binder.candidate_pages.is_empty(),egui::Button::new("← Previous")).clicked(){self.binder.candidate_cursor=self.binder.candidate_pages.pop().flatten();self.binder_fetch_candidates();}
                if let Some(cursor)=self.binder.candidates["next_cursor"].as_str().map(str::to_owned)
                    && ui.button("Next →").clicked(){self.binder.candidate_pages.push(self.binder.candidate_cursor.clone());self.binder.candidate_cursor=Some(cursor);self.binder_fetch_candidates();}
                if ui.button("×").on_hover_text("Close candidate panel").clicked(){self.binder.results_open=false;}
            });
            let candidates=rows(&self.binder.candidates,"candidates");
            let accepted=self.binder.candidates["summary"]["accepted"].as_u64().unwrap_or(0);
            let total=self.binder.candidates["summary"]["total"].as_u64().unwrap_or(candidates.len() as u64);
            ui.horizontal_wrapped(|ui|{
                ui.colored_label(GREEN,format!("{accepted} accepted"));
                ui.label(format!("{total} retained candidates · {} on this page",candidates.len()));
                ui.weak("Software confidence/interface scores; no experimental affinity implied.");
            });
            if !self.binder.error.is_empty(){ui.colored_label(RED,&self.binder.error);}
            ui.collapsing("Run filter failures",|ui|{
                if let Some(failures)=self.binder.candidates["summary"]["filter_failures"].as_object(){for (name,count) in failures {ui.label(format!("{name}: {count}"));}}
                ui.weak("Aggregate native counts across this run; not assigned to individual candidates.");
            });
            if candidates.is_empty() {
                ui.weak(if self.binder.candidates.is_null(){"Loading candidates…"}else{"No retained candidates yet. Follow the run log for current design attempts."});
            }
            let mut candidates=candidates.to_vec();
            let sort=self.binder.result_sort.as_str();
            candidates.sort_by(|a,b|{
                let order=match sort {
                    "name"=>text(a,"name").cmp(text(b,"name")),
                    "pLDDT"=>metric(a,&["plddt","Average_pLDDT","pLDDT"]).unwrap_or(f64::NEG_INFINITY).total_cmp(&metric(b,&["plddt","Average_pLDDT","pLDDT"]).unwrap_or(f64::NEG_INFINITY)),
                    "ipTM"=>metric(a,&["iptm","Average_i_pTM","i_pTM","ipTM"]).unwrap_or(f64::NEG_INFINITY).total_cmp(&metric(b,&["iptm","Average_i_pTM","i_pTM","ipTM"]).unwrap_or(f64::NEG_INFINITY)),
                    _=>rank(text(a,"status")).cmp(&rank(text(b,"status"))).then_with(||text(a,"name").cmp(text(b,"name"))),
                };
                if self.binder.descending {order.reverse()}else{order}
            });
            let mut open=None;let mut save=None;let mut details=None;let mut context=None;
            egui::ScrollArea::both().id_salt("binder-candidate-table").auto_shrink([false,false]).show(ui,|ui|{
                ui.style_mut().wrap_mode=Some(egui::TextWrapMode::Extend);
                ui.set_min_width(1150.);
                egui::Grid::new("binder-candidate-grid").striped(true).min_col_width(60.).show(ui,|ui|{
                    for (label,key) in [("Status","status"),("Candidate","name"),("pLDDT","pLDDT"),("ipTM","ipTM")] {
                        if ui.button(label).clicked(){if self.binder.result_sort==key{self.binder.descending = !self.binder.descending;}else{self.binder.result_sort=key.into();self.binder.descending=matches!(key,"pLDDT"|"ipTM");}}
                    }
                    for label in ["iPAE","Rosetta ΔG","Length","Actions"]{ui.strong(label);}ui.end_row();
                    for candidate in &candidates {
                            let status=text(candidate,"status");
                            ui.colored_label(if status=="accepted"{GREEN}else if status=="rejected"{RED}else{AMBER},status);
                            if ui.selectable_label(self.binder.selected_candidate==text(candidate,"candidate_id"),text(candidate,"name")).clicked(){
                                self.binder.selected_candidate=text(candidate,"candidate_id").into();
                                if let Some(artifact)=rows(candidate,"structure_artifacts").first(){open=Some((candidate.clone(),artifact.clone()));}
                            }
                            ui.monospace(formatted(metric(candidate,&["plddt","Average_pLDDT","pLDDT"])));
                            ui.monospace(formatted(metric(candidate,&["iptm","Average_i_pTM","i_pTM","ipTM"])));
                            ui.monospace(formatted(metric(candidate,&["interface_pae","Average_i_pAE","i_pAE"])));
                            ui.monospace(formatted(metric(candidate,&["rosetta_dg","Average_dG","dG"])));
                            ui.monospace(text(candidate,"sequence").len().to_string());
                            ui.horizontal(|ui|{
                                if let Some(artifact)=rows(candidate,"structure_artifacts").first()
                                    && ui.add_sized([72.,22.],egui::Button::new("Full target").small()).clicked(){context=Some((candidate.clone(),artifact.clone()));}
                                if ui.add_sized([50.,22.],egui::Button::new("Details").small()).clicked(){details=Some(candidate.clone());}
                                if ui.add_enabled(!text(candidate,"sequence").is_empty(),egui::Button::new("Save to project").small().min_size(Vec2::new(100.,22.))).clicked(){save=Some(candidate.clone());}
                                if ui.add_sized([96.,22.],egui::Button::new("Copy sequence").small()).clicked(){ctx.copy_text(text(candidate,"sequence").into());}
                            });
                        ui.end_row();
                    }
                });
            });
            if let Some((candidate,artifact))=open{let _=self.binder_open_candidate(&candidate,&artifact,ctx);}
            if let Some((candidate,artifact))=context
                && let Some(slot)=self.binder_open_candidate(&candidate,&artifact,ctx){
                    let job=self.binder.draft.results_job.clone();let artifact=text(&artifact,"artifact_id").to_owned();
                    self.binder.context_pending=None;self.binder.context_request=Some((job.clone(),artifact.clone(),slot));
                    self.request("binder.context",json!({"job_id":job,"artifact_id":artifact}),Purpose::Binder(Request::Context(job,artifact,slot)));
            }
            if let Some(candidate)=details{self.text_preview=Some((text(&candidate,"name").into(),serde_json::to_string_pretty(&candidate).unwrap_or_default()));}
            if let Some(mut candidate)=save{
                candidate["save_endpoint"]=json!(self.run_endpoint());
                self.binder.save_name=text(&candidate,"name").into();self.binder.save_candidate=Some(candidate);self.binder.save_error.clear();
                self.request("library.list",json!({"kind":"project","limit":500}),Purpose::Binder(Request::Projects));
            }
        });
    }

    fn binder_align_candidates(&mut self) {
        let job = &self.binder.draft.results_job;
        let candidates: Vec<_> = self
            .views
            .iter()
            .filter(|(_, v)| text(&v.metadata, "binder_job_id") == job)
            .map(|(&id, _)| id)
            .collect();
        let reference_id = if candidates.contains(&self.state.selected_view) {
            self.state.selected_view
        } else if let Some(id) = candidates.first() {
            *id
        } else {
            self.binder.error = "Open at least two candidate structures to compare them.".into();
            return;
        };
        let reference = self.views[&reference_id].molecule.clone();
        let reference_sha = self.views[&reference_id].metadata["sha256"].clone();
        let camera = self.views[&reference_id].camera;
        for id in candidates.into_iter().filter(|&id| id != reference_id) {
            let view = &self.views[&id];
            let parsed =
                scene::Molecule::parse(&view.bytes, &view.molecule.format, &view.molecule.name);
            let result = parsed.and_then(|mut molecule| {
                let pairs = target_pairs(&reference, &molecule)?;
                let receipt =
                    alignment::align_with_residue_pairs(&reference, &mut molecule, &pairs)?;
                Ok((molecule, receipt))
            });
            match result {
                Ok((mut molecule, receipt)) => {
                    molecule.center = reference.center;
                    match scene::Renderer::new(&self.gl, &molecule) {
                        Ok(renderer) => {
                            let view = self.views.get_mut(&id).unwrap();
                            view.molecule = molecule;
                            view.camera = camera;
                            view.metadata["binder_alignment"] = json!(receipt);
                            view.metadata["binder_alignment_source_sha256"] =
                                view.metadata["sha256"].clone();
                            view.metadata["binder_alignment_reference_sha256"] =
                                reference_sha.clone();
                            view.metadata["binder_alignment_center"] =
                                json!([reference.center.0, reference.center.1, reference.center.2]);
                            let old = std::mem::replace(&mut view.renderer, renderer);
                            view.refresh_domain_colors();
                            self.retire_renderer(old);
                        }
                        Err(error) => self.log(format!("Candidate renderer: {error}")),
                    }
                }
                Err(error) => self.log(format!("Candidate alignment: {error}")),
            }
        }
        self.log("Fitted candidate tabs using only their common target chain. Binder poses remain independent; original files are unchanged.");
    }

    pub(super) fn binder_save_dialog(&mut self, ctx: &egui::Context) {
        let Some(candidate) = self.binder.save_candidate.clone() else {
            if self
                .binder
                .draft
                .save_intent
                .as_ref()
                .is_some_and(binder_state::Intent::unresolved)
            {
                egui::Window::new("Recover binder save").default_width(480.).show(ctx,|ui|{
                    ui.label("A saved candidate publication needs reconciliation. Recovery uses its original project, candidate and request key.");
                    if let Some(intent)=&self.binder.draft.save_intent {ui.monospace(text(&intent.request,"project_ref"));if !intent.error.is_empty(){ui.colored_label(RED,&intent.error);}}
                    if ui.button("Recover exact save").clicked(){self.binder_retry_save();}
                });
            }
            return;
        };
        let mut open = true;
        egui::Window::new("Save binder to project").open(&mut open).default_width(480.).show(ctx,|ui|{
            ui.strong(text(&candidate,"name"));
            ui.label(format!("{} amino acids · {}",text(&candidate,"sequence").len(),text(&candidate,"status")));
            ui.horizontal(|ui|{ui.label("Alt name");ui.text_edit_singleline(&mut self.binder.save_name);});
            let selected=self.binder.projects.iter().find(|p|text(p,"ref")==self.binder.save_project);
            egui::ComboBox::from_id_salt("binder-save-project").selected_text(selected.map(|p|text(p,"name")).unwrap_or("Select project")).show_ui(ui,|ui|{
                for project in &self.binder.projects {ui.selectable_value(&mut self.binder.save_project,text(project,"ref").into(),text(project,"name"));}
            });
            ui.weak("Creates a standalone protein with the exact generated sequence and retained target, hotspot, settings, and run provenance.");
            let unresolved=self.binder.draft.save_intent.as_ref().is_some_and(binder_state::Intent::unresolved);
            let current_head=text(&candidate,"save_endpoint")==self.run_endpoint();
            if ui.add_enabled(!unresolved && current_head && !self.binder.save_project.is_empty(),egui::Button::new(RichText::new("Save binder").color(GREEN))).clicked()
                && let Some(project)=self.binder.projects.iter().find(|p|text(p,"ref")==self.binder.save_project) {
                let id=uid();
                let params=json!({"request_key":uid(),"job_id":candidate["provenance"]["job_id"],"candidate_id":candidate["candidate_id"],"project_ref":project["ref"],"expected_sha256":project["sha256"],"alt_name":self.binder.save_name});
                self.binder.draft.save_intent=Some(binder_state::Intent{id,endpoint:self.run_endpoint(),request:params,..Default::default()});
                self.binder_retry_save();
            }
            if !self.binder.save_error.is_empty(){ui.colored_label(RED,&self.binder.save_error);}
            if self.binder.draft.save_intent.as_ref().is_some_and(|i|!i.error.is_empty())
                && ui.button("Recover exact save").clicked(){self.binder_retry_save();}
        });
        if !open {
            self.binder.save_candidate = None;
        }
    }

    fn binder_retry_save(&mut self) {
        let Some(intent) = self.binder.draft.save_intent.clone() else {
            return;
        };
        if intent.endpoint != self.run_endpoint() {
            self.binder.save_error =
                "Reconnect to the original head to recover this publication.".into();
            return;
        }
        self.persist();
        if !self.save_error.is_empty() {
            self.binder.save_error = self.save_error.clone();
            return;
        }
        if intent.operation.is_empty() {
            if let Some(operation) = self.request(
                "binder.save",
                intent.request,
                Purpose::Binder(Request::Save(intent.id)),
            ) {
                let intent = self.binder.draft.save_intent.as_mut().unwrap();
                intent.operation = operation;
                intent.error.clear();
                self.library_invalidate_lists();
            } else {
                let intent = self.binder.draft.save_intent.as_mut().unwrap();
                intent.error =
                    "Could not enqueue the saved publication. Recover this exact request.".into();
                intent.uncertain = true;
            }
        } else {
            self.retry(
                &intent.operation,
                Purpose::Binder(Request::Save(intent.id)),
                "binder.save".into(),
            );
        }
        self.persist();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unavailable_and_nonfinite_metrics_remain_unavailable() {
        let candidate = json!({"metrics":{"pLDDT":"0.92","i_pTM":"nan","dG":null}});
        assert_eq!(
            metric(&candidate, &["plddt", "Average_pLDDT", "pLDDT"]),
            Some(0.92)
        );
        assert_eq!(metric(&candidate, &["i_pTM"]), None);
        assert_eq!(metric(&candidate, &["dG"]), None);
        assert_eq!(formatted(None), "—");
    }
    #[test]
    fn candidate_comparison_never_fits_the_binder_or_other_chains() {
        let molecule = scene::Molecule::reference();
        let pairs = target_pairs(&molecule, &molecule).unwrap();
        assert!(pairs.len() > 100);
        assert!(pairs.iter().all(|(a, b)| a.chain == "A" && b.chain == "A"));
        assert!(pairs.len() < molecule.residues.len());
    }
}
