# Bio Workbench — native Rust design prototype

This is an **offline design prototype for feedback**, implemented in Rust with
native egui/eframe and OpenGL. It is a separate application from the working
Electron client. It does not connect to the head, call an API, rent compute,
execute programs, import files, or submit predictions.

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

## Try the design

- Rotate the sample with a left drag, pan with a right drag or Shift+drag,
  zoom with the wheel, and reset with a double click.
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

The Rust renderer reads the fixed fixture's ATOM, HELIX and SHEET records. It
makes a smoothed protein ribbon sketch, phosphate-backbone sketches for RNA/DNA,
and distance-based illustrative bonds using a spatial grid. Ribbon widths,
atom sizes and depth-sorted projections are presentation approximations; the
nucleic-acid sketch can overlay the protein without full occlusion. This is an
interactive interface study, not a scientific renderer, chemistry validator,
confidence analysis, or replacement for PyMOL. No confidence scores are fabricated.

## Development

```sh
nix develop path:.
export CARGO_TARGET_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bio-workbench-rust/target"
cargo run --release
cargo fmt --check
cargo clippy --all-targets -- -D warnings
```

Source and UI are Rust; Nix and Cargo files provide packaging. Eframe's optional
screenshot helper is enabled for capturing the native OpenGL window:

```sh
EFRAME_SCREENSHOT_TO="$PWD/prototype.png" nix run path:.
```

The helper writes a PNG and exits. Normal launches have no screenshot path.
The API implementation follows the pinned
[eframe 0.33.3 native App documentation](https://docs.rs/eframe/0.33.3/eframe/trait.App.html).
