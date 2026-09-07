mod pymol;
mod scene;
use eframe::egui::{self, Color32, FontFamily, FontId, RichText, Stroke, Vec2};
use scene::{Camera, Molecule, Representation};
const SEQUENCE: &str = include_str!("../fixtures/cas9-sequence.txt");
const AMBER: Color32 = Color32::from_rgb(214, 184, 109);
const GREEN: Color32 = Color32::from_rgb(113, 174, 143);
struct Workbench {
    pymol: pymol::Launcher,
    molecule: Molecule,
    renderer: scene::Renderer,
    cameras: [Camera; 2],
    styles: [Representation; 2],
    visible: [bool; 2],
    labels: [bool; 2],
    chains: [bool; 3],
    draft: String,
    name: String,
    modality: usize,
    models: [bool; 4],
    console_input: String,
    console: Vec<String>,
    link_views: bool,
    show_axes: bool,
    comparison: bool,
    selected: usize,
    selected_object: usize,
    sidebar_tab: usize,
    show_help: bool,
    msa: usize,
    notes: String,
}
impl Workbench {
    fn new(cc: &eframe::CreationContext<'_>) -> Self {
        let ctx = &cc.egui_ctx;
        let mut style = egui::Style::default();
        for text in [egui::TextStyle::Body, egui::TextStyle::Button] {
            style
                .text_styles
                .insert(text, FontId::new(11.5, FontFamily::Proportional));
        }
        style
            .text_styles
            .insert(egui::TextStyle::Monospace, FontId::monospace(11.));
        style.text_styles.insert(
            egui::TextStyle::Small,
            FontId::new(10., FontFamily::Proportional),
        );
        style.text_styles.insert(
            egui::TextStyle::Heading,
            FontId::new(13., FontFamily::Proportional),
        );
        style.spacing.item_spacing = Vec2::new(5., 4.);
        style.spacing.button_padding = Vec2::new(7., 3.);
        style.spacing.interact_size = Vec2::new(22., 20.);
        style.spacing.indent = 12.;
        style.visuals = egui::Visuals::dark();
        style.visuals.panel_fill = Color32::from_rgb(49, 51, 54);
        style.visuals.window_fill = Color32::from_rgb(49, 51, 54);
        style.visuals.extreme_bg_color = Color32::from_rgb(26, 28, 30);
        style.visuals.faint_bg_color = Color32::from_rgb(57, 59, 62);
        style.visuals.override_text_color = Some(Color32::from_gray(219));
        style.visuals.selection.bg_fill = Color32::from_rgb(66, 91, 111);
        style.visuals.selection.stroke = Stroke::new(1., Color32::from_rgb(133, 162, 183));
        for visual in [
            &mut style.visuals.widgets.noninteractive,
            &mut style.visuals.widgets.inactive,
            &mut style.visuals.widgets.hovered,
            &mut style.visuals.widgets.active,
            &mut style.visuals.widgets.open,
        ] {
            visual.corner_radius = egui::CornerRadius::ZERO;
            visual.bg_stroke = Stroke::new(1., Color32::from_rgb(80, 82, 86));
        }
        style.visuals.widgets.inactive.bg_fill = Color32::from_rgb(62, 64, 67);
        style.visuals.widgets.inactive.weak_bg_fill = Color32::from_rgb(57, 59, 62);
        style.visuals.widgets.hovered.bg_fill = Color32::from_rgb(78, 88, 96);
        style.visuals.widgets.hovered.weak_bg_fill = Color32::from_rgb(73, 81, 88);
        style.visuals.window_corner_radius = egui::CornerRadius::ZERO;
        style.visuals.menu_corner_radius = egui::CornerRadius::ZERO;
        ctx.set_style(style);
        let molecule = Molecule::reference();
        let renderer =
            scene::Renderer::new(cc.gl.as_ref().expect("OpenGL renderer required"), &molecule)
                .expect("Cannot initialize molecular GPU renderer");
        Self{renderer,pymol:pymol::Launcher::default(),molecule,cameras:[Camera::default();2],styles:[Representation::Cartoon,Representation::Sticks],visible:[true;2],labels:[false;2],chains:[true;3],draft:SEQUENCE.into(),name:"Cas9_sgRNA_DNA_reference".into(),modality:0,models:[true;4],console_input:String::new(),console:vec!["Bio Workbench / native Rust interface study 0.2".into(),"OFFLINE DESIGN PROTOTYPE — cloud submission, imports and result analysis are disabled.".into(),"Loaded embedded reference: PDB 4OO8, chains A/B/C (Cas9 + sgRNA + target DNA). Both panes show the same experimental structure.".into(),"Mouse: left-drag rotate | right-drag pan | wheel zoom | double-click reset | click residue to select".into()],link_views:true,show_axes:true,comparison:true,selected:840,selected_object:0,sidebar_tab:0,show_help:false,msa:0,notes:"Selection note (local mockup): inspect this region in both views.".into()}
    }
    fn log(&mut self, text: impl Into<String>) {
        self.console.push(text.into());
        if self.console.len() > 100 {
            self.console.remove(0);
        }
    }
    fn section(ui: &mut egui::Ui, label: &str) {
        ui.add_space(3.);
        ui.label(
            RichText::new(label)
                .strong()
                .size(11.)
                .color(Color32::from_gray(236)),
        );
        ui.separator();
    }
    fn top(&mut self, ctx: &egui::Context) {
        egui::TopBottomPanel::top("menus")
            .exact_height(28.)
            .show(ctx, |ui| {
                egui::MenuBar::new().ui(ui, |ui| {
                    ui.label(RichText::new("BIO WORKBENCH").strong().size(12.));
                    ui.separator();
                    ui.menu_button("File", |ui| {
                        if ui.button("Reset local sample").clicked() {
                            self.draft = SEQUENCE.into();
                            self.cameras = [Camera::default(); 2];
                            self.log("reset: local reference restored");
                            ui.close();
                        }
                        ui.add_enabled(false, egui::Button::new("Open structure…"));
                        ui.add_enabled(false, egui::Button::new("Save session…"));
                        ui.separator();
                        if ui.button("Quit").clicked() {
                            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                        }
                    });
                    ui.menu_button("Edit", |ui| {
                        if ui.button("Clear selection").clicked() {
                            self.selected = 0;
                            ui.close();
                        }
                        ui.add_enabled(false, egui::Button::new("Undo / Redo"));
                    });
                    ui.menu_button("View", |ui| {
                        ui.checkbox(&mut self.comparison, "Split comparison");
                        ui.checkbox(&mut self.link_views, "Link camera controls");
                        ui.checkbox(&mut self.show_axes, "Orientation axes");
                    });
                    ui.menu_button("Display", |ui| {
                        for r in [
                            Representation::Cartoon,
                            Representation::Sticks,
                            Representation::Spheres,
                            Representation::Trace,
                        ] {
                            if ui
                                .selectable_label(self.styles[self.selected_object] == r, r.name())
                                .clicked()
                            {
                                self.styles[self.selected_object] = r;
                                ui.close();
                            }
                        }
                    });
                    ui.menu_button("Selection", |ui| {
                        if ui.button("Select residue 840").clicked() {
                            self.selected = 840;
                            ui.close();
                        }
                        if ui.button("Clear").clicked() {
                            self.selected = 0;
                            ui.close();
                        }
                    });
                    ui.menu_button("Tools", |ui| {
                        ui.add_enabled(false, egui::Button::new("Align structures…"));
                        ui.add_enabled(false, egui::Button::new("Measure distance…"));
                        ui.add_enabled(false, egui::Button::new("Submit cloud run…"));
                    });
                    if ui.button("Help").clicked() {
                        self.show_help = true;
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.colored_label(AMBER, "OFFLINE DESIGN PROTOTYPE");
                    });
                });
            });
        egui::TopBottomPanel::top("toolbar")
            .exact_height(33.)
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    if ui.button("Reset view").clicked() {
                        self.cameras = [Camera::default(); 2];
                    }
                    ui.separator();
                    ui.selectable_value(&mut self.comparison, false, "Single");
                    ui.selectable_value(&mut self.comparison, true, "Compare");
                    ui.separator();
                    ui.checkbox(&mut self.link_views, "Link cameras");
                    ui.checkbox(&mut self.show_axes, "Axes");
                    ui.separator();
                    ui.label("Style:");
                    egui::ComboBox::from_id_salt("style-toolbar")
                        .width(94.)
                        .selected_text(self.styles[self.selected_object].name())
                        .show_ui(ui, |ui| {
                            for r in [
                                Representation::Cartoon,
                                Representation::Sticks,
                                Representation::Spheres,
                                Representation::Trace,
                            ] {
                                ui.selectable_value(
                                    &mut self.styles[self.selected_object],
                                    r,
                                    r.name(),
                                );
                            }
                        });
                    ui.add_enabled(false, egui::Button::new("Align"));
                    ui.add_enabled(false, egui::Button::new("Measure"));
                    ui.add_enabled(false, egui::Button::new("Export"));
                    if ui
                        .add_enabled(!self.pymol.active(), egui::Button::new("Launch in PyMOL"))
                        .on_hover_text(
                            "Open experimental 4OO8 chains A/B/C in a separate local PyMOL window.",
                        )
                        .clicked()
                    {
                        self.pymol.launch();
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(
                            RichText::new("head: disconnected   |   sample: 4OO8")
                                .monospace()
                                .size(10.)
                                .color(Color32::from_gray(165)),
                        );
                    });
                });
            });
    }
    fn left(&mut self, ctx: &egui::Context) {
        egui::SidePanel::left("submission").default_width(252.).width_range(225.0..=350.).resizable(true).show(ctx,|ui|{
   ui.horizontal(|ui|{ui.selectable_value(&mut self.sidebar_tab,0,"Inputs / models");ui.selectable_value(&mut self.sidebar_tab,1,"Run queue");});ui.separator();
   if self.sidebar_tab==0{
    Self::section(ui,"INPUT DRAFT");egui::Grid::new("input-fields").num_columns(2).spacing(Vec2::new(6.,6.)).show(ui,|ui|{
     ui.label("Name");ui.add(egui::TextEdit::singleline(&mut self.name).desired_width(164.));ui.end_row();ui.label("Type");
     egui::ComboBox::from_id_salt("modality").width(159.).selected_text(["Protein","DNA","RNA","Ligand / SMILES","Complex"][self.modality]).show_ui(ui,|ui|{for(i,text)in["Protein","DNA","RNA","Ligand / SMILES","Complex"].iter().enumerate(){ui.selectable_value(&mut self.modality,i,*text);}});ui.end_row();
    });ui.add_space(5.);ui.label("Sequence / molecular input");egui::ScrollArea::vertical().id_salt("sequence-draft-scroll").max_height(130.).show(ui,|ui|{ui.add(egui::TextEdit::multiline(&mut self.draft).font(egui::TextStyle::Monospace).desired_rows(7).desired_width(f32::INFINITY));});
    let residues=self.draft.chars().filter(|c|c.is_ascii_alphabetic()).count();ui.label(RichText::new(format!("{residues} letters  |  unsaved local draft")).small().color(Color32::from_gray(162)));
    ui.horizontal(|ui|{if ui.small_button("Load demo").clicked(){self.draft=SEQUENCE.into();self.name="Cas9_sgRNA_DNA_reference".into();}
if ui.small_button("Clear").clicked(){self.draft.clear();}ui.add_enabled(false,egui::Button::new("Files…"));ui.add_enabled(false,egui::Button::new("Library…"));});
    Self::section(ui,"PREDICTION MODELS");egui::Grid::new("models").num_columns(2).min_col_width(110.).show(ui,|ui|{for(i,name)in["Boltz","Protenix","OpenFold3","RoseTTAFold3"].iter().enumerate(){ui.checkbox(&mut self.models[i],*name);if i%2==1{ui.end_row();}}});
    ui.add_space(6.);egui::Grid::new("run-settings").num_columns(2).spacing(Vec2::new(6.,6.)).show(ui,|ui|{
     ui.label("MSA");egui::ComboBox::from_id_salt("msa").width(145.).selected_text(["Public server","Private database"][self.msa]).show_ui(ui,|ui|{ui.selectable_value(&mut self.msa,0,"Public server");ui.selectable_value(&mut self.msa,1,"Private database");});ui.end_row();ui.label("Seeds");ui.label("1");ui.end_row();ui.label("Execution");ui.label("Ephemeral / on demand");ui.end_row();
    });ui.add_space(8.);ui.add_enabled(false,egui::Button::new("Check compatibility").min_size(Vec2::new(ui.available_width(),24.)));ui.add_enabled(false,egui::Button::new(format!("Submit {} models",self.models.iter().filter(|x|**x).count())).min_size(Vec2::new(ui.available_width(),26.)));ui.label(RichText::new("Layout only. Draft edits do not change the reference structure or start any job.").small().color(AMBER));
    Self::section(ui,"SESSION");ui.monospace("workspace   local preview\nconnection  offline\nqueue       illustrative\ncompute     none");
   }else{
    Self::section(ui,"MOCK RUN QUEUE");ui.colored_label(AMBER,"No jobs are connected or executing.");ui.add_space(8.);for(n,model,state)in[(1,"Boltz","example: running"),(2,"Protenix","example: running"),(3,"OpenFold3","example: queued"),(4,"RF3","example: ready")]{ui.horizontal(|ui|{ui.monospace(format!("{n:02}"));ui.label(RichText::new(model).strong());});ui.label(RichText::new(state).small().color(Color32::from_gray(157)));ui.separator();}ui.add_enabled(false,egui::Button::new("Cancel selected"));
   }
  });
    }
    fn right(&mut self, ctx: &egui::Context) {
        egui::SidePanel::right("objects").default_width(285.).width_range(275.0..=380.).resizable(true).show(ctx,|ui|{
   Self::section(ui,"OBJECTS / SELECTIONS");ui.horizontal(|ui|{ui.monospace("scene");ui.with_layout(egui::Layout::right_to_left(egui::Align::Center),|ui|{ui.label(RichText::new("A   S   H   L   C").monospace().size(10.).color(Color32::from_gray(150)));});});
   for index in 0..2{
    ui.horizontal(|ui|{
     ui.checkbox(&mut self.visible[index],"");if ui.selectable_label(self.selected_object==index,RichText::new(if index==0{"cas9_cartoon"}else{"cas9_atoms"}).monospace().size(11.)).clicked(){self.selected_object=index;}
     ui.spacing_mut().item_spacing.x=2.;ui.menu_button("A",|ui|{if ui.button("Reset view").clicked(){self.cameras[index]=Camera::default();ui.close();}
if ui.button("Select chain A").clicked(){self.selected=840;ui.close();}});
     ui.menu_button("S",|ui|{for r in[Representation::Cartoon,Representation::Sticks,Representation::Spheres,Representation::Trace]{if ui.button(r.name()).clicked(){self.styles[index]=r;self.visible[index]=true;ui.close();}}});
     if ui.small_button("H").on_hover_text("Hide / show object").clicked(){self.visible[index]= !self.visible[index];}
if ui.small_button("L").on_hover_text("Toggle selection label").clicked(){self.labels[index]= !self.labels[index];if !self.labels[index]{self.selected=0;}}
     ui.menu_button("C",|ui| {ui.label("Protein: teal / RNA: amber / DNA: violet");ui.add_enabled(false,egui::Button::new("Other palettes (design only)"));});
    });ui.indent(index,|ui|{ui.label(RichText::new("experimental 4OO8 / same complex").monospace().size(10.).color(Color32::from_gray(162)));});
   }
   ui.add_space(4.);
   for (index,chain,label) in [(0,'A',"A  Cas9 protein    1301 aa"),(1,'B',"B  sgRNA             97 nt"),(2,'C',"C  target DNA        21 nt")] {
    ui.horizontal(|ui| {ui.checkbox(&mut self.chains[index],"");ui.colored_label(scene::chain_color(chain),RichText::new(label).monospace().size(11.));});
   }
   ui.add_space(6.);ui.horizontal(|ui|{ui.colored_label(AMBER,"●");ui.monospace(format!("(sele)   A/{}",self.selected));if ui.small_button("clear").clicked(){self.selected=0;}});
   Self::section(ui,"OBJECT INSPECTOR");egui::Grid::new("inspector").spacing(Vec2::new(10.,6.)).show(ui,|ui|{for(label,value)in[("Source","PDB 4OO8"),("Kind","Experimental reference"),("Model","none / reference only"),("Chains","A / B / C"),("Modeled","1301 aa + 97 RNA + 21 DNA"),("Representation",self.styles[self.selected_object].name()),("Confidence","not applicable"),("Alignment","not calculated")]{ui.label(RichText::new(label).color(Color32::from_gray(159)));ui.label(value);ui.end_row();}});
   Self::section(ui,"SELECTION NOTE");ui.label(RichText::new(format!("4OO8 / chain A / residue {}",self.selected)).monospace().size(10.));ui.add(egui::TextEdit::multiline(&mut self.notes).desired_rows(4).desired_width(f32::INFINITY));ui.label(RichText::new("Editable locally; notes are not saved.").small().color(Color32::from_gray(154)));
   Self::section(ui,"MOUSE CONTROLS");ui.monospace("Left drag     rotate\nRight drag    pan\nWheel         zoom\nDouble click  reset\nClick trace   select residue");ui.add_space(8.);ui.label(RichText::new("Both panes contain the same reference. Camera linking does not align structures.").small().color(Color32::from_gray(166)));
  });
    }
    fn bottom(&mut self, ctx: &egui::Context) {
        egui::TopBottomPanel::bottom("status")
            .exact_height(23.)
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.colored_label(AMBER, "OFFLINE");
                    ui.separator();
                    ui.label("Native Rust / OpenGL");
                    ui.separator();
                    ui.label("1 reference · 2 views · 0 predictions");
                    ui.separator();
                    ui.colored_label(
                        if self.pymol.failed {
                            Color32::LIGHT_RED
                        } else {
                            Color32::from_gray(190)
                        },
                        self.pymol.status,
                    )
                    .on_hover_text(&self.pymol.detail);
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(format!(
                            "Local design study   |   v{}",
                            env!("CARGO_PKG_VERSION")
                        ));
                    });
                });
            });
        egui::TopBottomPanel::bottom("console").default_height(168.).min_height(115.).resizable(true).show(ctx,|ui|{
   ui.horizontal(|ui|{ui.label(RichText::new("CONSOLE").strong());ui.separator();ui.label(RichText::new("local view commands only").small().color(Color32::from_gray(150)));if ui.small_button("Clear log").clicked(){self.console.clear();}ui.with_layout(egui::Layout::right_to_left(egui::Align::Center),|ui|{ui.label(RichText::new("help  ·  reset  ·  cartoon  ·  sticks  ·  spheres  ·  trace  ·  select N").monospace().size(10.));});});ui.separator();
   egui::ScrollArea::vertical().stick_to_bottom(true).max_height(ui.available_height()-28.).show(ui,|ui|{for(line,text)in self.console.iter().enumerate(){ui.horizontal(|ui|{ui.label(RichText::new(format!("{:03}",line+1)).monospace().color(Color32::from_gray(111)));ui.label(RichText::new(text).monospace().size(11.).color(if text.contains("OFFLINE"){AMBER}else{Color32::from_gray(194)}));});}});
   ui.horizontal(|ui|{ui.colored_label(GREEN,RichText::new("bio>").monospace());let response=ui.add(egui::TextEdit::singleline(&mut self.console_input).font(egui::TextStyle::Monospace).desired_width(f32::INFINITY));if response.lost_focus()&&ui.input(|i|i.key_pressed(egui::Key::Enter)){let command=std::mem::take(&mut self.console_input);self.command(command);response.request_focus();}});
  });
    }
    fn command(&mut self, text: String) {
        self.log(format!("bio> {text}"));
        match text.trim().to_lowercase().as_str(){"reset"=>{self.cameras=[Camera::default();2];self.log("View reset.");},"cartoon"=>self.styles[self.selected_object]=Representation::Cartoon,"sticks"=>self.styles[self.selected_object]=Representation::Sticks,"spheres"=>self.styles[self.selected_object]=Representation::Spheres,"trace"=>self.styles[self.selected_object]=Representation::Trace,"help"=>self.log("Local commands: reset, cartoon, sticks, spheres, trace, select N. No shell or cloud commands exist."),text if text.starts_with("select ")=>{if let Ok(residue)=text[7..].parse::<usize>()&& self.molecule.ca_ids.contains(&residue){self.selected=residue;return;}self.log("Choose a modeled Cas9 residue ID; missing residues cannot be selected.");},""=>{},_=>self.log("Unknown local command. Type help. This console cannot execute programs.")}
    }
}
impl eframe::App for Workbench {
    fn on_exit(&mut self, gl: Option<&eframe::glow::Context>) {
        if let Some(gl) = gl {
            self.renderer.destroy(gl);
        }
    }
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        for message in self.pymol.poll() {
            self.log(message);
        }
        if self.pymol.active() {
            ctx.request_repaint_after(std::time::Duration::from_millis(200));
        }
        self.top(ctx);
        self.bottom(ctx);
        self.left(ctx);
        self.right(ctx);
        egui::CentralPanel::default()
            .frame(egui::Frame::NONE.fill(Color32::from_rgb(36, 39, 42)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.selected_object, 0, "A  reference / cartoon");
                    ui.selectable_value(&mut self.selected_object, 1, "B  reference / atoms");
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(
                            RichText::new("4OO8   /   same coordinates")
                                .monospace()
                                .size(10.)
                                .color(Color32::from_gray(150)),
                        );
                    });
                });
                egui::Frame::new()
                    .fill(Color32::from_rgb(25, 28, 31))
                    .inner_margin(6)
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.colored_label(GREEN, RichText::new("A").monospace());
                            egui::ScrollArea::horizontal().show(ui, |ui| {
                                ui.horizontal(|ui| {
                                    ui.spacing_mut().item_spacing.x = 0.;
                                    for (i, residue) in self.molecule.sequence.iter().enumerate() {
                                        let selected = self.molecule.ca_ids[i] == self.selected;
                                        let text = RichText::new(residue.to_string())
                                            .monospace()
                                            .size(11.)
                                            .color(if selected {
                                                AMBER
                                            } else {
                                                Color32::from_gray(174)
                                            });
                                        if ui.selectable_label(selected, text).clicked() {
                                            self.selected = self.molecule.ca_ids[i];
                                        }
                                    }
                                });
                            });
                        });
                    });
                let height = ui.available_height();
                if self.comparison {
                    ui.columns(2, |columns| {
                        for (index, column) in columns.iter_mut().enumerate() {
                            column.set_min_height(height);
                            let changed = scene::viewport(
                                column,
                                &self.molecule,
                                &self.renderer,
                                &mut self.cameras[index],
                                self.styles[index],
                                index,
                                &mut self.selected,
                                self.visible[index],
                                self.labels[index],
                                self.show_axes,
                                self.chains,
                            );
                            if changed && self.link_views {
                                self.cameras[1 - index] = self.cameras[index];
                            }
                        }
                    });
                } else {
                    let index = self.selected_object;
                    let changed = scene::viewport(
                        ui,
                        &self.molecule,
                        &self.renderer,
                        &mut self.cameras[index],
                        self.styles[index],
                        index,
                        &mut self.selected,
                        self.visible[index],
                        self.labels[index],
                        self.show_axes,
                        self.chains,
                    );
                    if changed && self.link_views {
                        self.cameras[1 - index] = self.cameras[index];
                    }
                }
            });
        egui::Window::new("About this design prototype")
            .open(&mut self.show_help)
            .resizable(false)
            .show(ctx, |ui| {
                ui.label("Native Rust + egui. A dense technical interface study for feedback.");
                ui.label("Interactive: draft editing, model toggles, reference rotation/pan/zoom,");
                ui.label(
                    "representations, selection, object visibility, notes, and local console.",
                );
                ui.separator();
                ui.colored_label(
                    AMBER,
                    "No backend, SSH, APIs, submissions, imports, or scientific analysis.",
                );
                ui.label("Nothing here is a model output. All 3D views use embedded PDB 4OO8.");
                ui.label("Launch in PyMOL opens that reference in a separate local viewer.");
            });
    }
}
fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1450., 940.])
            .with_min_inner_size([1100., 720.]),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        &format!(
            "Bio Workbench {} — native Rust design prototype",
            env!("CARGO_PKG_VERSION")
        ),
        options,
        Box::new(|cc| Ok(Box::new(Workbench::new(cc)))),
    )
}
