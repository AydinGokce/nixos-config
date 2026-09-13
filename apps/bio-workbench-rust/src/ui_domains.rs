//! Per-tab protein domains and exact plasmid-annotation import.
use super::*;
use domain_state::{Layer, Span};

#[derive(Clone, Debug, PartialEq)]
pub(super) struct Request {
    token: String,
    endpoint: String,
    slot: usize,
    sha256: String,
    reference: String,
}
impl Request {
    fn matches(&self, current: Option<&Self>, endpoint: &str, sha: Option<&str>) -> bool {
        current == Some(self) && self.endpoint == endpoint && sha == Some(self.sha256.as_str())
    }
}
#[derive(Default)]
pub(super) struct Import {
    pub open: bool,
    request: Option<Request>,
    slot: usize,
    sha256: String,
    endpoint: String,
    reference: String,
    response: Value,
    selected: BTreeSet<String>,
    chain: usize,
    filter: String,
    error: String,
    proteins: Vec<Value>,
}

fn chain_name(molecule: &scene::Molecule, index: usize) -> String {
    molecule
        .chains
        .get(index)
        .map(|chain| {
            format!(
                "{}{} · {} aa",
                if chain.id.is_empty() {
                    "(blank)"
                } else {
                    &chain.id
                },
                if chain.label_id.is_empty() || chain.label_id == chain.id {
                    String::new()
                } else {
                    format!(" [{}]", chain.label_id)
                },
                domain_state::protein_chain(molecule, index).len()
            )
        })
        .unwrap_or_else(|| "Choose protein chain".into())
}
fn chains(ui: &mut egui::Ui, molecule: &scene::Molecule, chain: &mut usize, id: &str) {
    egui::ComboBox::from_id_salt(id)
        .selected_text(chain_name(molecule, *chain))
        .width(200.)
        .show_ui(ui, |ui| {
            for (index, item) in molecule
                .chains
                .iter()
                .enumerate()
                .filter(|(_, c)| c.kind == scene::MoleculeKind::Protein)
            {
                let _ = item;
                ui.selectable_value(chain, index, chain_name(molecule, index));
            }
        });
}

fn feature_mapping(
    feature: &Value,
    mapping: &[Option<usize>],
) -> Result<(Vec<Span>, usize, usize), String> {
    if feature["source"] == "autodetected"
        || feature["is_orf"] == true
        || text(feature, "id").starts_with("orf:")
    {
        return Err("Autodetected CDS/ORF suggestions are not domain annotations.".into());
    }
    let ranges: Vec<Span> = serde_json::from_value(feature["segments"].clone())
        .map_err(|_| "Invalid protein annotation coordinates.")?;
    let mut original = BTreeSet::new();
    let mut indices = Vec::new();
    for span in &ranges {
        if span.start >= span.end || span.end > mapping.len() {
            return Err("Annotation coordinates exceed the verified protein sequence.".into());
        }
        for (index, residue) in mapping.iter().enumerate().take(span.end).skip(span.start) {
            original.insert(index);
            if let Some(residue) = residue {
                indices.push(*residue);
            }
        }
    }
    indices.sort_unstable();
    indices.dedup();
    if indices.is_empty() {
        return Err("This annotation has no resolved residues in the selected chain.".into());
    }
    let count = indices.len();
    let total = original.len();
    Ok((domain_state::spans(indices), count, total))
}

fn import_id(response: &Value, feature: &Value, chain: usize) -> String {
    domain_state::digest(
        &serde_json::to_vec(&json!([
            response["ref"],
            response["sequence_sha256"],
            response["source"],
            feature["id"],
            chain
        ]))
        .unwrap(),
    )
}

