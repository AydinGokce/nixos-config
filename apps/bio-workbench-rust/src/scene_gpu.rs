//! Native OpenGL rasterizer. The UI callback owns no transient molecular meshes.
//! Lighting/contact shading are presentation effects, not ray tracing or analysis.
use super::{Camera, Molecule, Representation, ResidueColors, V3, geometry, surface};
use eframe::{egui, egui_glow, glow};
use glow::HasContext as _;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};

// Only one CPU surface builder runs in this process. Other visible surface tabs
// retry without allocating a grid or spawning waiting threads.
static SURFACE_BUILDING: AtomicBool = AtomicBool::new(false);
struct SurfacePermit;
impl Drop for SurfacePermit {
    fn drop(&mut self) {
        SURFACE_BUILDING.store(false, Ordering::Release);
    }
}
struct SurfaceWork {
    receiver: mpsc::Receiver<(Result<surface::Surface, String>, SurfacePermit)>,
    cancel: Arc<AtomicBool>,
}

struct Mesh {
    vao: glow::VertexArray,
    vertices: glow::Buffer,
    indices: glow::Buffer,
    count: i32,
}
struct Atoms {
    vao: glow::VertexArray,
    instances: glow::Buffer,
    count: i32,
}
struct Chain {
    cartoon: Mesh,
    trace: Mesh,
    sticks: Mesh,
    atoms: Atoms,
    details: Atoms,
}
struct Target {
    fbo: glow::Framebuffer,
    color: glow::Texture,
    normal_depth: glow::Texture,
    residue: glow::Texture,
    depth: glow::Renderbuffer,
    size: [i32; 2],
}
struct ResiduePalette {
    // Index zero is background; all geometry uses one-based residue IDs. A
    // transparent texel means preserve the original chain/element material.
    texels: Vec<[u8; 4]>,
    count: usize,
    active: bool,
    dirty: bool,
}
impl ResiduePalette {
    fn new(count: usize) -> Self {
        Self {
            texels: vec![[0; 4]; (count + 1).div_ceil(256).max(1) * 256],
            count,
            active: false,
            dirty: false,
        }
    }
    fn update(&mut self, molecule: &Molecule, colors: &ResidueColors) {
        if colors.is_empty() || molecule.residues.len() != self.count {
            if self.active {
                self.texels.fill([0; 4]);
                self.active = false;
                self.dirty = true;
            }
            return;
        }
        let mut active = false;
        for (residue, texel) in molecule.residues.iter().zip(&mut self.texels[1..]) {
            let color = colors
                .get(&residue.key)
                .map_or([0; 4], |&[r, g, b]| [r, g, b, 255]);
            active |= color[3] != 0;
            if *texel != color {
                *texel = color;
                self.dirty = true;
            }
        }
        self.active = active;
    }
    fn color(&self, index: usize) -> Option<[u8; 3]> {
        if index >= self.count {
            return None;
        }
        let [r, g, b, alpha] = self.texels[index + 1];
        (alpha != 0).then_some([r, g, b])
    }
}
pub(super) struct Renderer {
    mesh_program: glow::Program,
    atom_program: glow::Program,
    post_program: glow::Program,
    empty_vao: glow::VertexArray,
    chains: Vec<Chain>,
    surface: Option<Vec<Mesh>>,
    surface_work: Option<SurfaceWork>,
    surface_message: String,
    surface_failed: bool,
    highlights: glow::Texture,
    highlight_ids: Vec<usize>,
    selection_ids: Vec<usize>,
    colors: glow::Texture,
    palette: ResiduePalette,
    residue_count: usize,
    pick_request: Option<[f32; 2]>,
    pick_result: Option<Option<usize>>,
    target: Option<Target>,
    pub(super) error: Option<String>,
    destroyed: bool,
}

// Creation guards free partial allocations when a driver rejects a resource.
#[derive(Default)]
struct Allocations {
    vaos: Vec<glow::VertexArray>,
    buffers: Vec<glow::Buffer>,
    textures: Vec<glow::Texture>,
    fbos: Vec<glow::Framebuffer>,
    renderbuffers: Vec<glow::Renderbuffer>,
    programs: Vec<glow::Program>,
}
struct Pending<'a> {
    gl: &'a glow::Context,
    resources: Allocations,
    committed: bool,
}
impl<'a> Pending<'a> {
    fn new(gl: &'a glow::Context) -> Self {
        Self {
            gl,
            resources: Allocations::default(),
            committed: false,
        }
    }
    fn vao(&mut self) -> Result<glow::VertexArray, String> {
        let value = unsafe { self.gl.create_vertex_array()? };
        self.resources.vaos.push(value);
        Ok(value)
    }
    fn buffer(&mut self) -> Result<glow::Buffer, String> {
        let value = unsafe { self.gl.create_buffer()? };
        self.resources.buffers.push(value);
        Ok(value)
    }
    fn texture(&mut self) -> Result<glow::Texture, String> {
        let value = unsafe { self.gl.create_texture()? };
        self.resources.textures.push(value);
        Ok(value)
    }
    fn fbo(&mut self) -> Result<glow::Framebuffer, String> {
        let value = unsafe { self.gl.create_framebuffer()? };
        self.resources.fbos.push(value);
        Ok(value)
    }
    fn renderbuffer(&mut self) -> Result<glow::Renderbuffer, String> {
        let value = unsafe { self.gl.create_renderbuffer()? };
        self.resources.renderbuffers.push(value);
        Ok(value)
    }
    fn keep_program(&mut self, value: glow::Program) -> glow::Program {
        self.resources.programs.push(value);
        value
    }
}
impl Drop for Pending<'_> {
    fn drop(&mut self) {
        if !self.committed {
            unsafe {
                for &value in &self.resources.vaos {
                    self.gl.delete_vertex_array(value);
                }
                for &value in &self.resources.buffers {
                    self.gl.delete_buffer(value);
                }
                for &value in &self.resources.textures {
                    self.gl.delete_texture(value);
                }
                for &value in &self.resources.fbos {
                    self.gl.delete_framebuffer(value);
                }
                for &value in &self.resources.renderbuffers {
                    self.gl.delete_renderbuffer(value);
                }
                for &value in &self.resources.programs {
                    self.gl.delete_program(value);
                }
            }
        }
    }
}

