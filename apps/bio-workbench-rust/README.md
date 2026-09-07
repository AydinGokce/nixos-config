# Bio Workbench — native desktop

Bio Workbench is a Rust desktop client for the existing cloud head. It replaces
Electron as the main `bio-workbench` application while retaining the compact
gray controls, black molecular viewports, object inspector, and console. The
interface uses egui/eframe and native OpenGL; it has no browser or JavaScript
runtime.

## Run and connect

```sh
nix run path:.
# Or build once:
nix build path:.
./result/bin/bio-workbench
```

The standalone flake supports x86_64/aarch64 Linux and Darwin. NixOS/Linux native
windows have been exercised; macOS code has been cross-checked, but macOS and
WSLg still require runtime qualification on those systems. WSL needs WSLg or an
X server. A separate native Windows installer is not provided. An OpenGL context
with floating-point render targets is required; Mesa software rendering works,
with lower performance than a graphics card.

Open **Connection…** to set the SSH host, user, port, and optional key path.
An empty key path uses the local SSH agent. The default profile follows the
existing head configuration and uses `~/.ssh/datacrunch_ed25519` when present.
Verify a new head's SSH host key through ordinary SSH first; unknown or changed
keys are not silently accepted. The Nix package supplies OpenSSH and Linux file
dialog/runtime dependencies. No API key is required for the desktop itself.

The desktop invokes the existing SSH JSONL Workbench RPC. It does not add a web
listener or a new cloud service. Existing head authorization, model adapters,
worker scheduling, and accounting continue to apply.

## Submit and follow work

1. Paste a sequence, FASTA, ligand/assembly description, or other supported input
   format. **The active editor is included in Preview even before clicking Add
   input.** Add input moves it into the list for composing larger batches. Paste
   and library-reference editors retain separate drafts.
2. Use **Files…**, drag files into the window, or browse the head construct
   library. Multiple files are supported. Original file bytes are uploaded as
   immutable inputs before validation. Library references are pinned to revisions
   by the head. For `library-json` inputs, **Attach SDF…** supplies referenced
   ligand files; EVOLVEpro settings include a CSV label upload.
3. Select independent inputs or an assembly, then choose modalities and unique
   assembly chain IDs. Select models and settings from the live head catalog.
   Unchecked settings retain native defaults. The catalog includes folding,
   sequence analysis, variant ranking, sequence design, and backbone design;
   parked models remain visibly unavailable.
4. Choose the public/private MSA backend and execution preference, then **Check
   compatibility (CPU)**. This calls native input validation without inference.
   Every input/model pair appears with its result and rejection reason. Select
   the compatible pairs explicitly, then submit the reviewed selection.
5. **Runs / results** follows real batch/job states, observed progress messages,
   queue information when supplied by the head, and native logs. It supports
   explicit job/batch cancellation and history navigation. Closing the desktop
   does not cancel work on the head.

Preview and submission payloads are persisted before transmission. **Saved
request receipts** and **Recover exact submission** recover the original
operation and request key after a lost reply or restart. Recovery does not
create another inference request. A changed input/settings draft requires a new
compatibility preview. No prediction is automatically submitted on startup.

A file upload whose initial receipt was lost cannot safely reuse an unknown
upload ID; the client retains the uncertain operation and explains when a new
explicit upload is required. Requests from a different head/user/port cannot be
replayed against the current connection.

## Compare structures and inspect evidence

Each job lists its actual artifacts. Structure artifacts are shown first;
additional logs, confidence/QA files, sequences, and other results remain under
**Other artifacts**. Choose up to four structures and **Compare selected**, or
load a structure into the selected pane with **View**. Text and CSV/TSV previews
are bounded; **Export…** writes the complete original artifact.

The native viewer reads PDB and mmCIF, including protein, nucleic acid, ligand,
ion, modified residue, author/label chain IDs, insertion codes, and coordinate
model identifiers. It shows the first coordinate model and one alternate
conformer per residue with explicit warnings. Viewing is bounded to 32 MiB and
100,000 displayed atoms; original exports and PyMOL retain all source bytes.

