//! The bot and desktop share the exact parser, geometry, shaders and capture code.
#![allow(dead_code, unused_imports)]
#[path = "../render.rs"]
mod render;
#[path = "../scene.rs"]
mod scene;

fn main() {
    if let Err(error) = render::run(std::env::args_os().skip(1).collect()) {
        eprintln!("Bio renderer: {error}");
        std::process::exit(1);
    }
}
