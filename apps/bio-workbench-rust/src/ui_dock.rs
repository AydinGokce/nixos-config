use super::*;
use egui_dock::{DockArea, DockState, LeafNode, Node, NodeIndex, Split, SurfaceIndex, TabViewer};

// Persist only the editor layout. DockState also contains transient screen rectangles,
// which should not become session state or be trusted when reading an older session.
enum Layout {
    Tabs {
        tabs: Vec<usize>,
        active: usize,
    },
    Split {
        horizontal: bool,
        fraction: f32,
        first: Box<Layout>,
        second: Box<Layout>,
    },
}

impl Layout {
    fn first_tab(&self) -> usize {
        match self {
            Self::Tabs { tabs, .. } => tabs[0],
            Self::Split { first, .. } => first.first_tab(),
        }
    }
}

fn read_layout(
    value: &Value,
    valid: &BTreeSet<usize>,
    seen: &mut BTreeSet<usize>,
    depth: usize,
    remaining: &mut usize,
) -> Result<Option<Layout>, ()> {
    // A corrupt file must not create a huge sparse egui_dock tree. This limits
    // malformed layout topology, not the number of open structure tabs.
    if depth > 16 || *remaining == 0 {
        return Err(());
    }
    *remaining -= 1;
    if value.is_null() {
        return Ok(None);
    }
    match text(value, "type") {
        "tabs" => {
            let source = value["tabs"].as_array().ok_or(())?;
            let mut tabs = Vec::new();
            for item in source {
                let slot = usize::try_from(item.as_u64().ok_or(())?).map_err(|_| ())?;
                if valid.contains(&slot) && seen.insert(slot) {
                    tabs.push(slot);
                }
            }
            if tabs.is_empty() {
                return Ok(None);
            }
            let active = value["active"]
                .as_u64()
                .and_then(|n| usize::try_from(n).ok())
                .filter(|slot| tabs.contains(slot))
                .unwrap_or(tabs[0]);
            Ok(Some(Layout::Tabs { tabs, active }))
        }
        "split" => {
            let horizontal = match text(value, "axis") {
                "horizontal" => true,
                "vertical" => false,
                _ => return Err(()),
            };
            let fraction = value["fraction"].as_f64().ok_or(())? as f32;
            if !fraction.is_finite() || !(0.0..=1.0).contains(&fraction) {
                return Err(());
            }
            let first = read_layout(&value["first"], valid, seen, depth + 1, remaining)?;
            let second = read_layout(&value["second"], valid, seen, depth + 1, remaining)?;
            Ok(match (first, second) {
                (Some(first), Some(second)) => Some(Layout::Split {
                    horizontal,
                    fraction: fraction.clamp(0.05, 0.95),
                    first: Box::new(first),
                    second: Box::new(second),
                }),
                (Some(child), None) | (None, Some(child)) => Some(child),
                (None, None) => None,
            })
        }
        _ => Err(()),
    }
}

fn install_layout(tree: &mut egui_dock::Tree<usize>, node: NodeIndex, layout: &Layout) {
    match layout {
        Layout::Tabs { tabs, active } => {
            let mut leaf = LeafNode::new(tabs.clone());
            leaf.set_active_tab(tabs.iter().position(|slot| slot == active).unwrap_or(0));
            tree[node] = Node::Leaf(leaf);
        }
        Layout::Split {
            horizontal,
            fraction,
            first,
            second,
        } => {
            let [first_node, second_node] = tree.split(
                node,
                if *horizontal {
                    Split::Right
                } else {
                    Split::Below
                },
                *fraction,
                Node::leaf(second.first_tab()),
            );
            install_layout(tree, first_node, first);
            install_layout(tree, second_node, second);
        }
    }
}

