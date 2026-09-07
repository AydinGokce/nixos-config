//! Native OpenGL rasterizer. The UI callback owns no transient molecular meshes.
//! Lighting/contact shading are presentation effects, not ray tracing or analysis.
use super::{Camera, Molecule, Representation, V3, geometry};
use eframe::{egui, egui_glow, glow};
use glow::HasContext as _;

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
}
struct Target {
    fbo: glow::Framebuffer,
    color: glow::Texture,
    normal_depth: glow::Texture,
    depth: glow::Renderbuffer,
    size: [i32; 2],
}
pub(super) struct Renderer {
    mesh_program: glow::Program,
    atom_program: glow::Program,
    post_program: glow::Program,
    empty_vao: glow::VertexArray,
    chains: [Chain; 3],
    targets: [Option<Target>; 2],
    destroyed: bool,
}

// All GL calls run on eframe's current GL context, including cleanup in on_exit.
impl Mesh {
    fn new(gl: &glow::Context, mesh: &geometry::Mesh) -> Result<Self, String> {
        unsafe {
            let vao = gl.create_vertex_array()?;
            let vertices = gl.create_buffer()?;
            let indices = gl.create_buffer()?;
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
    fn new(gl: &glow::Context, data: &[[f32; 8]]) -> Result<Self, String> {
        unsafe {
            let vao = gl.create_vertex_array()?;
            let instances = gl.create_buffer()?;
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
        unsafe {
            let fbo = gl.create_framebuffer()?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            let color = gl.create_texture()?;
            let normal_depth = gl.create_texture()?;
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
            let depth = gl.create_renderbuffer()?;
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
            gl.draw_buffers(&[glow::COLOR_ATTACHMENT0, glow::COLOR_ATTACHMENT1]);
            let result = Self {
                fbo,
                color,
                normal_depth,
                depth,
                size,
            };
            if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
                result.destroy(gl);
                return Err(
                    "OpenGL does not support the molecular renderer's HDR/depth framebuffer".into(),
                );
            }
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            Ok(result)
        }
    }
    fn destroy(&self, gl: &glow::Context) {
        unsafe {
            gl.delete_framebuffer(self.fbo);
            gl.delete_texture(self.color);
            gl.delete_texture(self.normal_depth);
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
        "#version 300 es\nprecision highp float;\n#define OUT0 layout(location=0)\n#define OUT1 layout(location=1)\n"
    } else {
        "#version 140\n#define OUT0\n#define OUT1\n"
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
        let mut chains = Vec::new();
        for chain in &geometry {
            chains.push(Chain {
                cartoon: Mesh::new(gl, &chain.cartoon)?,
                trace: Mesh::new(gl, &chain.trace)?,
                sticks: Mesh::new(gl, &chain.sticks)?,
                atoms: Atoms::new(gl, &chain.atoms)?,
            });
        }
        let mesh_program = program(
            gl,
            &format!("{CAMERA}\n{MESH_VERTEX}"),
            &format!("{SURFACE}\n{MESH_FRAGMENT}"),
            &["a_position", "a_normal", "a_color", "a_residue"],
        )?;
        let atom_program = program(
            gl,
            &format!("{CAMERA}\n{ATOM_VERTEX}"),
            &format!("{CAMERA}\n{SURFACE}\n{ATOM_FRAGMENT}"),
            &["a_center_radius", "a_color_residue"],
        )?;
        let post_program = program(gl, POST_VERTEX, POST_FRAGMENT, &[])?;
        Ok(Self {
            mesh_program,
            atom_program,
            post_program,
            empty_vao: unsafe { gl.create_vertex_array()? },
            chains: chains.try_into().ok().expect("three chains"),
            targets: [None, None],
            destroyed: false,
        })
    }
    pub(super) fn destroy(&mut self, gl: &glow::Context) {
        if self.destroyed {
            return;
        }
        for chain in &self.chains {
            chain.cartoon.destroy(gl);
            chain.trace.destroy(gl);
            chain.sticks.destroy(gl);
            chain.atoms.destroy(gl);
        }
        for target in self.targets.iter().flatten() {
            target.destroy(gl);
        }
        unsafe {
            gl.delete_program(self.mesh_program);
            gl.delete_program(self.atom_program);
            gl.delete_program(self.post_program);
            gl.delete_vertex_array(self.empty_vao);
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
        index: usize,
        selected: usize,
        chains: [bool; 3],
    ) {
        if self.destroyed {
            return;
        }
        let viewport = info.viewport_in_pixels();
        if viewport.width_px < 1 || viewport.height_px < 1 {
            return;
        }
        // Two samples per axis, bounded for unusually large/HiDPI windows.
        let max_side = unsafe { gl.get_parameter_i32(glow::MAX_TEXTURE_SIZE) }.min(4096) as f32;
        let scale = 2.0_f32.min(max_side / viewport.width_px.max(viewport.height_px) as f32);
        let size = [
            (viewport.width_px as f32 * scale).max(1.) as i32,
            (viewport.height_px as f32 * scale).max(1.) as i32,
        ];
        if self.targets[index]
            .as_ref()
            .is_none_or(|target| target.size != size)
        {
            if let Some(old) = self.targets[index].take() {
                old.destroy(gl);
            }
            self.targets[index] =
                Some(Target::new(gl, size).expect("Cannot allocate molecular render target"));
        }
        let target = self.targets[index].as_ref().expect("render target");
        let rect = info.viewport;
        let units = rect.width().min(rect.height()) / 135. * camera.zoom;
        let projection = [
            420. * units / rect.width(),
            420. * units / rect.height(),
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
                    gl.get_uniform_location(program, "u_selected").as_ref(),
                    selected as f32,
                );
            }
            gl.use_program(Some(self.mesh_program));
            for (chain, visible) in self.chains.iter().zip(chains) {
                if !visible {
                    continue;
                }
                match representation {
                    Representation::Cartoon => chain.cartoon.draw(gl),
                    Representation::Trace => chain.trace.draw(gl),
                    Representation::Sticks => chain.sticks.draw(gl),
                    Representation::Spheres => {}
                }
            }
            if matches!(
                representation,
                Representation::Spheres | Representation::Sticks
            ) {
                gl.use_program(Some(self.atom_program));
                gl.uniform_1_f32(
                    gl.get_uniform_location(self.atom_program, "u_radius_scale")
                        .as_ref(),
                    if representation == Representation::Spheres {
                        1.
                    } else {
                        0.14
                    },
                );
                for (chain, visible) in self.chains.iter().zip(chains) {
                    if visible {
                        chain.atoms.draw(gl);
                    }
                }
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
vec4 project(vec3 p) {
    float w=-p.z;
    return vec4(p.xy*u_projection.xy+u_projection.zw*w,1.01005025*w-10.050251,w);
}
"#;
const MESH_VERTEX: &str = r#"
in vec3 a_position; in vec3 a_normal; in vec3 a_color; in float a_residue;
out vec3 v_position; out vec3 v_normal; out vec3 v_color; out float v_residue;
void main() {
    v_position=u_rotation*a_position-vec3(0.,0.,210.);
    v_normal=u_rotation*a_normal;v_color=a_color;v_residue=a_residue;
    gl_Position=project(v_position);
}
"#;
const SURFACE: &str = r#"
uniform float u_selected;
OUT0 out vec4 out_color;
OUT1 out vec4 out_normal;
void surface(vec3 p,vec3 normal,vec3 color,float residue) {
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
    float selected=1.-smoothstep(0.25,1.0,abs(residue-u_selected));
    lit=mix(lit,lit*0.45+vec3(0.68,0.47,0.13),selected*0.55);
    // Mild atmospheric attenuation makes the interior less visually crowded.
    lit*=mix(1.06,0.74,smoothstep(160.,265.,-p.z));
    out_color=vec4(lit,1.);out_normal=vec4(n,-p.z);
}
"#;
const MESH_FRAGMENT: &str = r#"
in vec3 v_position;in vec3 v_normal;in vec3 v_color;in float v_residue;
void main() {surface(v_position,v_normal,v_color,v_residue);}
"#;
const ATOM_VERTEX: &str = r#"
in vec4 a_center_radius;in vec4 a_color_residue;
uniform float u_radius_scale;
out vec3 v_plane;
flat out vec3 v_center;flat out float v_radius;flat out vec4 v_color_residue;
const vec2 corners[6]=vec2[6](vec2(-1.,-1.),vec2(1.,-1.),vec2(1.,1.),vec2(-1.,-1.),vec2(1.,1.),vec2(-1.,1.));
void main() {
    v_center=u_rotation*a_center_radius.xyz-vec3(0.,0.,210.);
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
uniform float u_ambient;uniform float u_bloom;
vec3 position(vec2 at,float depth) {return vec3(((at*2.-1.)-u_projection.zw)*depth/u_projection.xy,-depth);}
vec3 filmic(vec3 x) {return clamp((x*(2.51*x+0.03))/(x*(2.43*x+0.59)+0.14),0.,1.);}
void main() {
    vec2 pixel=1./u_size;
    vec4 c=(texture(u_color,uv+pixel*vec2(-0.25,-0.25))+texture(u_color,uv+pixel*vec2(0.25,-0.25))+texture(u_color,uv+pixel*vec2(-0.25,0.25))+texture(u_color,uv+pixel*vec2(0.25,0.25)))*0.25;
    vec4 nd=texture(u_normal,uv);
    float occlusion=0.;
    if(nd.w>0. && u_ambient>0.) {
        vec3 p=position(uv,nd.w);
        vec2 radius=u_projection.xy*3.8/nd.w*0.5;
        for(int i=0;i<16;i++) {
            float fi=float(i);float angle=fi*2.39996323;
            vec2 at=uv+vec2(cos(angle),sin(angle))*radius*sqrt((fi+0.5)/16.);
            float d=texture(u_normal,at).w;
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
