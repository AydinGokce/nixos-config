# GC Protein Engineering Console

GC Protein Engineering Console is the Rust desktop client for the existing cloud head. It provides
the `gc-protein-engineering-console` application with compact
gray controls, black molecular viewports, object inspector, and console. The
interface uses egui/eframe and native OpenGL; it has no browser or JavaScript
runtime.

## Run and connect

```sh
nix run path:.
# Or build once:
nix build path:.
./result/bin/gc-protein-engineering-console
```

The named flake package and app are `gc-protein-engineering-console`, for example
`nix run path:.#gc-protein-engineering-console`. Existing `bio-workbench` and
`bio-workbench-rust` commands remain available and open the same saved session.
The Linux software launcher is `gc-protein-engineering-console-software`;
`bio-workbench-software` remains available too. The macOS bundle is
`Applications/GC Protein Engineering Console.app`.

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
   format. **The active editor is included in Run even before clicking Add
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
   unavailable models remain disabled.
4. Choose the MSA backend (default **private**) and execution preference, then
   press the green **Run** button. Its status dialog follows uploads, native
   compatibility checks, queueing and execution. The head automatically submits
   every compatible input/model pair; incompatible entries remain visible with
   their reasons and are skipped. There is no separate review or submission step.
5. **Runs / results** follows real batch/job states, observed progress messages,
   queue information when supplied by the head, and native logs. It supports
   explicit job/batch cancellation and history navigation. Closing the desktop
   does not cancel work on the head.

Run payloads are persisted before transmission. Saved request receipts recover
the original operation and key after a lost reply or restart. An accepted Run
continues on the head even if the desktop closes during validation. Each later
Run creates an independent batch, so existing jobs can continue in parallel.
An uncertain request retains its exact key instead of silently launching a
replacement. Legacy compatibility previews stay unsubmitted after upgrading.
New and existing unversioned drafts receive the private MSA default once; later
explicit public/private choices are retained.

A file upload whose initial receipt was lost cannot safely reuse an unknown
upload ID; the client retains the uncertain operation and explains when a new
explicit upload is required. Requests from a different head/user/port cannot be
replayed against the current connection.

## Explore the molecular library

Open **Library** beside Inputs and Runs, or choose **Browse library** from the
input editor. This native workspace reads the shared head library; switching
back to **Molecular viewer** retains every structure tab and its display state.

The sidebar opens with projects. Select a project to expand its constructs and
use **Back to projects** to return. Plasmids are expandable parent rows with their
derived proteins underneath. Standalone proteins stay at the project level.
Filtering a child keeps its parent visible for context; a derived entry whose
parent is outside the current view retains an **Open parent** link.
Construct rows use the curated **Alt name**;
missing names show a small italic *no alt name* placeholder. Inventory IDs and
modalities are separate tags. The original verbose source name appears only as a
tag in the selected detail pane. Search includes IDs, Alt names and source names;
filter by modality or **Needs review**. **Load more records** is explicit when
the server returns multiple pages.

Project, construct and archive lists are cached locally, including loaded pages,
so returning to them is immediate. Navigation reuses a list for 30 seconds; older
lists remain visible while a background refresh runs. **Refresh library** always
checks the head. Cached summaries survive restart (with a background refresh on
first use), are isolated by SSH endpoint and actor, and contain no full sequences
or attachments. Edits, archive/restore and Undo/Redo invalidate all list caches.
The disposable `library-list-cache.json` is limited to 32 scopes and 8 MiB;
invalid caches are ignored without affecting saved inputs or request receipts.

Double-click an Alt name (or a project name) to edit it. The pencil button opens
an exact nucleotide editor or a standalone protein amino-acid editor. Derived
proteins use **Edit definition** instead of a literal sequence editor. **Save** creates a new immutable
revision; existing results keep their original input references. Previous source
annotations remain retained evidence and are not silently remapped onto an edited
sequence. Historical revisions are read-only, with a link to the current revision.

Trash buttons immediately archive a construct or project without deleting it or
opening a confirmation dialog. **File → Library archive** shows hidden records;
**Restore** returns them to the library. **Undo** and **Redo** use durable head
history for this actor, including name, sequence, archive and restore operations.
Concurrent edits are checked against the exact revision and preserved on conflict.
Lost write replies can be recovered through the existing saved request receipts.