pub(super) fn restore_layout(saved: &Value, refs: &[Value], selected: usize) -> DockState<usize> {
    let valid: BTreeSet<usize> = refs
        .iter()
        .filter_map(|reference| reference["slot"].as_u64())
        .filter_map(|slot| usize::try_from(slot).ok())
        .collect();
    let mut seen = BTreeSet::new();
    let parsed = if saved["version"].as_u64() == Some(1) {
        read_layout(&saved["root"], &valid, &mut seen, 0, &mut 4096)
            .ok()
            .flatten()
    } else {
        None
    };
    let mut dock = if let Some(layout) = parsed {
        let mut dock = DockState::new(vec![layout.first_tab()]);
        install_layout(dock.main_surface_mut(), NodeIndex::root(), &layout);
        dock
    } else {
        DockState::new(Vec::new())
    };
    let active_tabs: Vec<usize> = dock
        .iter_leaves()
        .filter_map(|(_, leaf)| leaf.tabs.get(leaf.active.0).copied())
        .collect();
    for slot in valid {
        if dock.find_tab(&slot).is_none() {
            dock.push_to_first_leaf(slot);
        }
    }
    for slot in active_tabs {
        if let Some(location) = dock.find_tab(&slot) {
            dock.set_active_tab(location);
        }
    }
    focus_tab(&mut dock, selected);
    dock
}

fn save_node(tree: &egui_dock::Tree<usize>, index: NodeIndex) -> Value {
    if index.0 >= tree.len() {
        return Value::Null;
    }
    match &tree[index] {
        Node::Empty => Value::Null,
        Node::Leaf(leaf) => json!({
            "type":"tabs", "tabs":leaf.tabs,
            "active":leaf.tabs.get(leaf.active.0).or(leaf.tabs.first())
        }),
        Node::Horizontal(split) | Node::Vertical(split) => json!({
            "type":"split", "axis":if matches!(&tree[index],Node::Horizontal(_)){"horizontal"}else{"vertical"},
            "fraction":split.fraction,
            "first":save_node(tree,index.left()), "second":save_node(tree,index.right())
        }),
    }
}

pub(super) fn save_layout(dock: &DockState<usize>) -> Value {
    json!({"version":1,"root":save_node(dock.main_surface(),NodeIndex::root())})
}

fn focus_tab(dock: &mut DockState<usize>, slot: usize) -> bool {
    if let Some(location @ (surface, node, _)) = dock.find_tab(&slot) {
        dock.set_active_tab(location);
        dock.set_focused_node_and_surface((surface, node));
        true
    } else {
        false
    }
}

pub(super) fn move_to_split(dock: &mut DockState<usize>, slot: usize, direction: Split) -> bool {
    let Some(location @ (surface, node, _)) = dock.find_tab(&slot) else {
        return false;
    };
    if dock[surface][node].tabs_count() < 2 {
        return false;
    }
    dock.move_tab(
        location,
        (surface, node, egui_dock::TabInsert::Split(direction)),
    );
    focus_tab(dock, slot);
    true
}

fn settle_focus(
    dock: &mut DockState<usize>,
    selected_before: usize,
    requested: Option<usize>,
    closed: &BTreeSet<usize>,
) -> Option<usize> {
    // The dock's title interaction focuses even an inactive tab's close target.
    // Closing that tab should preserve the editor the user was working in.
    let keep_active =
        (!closed.is_empty() && !closed.contains(&selected_before)).then_some(selected_before);
    if let Some(slot) = requested.or(keep_active) {
        focus_tab(dock, slot);
    }
    dock.find_active_focused().map(|(_, slot)| *slot)
}