fn linked_plasmid_proteins(metadata: &Value, endpoint: &str) -> Vec<Value> {
    if text(metadata, "library_endpoint") != endpoint {
        return Vec::new();
    }
    rows(metadata, "library_proteins")
        .iter()
        .filter(|protein| {
            text(protein, "derivation_kind") == "derived"
                && text(&protein["parent"], "molecular_form") == "plasmid"
                && text(protein, "ref").starts_with("construct:")
                && !text(&protein["parent"], "ref").is_empty()
        })
        .cloned()
        .collect()
}

pub(super) fn imported_layer(
    response: &Value,
    feature: &Value,
    mapping: &[Option<usize>],
    chain: usize,
    color: [u8; 3],
) -> Result<(Layer, usize, usize), String> {
    let (spans, count, total) = feature_mapping(feature, mapping)?;
    let id = import_id(response, feature, chain);
    let color = text(feature, "color")
        .strip_prefix('#')
        .filter(|s| s.len() == 6)
        .and_then(|s| u32::from_str_radix(s, 16).ok())
        .map(|v| [(v >> 16) as u8, (v >> 8) as u8, v as u8])
        .unwrap_or(color);
    Ok((
        Layer {
            id,
            label: text(feature, "label").to_owned(),
            color,
            enabled: true,
            spans,
            origin: json!({"kind":"plasmid_annotation","protein_ref":response["ref"],"protein_record_sha256":response["sha256"],
            "protein_sequence_sha256":response["sequence_sha256"],"source":response["source"],"feature":feature,
            "chain_index":chain,"mapped_structure_residues":count,"annotated_protein_residues":total}),
        },
        count,
        total,
    ))
}

