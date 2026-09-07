# Bio Workbench — native Rust design prototype

This is an **offline design prototype for feedback**, implemented in Rust with
native egui/eframe and OpenGL. It is a separate application from the working
Electron client. It does not connect to the head, call an API, rent compute,
import files, or submit predictions. The **Launch in PyMOL** button opens the
embedded experimental reference in a separate local PyMOL application.

The design uses a compact gray menu and toolbar, black molecular viewports,
a right-side object tree and inspector, a submission draft/model panel, and a
bottom command console. Panels resize. The emphasis is on dense technical
controls and keeping structure manipulation visible alongside inputs.

## Run

From this directory on NixOS, Linux, macOS, or WSL with WSLg:

```sh
nix run path:.
```

Or from anywhere:

```sh
nix run path:/home/aydin/nixos-config/apps/bio-workbench-rust
```

Build without launching:

```sh
nix build path:.
./result/bin/bio-workbench-rust
```

The standalone flake pins Nixpkgs and supports x86_64/aarch64 Linux and Darwin.
Linux uses X11 or Wayland. macOS and WSLg require runtime verification on those
hosts; Linux is the initial development target. Native Windows packaging is
not included in this prototype. No modifications to the repository's root
flake or the existing Electron application are needed.

## Open the reference in PyMOL

Click **Launch in PyMOL** on the toolbar. It opens PDB 4OO8 chains A/B/C as
separate objects: Cas9 in teal cartoon, guide RNA in amber, and target DNA in
violet. The loaded coordinates are the experimental reference currently shown
in the prototype; draft edits and illustrative prediction rows are not exported.

The Linux Nix package includes its pinned PyMOL executable. Cargo launches and
other platforms look for `pymol` on PATH; set `BIO_WORKBENCH_PYMOL` to an executable
path to select another installation. The value is one executable path, without
extra command-line arguments. A missing application or startup failure appears
in the status bar and console. macOS and WSLg launches still require verification
on those hosts.

The Rust launcher writes the embedded PDB, a fixed PML view script, and a process
log into a unique local cache directory, then starts PyMOL directly without a
shell. The cache uses XDG_CACHE_HOME when set, otherwise the platform user cache
directory. Startup scripts and plugins are disabled for this reference launch.
The status changes to ready only when PyMOL executes the final script marker.
Closing the prototype leaves the separate PyMOL application open. Neither the
button nor the local console can submit cloud work.

## Try the design

- Rotate the sample with a left drag, pan with a right drag or Shift+drag,
  zoom with the wheel, and reset with a double click. Right-click a viewport
  to toggle contact shading or soft highlight glow.
- Toggle linked cameras and single/comparison layout. Linking cameras does
  not calculate alignment.
- Choose cartoon, sticks, spheres, or a C-alpha trace from the Display menu,
  toolbar, or object `S` menu. `A` contains local view actions, `H` toggles
  visibility, and `L` toggles a selection label. `C` explains the chain palette.
  Separate chain checkboxes control Cas9, sgRNA, and target DNA.
- Pick a residue in the sequence strip or near its C-alpha coordinate in a
  viewport; the local inspector and selection note show the choice.
- Edit the draft, modality, model checkboxes, MSA option, and note text to try
  the control layout. These edits are not saved and do not alter the structure.
- Open the Run queue tab to see explicitly labeled illustrative rows.
- The console accepts only built-in view commands: `help`, `reset`, `cartoon`,
  `sticks`, `spheres`, `trace`, and `select N` (an actually modeled Cas9 residue ID). It is not a shell.

Cloud submission, compatibility checking, file/library import, measurement,
alignment, and export controls are disabled. Nothing displayed is a new model
prediction. The startup labels and console keep this explicit.

## Reference data and rendering limits

Both viewports show the **same experimentally determined Cas9–guide RNA–target
DNA complex**, [PDB 4OO8](https://www.rcsb.org/structure/4OO8), at 2.50 Å resolution.
The original PDB file is embedded; the renderer selects chains A/B/C (one complex)
and excludes the second crystallographic copy and nonpolymer atoms.

| Chain | Molecule | Deposited sequence | Modeled residues | Displayed atoms |
| --- | --- | ---: | ---: | ---: |
| A | SpCas9 protein | 1,372 aa | 1,301 | 9,999 |
| B | Guide RNA | 98 nt | 97 | 2,082 |
| C | Target DNA | 23 nt | 21 | 404 |

The source is [RCSB's original coordinate file](https://files.rcsb.org/download/4OO8.pdb).
The left draft contains the deposited protein sequence. The sequence strip and
selection use actual modeled residue numbers, including missing-residue gaps.
The local rendering has 12,485 atoms; teal denotes protein, amber RNA, violet DNA.

The native OpenGL renderer builds static 3D meshes from the fixed fixture's ATOM,
HELIX and SHEET records. Protein cartoons use continuous rounded ribbon/tube
cross-sections, smoothly transported frames guided by carbonyl directions,
shared vertices and continuous normals. Missing-residue gaps remain open.
RNA/DNA show rounded phosphate backbones, coordinate-derived base plates and
schematic connectors. The same coordinates drive sticks and depth-correct
sphere impostors. Both views use perspective and a real depth buffer, so all
visible chains occlude each other correctly.

The studio look combines key/fill/rim lighting, polished material highlights,
HDR tone mapping, bounded 2× supersampling, screen-space contact shading and
optional soft highlight glow. Right-click a viewport to toggle the last two
effects. This is GPU rasterization, not hardware RTX or ray tracing. Geometry
is uploaded once and reused during rotation; idle windows do not animate.
An OpenGL context with floating-point render targets is required. Software
Mesa also runs the renderer, with lower performance than a graphics card.

Cartoon dimensions, smoothed backbone paths, base plates, atom radii and
lighting are display conventions. Sticks use distance-based illustrative
bonds built with a spatial grid, not validated chemistry. Picking selects a
nearby projected protein C-alpha; it does not perform scientific feature
analysis or restrict picks to the frontmost atom. This remains a fixed
reference viewer and interface study, not a chemistry validator, confidence
analysis or replacement for PyMOL. No confidence scores are fabricated.

## Development

```sh
nix develop path:.
export CARGO_TARGET_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bio-workbench-rust/target"
cargo run --release
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --release
```

The application and UI are Rust, with embedded native GLSL shaders; Nix and
Cargo files provide packaging. Eframe's optional
screenshot helper is enabled for capturing the native OpenGL window:

```sh
EFRAME_SCREENSHOT_TO="$PWD/prototype.png" nix run path:.
```

The helper writes a PNG and exits. Normal launches have no screenshot path.
The API implementation follows the pinned
[eframe 0.33.3 native App documentation](https://docs.rs/eframe/0.33.3/eframe/trait.App.html)
and its [official native OpenGL callback example](https://github.com/emilk/egui/blob/0.33.3/crates/egui_demo_app/src/apps/custom3d_glow.rs).