fn dock_style(ui: &egui::Ui) -> egui_dock::Style {
    let mut style = egui_dock::Style::from_egui(ui.style());
    style.dock_area_padding = Some(egui::Margin::ZERO);
    style.main_surface_border_stroke = egui::Stroke::NONE;
    style.main_surface_border_rounding = egui::CornerRadius::ZERO;
    style.tab_bar.height = 27.;
    style.tab_bar.fill_tab_bar = false;
    style.tab_bar.bg_fill = Color32::from_rgb(24, 27, 29);
    style.tab_bar.hline_color = Color32::from_gray(63);
    style.tab_bar.corner_radius = egui::CornerRadius::ZERO;
    style.tab.minimum_width = Some(105.);
    style.tab.tab_body.inner_margin = egui::Margin::same(3);
    style.tab.tab_body.bg_fill = Color32::from_rgb(12, 15, 17);
    style.tab.tab_body.stroke = egui::Stroke::NONE;
    style.tab.tab_body.corner_radius = egui::CornerRadius::ZERO;
    for interaction in [
        &mut style.tab.active,
        &mut style.tab.focused,
        &mut style.tab.inactive,
        &mut style.tab.hovered,
        &mut style.tab.inactive_with_kb_focus,
        &mut style.tab.active_with_kb_focus,
        &mut style.tab.focused_with_kb_focus,
    ] {
        interaction.corner_radius = egui::CornerRadius::ZERO;
        interaction.outline_color = Color32::from_gray(53);
        interaction.bg_fill = Color32::from_rgb(35, 39, 42);
        interaction.text_color = Color32::LIGHT_GRAY;
    }
    style.tab.inactive.bg_fill = Color32::from_rgb(24, 27, 29);
    style.tab.focused.text_color = AMBER;
    style.tab.focused_with_kb_focus.text_color = AMBER;
    style.tab.focused.outline_color = Color32::from_rgb(124, 110, 74);
    style.separator.width = 4.;
    style.separator.extra_interact_width = 6.;
    style.separator.extra = 95.;
    style.separator.color_idle = Color32::from_gray(52);
    style.separator.color_hovered = AMBER;
    style.separator.color_dragged = AMBER;
    style.overlay.overlay_type = egui_dock::OverlayType::HighlightedAreas;
    style.overlay.selection_color = Color32::from_rgba_unmultiplied(214, 184, 109, 65);
    style.overlay.button_color = AMBER;
    style
}

fn abbreviated_title(title: &str) -> String {
    let mut chars = title.chars();
    let mut result: String = chars.by_ref().take(44).collect();
    if chars.next().is_some() {
        result.push('…');
    }
    result
}

struct StructureTabs<'a> {
    views: &'a mut BTreeMap<usize, ui_views::View>,
    loading: &'a BTreeMap<usize, String>,
    errors: &'a BTreeMap<usize, String>,
    refs: &'a [Value],
    selected: &'a mut usize,
    show_axes: bool,
    link_views: bool,
    linked_camera: Option<scene::Camera>,
    focus: Option<usize>,
    closed: BTreeSet<usize>,
    duplicate: Option<usize>,
    split: Option<(usize, Split)>,
}

impl StructureTabs<'_> {
    fn metadata(&self, slot: usize) -> Option<&Value> {
        self.views
            .get(&slot)
            .map(|view| &view.metadata)
            .or_else(|| {
                self.refs
                    .iter()
                    .find(|reference| reference["slot"].as_u64() == Some(slot as u64))
            })
    }

    fn name(&self, slot: usize) -> String {
        if let Some(view) = self.views.get(&slot) {
            return view.molecule.name.clone();
        }
        self.metadata(slot)
            .map(ui_views::display_name)
            .filter(|name: &String| !name.is_empty())
            .unwrap_or_else(|| "Structure".into())
    }

    fn select(&mut self, slot: usize) {
        *self.selected = slot;
        self.focus = Some(slot);
    }
}

