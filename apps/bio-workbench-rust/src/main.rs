mod alignment;
mod binder_state;
mod domain_state;
mod library_cache;
mod navigation;
mod pymol;
mod render;
mod rpc;
mod scene;
mod session;
mod ui_annotations;
mod ui_binder;
mod ui_binder_context;
mod ui_binder_results;
mod ui_capacity;
mod ui_dock;
mod ui_domains;
mod ui_inputs;
mod ui_jobs;
mod ui_library;
mod ui_library_runs;
mod ui_library_structures;
mod ui_runtime;
mod ui_sequence;
mod ui_state;
mod ui_structure_uploads;
mod ui_style;
mod ui_views;
mod ui_worker;
mod ui_workspace;
use eframe::egui::{self, Color32, RichText, Vec2};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::{Arc, mpsc};
use std::time::{Duration, Instant};
use ui_state::{Input, UiState, rows, text, uid};
const AMBER: Color32 = Color32::from_rgb(214, 184, 109);
const GREEN: Color32 = Color32::from_rgb(113, 174, 143);
const RED: Color32 = Color32::from_rgb(237, 131, 123);
#[derive(Clone, PartialEq)]
enum UploadTarget {
    Input(String),
    Attachment(String, String),
    Labels(String),
    Run(String, usize),
    Binder(String),
    LibraryStructure(String, String),
}
#[derive(Clone, PartialEq)]
enum ArtifactTarget {
    View(usize),
    Export,
    Text,
}
#[derive(Clone, PartialEq)]
enum Purpose {
    LibraryStructures(ui_library_structures::Request),
    ProteinDomains(ui_domains::Request),
    Binder(ui_binder::Request),
    Catalog,
    History,
    WorkerStatus(u64),
    WorkerCapacity(ui_capacity::Request),
    WorkerControl(String),
    WorkerReceipt(String),
    Batch(String),
    Job(String),
    Run(String),
    Preview,
    Commit,
    CancelBatch,
    CancelJob,
    Logs(String, u64),
    Upload(UploadTarget),
    Artifact(ArtifactTarget),
    Library(library_cache::Request),
    LibraryRecord(String),
    LibrarySequence(Value),
    LibraryProductPreview(Value),
    LibraryAttachment(String, String),
    LibraryHistory,
    LibraryWrite(Value),
    LibraryRuns(String),
    Annotations(String),
    SaveNote(String, String),
}
struct Pending {
    purpose: Purpose,
    label: String,
    done: u64,
    total: u64,
}
struct Failure {
    id: String,
    label: String,
    message: String,
    purpose: Purpose,
}
#[derive(Clone)]
enum Pick {
    LibraryStructures(String),
    BinderTarget(String),
    Inputs,
    Structure,
    Key,
    Labels(String),
    Attachment(String),
}
enum UiEvent {
    Files(Pick, Vec<PathBuf>),
    Error(String),
    Parsed {
        slot: usize,
        metadata: Value,
        bytes: Arc<Vec<u8>>,
        molecule: Result<scene::Molecule, String>,
    },
    Text(String, String),
    Exported(Result<PathBuf, String>),
}
struct Workbench {
    session: Option<session::Session>,
    state: UiState,
    connection: rpc::Connection,
    catalog: Value,
    batches: Vec<Value>,
    batch: Option<Value>,
    connected: bool,
    worker: ui_worker::Worker,
    pending: BTreeMap<String, Pending>,
    failures: Vec<Failure>,
    library: ui_library::Explorer,
    library_runs: ui_library_runs::RunControls,
    binder: ui_binder::Panel,
    domain_import: ui_domains::Import,
    library_structures: ui_library_structures::Panel,
    connection_open: bool,
    preview_open: bool,
    help_open: bool,
    settings_model: Option<String>,
    run_after_uploads: bool,
    run_batch: Option<Value>,
    run_input_error: String,
    input_flash: Option<(String, Instant, bool)>,
    sidebar_tab: usize,
    focused_job: String,
    job_log: String,
    log_offset: u64,
    console: Vec<String>,
    console_input: String,
    console_tab: usize,
    selected_artifacts: BTreeSet<String>,
    artifact_metadata: BTreeMap<String, Value>,
    annotation_records: BTreeMap<String, Vec<Value>>,
    text_preview: Option<(String, String)>,
    views: BTreeMap<usize, ui_views::View>,
    view_loading: BTreeMap<usize, String>,
    view_errors: BTreeMap<usize, String>,
    dock: egui_dock::DockState<usize>,
    next_view_id: usize,
    retired_renderers: Vec<scene::Renderer>,
    gl: Arc<eframe::glow::Context>,
    pymol: pymol::Launcher,
    ui_tx: mpsc::Sender<UiEvent>,
    ui_rx: mpsc::Receiver<UiEvent>,
    navigation: mpsc::Receiver<String>,
    last_poll: Instant,
    last_history: Instant,
    last_save: Instant,
    saved_state: String,
    save_error: String,
    restoring_views: bool,
}
impl Workbench {
    fn new(cc: &eframe::CreationContext<'_>, navigation: mpsc::Receiver<String>) -> Self {
        ui_style::configure(&cc.egui_ctx);
        let (session, notices) = match session::Session::open(cc.egui_ctx.clone()) {
            Ok(session) => {
                let notices = session.notices.clone();
                (Some(session), notices)
            }
            Err(error) => (None, vec![format!("Local session unavailable: {error}")]),
        };
        let state = session
            .as_ref()
            .map(|s| UiState::restore(&s.draft))
            .unwrap_or_default();
        let connection = session
            .as_ref()
            .map(|s| s.connection.clone())
            .unwrap_or_default();
        let (ui_tx, ui_rx) = mpsc::channel();
        let library = ui_library::Explorer::restore(&state.extra);
        let library_runs = ui_library_runs::RunControls::default();
        let binder = ui_binder::Panel::restore(&state);
        let domain_import = ui_domains::Import::default();
        let mut app=Self{session,state,connection,catalog:Value::Null,batches:Vec::new(),batch:None,connected:false,worker:ui_worker::Worker::default(),pending:BTreeMap::new(),failures:Vec::new(),library,library_runs,binder,domain_import,library_structures:ui_library_structures::Panel::default(),connection_open:false,preview_open:false,help_open:false,settings_model:None,run_after_uploads:false,run_batch:None,run_input_error:String::new(),input_flash:None,sidebar_tab:0,focused_job:String::new(),job_log:String::new(),log_offset:0,console:vec!["GC Protein Engineering Console — native cloud client".into(),"Cas9 demo: experimental 4OO8. Open a run tab to inspect its retained model result.".into()],console_input:String::new(),console_tab:0,selected_artifacts:BTreeSet::new(),artifact_metadata:BTreeMap::new(),annotation_records:BTreeMap::new(),text_preview:None,views:BTreeMap::new(),view_loading:BTreeMap::new(),view_errors:BTreeMap::new(),dock:egui_dock::DockState::new(Vec::new()),next_view_id:0,retired_renderers:Vec::new(),gl:cc.gl.as_ref().expect("OpenGL renderer required").clone(),pymol:pymol::Launcher::default(),ui_tx,ui_rx,navigation,last_poll:Instant::now(),last_history:Instant::now(),last_save:Instant::now(),saved_state:String::new(),save_error:String::new(),restoring_views:true};
        for notice in notices {
            app.log(notice);
        }
        app.next_view_id = app
            .state
            .view_refs
            .iter()
            .filter_map(|value| value["slot"].as_u64())
            .max()
            .map(|slot| slot as usize + 1)
            .unwrap_or(0);
        if app.state.view_refs.is_empty() && app.state.dock_layout.is_null() {
            app.demo_views();
        } else {
            app.restore_views(&cc.egui_ctx);
        }
        app.dock = ui_dock::restore_layout(
            &app.state.dock_layout,
            &app.state.view_refs,
            app.state.selected_view,
        );
        app.restoring_views = false;
        app.request("catalog", json!({}), Purpose::Catalog);
        app
    }
    fn log(&mut self, value: impl Into<String>) {
        self.console.push(value.into());
        if self.console.len() > 250 {
            self.console.remove(0);
        }
    }
    fn section(ui: &mut egui::Ui, label: &str) {
        ui.add_space(3.);
        ui.label(RichText::new(label).strong().size(11.));
        ui.separator();
    }
    fn top(&mut self, ctx: &egui::Context) {
        egui::TopBottomPanel::top("menus")
            .exact_height(28.)
            .show(ctx, |ui| {
                egui::MenuBar::new().ui(ui, |ui| {
                    ui.label(
                        RichText::new("GC Protein Engineering Console")
                            .strong()
                            .size(12.),
                    );
                    ui.separator();
                    ui.menu_button("File", |ui| {
                        if ui.button("Add input files…").clicked() {
                            self.choose_files(Pick::Inputs, ctx);
                            ui.close();
                        }
                        if ui.button("Open local structure…").clicked() {
                            self.choose_files(Pick::Structure, ctx);
                            ui.close();
                        }
                        if ui
                            .button("Design binders against active structure")
                            .clicked()
                        {
                            self.binder_use_active();
                            ui.close();
                        }
                        if ui.button("Library archive").clicked() {
                            self.open_library_archive();
                            ui.close();
                        }
                        if ui.button("Experimental Cas9 demo").clicked() {
                            self.demo_views();
                            ui.close();
                        }
                        if ui.button("Save local session").clicked() {
                            self.persist();
                            ui.close();
                        }
                        if ui.button("Connection settings…").clicked() {
                            self.connection_open = true;
                            ui.close();
                        }
                        ui.separator();
                        if ui.button("Quit").clicked() {
                            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                        }
                    });
                    ui.menu_button("View", |ui| {
                        if ui.button("Duplicate tab").clicked() {
                            self.duplicate_view(self.state.selected_view, ctx);
                            ui.close();
                        }
                        if ui.button("Split right").clicked() {
                            self.split_active(egui_dock::Split::Right, ctx);
                            ui.close();
                        }
                        if ui.button("Split down").clicked() {
                            self.split_active(egui_dock::Split::Below, ctx);
                            ui.close();
                        }
                        if ui.button("Close selected tab").clicked() {
                            self.close_view(self.state.selected_view);
                            ui.close();
                        }
                        ui.checkbox(&mut self.state.link_views, "Link cameras");
                        ui.checkbox(&mut self.state.show_axes, "Orientation axes");
                    });
                    ui.menu_button("Run", |ui| {
                        if ui.button("Run").clicked() {
                            self.begin_run();
                            ui.close();
                        }
                        if ui.button("Run history").clicked() {
                            self.sidebar_tab = 1;
                            ui.close();
                        }
                        if ui.button("Construct library").clicked() {
                            self.open_library();
                            ui.close();
                        }
                    });
                    if ui.button("Help").clicked() {
                        self.help_open = true;
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.colored_label(
                            if self.connected { GREEN } else { AMBER },
                            if self.connected {
                                "HEAD CONNECTED"
                            } else {
                                "DISCONNECTED"
                            },
                        );
                        ui.separator();
                        self.worker_indicator(ui);
                    });
                });
            });
        egui::TopBottomPanel::top("toolbar")
            .exact_height(33.)
            .show(ctx, |ui| {
                if self.sidebar_tab == 2 {
                    self.library_toolbar(ui);
                    return;
                }
                ui.horizontal(|ui| {
                    if ui.button("Reset view").clicked() {
                        self.reset_view();
                    }
                    ui.separator();
                    if ui
                        .button("Split right")
                        .on_hover_text(
                            "Open an independent copy of the active tab in a group on the right.",
                        )
                        .clicked()
                    {
                        self.split_active(egui_dock::Split::Right, ctx);
                    }
                    if ui
                        .button("Split down")
                        .on_hover_text(
                            "Open an independent copy of the active tab in a group below.",
                        )
                        .clicked()
                    {
                        self.split_active(egui_dock::Split::Below, ctx);
                    }
                    ui.checkbox(&mut self.state.link_views, "Link cameras");
                    ui.checkbox(&mut self.state.show_axes, "Axes");
                    ui.separator();
                    if let Some(view) = self.views.get_mut(&self.state.selected_view) {
                        egui::ComboBox::from_id_salt("style")
                            .width(88.)
                            .selected_text(view.style.name())
                            .show_ui(ui, |ui| {
                                for style in [
                                    scene::Representation::Cartoon,
                                    scene::Representation::Sticks,
                                    scene::Representation::Spheres,
                                    scene::Representation::Surface,
                                    scene::Representation::Trace,
                                ] {
                                    ui.selectable_value(&mut view.style, style, style.name());
                                }
                            });
                    }
                    if ui.button("Export…").clicked() {
                        self.export_active(ctx);
                    }
                    if ui
                        .add_enabled(!self.pymol.active(), egui::Button::new("Launch in PyMOL"))
                        .clicked()
                    {
                        self.pymol_active();
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("Connection…").clicked() {
                            self.connection_open = true;
                        }
                        ui.small(&self.connection.host);
                    });
                });
            });
    }
    fn console_panel(&mut self, ctx: &egui::Context) {
        egui::TopBottomPanel::bottom("status")
            .exact_height(24.)
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.colored_label(
                        if self.connected { GREEN } else { AMBER },
                        if self.connected {
                            "CONNECTED"
                        } else {
                            "OFFLINE"
                        },
                    );
                    ui.separator();
                    ui.small(format!("{} client requests", self.pending.len()));
                    ui.separator();
                    ui.small(self.pymol.status);
                    if !self.save_error.is_empty() {
                        ui.colored_label(RED, format!("Local save: {}", self.save_error));
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.small(format!("Native Rust / v{}", env!("CARGO_PKG_VERSION")));
                    });
                });
            });
        egui::TopBottomPanel::bottom("console")
            .resizable(true)
            .default_height(164.)
            .min_height(70.)
            .show(ctx, |ui| {
                // Retain the dragged panel height even when its contents use less space.
                ui.take_available_height();
                ui.horizontal_wrapped(|ui| {
                    ui.selectable_value(&mut self.console_tab, 0, "CONSOLE");
                    ui.selectable_value(&mut self.console_tab, 1, "JOB LOG");
                    if ui.button("Clear local log").clicked() {
                        self.console.clear();
                    }
                    ui.separator();
                    for (command, tooltip) in [
                        (
                            "reset",
                            "Fit the selected structure; also fit linked cameras.",
                        ),
                        ("cartoon", "Show the selected structure as a cartoon."),
                        ("sticks", "Show the selected structure as sticks."),
                        ("spheres", "Show the selected structure as spheres."),
                        ("surface", "Show the solvent-accessible molecular surface."),
                        ("trace", "Show the selected structure as a backbone trace."),
                        ("help", "Open GC Protein Engineering Console help."),
                    ] {
                        let view = self.views.get(&self.state.selected_view);
                        let selected = view.is_some_and(|view| {
                            view.style.name() == command
                                || (command == "trace"
                                    && view.style == scene::Representation::Trace)
                        });
                        if ui
                            .add_enabled(
                                command == "help" || view.is_some(),
                                egui::Button::new(command).small().selected(selected),
                            )
                            .on_hover_text(tooltip)
                            .clicked()
                        {
                            self.console_command(command);
                        }
                    }
                });
                egui::ScrollArea::both()
                    .id_salt("console-output")
                    .auto_shrink([false, false])
                    .max_height((ui.available_height() - 28.).max(20.))
                    .stick_to_bottom(true)
                    .show(ui, |ui| {
                        if self.console_tab == 1 {
                            ui.monospace(if self.job_log.is_empty() {
                                "Select a job's Log button to follow its output."
                            } else {
                                &self.job_log
                            });
                        } else {
                            for (i, line) in self.console.iter().enumerate() {
                                ui.horizontal(|ui| {
                                    ui.weak(format!("{:03}", i + 1));
                                    ui.monospace(line);
                                });
                            }
                        }
                    });
                ui.horizontal(|ui| {
                    ui.colored_label(GREEN, "bio>");
                    let response = ui.add(
                        egui::TextEdit::singleline(&mut self.console_input)
                            .font(egui::TextStyle::Monospace)
                            .desired_width(f32::INFINITY),
                    );
                    if response.lost_focus()
                        && ui.input(|input| input.key_pressed(egui::Key::Enter))
                    {
                        let command = std::mem::take(&mut self.console_input);
                        self.console_command(&command);
                        response.request_focus();
                    }
                });
            });
    }
    fn console_command(&mut self, command: &str) {
        self.console_tab = 0;
        self.log(format!("bio> {command}"));
        match command.trim() {
            "reset" => self.reset_view(),
            "help" => {
                self.help_open = true;
                self.log("View commands: reset, cartoon, sticks, spheres, surface, trace, help. Start cloud jobs with the Run button.");
            }
            name => {
                if let Some(style) = [
                    scene::Representation::Cartoon,
                    scene::Representation::Sticks,
                    scene::Representation::Spheres,
                    scene::Representation::Surface,
                    scene::Representation::Trace,
                ]
                .into_iter()
                .find(|style| {
                    style.name() == name
                        || (name == "trace" && *style == scene::Representation::Trace)
                }) {
                    if let Some(view) = self.views.get_mut(&self.state.selected_view) {
                        view.style = style;
                    }
                } else {
                    self.log("Unknown local view command.");
                }
            }
        }
    }
}
impl eframe::App for Workbench {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        for renderer in self.retired_renderers.drain(..) {
            renderer.destroy(&self.gl);
        }
        self.events(ctx);
        self.poll();
        if self.sidebar_tab != 2
            && ctx.input_mut(|input| input.consume_key(egui::Modifiers::COMMAND, egui::Key::W))
        {
            self.close_view(self.state.selected_view);
        }
        let dropped = ctx.input(|input| input.raw.dropped_files.clone());
        if !dropped.is_empty() {
            self.picked(
                Pick::Inputs,
                dropped.into_iter().filter_map(|file| file.path).collect(),
                ctx,
            );
        }
        self.top(ctx);
        self.console_panel(ctx);
        self.binder_results_panel(ctx);
        egui::SidePanel::left("inputs")
            .resizable(true)
            .default_width(270.)
            .width_range(225.0..=500.0)
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.sidebar_tab, 0, "Inputs");
                    ui.selectable_value(&mut self.sidebar_tab, 1, "Runs");
                    if ui
                        .selectable_label(self.sidebar_tab == 2, "Library")
                        .clicked()
                    {
                        self.open_library();
                    }
                });
                ui.separator();
                egui::ScrollArea::vertical()
                    .id_salt("left-scroll")
                    .show(ui, |ui| {
                        if self.sidebar_tab == 0 {
                            self.inputs_panel(ui, ctx);
                        } else if self.sidebar_tab == 1 {
                            self.jobs_panel(ui, ctx);
                        } else {
                            self.library_sidebar(ui);
                        }
                    });
            });
        if self.sidebar_tab != 2 {
            egui::SidePanel::right("inspector")
                .resizable(true)
                .default_width(285.)
                .width_range(235.0..=520.0)
                .show(ctx, |ui| {
                    egui::ScrollArea::vertical()
                        .id_salt("right-scroll")
                        .show(ui, |ui| {
                            if self.binder.draft.enabled {
                                self.binder_inspector(ui);
                            }
                            self.structure_links_panel(ui);
                            self.domains_panel(ui);
                            self.inspector(ui, ctx);
                        });
                });
        }
        egui::CentralPanel::default()
            .frame(egui::Frame::NONE.fill(if self.sidebar_tab == 2 {
                Color32::from_rgb(33, 35, 38)
            } else {
                Color32::from_rgb(3, 5, 7)
            }))
            .show(ctx, |ui| {
                if self.sidebar_tab == 2 {
                    self.library_details(ui, ctx);
                } else {
                    self.viewports(ui);
                }
            });
        self.connection_dialog(ctx);
        self.worker_dialog(ctx);
        self.run_dialog(ctx);
        self.binder_progress_dialog(ctx);
        self.binder_save_dialog(ctx);
        self.binder_target_dialog(ctx);
        self.domains_dialog(ctx);
        self.library_structure_dialog(ctx);
        self.settings_dialog(ctx);
        self.text_dialog(ctx);
        let mut help = self.help_open;
        egui::Window::new("GC Protein Engineering Console help").open(&mut help).show(ctx,|ui|{
            ui.label("Paste or load inputs, select models, and click Run. Compatible jobs queue automatically; follow their progress in Run status or history.");
            ui.label("Jobs and artifacts live on the head. Closing this client does not cancel them. Use the explicit job/batch Cancel controls.");
            ui.label("Click a run or structure under Runs / results to open its tab. Drag tabs to reorder, onto another tab bar to group, or to a viewer edge to split. Close a tab with its left X. Linking cameras does not align structures.");
            ui.label("Left drag rotates; right drag pans; wheel zooms; double-click fits. Right-click for lighting. Annotation edits remain local until Save to head.");
            ui.label("The bottom-bar buttons and bio> commands change the selected structure: cartoon, sticks, spheres, surface, or trace. Reset fits the selected structure and any linked cameras; help opens this window.");
            ui.label("Startup Cas9 is experimental 4OO8, not a prediction. GPU lighting uses rasterization; confidence/chemistry QA comes only from native metadata.");
        });
        self.help_open = help;
        if self.last_save.elapsed() > Duration::from_secs(2) {
            self.last_save = Instant::now();
            self.persist();
        }
        ctx.request_repaint_after(Duration::from_millis(if self.pending.is_empty() {
            500
        } else {
            100
        }));
    }
    fn on_exit(&mut self, gl: Option<&eframe::glow::Context>) {
        self.persist();
        if let Some(gl) = gl {
            for renderer in self.retired_renderers.drain(..) {
                renderer.destroy(gl);
            }
            for view in self.views.values() {
                view.renderer.destroy(gl);
            }
        }
    }
}
fn main() -> eframe::Result {
    if std::env::args_os()
        .nth(1)
        .is_some_and(|arg| arg == "--render")
    {
        if let Err(error) = render::run(std::env::args_os().skip(2).collect()) {
            eprintln!("GC Protein Engineering Console render: {error}");
            std::process::exit(1);
        }
        return Ok(());
    }
    let mut navigation = match navigation::start(std::env::args_os().skip(1)) {
        Ok(navigation::Launch::Forwarded) => return Ok(()),
        Ok(navigation::Launch::Primary(navigation)) => navigation,
        Err(error) => {
            eprintln!("GC Protein Engineering Console: {error}");
            return Ok(());
        }
    };
    let receiver = navigation.take_receiver();
    let wake = navigation.wake_handle();
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_app_id("bio-workbench")
            .with_inner_size([1500., 960.])
            .with_min_inner_size([1100., 720.]),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        &format!(
            "GC Protein Engineering Console {}",
            env!("CARGO_PKG_VERSION")
        ),
        options,
        Box::new(move |cc| {
            wake.attach(cc.egui_ctx.clone());
            Ok(Box::new(Workbench::new(cc, receiver)))
        }),
    )
}