// All GL calls run on eframe's current GL context, including cleanup in on_exit.
impl Mesh {
    fn new(
        gl: &glow::Context,
        mesh: &geometry::Mesh,
        pending: &mut Pending<'_>,
    ) -> Result<Self, String> {
        unsafe {
            let vao = pending.vao()?;
            let vertices = pending.buffer()?;
            let indices = pending.buffer()?;
            gl.bind_vertex_array(Some(vao));
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vertices));
            gl.buffer_data_u8_slice(
                glow::ARRAY_BUFFER,
                bytemuck::cast_slice(&mesh.vertices),
                glow::STATIC_DRAW,
            );
            gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(indices));
            gl.buffer_data_u8_slice(
                glow::ELEMENT_ARRAY_BUFFER,
                bytemuck::cast_slice(&mesh.indices),
                glow::STATIC_DRAW,
            );
            for (location, size, offset) in [(0, 3, 0), (1, 3, 12), (2, 3, 24), (3, 1, 36)] {
                gl.enable_vertex_attrib_array(location);
                gl.vertex_attrib_pointer_f32(location, size, glow::FLOAT, false, 40, offset);
            }
            gl.bind_vertex_array(None);
            Ok(Self {
                vao,
                vertices,
                indices,
                count: mesh.indices.len() as i32,
            })
        }
    }
    fn draw(&self, gl: &glow::Context) {
        unsafe {
            gl.bind_vertex_array(Some(self.vao));
            gl.draw_elements(glow::TRIANGLES, self.count, glow::UNSIGNED_INT, 0);
        }
    }
    fn destroy(&self, gl: &glow::Context) {
        unsafe {
            gl.delete_vertex_array(self.vao);
            gl.delete_buffer(self.vertices);
            gl.delete_buffer(self.indices);
        }
    }
}
impl Atoms {
    fn new(
        gl: &glow::Context,
        data: &[[f32; 8]],
        pending: &mut Pending<'_>,
    ) -> Result<Self, String> {
        unsafe {
            let vao = pending.vao()?;
            let instances = pending.buffer()?;
            gl.bind_vertex_array(Some(vao));
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(instances));
            gl.buffer_data_u8_slice(
                glow::ARRAY_BUFFER,
                bytemuck::cast_slice(data),
                glow::STATIC_DRAW,
            );
            for (location, offset) in [(0, 0), (1, 16)] {
                gl.enable_vertex_attrib_array(location);
                gl.vertex_attrib_pointer_f32(location, 4, glow::FLOAT, false, 32, offset);
                gl.vertex_attrib_divisor(location, 1);
            }
            gl.bind_vertex_array(None);
            Ok(Self {
                vao,
                instances,
                count: data.len() as i32,
            })
        }
    }
    fn draw(&self, gl: &glow::Context) {
        unsafe {
            gl.bind_vertex_array(Some(self.vao));
            gl.draw_arrays_instanced(glow::TRIANGLES, 0, 6, self.count);
        }
    }
    fn destroy(&self, gl: &glow::Context) {
        unsafe {
            gl.delete_vertex_array(self.vao);
            gl.delete_buffer(self.instances);
        }
    }
}
impl Target {
    fn new(gl: &glow::Context, size: [i32; 2]) -> Result<Self, String> {
        let mut pending = Pending::new(gl);
        unsafe {
            let fbo = pending.fbo()?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            let color = pending.texture()?;
            let normal_depth = pending.texture()?;
            let residue = pending.texture()?;
            for (texture, attachment, filter) in [
                (color, glow::COLOR_ATTACHMENT0, glow::LINEAR),
                (normal_depth, glow::COLOR_ATTACHMENT1, glow::NEAREST),
            ] {
                gl.bind_texture(glow::TEXTURE_2D, Some(texture));
                gl.tex_image_2d(
                    glow::TEXTURE_2D,
                    0,
                    glow::RGBA16F as i32,
                    size[0],
                    size[1],
                    0,
                    glow::RGBA,
                    glow::FLOAT,
                    glow::PixelUnpackData::Slice(None),
                );
                gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, filter as i32);
                gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, filter as i32);
                gl.tex_parameter_i32(
                    glow::TEXTURE_2D,
                    glow::TEXTURE_WRAP_S,
                    glow::CLAMP_TO_EDGE as i32,
                );
                gl.tex_parameter_i32(
                    glow::TEXTURE_2D,
                    glow::TEXTURE_WRAP_T,
                    glow::CLAMP_TO_EDGE as i32,
                );
                gl.framebuffer_texture_2d(
                    glow::FRAMEBUFFER,
                    attachment,
                    glow::TEXTURE_2D,
                    Some(texture),
                    0,
                );
            }
            gl.bind_texture(glow::TEXTURE_2D, Some(residue));
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::R32F as i32,
                size[0],
                size[1],
                0,
                glow::RED,
                glow::FLOAT,
                glow::PixelUnpackData::Slice(None),
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MIN_FILTER,
                glow::NEAREST as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MAG_FILTER,
                glow::NEAREST as i32,
            );
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER,
                glow::COLOR_ATTACHMENT2,
                glow::TEXTURE_2D,
                Some(residue),
                0,
            );
            let depth = pending.renderbuffer()?;
            gl.bind_renderbuffer(glow::RENDERBUFFER, Some(depth));
            gl.renderbuffer_storage(
                glow::RENDERBUFFER,
                glow::DEPTH_COMPONENT24,
                size[0],
                size[1],
            );
            gl.framebuffer_renderbuffer(
                glow::FRAMEBUFFER,
                glow::DEPTH_ATTACHMENT,
                glow::RENDERBUFFER,
                Some(depth),
            );
            gl.draw_buffers(&[
                glow::COLOR_ATTACHMENT0,
                glow::COLOR_ATTACHMENT1,
                glow::COLOR_ATTACHMENT2,
            ]);
            let result = Self {
                fbo,
                color,
                normal_depth,
                residue,
                depth,
                size,
            };
            if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
                return Err(
                    "OpenGL does not support the molecular renderer's HDR/depth framebuffer".into(),
                );
            }
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            pending.committed = true;
            Ok(result)
        }
    }
    fn destroy(&self, gl: &glow::Context) {
        unsafe {
            gl.delete_framebuffer(self.fbo);
            gl.delete_texture(self.color);
            gl.delete_texture(self.normal_depth);
            gl.delete_texture(self.residue);
            gl.delete_renderbuffer(self.depth);
        }
    }
}
fn program(
    gl: &glow::Context,
    vertex: &str,
    fragment: &str,
    attributes: &[&str],
) -> Result<glow::Program, String> {
    let embedded = egui_glow::ShaderVersion::get(gl).is_embedded();
    let version = if embedded {
        "#version 300 es\nprecision highp float;\nprecision highp int;\n#define OUT0 layout(location=0)\n#define OUT1 layout(location=1)\n#define OUT2 layout(location=2)\n"
    } else {
        "#version 140\n#define OUT0\n#define OUT1\n#define OUT2\n"
    };
    unsafe {
        let program = gl.create_program()?;
        for (kind, source) in [
            (glow::VERTEX_SHADER, vertex),
            (glow::FRAGMENT_SHADER, fragment),
        ] {
            let shader = gl.create_shader(kind)?;
            gl.shader_source(shader, &format!("{version}{source}"));
            gl.compile_shader(shader);
            if !gl.get_shader_compile_status(shader) {
                let message = gl.get_shader_info_log(shader);
                gl.delete_shader(shader);
                gl.delete_program(program);
                return Err(format!("Molecular shader compilation: {message}"));
            }
            gl.attach_shader(program, shader);
            gl.delete_shader(shader);
        }
        for (index, name) in attributes.iter().enumerate() {
            gl.bind_attrib_location(program, index as u32, name);
        }
        if !embedded {
            gl.bind_frag_data_location(program, 0, "out_color");
            gl.bind_frag_data_location(program, 1, "out_normal");
            gl.bind_frag_data_location(program, 2, "out_residue");
        }
        gl.link_program(program);
        if !gl.get_program_link_status(program) {
            let message = gl.get_program_info_log(program);
            gl.delete_program(program);
            return Err(message);
        }
        Ok(program)
    }
}
impl Renderer {
    pub(super) fn new(gl: &glow::Context, molecule: &Molecule) -> Result<Self, String> {
        let geometry = geometry::build(molecule);
        let mut pending = Pending::new(gl);
        let mut chains = Vec::new();
        for chain in &geometry {
            chains.push(Chain {
                cartoon: Mesh::new(gl, &chain.cartoon, &mut pending)?,
                trace: Mesh::new(gl, &chain.trace, &mut pending)?,
                sticks: Mesh::new(gl, &chain.sticks, &mut pending)?,
                atoms: Atoms::new(gl, &chain.atoms, &mut pending)?,
                details: Atoms::new(gl, &chain.details, &mut pending)?,
            });
        }
        let mesh_program = program(
            gl,
            &format!("{CAMERA}\n{MESH_VERTEX}"),
            &format!("{SURFACE}\n{MESH_FRAGMENT}"),
            &["a_position", "a_normal", "a_color", "a_residue"],
        )?;
        pending.keep_program(mesh_program);
        let atom_program = program(
            gl,
            &format!("{CAMERA}\n{ATOM_VERTEX}"),
            &format!("{CAMERA}\n{SURFACE}\n{ATOM_FRAGMENT}"),
            &["a_center_radius", "a_color_residue"],
        )?;
        pending.keep_program(atom_program);
        let post_program = program(gl, POST_VERTEX, POST_FRAGMENT, &[])?;
        pending.keep_program(post_program);
        let empty_vao = pending.vao()?;
        let highlights = pending.texture()?;
        let colors = pending.texture()?;
        let palette = ResiduePalette::new(molecule.residues.len());
        unsafe {
            gl.bind_texture(glow::TEXTURE_2D, Some(highlights));
            let rows = (molecule.residues.len() + 1).div_ceil(256).max(1);
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RG8 as i32,
                256,
                rows as i32,
                0,
                glow::RG,
                glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(Some(&vec![0; 2 * 256 * rows])),
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MIN_FILTER,
                glow::NEAREST as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MAG_FILTER,
                glow::NEAREST as i32,
            );
            gl.bind_texture(glow::TEXTURE_2D, Some(colors));
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RGBA8 as i32,
                256,
                rows as i32,
                0,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(Some(bytemuck::cast_slice(&palette.texels))),
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MIN_FILTER,
                glow::NEAREST as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MAG_FILTER,
                glow::NEAREST as i32,
            );
            gl.bind_texture(glow::TEXTURE_2D, None);
        }
        pending.committed = true;
        Ok(Self {
            mesh_program,
            atom_program,
            post_program,
            empty_vao,
            chains,
            surface: None,
            surface_work: None,
            surface_message: "Surface has not been requested".into(),
            surface_failed: false,
            highlights,
            highlight_ids: Vec::new(),
            selection_ids: Vec::new(),
            colors,
            palette,
            residue_count: molecule.residues.len(),
            pick_request: None,
            pick_result: None,
            target: None,
            error: None,
            destroyed: false,
        })
    }
    pub(super) fn request_surface(&mut self, molecule: &Molecule) {
        if self.destroyed
            || self.surface.is_some()
            || self.surface_work.is_some()
            || self.surface_failed
        {
            return;
        }
        if SURFACE_BUILDING
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            self.surface_message = "Waiting for the molecular surface builder…".into();
            return;
        }
        let input = match surface::Input::new(molecule) {
            Ok(input) => input,
            Err(error) => {
                SURFACE_BUILDING.store(false, Ordering::Release);
                self.surface_message = error;
                self.surface_failed = true;
                return;
            }
        };
        let (sender, receiver) = mpsc::sync_channel(1);
        let cancel = Arc::new(AtomicBool::new(false));
        let flag = cancel.clone();
        match std::thread::Builder::new()
            .name("molecular-surface".into())
            .spawn(move || {
                let permit = SurfacePermit;
                let result = surface::build(input, &flag);
                // Retain the permit until upload/disposal, bounding completed CPU
                // meshes as well as active grids even if the user hides this tab.
                let _ = sender.send((result, permit));
            }) {
            Ok(_) => {
                self.surface_work = Some(SurfaceWork { receiver, cancel });
                self.surface_message = "Building molecular surface…".into();
            }
            Err(error) => {
                SURFACE_BUILDING.store(false, Ordering::Release);
                self.surface_failed = true;
                self.surface_message = format!("Cannot start molecular surface builder: {error}");
            }
        }
    }
    pub(super) fn surface_ready(&self) -> bool {
        self.surface.is_some()
    }
    pub(super) fn surface_message(&self) -> &str {
        &self.surface_message
    }
    pub(super) fn surface_error(&self) -> Option<&str> {
        self.surface_failed.then_some(&self.surface_message)
    }
    pub(super) fn request_pick(&mut self, at: [f32; 2]) {
        self.pick_request = Some(at);
    }
    pub(super) fn take_pick(&mut self) -> Option<Option<usize>> {
        self.pick_result.take()
    }
    pub(super) fn set_residue_colors(&mut self, molecule: &Molecule, colors: &ResidueColors) {
        if !self.destroyed {
            self.palette.update(molecule, colors);
        }
    }
    pub(super) fn residue_color(&self, index: usize) -> Option<[u8; 3]> {
        self.palette.color(index)
    }
    pub(super) fn accept_surface(&mut self, gl: &glow::Context) {
        let Some(work) = self.surface_work.as_ref() else {
            return;
        };
        let (result, _permit) = match work.receiver.try_recv() {
            Ok((result, permit)) => (result, Some(permit)),
            Err(mpsc::TryRecvError::Empty) => return,
            Err(mpsc::TryRecvError::Disconnected) => (
                Err("Molecular surface builder stopped before returning geometry".into()),
                None,
            ),
        };
        self.surface_work = None;
        match result {
            Ok(surface) => {
                let mut pending = Pending::new(gl);
                let meshes: Result<Vec<_>, _> = surface
                    .chains
                    .iter()
                    .map(|mesh| Mesh::new(gl, mesh, &mut pending))
                    .collect();
                match meshes {
                    Ok(meshes) => {
                        pending.committed = true;
                        self.surface = Some(meshes);
                        self.surface_message = format!(
                            "SAS · 1.4 Å probe · {:.2} Å grid · {} triangles",
                            surface.spacing, surface.triangles
                        );
                    }
                    Err(error) => {
                        self.surface_failed = true;
                        self.surface_message = format!("Surface GPU upload: {error}");
                    }
                }
            }
            Err(error) => {
                self.surface_failed = true;
                self.surface_message = error;
            }
        }
    }
    pub(super) fn destroy(&mut self, gl: &glow::Context) {
        if self.destroyed {
            return;
        }
        if let Some(work) = self.surface_work.take() {
            work.cancel.store(true, Ordering::Relaxed);
        }
        if let Some(surface) = self.surface.take() {
            for mesh in surface {
                mesh.destroy(gl);
            }
        }
        for chain in &self.chains {
            chain.cartoon.destroy(gl);
            chain.trace.destroy(gl);
            chain.sticks.destroy(gl);
            chain.atoms.destroy(gl);
            chain.details.destroy(gl);
        }
        if let Some(target) = &self.target {
            target.destroy(gl);
        }
        unsafe {
            gl.delete_program(self.mesh_program);
            gl.delete_program(self.atom_program);
            gl.delete_program(self.post_program);
            gl.delete_vertex_array(self.empty_vao);
            gl.delete_texture(self.highlights);
            gl.delete_texture(self.colors);
        }
        self.destroyed = true;
    }
    #[allow(clippy::too_many_arguments)]
    pub(super) fn paint(
        &mut self,
        gl: &glow::Context,
        info: egui::PaintCallbackInfo,
        destination: Option<glow::Framebuffer>,
        camera: Camera,
        representation: Representation,
        _index: usize,
        selected: usize,
        chains: &[bool],
        hotspots: &[usize],
        selection: &[usize],
    ) {
        if self.destroyed || self.error.is_some() {
            return;
        }
        self.accept_surface(gl);
        let viewport = info.viewport_in_pixels();
        if viewport.width_px < 1 || viewport.height_px < 1 {
            return;
        }
        // Two samples per axis, bounded for unusually large/HiDPI windows.
        let max_side = unsafe { gl.get_parameter_i32(glow::MAX_TEXTURE_SIZE) }.min(4096) as f32;
        let pixel_budget =
            (4_194_304. / (viewport.width_px as f32 * viewport.height_px as f32)).sqrt();
        let scale = 2.0_f32
            .min(max_side / viewport.width_px.max(viewport.height_px) as f32)
            .min(pixel_budget);
        let size = [
            (viewport.width_px as f32 * scale).max(1.) as i32,
            (viewport.height_px as f32 * scale).max(1.) as i32,
        ];
        if self
            .target
            .as_ref()
            .is_none_or(|target| target.size != size)
        {
            if let Some(old) = self.target.take() {
                old.destroy(gl);
            }
            match Target::new(gl, size) {
                Ok(target) => self.target = Some(target),
                Err(error) => {
                    unsafe {
                        gl.bind_framebuffer(glow::FRAMEBUFFER, destination);
                    }
                    self.error = Some(error);
                    return;
                }
            }
        }
        let target = self.target.as_ref().expect("render target");
        let rect = info.viewport;
        let units = rect.width().min(rect.height()) / camera.span * camera.zoom;
        let projection = [
            2. * camera.distance * units / rect.width(),
            2. * camera.distance * units / rect.height(),
            2. * camera.pan.x / rect.width(),
            -2. * camera.pan.y / rect.height(),
        ];
        let basis = [
            camera.rotate(V3(1., 0., 0.)),
            camera.rotate(V3(0., 1., 0.)),
            camera.rotate(V3(0., 0., 1.)),
        ];
        let rotation = [
            basis[0].0, basis[0].1, basis[0].2, basis[1].0, basis[1].1, basis[1].2, basis[2].0,
            basis[2].1, basis[2].2,
        ];
        unsafe {
            gl.active_texture(glow::TEXTURE2);
            gl.bind_texture(glow::TEXTURE_2D, Some(self.highlights));
            if hotspots != self.highlight_ids || selection != self.selection_ids {
                let rows = (self.residue_count + 1).div_ceil(256).max(1);
                let mut data = vec![0u8; 2 * 256 * rows];
                for &id in hotspots {
                    if id > 0 && id <= self.residue_count {
                        data[2 * id] = 255;
                    }
                }
                for &id in selection {
                    if id > 0 && id <= self.residue_count {
                        data[2 * id + 1] = 255;
                    }
                }
                gl.tex_sub_image_2d(
                    glow::TEXTURE_2D,
                    0,
                    0,
                    0,
                    256,
                    rows as i32,
                    glow::RG,
                    glow::UNSIGNED_BYTE,
                    glow::PixelUnpackData::Slice(Some(&data)),
                );
                self.highlight_ids = hotspots.to_vec();
                self.selection_ids = selection.to_vec();
            }
            gl.active_texture(glow::TEXTURE3);
            gl.bind_texture(glow::TEXTURE_2D, Some(self.colors));
            if self.palette.dirty {
                gl.tex_sub_image_2d(
                    glow::TEXTURE_2D,
                    0,
                    0,
                    0,
                    256,
                    (self.palette.texels.len() / 256) as i32,
                    glow::RGBA,
                    glow::UNSIGNED_BYTE,
                    glow::PixelUnpackData::Slice(Some(bytemuck::cast_slice(&self.palette.texels))),
                );
                self.palette.dirty = false;
            }
            gl.disable(glow::SCISSOR_TEST);
            gl.disable(glow::BLEND);
            gl.disable(glow::CULL_FACE);
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(target.fbo));
            gl.viewport(0, 0, size[0], size[1]);
            gl.enable(glow::DEPTH_TEST);
            gl.depth_func(glow::LESS);
            gl.depth_mask(true);
            gl.clear_buffer_f32_slice(glow::COLOR, 0, &[0., 0., 0., 0.]);
            gl.clear_buffer_f32_slice(glow::COLOR, 1, &[0., 0., 0., 0.]);
            gl.clear_buffer_f32_slice(glow::COLOR, 2, &[0., 0., 0., 0.]);
            gl.clear_depth_f32(1.);
            gl.clear(glow::DEPTH_BUFFER_BIT);
            for program in [self.mesh_program, self.atom_program] {
                gl.use_program(Some(program));
                gl.uniform_matrix_3_f32_slice(
                    gl.get_uniform_location(program, "u_rotation").as_ref(),
                    false,
                    &rotation,
                );
                gl.uniform_4_f32_slice(
                    gl.get_uniform_location(program, "u_projection").as_ref(),
                    &projection,
                );
                gl.uniform_1_f32(
                    gl.get_uniform_location(program, "u_distance").as_ref(),
                    camera.distance,
                );
                gl.uniform_1_f32(
                    gl.get_uniform_location(program, "u_atmosphere").as_ref(),
                    camera.distance,
                );
                let near = (camera.distance * 0.005).max(0.02);
                let far = camera.distance * 10.;
                gl.uniform_2_f32(
                    gl.get_uniform_location(program, "u_clip").as_ref(),
                    (far + near) / (far - near),
                    -2. * far * near / (far - near),
                );
                gl.uniform_1_f32(
                    gl.get_uniform_location(program, "u_selected").as_ref(),
                    selected as f32,
                );
                gl.uniform_1_i32(gl.get_uniform_location(program, "u_hotspots").as_ref(), 2);
                gl.uniform_1_i32(
                    gl.get_uniform_location(program, "u_residue_colors")
                        .as_ref(),
                    3,
                );
            }
            gl.use_program(Some(self.mesh_program));
            for (index, (chain, visible)) in
                self.chains.iter().zip(chains.iter().copied()).enumerate()
            {
                if !visible {
                    continue;
                }
                match representation {
                    Representation::Cartoon => chain.cartoon.draw(gl),
                    Representation::Trace => chain.trace.draw(gl),
                    Representation::Sticks => chain.sticks.draw(gl),
                    Representation::Spheres => {}
                    Representation::Surface => {
                        if let Some(meshes) = &self.surface {
                            meshes[index].draw(gl);
                        }
                    }
                }
            }
            gl.use_program(Some(self.atom_program));
            gl.uniform_1_f32(
                gl.get_uniform_location(self.atom_program, "u_radius_scale")
                    .as_ref(),
                match representation {
                    Representation::Spheres => 1.,
                    Representation::Sticks => 0.14,
                    _ => 0.24,
                },
            );
            for (chain, visible) in self.chains.iter().zip(chains.iter().copied()) {
                if visible {
                    match representation {
                        Representation::Spheres | Representation::Sticks => chain.atoms.draw(gl),
                        Representation::Surface => {}
                        _ => chain.details.draw(gl),
                    }
                }
            }
            if let Some(at) = self.pick_request.take() {
                let x = (at[0] * size[0] as f32).floor() as i32;
                let y = ((1. - at[1]) * size[1] as f32).floor() as i32;
                let mut residue = 0f32;
                gl.read_buffer(glow::COLOR_ATTACHMENT2);
                gl.read_pixels(
                    x.clamp(0, size[0] - 1),
                    y.clamp(0, size[1] - 1),
                    1,
                    1,
                    glow::RED,
                    glow::FLOAT,
                    glow::PixelPackData::Slice(Some(bytemuck::bytes_of_mut(&mut residue))),
                );
                gl.read_buffer(glow::COLOR_ATTACHMENT0);
                self.pick_result = Some(
                    (residue.is_finite() && residue >= 1. && residue <= self.residue_count as f32)
                        .then(|| residue as usize - 1),
                );
            }
            // Return to egui's destination before compositing. Its painter will
            // restore program, viewport, VAO, blend and scissor state afterwards.
            gl.bind_framebuffer(glow::FRAMEBUFFER, destination);
            gl.viewport(
                viewport.left_px,
                viewport.from_bottom_px,
                viewport.width_px,
                viewport.height_px,
            );
            let clip = info.clip_rect_in_pixels();
            gl.enable(glow::SCISSOR_TEST);
            gl.scissor(
                clip.left_px,
                clip.from_bottom_px,
                clip.width_px,
                clip.height_px,
            );
            gl.disable(glow::DEPTH_TEST);
            gl.depth_mask(false);
            gl.use_program(Some(self.post_program));
            gl.uniform_1_f32(
                gl.get_uniform_location(self.post_program, "u_distance")
                    .as_ref(),
                camera.distance,
            );
            gl.bind_vertex_array(Some(self.empty_vao));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(target.color));
            gl.active_texture(glow::TEXTURE1);
            gl.bind_texture(glow::TEXTURE_2D, Some(target.normal_depth));
            gl.uniform_1_i32(
                gl.get_uniform_location(self.post_program, "u_color")
                    .as_ref(),
                0,
            );
            gl.uniform_1_i32(
                gl.get_uniform_location(self.post_program, "u_normal")
                    .as_ref(),
                1,
            );
            gl.uniform_2_f32(
                gl.get_uniform_location(self.post_program, "u_size")
                    .as_ref(),
                viewport.width_px as f32,
                viewport.height_px as f32,
            );
            gl.uniform_4_f32_slice(
                gl.get_uniform_location(self.post_program, "u_projection")
                    .as_ref(),
                &projection,
            );
            gl.uniform_1_f32(
                gl.get_uniform_location(self.post_program, "u_ambient")
                    .as_ref(),
                if camera.ambient { 1. } else { 0. },
            );
            gl.uniform_1_f32(
                gl.get_uniform_location(self.post_program, "u_bloom")
                    .as_ref(),
                if camera.bloom { 1. } else { 0. },
            );
            gl.draw_arrays(glow::TRIANGLES, 0, 3);
            gl.active_texture(glow::TEXTURE1);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.active_texture(glow::TEXTURE2);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.active_texture(glow::TEXTURE3);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, None);
            gl.bind_vertex_array(None);
            gl.depth_mask(true);
        }
    }
}