impl TabViewer for StructureTabs<'_> {
    type Tab = usize;

    fn title(&mut self, slot: &mut usize) -> egui::WidgetText {
        let name = abbreviated_title(&self.name(*slot));
        // Four monospace spaces reserve the left-hand close target.
        RichText::new(format!("    {name}"))
            .monospace()
            .size(11.)
            .into()
    }

    fn id(&mut self, slot: &mut usize) -> egui::Id {
        egui::Id::new(("structure-tab", *slot))
    }

    fn on_tab_button(&mut self, slot: &mut usize, response: &egui::Response) {
        let close_rect = egui::Rect::from_center_size(
            egui::pos2(response.rect.left() + 15., response.rect.center().y),
            Vec2::splat(19.),
        );
        let close_hovered = response.hovered()
            && response
                .hover_pos()
                .is_some_and(|position| close_rect.contains(position));
        let painter = response
            .ctx
            .layer_painter(response.layer_id)
            .with_clip_rect(response.interact_rect);
        if close_hovered {
            painter.rect_filled(close_rect, 2., Color32::from_gray(72));
            response.ctx.set_cursor_icon(egui::CursorIcon::PointingHand);
        }
        let center = close_rect.center();
        let stroke = egui::Stroke::new(
            1.2,
            if close_hovered {
                Color32::WHITE
            } else {
                Color32::from_gray(160)
            },
        );
        painter.line_segment(
            [center + Vec2::new(-3.5, -3.5), center + Vec2::new(3.5, 3.5)],
            stroke,
        );
        painter.line_segment(
            [center + Vec2::new(-3.5, 3.5), center + Vec2::new(3.5, -3.5)],
            stroke,
        );
        response.clone().on_hover_text(if close_hovered {
            "Close this viewer tab. The run and its artifacts remain available.".into()
        } else {
            format!("{}\nDrag to reorder; drop at a viewer edge to split, or on its tab bar to combine.", self.name(*slot))
        });
        if response.middle_clicked() || (response.clicked() && close_hovered) {
            self.closed.insert(*slot);
        } else if response.clicked() {
            self.select(*slot);
        }
    }

    fn context_menu(&mut self, ui: &mut egui::Ui, slot: &mut usize, _: SurfaceIndex, _: NodeIndex) {
        if ui.button("Duplicate tab").clicked() {
            self.duplicate = Some(*slot);
            ui.close();
        }
        for (label, direction) in [("Split down", Split::Below), ("Split right", Split::Right)] {
            if ui
                .button(label)
                .on_hover_text("Open an independent copy in a new viewer group.")
                .clicked()
            {
                self.split = Some((*slot, direction));
                ui.close();
            }
        }
        ui.separator();
        if ui.button("Close tab").clicked() {
            self.closed.insert(*slot);
            ui.close();
        }
    }

    fn on_close(&mut self, slot: &mut usize) -> egui_dock::tab_viewer::OnCloseResponse {
        self.closed.insert(*slot);
        egui_dock::tab_viewer::OnCloseResponse::Ignore
    }

    fn allowed_in_windows(&self, _: &mut usize) -> bool {
        false
    }

    fn scroll_bars(&self, _: &usize) -> [bool; 2] {
        [false, false]
    }

    fn ui(&mut self, ui: &mut egui::Ui, slot: &mut usize) {
        let inside_body = ui.rect_contains_pointer(ui.max_rect());
        if inside_body && ui.input(|input| input.pointer.any_pressed()) {
            self.select(*slot);
        }
        if self.loading.contains_key(slot) {
            let status = ui_views::loading_status(self.metadata(*slot));
            ui.horizontal(|ui| {
                ui.spinner();
                ui.add(egui::Label::new(&status).truncate())
                    .on_hover_text(status);
            });
        }
        if let Some(error) = self.errors.get(slot) {
            ui.colored_label(RED, error);
        }
        let Some(view) = self.views.get_mut(slot) else {
            ui.centered_and_justified(|ui| {
                ui.weak(if self.loading.contains_key(slot) {
                    "The structure will appear here when it is ready."
                } else {
                    "Open a result from the sidebar, or a local PDB / mmCIF file."
                });
            });
            return;
        };
        let source = match text(&view.metadata, "source_kind") {
            "demo" => "EXPERIMENTAL DEMO · 4OO8 · 2.50 Å · not a prediction".into(),
            "local" => "LOCAL STRUCTURE · original coordinates".into(),
            _ => format!(
                "{} · {} · {}",
                text(&view.metadata, "model"),
                ui_views::short_name(text(&view.metadata, "sample_id")),
                text(&view.metadata, "job_state")
            ),
        };
        ui.add(
            egui::Label::new(RichText::new(source).size(10.).color(
                if text(&view.metadata, "source_kind") == "demo" {
                    AMBER
                } else {
                    GREEN
                },
            ))
            .truncate(),
        );
        ui.horizontal(|ui| {
            ui.toggle_value(&mut view.hotspots.enabled, "Pick hotspots")
                .on_hover_text("Click protein residues in the structure or sequence to toggle hotspots. Ctrl/Cmd-click also toggles; Shift-click the sequence selects a range. Drag still rotates.");
            ui.small(format!("{} selected", view.hotspots.residues.len()));
            if !view.hotspots.residues.is_empty() && ui.small_button("Clear hotspots").clicked() {
                view.hotspots.residues.clear();
                view.hotspots.anchor = None;
            }
        });
        let mut sequence_pick = None;
        egui::ScrollArea::horizontal()
            .id_salt(("sequence", *slot))
            .max_height(22.)
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    for residue in &view.molecule.residues {
                        if !view.chains[residue.chain] {
                            continue;
                        }
                        let selected = view.selected.as_ref() == Some(&residue.key);
                        let hotspot = view.hotspots.residues.contains(&residue.key);
                        if ui
                            .add(
                                egui::Button::new(
                                    RichText::new(residue.letter.to_string())
                                        .monospace()
                                        .color(view.molecule.chains[residue.chain].color),
                                )
                                .fill(if hotspot {
                                    Color32::from_rgb(145, 66, 31)
                                } else {
                                    ui.visuals().widgets.inactive.bg_fill
                                })
                                .min_size(Vec2::new(9., 17.))
                                .selected(selected),
                            )
                            .on_hover_text(residue.key.to_string())
                            .clicked()
                        {
                            view.selected = Some(residue.key.clone());
                            sequence_pick =
                                Some((residue.key.clone(), ui.input(|input| input.modifiers)));
                            *self.selected = *slot;
                            self.focus = Some(*slot);
                        }
                    }
                });
            });
        if let Some((key, modifiers)) = sequence_pick {
            view.hotspots.pick(
                &view.molecule,
                &key,
                modifiers.ctrl || modifiers.command,
                modifiers.shift && (view.hotspots.enabled || modifiers.ctrl || modifiers.command),
            );
        }
        let before = view.selected.clone();
        let changed = scene::viewport(
            ui,
            &view.molecule,
            &view.renderer,
            &mut view.camera,
            view.style,
            *slot,
            &mut view.selected,
            &mut view.hotspots,
            view.visible,
            view.labels,
            self.show_axes,
            &view.chains,
        );
        if before != view.selected || changed {
            *self.selected = *slot;
            self.focus = Some(*slot);
        }
        if changed && self.link_views {
            self.linked_camera = Some(view.camera);
        }
    }
}