impl Workbench {
    pub(super) fn domains_panel(&mut self, ui: &mut egui::Ui) {
        let slot = self.state.selected_view;
        let endpoint = self.run_endpoint();
        let Some(view) = self.views.get_mut(&slot).filter(|v| {
            v.molecule
                .chains
                .iter()
                .any(|c| c.kind == scene::MoleculeKind::Protein)
        }) else {
            return;
        };
        view.renderer.consume_pick(
            &view.molecule,
            &mut view.selected,
            &mut view.hotspots,
            &mut view.selection_range,
        );
        let can_import = !linked_plasmid_proteins(&view.metadata, &endpoint).is_empty();
        let mut import = false;
        let mut changed = false;
        egui::CollapsingHeader::new("Protein domains")
            .id_salt(("domains", slot))
            .default_open(true)
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    if ui
                        .add_enabled(!view.domains.undo.is_empty(), egui::Button::new("Undo"))
                        .clicked()
                    {
                        view.domains.undo();
                        changed = true;
                    }
                    if ui
                        .add_enabled(!view.domains.redo.is_empty(), egui::Button::new("Redo"))
                        .clicked()
                    {
                        view.domains.redo();
                        changed = true;
                    }
                    if ui.button("Add range…").clicked() {
                        view.domains.open = true;
                        view.domains.editing = None;
                        view.domains.label.clear();
                        view.domains.ranges.clear();
                        view.domains.color = domain_state::palette(view.domains.data.layers.len());
                    }
                });
                if !view.selection_range.residues.is_empty() {
                    ui.horizontal_wrapped(|ui| {
                        ui.weak(format!(
                            "{} selected residues",
                            view.selection_range.residues.len()
                        ));
                        if ui.button("Create annotation…").clicked()
                            && let Some((chain, indices)) = view
                                .molecule
                                .chains
                                .iter()
                                .enumerate()
                                .find_map(|(chain, _)| {
                                    let residues =
                                        domain_state::protein_chain(&view.molecule, chain);
                                    let indices: Vec<_> = residues
                                        .iter()
                                        .enumerate()
                                        .filter_map(|(position, &index)| {
                                            view.selection_range
                                                .residues
                                                .contains(&view.molecule.residues[index].key)
                                                .then_some(position)
                                        })
                                        .collect();
                                    (indices.len() == view.selection_range.residues.len())
                                        .then_some((chain, indices))
                                })
                        {
                            view.domains.chain = chain;
                            view.domains.ranges =
                                domain_state::ranges_label(&domain_state::spans(indices));
                            view.domains.label.clear();
                            view.domains.color =
                                domain_state::palette(view.domains.data.layers.len());
                            view.domains.editing = None;
                            view.domains.open = true;
                        }
                        if ui.small_button("Clear selection").clicked() {
                            view.selection_range = scene::SelectionRange::default();
                            view.selected = None;
                        }
                    });
                }
                let mut next = view.domains.data.clone();
                let mut remove = None;
                for layer in &mut next.layers {
                    ui.push_id(&layer.id, |ui| {
                        ui.horizontal(|ui| {
                            if ui.checkbox(&mut layer.enabled, "").changed() {
                                changed = true;
                            }
                            let color =
                                Color32::from_rgb(layer.color[0], layer.color[1], layer.color[2]);
                            if ui
                                .add(
                                    egui::Button::new(RichText::new("■").color(color))
                                        .min_size(Vec2::splat(22.)),
                                )
                                .on_hover_text("Edit domain color and name")
                                .clicked()
                            {
                                view.domains.open = true;
                                view.domains.editing = Some(layer.id.clone());
                                view.domains.label = layer.label.clone();
                                view.domains.color = layer.color;
                                view.domains.chain =
                                    layer.origin["chain_index"].as_u64().unwrap_or(0) as usize;
                                view.domains.ranges = serde_json::from_value::<Vec<Span>>(
                                    layer.origin["protein_segments"].clone(),
                                )
                                .map(|s| domain_state::ranges_label(&s))
                                .unwrap_or_default();
                            }
                            ui.add(egui::Label::new(&layer.label).truncate())
                                .on_hover_text(format!(
                                    "{} residues\n{}",
                                    layer.spans.iter().map(|s| s.end - s.start).sum::<usize>(),
                                    if layer.origin["kind"] == "manual" {
                                        "Manual protein range".to_owned()
                                    } else {
                                        format!(
                                            "{} · {}",
                                            text(&layer.origin, "protein_ref"),
                                            text(&layer.origin["feature"], "kind")
                                        )
                                    }
                                ));
                            if ui
                                .small_button("×")
                                .on_hover_text("Remove domain color")
                                .clicked()
                            {
                                remove = Some(layer.id.clone());
                                changed = true;
                            }
                        });
                    });
                }
                if let Some(id) = remove {
                    next.layers.retain(|l| l.id != id);
                }
                if next != view.domains.data
                    && let Err(error) = view.domains.replace(next, &view.molecule)
                {
                    view.domains.error = error;
                }
                if view.domains.data.layers.is_empty() {
                    ui.weak("Color named protein regions.");
                }
                if can_import && ui.button("Import plasmid annotations…").clicked() {
                    import = true;
                }
                if view.domains.open {
                    ui.separator();
                    let imported = view
                        .domains
                        .editing
                        .as_ref()
                        .and_then(|id| view.domains.data.layers.iter().find(|l| &l.id == id))
                        .is_some_and(|l| l.origin["kind"] != "manual");
                    ui.add(
                        egui::TextEdit::singleline(&mut view.domains.label)
                            .hint_text("Domain name")
                            .desired_width(f32::INFINITY),
                    );
                    if !imported {
                        chains(
                            ui,
                            &view.molecule,
                            &mut view.domains.chain,
                            "manual-domain-chain",
                        );
                        ui.add(
                            egui::TextEdit::singleline(&mut view.domains.ranges)
                                .hint_text("1-150, 320-400")
                                .desired_width(f32::INFINITY),
                        );
                        ui.weak("Protein positions in this chain, starting at 1.");
                    }
                    ui.horizontal(|ui| {
                        ui.label("Color");
                        ui.color_edit_button_srgb(&mut view.domains.color);
                    });
                    ui.horizontal(|ui| {
                        if ui
                            .button(if view.domains.editing.is_some() {
                                "Save domain"
                            } else {
                                "Add domain"
                            })
                            .clicked()
                        {
                            let result = (|| -> Result<(), String> {
                                let mut data = view.domains.data.clone();
                                let mut layer = if imported {
                                    data.layers
                                        .iter()
                                        .find(|l| Some(&l.id) == view.domains.editing.as_ref())
                                        .unwrap()
                                        .clone()
                                } else {
                                    domain_state::manual_layer(
                                        &view.molecule,
                                        view.domains.chain,
                                        &view.domains.ranges,
                                        &view.domains.label,
                                        view.domains.color,
                                    )?
                                };
                                layer.label = view.domains.label.trim().into();
                                layer.color = view.domains.color;
                                if let Some(existing) = view
                                    .domains
                                    .editing
                                    .as_ref()
                                    .and_then(|id| data.layers.iter_mut().find(|l| &l.id == id))
                                {
                                    layer.id = existing.id.clone();
                                    layer.enabled = existing.enabled;
                                    *existing = layer;
                                } else {
                                    data.layers.push(layer);
                                }
                                view.domains.replace(data, &view.molecule)?;
                                Ok(())
                            })();
                            match result {
                                Ok(()) => {
                                    changed = true;
                                    view.domains.open = false;
                                }
                                Err(error) => view.domains.error = error,
                            }
                        }
                        if ui.button("Cancel").clicked() {
                            view.domains.open = false;
                            view.domains.error.clear();
                        }
                    });
                }
                if !view.domains.error.is_empty() {
                    ui.colored_label(RED, &view.domains.error);
                }
                if !view.domains.data.layers.is_empty() {
                    ui.weak(
                        "Later layers color overlapping residues. Saved with this structure tab.",
                    );
                }
            });
        if changed {
            view.refresh_domain_colors();
            self.persist();
        }
        if import {
            self.domains_open(None);
        }
    }

    pub(super) fn domains_open(&mut self, reference: Option<String>) {
        let slot = self.state.selected_view;
        let Some(view) = self.views.get(&slot).filter(|v| {
            !self.view_loading.contains_key(&slot)
                && v.molecule
                    .chains
                    .iter()
                    .any(|c| c.kind == scene::MoleculeKind::Protein)
        }) else {
            self.log("Open a protein structure before importing its domains.");
            return;
        };
        let proteins = linked_plasmid_proteins(&view.metadata, &self.run_endpoint());
        let reference = reference
            .filter(|r| proteins.iter().any(|p| text(p, "ref") == r))
            .or_else(|| proteins.first().map(|p| text(p, "ref").to_owned()));
        let Some(reference) = reference else {
            self.log("This structure has no associated plasmid-derived protein.");
            return;
        };
        self.domain_import = Import {
            open: true,
            slot,
            sha256: text(&view.metadata, "sha256").into(),
            endpoint: self.run_endpoint(),
            reference,
            proteins,
            chain: view.domains.chain,
            ..Default::default()
        };
        self.sidebar_tab = 0;
        if !self.domain_import.reference.is_empty() {
            self.domains_fetch();
        }
    }
    fn domains_fetch(&mut self) {
        let request = Request {
            token: uid(),
            endpoint: self.domain_import.endpoint.clone(),
            slot: self.domain_import.slot,
            sha256: self.domain_import.sha256.clone(),
            reference: self.domain_import.reference.trim().into(),
        };
        self.domain_import.response = Value::Null;
        self.domain_import.selected.clear();
        self.domain_import.error.clear();
        if request.reference.is_empty() {
            self.domain_import.error =
                "Choose a library protein or enter its construct reference.".into();
            return;
        }
        self.domain_import.request = Some(request.clone());
        if self
            .request(
                "library.protein_domains",
                json!({"ref":request.reference}),
                Purpose::ProteinDomains(request),
            )
            .is_none()
        {
            self.domain_import.error =
                "Could not request annotations; check the head connection.".into();
        }
    }
    fn domains_current(&self, request: &Request) -> bool {
        request.matches(
            self.domain_import.request.as_ref(),
            &self.run_endpoint(),
            self.views
                .get(&request.slot)
                .map(|v| text(&v.metadata, "sha256")),
        ) && !self.view_loading.contains_key(&request.slot)
    }
    pub(super) fn domains_received(&mut self, request: Request, response: Value) {
        if !self.domains_current(&request) {
            return;
        }
        let sequence = text(&response, "sequence");
        let reference = text(&response, "ref");
        if response["schema"] != 1
            || text(&response, "coordinate_system") != "protein_0based_half_open"
            || domain_state::digest(sequence.as_bytes()) != text(&response, "sequence_sha256")
            || !reference.starts_with("construct:")
            || reference
                .rsplit_once('@')
                .is_none_or(|(_, n)| n.parse::<u64>().is_err())
            || (request.reference.contains('@') && request.reference != reference)
            || self
                .domain_import
                .proteins
                .iter()
                .find(|p| text(p, "ref") == reference)
                .is_none_or(|p| {
                    p["sha256"] != response["sha256"]
                        || p["sequence_sha256"] != response["sequence_sha256"]
                        || p["parent"]["ref"] != response["source"]["ref"]
                })
        {
            self.domain_import.error =
                "The annotation response failed its protein identity checks.".into();
            return;
        }
        if let Some(view) = self.views.get(&request.slot) {
            let matches: Vec<_> = view
                .molecule
                .chains
                .iter()
                .enumerate()
                .filter(|(_, c)| c.kind == scene::MoleculeKind::Protein)
                .filter_map(|(index, _)| {
                    domain_state::sequence_mapping(sequence, &view.molecule, index)
                        .ok()
                        .map(|_| index)
                })
                .collect();
            if matches.len() == 1 {
                self.domain_import.chain = matches[0];
            }
        }
        self.domain_import.response = response;
    }
    pub(super) fn domains_failed(&mut self, purpose: &Purpose, error: &str) {
        if let Purpose::ProteinDomains(request) = purpose
            && self.domains_current(request)
        {
            self.domain_import.error = error.into();
        }
    }
    pub(super) fn domains_dialog(&mut self, ctx: &egui::Context) {
        if !self.domain_import.open {
            return;
        }
        let mut panel = std::mem::take(&mut self.domain_import);
        if panel.endpoint != self.run_endpoint()
            || self
                .views
                .get(&panel.slot)
                .is_none_or(|v| text(&v.metadata, "sha256") != panel.sha256)
            || self.view_loading.contains_key(&panel.slot)
        {
            self.log("The domain import was closed because its structure or head changed.");
            return;
        }
        let view = &self.views[&panel.slot];
        let mut open = true;
        let mut fetch = false;
        let mut apply = false;
        egui::Window::new("Import plasmid annotations").open(&mut open).default_width(650.).default_height(500.).resizable(true).show(ctx,|ui| {
            ui.label(format!("Structure: {}",ui_views::display_name(&view.metadata)));
            ui.horizontal(|ui| {
                ui.label("Protein");
                if panel.proteins.len()>1 {
                    egui::ComboBox::from_id_salt("annotation-source-protein").selected_text(&panel.reference).show_ui(ui,|ui| {
                        for protein in &panel.proteins {let reference=text(protein,"ref");if ui.selectable_value(&mut panel.reference,reference.into(),reference).changed(){fetch=true;}}
                    });
                }else{ui.label(&panel.reference);}
                if ui.add_enabled(self.connected,egui::Button::new("Refresh")).clicked(){fetch=true;}
            });
            ui.weak("Curated plasmid annotations mapped through this protein's saved translation. No autodetected CDSs or ORFs.");
            ui.weak("Labels describe the original plasmid features; sequence or frame edits may change their biological meaning.");
            if panel.request.as_ref().is_some_and(|r|self.busy(&Purpose::ProteinDomains(r.clone()))){ui.spinner();}
            if !panel.error.is_empty(){ui.colored_label(RED,&panel.error);}
            if !panel.response.is_null() {
                ui.label(text(&panel.response,"ref"));
                ui.weak(format!("Parent {}",text(&panel.response["source"],"ref")));
                let previous_chain=panel.chain;
                chains(ui,&view.molecule,&mut panel.chain,"import-domain-chain");
                let mapping=domain_state::sequence_mapping(text(&panel.response,"sequence"),&view.molecule,panel.chain);
                if panel.chain!=previous_chain {
                    panel.selected.retain(|id| rows(&panel.response,"features").iter().any(|feature|
                        text(feature,"id")==id && mapping.as_ref().is_ok_and(|m|feature_mapping(feature,m).is_ok())));
                    panel.error.clear();
                }
                if let Err(error)=&mapping{ui.colored_label(AMBER,error);}
                ui.add(egui::TextEdit::singleline(&mut panel.filter).hint_text("Filter annotations (e.g. TadA)").desired_width(f32::INFINITY));
                let filter=panel.filter.to_lowercase();
                let features:Vec<_>=rows(&panel.response,"features").iter().filter(|feature|
                    !view.domains.data.layers.iter().any(|layer|layer.id==import_id(&panel.response,feature,panel.chain))
                ).collect();
                panel.selected.retain(|id|features.iter().any(|f|text(f,"id")==id));
                ui.horizontal(|ui| {
                    if ui.add_enabled(mapping.is_ok(),egui::Button::new("Select visible")).clicked(){for feature in &features{if text(feature,"label").to_lowercase().contains(&filter) && mapping.as_ref().is_ok_and(|m|feature_mapping(feature,m).is_ok()){panel.selected.insert(text(feature,"id").into());}}}
                    if ui.button("Clear").clicked(){panel.selected.clear();}
                });
                egui::ScrollArea::vertical().id_salt("domain-import-features").max_height((ui.available_height()-70.).max(160.)).show(ui,|ui| {
                    for feature in features.iter().filter(|f|text(f,"label").to_lowercase().contains(&filter)) {
                        let id=text(feature,"id");
                        let mapped=mapping.as_ref().ok().and_then(|m|feature_mapping(feature,m).ok());
                        let mut selected=panel.selected.contains(id);
                        ui.add_enabled_ui(mapped.is_some(),|ui| {
                            if ui.checkbox(&mut selected,format!("{} · {}",text(feature,"label"),text(feature,"kind"))).changed(){if selected{panel.selected.insert(id.into());}else{panel.selected.remove(id);}}
                        });
                        if let Some((_,count,total))=mapped {ui.small(format!("{count}/{total} protein residues present in this chain"));}
                        else{ui.weak("No reliably mapped residues in this chain.");}
                        for issue in rows(feature,"issues"){ui.colored_label(AMBER,text(issue,"message"));}
                    }
                    if features.is_empty(){ui.weak(if rows(&panel.response,"features").is_empty(){"No current plasmid annotations map to this protein."}else{"All available annotations have been imported into this chain."});}
                    for issue in rows(&panel.response,"issues"){ui.colored_label(AMBER,text(issue,"message"));}
                    ui.collapsing(format!("Excluded annotations ({})",rows(&panel.response,"excluded").len()),|ui| {
                        for feature in rows(&panel.response,"excluded"){ui.weak(format!("{} · {}",text(feature,"label"),text(feature,"reason")));}
                    });
                });
                if panel.response["complete"]==false{ui.colored_label(AMBER,"The annotation list is incomplete; only the displayed verified entries can be imported.");}
                if ui.add_enabled(mapping.is_ok()&&!panel.selected.is_empty(),egui::Button::new(RichText::new("Import selected domains").color(GREEN)).min_size(Vec2::new(200.,28.))).clicked(){apply=true;}
            }
        });
        panel.open = open;
        self.domain_import = panel;
        if fetch {
            self.domains_fetch();
        }
        if apply {
            self.domains_apply();
        }
    }
    fn domains_apply(&mut self) {
        let panel = &self.domain_import;
        if panel.endpoint != self.run_endpoint() {
            return;
        }
        let Some(view) = self
            .views
            .get_mut(&panel.slot)
            .filter(|v| text(&v.metadata, "sha256") == panel.sha256)
        else {
            return;
        };
        let result = (|| -> Result<usize, String> {
            let mapping = domain_state::sequence_mapping(
                text(&panel.response, "sequence"),
                &view.molecule,
                panel.chain,
            )?;
            let mut data = view.domains.data.clone();
            let mut count = 0;
            let mut imported = Vec::new();
            for (index, feature) in rows(&panel.response, "features")
                .iter()
                .enumerate()
                .filter(|(_, f)| panel.selected.contains(text(f, "id")))
            {
                let (layer, _, _) = imported_layer(
                    &panel.response,
                    feature,
                    &mapping,
                    panel.chain,
                    domain_state::palette(index),
                )?;
                if !data.layers.iter().any(|l| l.id == layer.id) {
                    imported.push(layer);
                    count += 1;
                }
            }
            // Draw whole-product features first so narrower domains remain visible.
            imported.sort_by_key(|layer| {
                std::cmp::Reverse(layer.spans.iter().map(|s| s.end - s.start).sum::<usize>())
            });
            data.layers.extend(imported);
            view.domains.replace(data, &view.molecule)?;
            view.refresh_domain_colors();
            Ok(count)
        })();
        match result {
            Ok(count) => {
                self.domain_import.open = false;
                self.log(format!(
                    "Imported {count} plasmid domain annotations into this structure tab."
                ));
                self.persist();
            }
            Err(error) => self.domain_import.error = error,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stale_import_replies_cannot_retarget_another_tab_structure_or_head() {
        let request = Request {
            token: "one".into(),
            endpoint: "head".into(),
            slot: 3,
            sha256: "target".into(),
            reference: "construct:p@2".into(),
        };
        assert!(request.matches(Some(&request), "head", Some("target")));
        let mut newer = request.clone();
        newer.token = "two".into();
        assert!(!request.matches(Some(&newer), "head", Some("target")));
        newer = request.clone();
        newer.slot = 4;
        assert!(!request.matches(Some(&newer), "head", Some("target")));
        assert!(!request.matches(Some(&request), "other-head", Some("target")));
        assert!(!request.matches(Some(&request), "head", Some("other-bytes")));
        assert!(!request.matches(Some(&request), "head", None));
    }
    #[test]
    fn imported_domains_use_only_explicit_mapped_residues_and_keep_source_provenance() {
        let response = json!({"ref":"construct:protein@2","sha256":"record","sequence_sha256":"sequence","source":{"ref":"construct:plasmid@3","annotations_receipt":{"sha256":"annotation-bytes"}}});
        let feature = json!({"id":"source:9","label":"TadA","kind":"misc_feature","segments":[{"start":1,"end":4}],"color":"#123456"});
        let (layer, count, total) = imported_layer(
            &response,
            &feature,
            &[Some(90), Some(91), None, Some(93)],
            0,
            domain_state::palette(0),
        )
        .unwrap();
        assert_eq!((count, total), (2, 3));
        assert_eq!(
            layer.spans,
            vec![Span { start: 91, end: 92 }, Span { start: 93, end: 94 }]
        );
        assert_eq!(layer.color, [0x12, 0x34, 0x56]);
        assert_eq!(
            layer.origin["source"]["annotations_receipt"]["sha256"],
            "annotation-bytes"
        );
        let mut inferred = feature.clone();
        inferred["is_orf"] = json!(true);
        assert!(imported_layer(&response, &inferred, &[Some(0); 4], 0, [0; 3]).is_err());
        assert!(imported_layer(&response, &feature, &[Some(0)], 0, [0; 3]).is_err());
    }
}