const CAMERA: &str = r#"
uniform mat3 u_rotation;
uniform vec4 u_projection;
uniform float u_distance;
uniform vec2 u_clip;
vec4 project(vec3 p) {
    float w=-p.z;
    return vec4(p.xy*u_projection.xy+u_projection.zw*w,u_clip.x*w+u_clip.y,w);
}
"#;
const MESH_VERTEX: &str = r#"
in vec3 a_position; in vec3 a_normal; in vec3 a_color; in float a_residue;
out vec3 v_position; out vec3 v_normal; out vec3 v_color; flat out float v_residue;
void main() {
    v_position=u_rotation*a_position-vec3(0.,0.,u_distance);
    v_normal=u_rotation*a_normal;v_color=a_color;v_residue=a_residue;
    gl_Position=project(v_position);
}
"#;
const SURFACE: &str = r#"
uniform float u_selected;
uniform float u_atmosphere;
uniform sampler2D u_hotspots;
uniform sampler2D u_residue_colors;
OUT0 out vec4 out_color;
OUT1 out vec4 out_normal;
OUT2 out float out_residue;
void surface(vec3 p,vec3 normal,vec3 color,float residue) {
    int identity=int(floor(residue+0.5));
    vec4 material=texelFetch(u_residue_colors,ivec2(identity%256,identity/256),0);
    // Match the existing chain palette's sRGB-to-linear material conversion.
    // Keep its exact original value when no explicit domain color is present.
    if(material.a>0.5) color=pow(material.rgb,vec3(2.2));
    vec3 n=normalize(normal);vec3 view=normalize(-p);
    if(dot(n,view)<0.) n=-n;
    vec3 key=normalize(vec3(-0.6,0.85,1.2));
    vec3 fill=normalize(vec3(0.9,0.1,0.55));
    vec3 rim=normalize(vec3(0.25,0.5,-1.));
    float diffuse=0.19+1.12*max(dot(n,key),0.)+0.34*max(dot(n,fill),0.);
    float specular=pow(max(dot(n,normalize(key+view)),0.),85.)*0.95;
    specular+=pow(max(dot(n,normalize(fill+view)),0.),32.)*0.16;
    float fresnel=pow(1.-max(dot(n,view),0.),3.);
    vec3 lit=color*diffuse+vec3(1.,0.97,0.92)*specular;
    lit+=vec3(0.13,0.26,0.32)*max(dot(n,rim),0.)*fresnel;
    float selected=step(0.5,u_selected)*(1.-smoothstep(0.25,0.75,abs(residue-u_selected)));
    vec2 highlights=texelFetch(u_hotspots,ivec2(identity%256,identity/256),0).rg;
    float hotspot=highlights.r;
    selected=max(selected,highlights.g);
    lit=mix(lit,lit*0.10+vec3(0.78,0.115,0.016)*diffuse,hotspot*0.90);
    lit=mix(lit,lit*0.45+vec3(0.68,0.47,0.13),selected*0.55*(1.-hotspot*0.8));
    // Mild atmospheric attenuation makes the interior less visually crowded.
    lit*=mix(1.06,0.74,smoothstep(0.76*u_atmosphere,1.26*u_atmosphere,-p.z));
    out_color=vec4(lit,1.);out_normal=vec4(n,-p.z/u_atmosphere);
    out_residue=float(identity);
}
"#;
const MESH_FRAGMENT: &str = r#"
in vec3 v_position;in vec3 v_normal;in vec3 v_color;flat in float v_residue;
void main() {surface(v_position,v_normal,v_color,v_residue);}
"#;
const ATOM_VERTEX: &str = r#"
in vec4 a_center_radius;in vec4 a_color_residue;
uniform float u_radius_scale;
out vec3 v_plane;
flat out vec3 v_center;flat out float v_radius;flat out vec4 v_color_residue;
const vec2 corners[6]=vec2[6](vec2(-1.,-1.),vec2(1.,-1.),vec2(1.,1.),vec2(-1.,-1.),vec2(1.,1.),vec2(-1.,1.));
void main() {
    v_center=u_rotation*a_center_radius.xyz-vec3(0.,0.,u_distance);
    v_radius=a_center_radius.w*u_radius_scale;v_color_residue=a_color_residue;
    v_plane=v_center+vec3(corners[gl_VertexID]*v_radius*1.12,0.);
    gl_Position=project(v_plane);
}
"#;
const ATOM_FRAGMENT: &str = r#"
in vec3 v_plane;
flat in vec3 v_center;flat in float v_radius;flat in vec4 v_color_residue;
void main() {
    vec3 ray=normalize(v_plane);
    float b=dot(ray,v_center);
    vec3 closest=v_center-b*ray;
    float discriminant=v_radius*v_radius-dot(closest,closest);
    if(discriminant<0.) discard;
    vec3 p=ray*(b-sqrt(discriminant));
    vec4 clip=project(p);gl_FragDepth=(clip.z/clip.w)*0.5+0.5;
    surface(p,(p-v_center)/v_radius,v_color_residue.xyz,v_color_residue.w);
}
"#;
const POST_VERTEX: &str = r#"
out vec2 uv;
void main() {
    vec2 p=vec2((gl_VertexID==1)?3.:-1.,(gl_VertexID==2)?3.:-1.);
    uv=p*0.5+0.5;gl_Position=vec4(p,0.,1.);
}
"#;
const POST_FRAGMENT: &str = r#"
in vec2 uv;OUT0 out vec4 out_color;
uniform sampler2D u_color;uniform sampler2D u_normal;
uniform vec2 u_size;uniform vec4 u_projection;
uniform float u_ambient;uniform float u_bloom;uniform float u_distance;
vec3 position(vec2 at,float depth) {return vec3(((at*2.-1.)-u_projection.zw)*depth/u_projection.xy,-depth);}
vec3 filmic(vec3 x) {return clamp((x*(2.51*x+0.03))/(x*(2.43*x+0.59)+0.14),0.,1.);}
void main() {
    vec2 pixel=1./u_size;
    vec4 c=(texture(u_color,uv+pixel*vec2(-0.25,-0.25))+texture(u_color,uv+pixel*vec2(0.25,-0.25))+texture(u_color,uv+pixel*vec2(-0.25,0.25))+texture(u_color,uv+pixel*vec2(0.25,0.25)))*0.25;
    vec4 nd=texture(u_normal,uv);nd.w*=u_distance;
    float occlusion=0.;
    if(nd.w>0. && u_ambient>0.) {
        vec3 p=position(uv,nd.w);
        vec2 radius=u_projection.xy*3.8/nd.w*0.5;
        for(int i=0;i<16;i++) {
            float fi=float(i);float angle=fi*2.39996323;
            vec2 at=uv+vec2(cos(angle),sin(angle))*radius*sqrt((fi+0.5)/16.);
            float d=texture(u_normal,at).w*u_distance;
            if(d>0.) {
                vec3 delta=position(at,d)-p;float len=length(delta);
                float horizon=max(dot(nd.xyz,delta/max(len,0.001))-0.13,0.);
                occlusion+=horizon*(1.-smoothstep(0.5,5.0,len));
            }
        }
    }
    vec3 color=c.rgb/max(c.a,0.001);
    color*=clamp(1.-occlusion*0.22,0.43,1.);
    vec3 glow=vec3(0.);
    if(u_bloom>0.) {
        for(int i=0;i<8;i++) {
            float angle=float(i)*0.785398;
            vec3 sample_color=texture(u_color,uv+vec2(cos(angle),sin(angle))*pixel*3.).rgb;
            glow+=max(sample_color-vec3(0.75),vec3(0.))*0.016;
        }
    }
    color=pow(filmic(color),vec3(1./2.2));
    float vignette=1.-0.16*dot(uv-0.5,uv-0.5);
    vec3 background=mix(vec3(0.010,0.016,0.023),vec3(0.025,0.036,0.046),max(0.,1.-length((uv-vec2(0.5,0.55))*1.5)));
    color=mix(background,color*vignette,c.a)+glow*u_bloom;
    out_color=vec4(color,1.);
}
"#;