impl Workbench {
    pub(super) fn focus_view(&mut self, slot: usize) {
        if !focus_tab(&mut self.dock, slot) {
            self.dock.push_to_focused_leaf(slot);
            focus_tab(&mut self.dock, slot);
        }
        self.state.selected_view = slot;
    }

    pub(super) fn split_active(&mut self, direction: Split, ctx: &egui::Context) {
        self.split_view(self.state.selected_view, direction, ctx);
    }

    fn split_view(
        &mut self,
        source: usize,
        direction: Split,
        ctx: &egui::Context,
    ) -> Option<usize> {
        let target = self.duplicate_view(source, ctx)?;
        move_to_split(&mut self.dock, target, direction);
        Some(target)
    }

    pub(super) fn viewports(&mut self, ui: &mut egui::Ui) {
        if self.dock.iter_all_tabs().next().is_none() {
            ui.centered_and_justified(|ui| {
                ui.vertical_centered(|ui| {
                    ui.add_space((ui.available_height() * 0.35).max(0.));
                    ui.heading("Open a structure to begin");
                    ui.label("Click a run or result in the sidebar, or open a local PDB / mmCIF.");
                    ui.weak(
                        "Drag tabs to the edges to split the viewer. Close a tab with its left ×.",
                    );
                });
            });
            return;
        }
        let style = dock_style(ui);
        let selected_before = self.state.selected_view;
        let mut viewer = StructureTabs {
            views: &mut self.views,
            loading: &self.view_loading,
            errors: &self.view_errors,
            refs: &self.state.view_refs,
            selected: &mut self.state.selected_view,
            show_axes: self.state.show_axes,
            link_views: self.state.link_views,
            linked_camera: None,
            focus: None,
            closed: BTreeSet::new(),
            duplicate: None,
            split: None,
        };
        DockArea::new(&mut self.dock)
            .id(egui::Id::new("structure-editor-dock"))
            .style(style)
            .show_close_buttons(false)
            .show_leaf_close_all_buttons(false)
            .show_leaf_collapse_buttons(false)
            .show_secondary_button_hint(false)
            .show_inside(ui, &mut viewer);
        let StructureTabs {
            mut focus,
            linked_camera: linked,
            closed,
            duplicate,
            split,
            ..
        } = viewer;
        for &slot in &closed {
            self.close_view(slot);
        }
        if let Some(slot) = duplicate {
            focus = self.duplicate_view(slot, ui.ctx());
        } else if let Some((slot, direction)) = split {
            focus = self.split_view(slot, direction, ui.ctx());
        }
        if let Some(slot) = settle_focus(&mut self.dock, selected_before, focus, &closed) {
            self.state.selected_view = slot;
        }
        if let Some(camera) = linked {
            for view in self.views.values_mut() {
                view.camera.yaw = camera.yaw;
                view.camera.pitch = camera.pitch;
                view.camera.zoom = camera.zoom;
                view.camera.pan = camera.pan;
                view.camera.ambient = camera.ambient;
                view.camera.bloom = camera.bloom;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn refs(slots: &[usize]) -> Vec<Value> {
        slots.iter().map(|slot| json!({"slot":slot})).collect()
    }

    fn ids(dock: &DockState<usize>) -> Vec<usize> {
        dock.iter_all_tabs().map(|(_, slot)| *slot).collect()
    }

    #[test]
    fn tabs_beyond_four_survive_split_roundtrip_and_close() {
        let mut dock = DockState::new((0..12).collect());
        assert!(move_to_split(&mut dock, 4, Split::Right));
        assert!(move_to_split(&mut dock, 5, Split::Below));
        let saved = save_layout(&dock);
        let mut restored = restore_layout(&saved, &refs(&(0..12).collect::<Vec<_>>()), 5);
        assert_eq!(save_layout(&restored), saved);
        assert_eq!(restored.iter_leaves().count(), 3);
        let location = restored.find_tab(&4).unwrap();
        restored.remove_tab(location);
        assert_eq!(restored.iter_leaves().count(), 2);
        assert!(!ids(&restored).contains(&4));
        assert_eq!(ids(&restored).len(), 11);
    }

    #[test]
    fn restore_filters_stale_duplicates_and_keeps_unrepresented_refs() {
        let saved = json!({"version":1,"root":{
            "type":"split","axis":"horizontal","fraction":0.3,
            "first":{"type":"tabs","tabs":[8,2,8,900],"active":2},
            "second":{"type":"tabs","tabs":[8,6],"active":6}
        }});
        let dock = restore_layout(&saved, &refs(&[2, 6, 8, 42, 55]), 6);
        assert_eq!(ids(&dock), vec![8, 2, 42, 55, 6]);
        assert_eq!(dock.iter_leaves().count(), 2);
        assert_eq!(save_layout(&dock)["root"]["first"]["active"], 2);
        assert_eq!(save_layout(&dock)["root"]["second"]["active"], 6);
    }

    #[test]
    fn malformed_layout_preserves_all_source_refs_as_tabs() {
        let invalid = json!({"version":1,"root":{
            "type":"split","axis":"horizontal","fraction":4.,
            "first":{"type":"tabs","tabs":[3],"active":3},
            "second":{"type":"tabs","tabs":[7],"active":7}
        }});
        let dock = restore_layout(&invalid, &refs(&[3, 7, 99]), 7);
        assert_eq!(ids(&dock), vec![3, 7, 99]);
        assert_eq!(dock.iter_leaves().count(), 1);
        assert_eq!(save_layout(&dock)["root"]["active"], 7);
    }

    #[test]
    fn closing_last_tab_restores_empty_without_recreating_demo() {
        let mut dock = DockState::new(vec![15]);
        dock.remove_tab(dock.find_tab(&15).unwrap());
        let saved = save_layout(&dock);
        assert_eq!(saved["version"], 1);
        assert!(ids(&restore_layout(&saved, &[], 15)).is_empty());
    }

    #[test]
    fn split_requires_another_tab_and_focus_never_duplicates() {
        let mut dock = DockState::new(vec![2, 12]);
        assert!(focus_tab(&mut dock, 12));
        assert!(!focus_tab(&mut dock, 999));
        assert_eq!(ids(&dock), vec![2, 12]);
        assert!(move_to_split(&mut dock, 12, Split::Below));
        assert!(!move_to_split(&mut dock, 12, Split::Right));
        assert_eq!(ids(&dock), vec![2, 12]);
    }

    struct PointerHarness {
        ctx: egui::Context,
        dock: DockState<usize>,
        selected: usize,
        time: f64,
        footer_top: Option<f32>,
    }

    impl PointerHarness {
        fn new() -> Self {
            Self {
                ctx: egui::Context::default(),
                dock: DockState::new(vec![0, 1, 2]),
                selected: 0,
                time: 0.,
                footer_top: None,
            }
        }

        fn frame(&mut self, events: Vec<egui::Event>) {
            self.time += 0.1;
            let input = egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    Vec2::new(800., 600.),
                )),
                time: Some(self.time),
                events,
                ..Default::default()
            };
            let mut views = BTreeMap::new();
            let loading = BTreeMap::new();
            let errors = BTreeMap::new();
            let refs = refs(&[0, 1, 2]);
            let mut closed = BTreeSet::new();
            let selected_before = self.selected;
            let mut requested = None;
            let ctx = self.ctx.clone();
            let _ = ctx.run(input, |ctx| {
                if self.footer_top.is_some() {
                    let panel = egui::TopBottomPanel::bottom("test-console")
                        .resizable(true)
                        .default_height(164.)
                        .min_height(70.)
                        .show(ctx, |ui| {
                            ui.take_available_height();
                            ui.label("Console");
                        });
                    self.footer_top = Some(panel.response.rect.top());
                }
                egui::CentralPanel::default().show(ctx, |ui| {
                    let mut viewer = StructureTabs {
                        views: &mut views,
                        loading: &loading,
                        errors: &errors,
                        refs: &refs,
                        selected: &mut self.selected,
                        show_axes: true,
                        link_views: false,
                        linked_camera: None,
                        focus: None,
                        closed: BTreeSet::new(),
                        duplicate: None,
                        split: None,
                    };
                    let style = dock_style(ui);
                    DockArea::new(&mut self.dock)
                        .id(egui::Id::new("dock-pointer-test"))
                        .style(style)
                        .show_close_buttons(false)
                        .show_leaf_close_all_buttons(false)
                        .show_leaf_collapse_buttons(false)
                        .show_inside(ui, &mut viewer);
                    closed.extend(viewer.closed);
                    requested = viewer.focus;
                });
            });
            for &slot in &closed {
                if let Some(location) = self.dock.find_tab(&slot) {
                    self.dock.remove_tab(location);
                }
            }
            if let Some(slot) = settle_focus(&mut self.dock, selected_before, requested, &closed) {
                self.selected = slot;
            }
        }

        fn tab_rect(&self, tab: usize) -> egui::Rect {
            let id = egui::Id::new("dock-pointer-test")
                .with((SurfaceIndex::main(), "surface"))
                .with((NodeIndex::root(), "node"))
                .with((tab, "tab"));
            self.ctx.read_response(id).unwrap().rect
        }

        fn pointer_button(position: egui::Pos2, pressed: bool) -> egui::Event {
            egui::Event::PointerButton {
                pos: position,
                button: egui::PointerButton::Primary,
                pressed,
                modifiers: egui::Modifiers::NONE,
            }
        }

        fn click(&mut self, position: egui::Pos2) {
            self.frame(vec![egui::Event::PointerMoved(position)]);
            self.frame(vec![Self::pointer_button(position, true)]);
            self.frame(vec![Self::pointer_button(position, false)]);
        }
    }