The selected record includes a rendered **Purpose** document, exact Markdown
source, **Sequence / identity**, clickable **Relationships**, **Attachments** and
**Record JSON**. Project briefs retain pinned members and roles. Incomplete purpose
scaffolds and unresolved protein candidates remain visible.

Project hierarchies open collapsed, including when returning to a cached project.
**Expand all**, beside the modalities dropdown, switches to **Collapse all** as
soon as any parent is expanded. Derived proteins are indented under their DNA/RNA
parent; standalone proteins remain independent entries.

**Sequence / identity** is a native vector viewer. Circular plasmids open as an
annotated ring; **Auto** zoom magnifies and progressively straightens the visible
arc into a linear map, then individual bases, complementary bases and one
selectable translation track. **Circular** retains curved magnification;
**Linear** and **Fit** provide explicit controls. Scroll over the map to move
left/right in Linear or rotate in Circular. **Ctrl+scroll** zooms. Right-drag
also pans, and left-drag selects a range. Click an annotation or detected
ORF to inspect its strand and ranges. Imported annotations carry historical,
fuzzy or unsupported-coordinate flags; the original attachments remain intact.
Annotated plasmids initially show their annotation tracks. **ORFs** exposes the
six-frame detector, with minimum-length and genetic-code controls that update
the scan automatically. ORFs are candidates, not confirmation of expression
or function.

**Create protein…** previews a selected ORF/range before creating a protein under
its parent. The definition stores ordered nucleotide ranges, strand, genetic code,
codon start and an optional amino-acid crop; the protein sequence is computed by
the head. UI positions are 1-based inclusive; the backend stores zero-based
half-open coordinates and preserves biological traversal order across joins.
For derived proteins, identity, aliases and provenance appear first in a dropdown.
The plain numbered **Protein sequence** pane is expanded by default and can be
collapsed. Its line numbers are a separate gutter; selecting across wrapped rows
and copying yields only the selected contiguous amino acids, without numbers,
spaces or newline characters. Copy sequence, Copy FASTA and edit/parent controls
remain accessible while New variant is closed. The nucleotide editor scrolls
inside the details pane, with a bounded text area and reachable Save/Cancel buttons.
**New variant…** opens its form immediately below the button, followed by the
CDS viewer and annotation selector. Selecting another annotated residue range
immediately updates the form's crop and invalidates any older preview. The form
creates a separately named product, such as a tag-removed variant. Closing it
hides these selection controls. Run history follows the sequence workspace.
**Edit definition** changes the parent or ranges of the existing product.
Preview must match the current definition before saving.
Invalid translations show diagnostics and cannot be used as stale peptides.
Parent edits update dependent product revisions and project references together;
previous revisions and run inputs remain unchanged. Creation and definition edits
participate in the same Undo/Redo history as other library writes.

Nucleotides and amino acids use colored letter blocks. A derived protein's single
translation row starts aligned to its current peptide, with each amino-acid block
spanning its exact three source bases, including reverse strands and joined
ranges. Dragging that row left/right changes the **current protein frame** on
release and saves an undoable revision. Strand, coding footprint and residue crop
stay fixed; the new frame translates complete codons to its first in-frame stop.
An unchanged phase does not write. Shift-drag selects a residue range for the
variant form; standalone proteins use ordinary residue selection and have no
invented nucleotide source or frame control. Historical revisions remain read-only.

**+ Standalone protein** creates an independent protein in the current project
from a literal amino-acid sequence. It uses the same construct/protein kind as a
derived entry, and its sequence stays directly editable.

**▶ Prepare prediction** adds the exact selected revision to the existing Inputs
composer and opens it so you can select models and submit. It preserves current
inputs and briefly highlights the new entry in green, then fades to normal. It
never validates or launches automatically. Whole plasmids and products
requiring review are blocked with the head's reason. Protein **Run history** lists
retained jobs explicitly submitted from that construct, with revision labels;
select a result to open its structure in the molecular viewer.

**Save…** under Attachments downloads the original retained file through bounded
SSH RPC chunks, checks its size and SHA-256 against the selected record, then
opens a native save dialog. Exports are limited to 256 MiB per attachment.

Changing the SSH host, user or port detaches library references from the active
composer. Their original inputs and Library-ref drafts remain in the local draft
archive; select records again from the new head to avoid reusing an identically
named reference from a different library. Pasted molecular sequences are preserved.