#[cfg(test)]
mod palette_tests {
    use super::*;

    fn molecule() -> Molecule {
        Molecule::parse(
            b"data_colors\nloop_\n_atom_site.group_PDB\n_atom_site.id\n_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_comp_id\n_atom_site.label_asym_id\n_atom_site.label_seq_id\n_atom_site.auth_asym_id\n_atom_site.auth_seq_id\n_atom_site.pdbx_PDB_ins_code\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\nATOM 1 C CA ALA A 1 A 42 ? 0 0 0\nATOM 2 C CA GLY A 2 A 42 A 3 0 0\nATOM 3 C CA ALA B 1 B 42 ? 0 5 0\n",
            "cif",
            "palette identities",
        )
        .unwrap()
    }

    #[test]
    fn domain_colors_require_exact_chain_insertion_and_component_identity() {
        let molecule = molecule();
        let mut palette = ResiduePalette::new(molecule.residues.len());
        let key = molecule.residues[1].key.clone();
        let mut absent = key.clone();
        absent.component = "ALA".into();
        let colors = ResidueColors::from([(key, [12, 220, 43]), (absent, [250, 0, 0])]);
        palette.update(&molecule, &colors);
        assert_eq!(palette.color(0), None);
        assert_eq!(palette.color(1), Some([12, 220, 43]));
        assert_eq!(palette.color(2), None);
        assert_eq!(palette.color(usize::MAX), None);
        assert_eq!(palette.texels[0], [0; 4]);
        assert!(palette.texels[4..].iter().all(|texel| *texel == [0; 4]));
        assert!(palette.dirty);
    }