    #[test]
    fn left_close_target_closes_only_its_tab_and_title_click_selects() {
        let mut harness = PointerHarness::new();
        harness.frame(Vec::new());
        harness.frame(Vec::new());
        harness.click(harness.tab_rect(2).center());
        assert_eq!(harness.selected, 2);
        assert_eq!(ids(&harness.dock), vec![0, 1, 2]);
        let tab = harness.tab_rect(0);
        harness.click(egui::pos2(tab.left() + 15., tab.center().y));
        assert_eq!(ids(&harness.dock), vec![1, 2]);
        assert_eq!(harness.selected, 2);
    }

    #[test]
    fn dragging_title_to_edge_splits_without_closing_or_duplicating() {
        let mut harness = PointerHarness::new();
        harness.frame(Vec::new());
        harness.frame(Vec::new());
        let origin = harness.tab_rect(1).center();
        harness.frame(vec![egui::Event::PointerMoved(origin)]);
        harness.frame(vec![PointerHarness::pointer_button(origin, true)]);
        for position in [
            origin + Vec2::new(40., 30.),
            egui::pos2(500., 200.),
            egui::pos2(770., 300.),
            egui::pos2(780., 300.),
        ] {
            harness.frame(vec![egui::Event::PointerMoved(position)]);
        }
        let destination = egui::pos2(780., 300.);
        harness.frame(vec![PointerHarness::pointer_button(destination, false)]);
        harness.frame(Vec::new());
        assert_eq!(harness.dock.iter_leaves().count(), 2);
        let mut slots = ids(&harness.dock);
        slots.sort();
        assert_eq!(slots, vec![0, 1, 2]);
    }