## Compare structures and inspect evidence

Each job lists its actual artifacts. Structure artifacts are shown first;
additional logs, confidence/QA files, sequences, and other results remain under
**Other artifacts**. Click a run to open its preferred structure in a tab, or
click individual artifacts and **Open selected** to compare any number of results.
Clicking a run or artifact title focuses its existing tab. **Open tab** and
**Open selected** always create a new tab, including for a result already open.
A pending run opens a tab that follows its progress and displays the output when
available, even if you select a different batch in the sidebar. Text and CSV/TSV previews
are bounded; **Export…** writes the complete original artifact.

Drag tabs to reorder them, drop at a viewer edge to split, or onto another tab
bar to combine groups. Right-click a tab for **Duplicate tab**, **Split down**,
or **Split right**. Duplicate opens a copy in the same group; the split commands
open a copy in a new group and leave the original in place. These actions are
also available under **View**. Copies start with the source tab's current camera
and display settings and then keep separate view state. **Link cameras**, when
enabled, synchronizes camera motion across views. Source annotations remain shared.
Close with
the tab's left **×**, middle-click, or Ctrl+W (Cmd+W on macOS). Closing a tab keeps
the run, artifacts and annotations. Tabs, splits and each structure's camera and
display settings survive restart. Closing every tab leaves an empty workspace.

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

On first launch, one tab shows an explicitly labeled
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
unknown migration fields are retained. The Electron application has been removed;
its saved molecular data remains readable through this one-time native import.

**Launch in PyMOL** opens the selected pane's exact PDB/mmCIF bytes in a separate
local process. Linux Nix packaging supplies a pinned PyMOL. Other installations
may set `BIO_WORKBENCH_PYMOL` to one executable path or place `pymol` on PATH.
Startup failure and the readiness marker appear in the status/console. Closing
GC Protein Engineering Console leaves that external viewer open. A demo launch exports only the
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
Manifests accept 1–16 structures and optional `"align": true`. Alignment fits
unique matching polymer sequences rigidly onto the first structure and uses a
shared display origin and scale. It preserves original files and records the
transformation and matched anchors. Unavailable fits show **UNALIGNED** on the
panel and an explicit reason in the receipt. Fit RMSD describes the display
alignment, not prediction quality. Labels and the bottom-right XYZ indicator
use the same viewer code as the desktop.

## Shared MSA worker

Beside **HEAD CONNECTED**, **MSA available** / **MSA unavailable** reports whether
Verda currently advertises capacity to launch the configured private MSA worker.
The console checks at startup and every five seconds. **MSA connected** separately
identifies an existing ready or busy session; other session stages remain visible.
Click either indicator to inspect measured startup stages and the shutdown countdown. Private runs
also show this service status in **Run status**. An ETA is labeled for its stage,
startup, or run scope; unavailable estimates remain unknown. A stage estimate
does not imply an estimate for the whole prediction.

The **Shared MSA worker** dialog lists available Verda GPUs by region and rental
type, with GPU memory, system RAM, hourly price and MSA eligibility. Small GPUs
can be available even when no machine satisfies the MSA worker's RAM, region,
image and price requirements. CPU capacity also contributes to MSA availability.
**Refresh** checks capacity immediately, and **Last update X sec ago** shows the
age of the provider snapshot. Incomplete, failed and stale checks are identified
explicitly. Availability is an observation, not a reservation or budget approval.
Provider credentials stay on the head. These capacity checks do not rent compute.

**+15 minutes** adds idle keep-warm time inside the worker's original paid runtime
limit, including credit for when an active search finishes. **Shut down now**
closes an idle worker. During accepted work, **Finish N searches and shut down**
drains those searches and rejects new ones. These controls affect the shared MSA
service; GPU prediction workers have separate lifetimes. The head enables each
button only when that exact worker supports it and the full extension fits its
runtime limit.

Countdowns use the head's clock and monotonic elapsed time. An observation older
than 30 seconds, a transport error, or a reached deadline disables the controls
and marks the timer unavailable or awaiting refresh. Status polling neither
launches a worker nor extends its idle timer. Commands retain their exact worker
generation and request key across reconnects and restarts. A pending receipt is
checked automatically; **Recover exact command** reconciles a lost reply without
issuing a replacement command against another worker.