    #[test]
    fn palette_edits_reuse_storage_and_upload_only_when_display_colors_change() {
        let molecule = molecule();
        let mut palette = ResiduePalette::new(molecule.residues.len());
        let address = palette.texels.as_ptr();
        let capacity = palette.texels.capacity();
        let key = molecule.residues[0].key.clone();
        let mut colors = ResidueColors::from([(key.clone(), [0, 0, 0])]);
        palette.update(&molecule, &colors);
        // Black is an explicit material, distinct from a transparent fallback.
        assert_eq!(palette.color(0), Some([0, 0, 0]));
        assert!(palette.dirty);
        palette.dirty = false; // simulate completed GPU upload
        for _ in 0..1000 {
            palette.update(&molecule, &colors);
            assert!(!palette.dirty);
        }
        for color in 0..=255 {
            colors.insert(key.clone(), [color, 50, 100]);
            palette.update(&molecule, &colors);
            assert!(palette.dirty);
            palette.dirty = false;
            assert_eq!(palette.texels.as_ptr(), address);
            assert_eq!(palette.texels.capacity(), capacity);
        }
        palette.update(&molecule, &ResidueColors::new());
        assert!(palette.dirty);
        assert!(palette.texels.iter().all(|texel| *texel == [0; 4]));
        palette.dirty = false;
        palette.update(&molecule, &ResidueColors::new());
        assert!(!palette.dirty);
        assert_eq!(palette.texels.as_ptr(), address);
    }

    #[test]
    fn unrelated_residue_colors_do_not_change_the_default_palette() {
        let molecule = molecule();
        let mut palette = ResiduePalette::new(molecule.residues.len());
        let mut key = molecule.residues[0].key.clone();
        key.chain = "unrelated".into();
        palette.update(&molecule, &ResidueColors::from([(key, [255, 0, 0])]));
        assert!(!palette.active);
        assert!(!palette.dirty);
        assert!(palette.texels.iter().all(|texel| *texel == [0; 4]));
    }
}