    #[test]
    fn body_press_selects_only_the_group_under_the_pointer() {
        let mut harness = PointerHarness::new();
        assert!(move_to_split(&mut harness.dock, 1, Split::Right));
        harness.selected = 1;
        harness.frame(Vec::new());
        harness.frame(Vec::new());
        harness.click(egui::pos2(200., 300.));
        assert_eq!(harness.selected, 0);
        harness.click(egui::pos2(600., 300.));
        assert_eq!(harness.selected, 1);
    }

    #[test]
    fn resizing_console_does_not_activate_the_editor_beneath_its_handle() {
        let mut harness = PointerHarness::new();
        assert!(move_to_split(&mut harness.dock, 1, Split::Right));
        focus_tab(&mut harness.dock, 0);
        harness.selected = 0;
        harness.footer_top = Some(0.);
        harness.frame(Vec::new());
        harness.frame(Vec::new());
        let origin = egui::pos2(600., harness.footer_top.unwrap() + 2.);
        harness.frame(vec![egui::Event::PointerMoved(origin)]);
        harness.frame(vec![PointerHarness::pointer_button(origin, true)]);
        let target = origin - Vec2::new(0., 100.);
        harness.frame(vec![egui::Event::PointerMoved(target)]);
        harness.frame(vec![PointerHarness::pointer_button(target, false)]);
        assert_eq!(harness.selected, 0);
    }
}