- Left drag rotates; right drag or Shift+drag pans; the wheel zooms; double-click
  fits the structure. Each pane has its own camera, representation, selection,
  visibility, and chain controls. Linked cameras synchronize manipulation while
  preserving each molecule's fitted scale; they do not align structures.
- Select cartoon, sticks, spheres, or backbone trace. Right-click the viewport
  to toggle screen-space contact shading and soft glow. The polished GPU
  lighting is rasterization, not RTX or ray tracing.
- Pick a residue in the sequence strip or viewport. The inspector retains its
  coordinate identifiers and can measure an actual anchor-to-anchor distance
  within that structure. Picking uses projected residue anchors, not frontmost
  atom occlusion or a binding-site analysis.
- **Native confidence**, **Native chemistry / RF3 QA**, and **Run provenance**
  display metadata supplied by the head. Missing metadata stays missing. The
  coordinate B/temperature field is labeled as such, not assumed to be pLDDT.

Cartoons use file secondary-structure annotations when available. Without them,
backbone geometry provides a clearly labeled display approximation, not DSSP
or experimental secondary-structure evidence. Cartoon widths, smoothed paths,
atom radii, and inferred proximity bonds are visualization conventions, not
chemical validation.

Until an actual result is selected, the panes show an explicitly labeled
experimental reference: [RCSB PDB 4OO8](https://www.rcsb.org/structure/4OO8), a
Cas9–guide RNA–target DNA complex at 2.50 Å. The local demo contains chains A/B/C
only: 9,999 protein atoms, 2,082 RNA atoms, and 404 DNA atoms. It is never presented
as a model prediction or used as a substitute for a failed result download.

## Notes, persistence, and PyMOL

Research notes and residue annotations have automatically saved local drafts.
**Save to head** explicitly publishes a note on the selected real artifact.
Edits and deletion tombstones use the head revision check; a concurrent change
leaves the local edit intact for review/export. The legacy residue annotation
schema remains interoperable, with an additional native residue key for richer
mmCIF identities. Notes on local files or the demo remain local. **Export notes…**
includes shared records, the current local draft, retained edits, and imported
Electron annotation documents.

Native state lives under the platform configuration directory in
`bio-workbench-native` (`~/.config` on Linux, `~/Library/Application Support` on
macOS). `BIO_WORKBENCH_NATIVE_STATE_DIR` selects an isolated directory for tests.
Connection settings, exact request receipts, input drafts, view references,
selections, and notes survive restarts. Verified artifact downloads are cached
locally and reauthorized against the head when opened again.

On first launch, the client imports the previous Electron input draft and
annotation documents, including
`~/.config/bio-workbench-desktop/molecular-state.json`. The original files and
unknown migration fields are retained. Existing Electron remains available
through the separately packaged `bio-workbench-electron` rollback command;
its state is not deleted by the native migration.

**Launch in PyMOL** opens the selected pane's exact PDB/mmCIF bytes in a separate
local process. Linux Nix packaging supplies a pinned PyMOL. Other installations
may set `BIO_WORKBENCH_PYMOL` to one executable path or place `pymol` on PATH.
Startup failure and the readiness marker appear in the status/console. Closing
Bio Workbench leaves that external viewer open. A demo launch exports only the
same experimental A/B/C coordinates displayed in the pane.

`bio-workbench://batch/ID` opens a batch. A second launch forwards links or local
input files to the existing native window through a private filesystem inbox;
it does not open another writer for the same session. The bottom console
accepts local view commands only: `help`, `reset`, `cartoon`, `sticks`, `spheres`,
and `trace`.

## Development

```sh
nix develop path:.
export CARGO_TARGET_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bio-workbench-rust/target"
cargo run --release --bin bio-workbench-rust
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --release
```

Keep build outputs outside this source directory for clean Nix source copies.
Native screenshot qualification can set `EFRAME_SCREENSHOT_TO=/absolute/path.png`;
the helper captures its OpenGL window and exits. Use an isolated native state
directory and display for testing so the current user window is not driven.

The companion `bio-render` executable and `bio-workbench --render --manifest
request.json` entry point use the same parser and GPU renderer for one-shot
structure PNGs. This rendering path does not submit predictions.
