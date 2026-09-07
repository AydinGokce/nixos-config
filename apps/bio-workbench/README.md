# Bio Workbench — retained Electron client

The main application is now the [native Rust Bio Workbench](../bio-workbench-rust/README.md).
This Electron source and package remain available for rollback. On this workstation,
`bio-workbench` starts the native app and `bio-workbench-electron` starts the retained
Electron release. Both use the same head service; use one editor at a time for a
shared annotation. The instructions below describe the retained Electron client.

A dedicated Electron desktop app for the Bio cloud cluster. Paste sequences,
import several files, compose a protein/DNA/RNA/ligand assembly, preview model
compatibility, then launch the selected pairs. The head retains jobs, uploaded
inputs, native outputs, logs, provenance and annotations after the app closes.
Harrison uses the same head workspace.

## Run with Nix

From this directory, on NixOS, macOS, or Linux under WSLg:

```sh
nix run path:.
```

Or from anywhere on this workstation:

```sh
nix run path:/home/aydin/nixos-config/apps/bio-workbench
```

The standalone flake pins Nixpkgs, Electron, Python and the renderer's complete
npm dependency tree. It does not modify your system configuration. For a
persistent user installation:

```sh
nix profile install path:.
```

The package contains a Linux application-menu entry and a macOS `.app` under
`Applications/Bio Workbench.app`. For Finder, copy that small app bundle from
the installed package into `~/Applications` while retaining the Nix profile.
The flake targets Intel/ARM Linux and Intel/Apple Silicon macOS. Linux builds
and native Electron execution are tested here; macOS derivations are evaluated
here, but macOS and WSL execution require those hosts. WSL needs a working WSLg
graphical session and Nix in its Linux distribution. Native Windows packaging
is not included.

## Connect

Open Connection settings and choose an SSH key file, or leave the key empty to
use your SSH agent. The default endpoint is `root@31.56.109.100:22`. Its verified
public SSH host key is bundled. A custom host must already be trusted in your
OpenSSH known_hosts; the app never silently accepts a new host key. Private key
contents stay on your device. A passphrase-protected key should first be loaded
with `ssh-add` in your session.

Desktop settings are in `$XDG_CONFIG_HOME/bio-workbench/connection.json`
(default `~/.config/bio-workbench`). Checked artifact downloads are cached under
`$XDG_CACHE_HOME/bio-workbench` (default `~/.cache/bio-workbench`). Draft input
text and unsaved viewer notes are saved in Electron's local profile. The head
is authoritative for committed run records and saved annotations. Raw local
imports upload only when you choose files in the input builder.

No cloud API key belongs in this app. The head's existing credential and total
estimated-spend guard govern prediction launches. CPU compatibility preview
performs no model inference or MSA search. The final launch button starts paid
work. Closing the app disconnects your view; it does not cancel cloud jobs.
Cancellation shows the actual state: an already executing resident prediction
can finish before cancellation takes effect.

The head admits up to ten model jobs at once. Jobs beyond that limit stay in
the durable queue with their position and the active-job count. When no
compatible resident worker is running, Auto uses temporary GPU workers that
are removed after results are collected. This supports occasional bursts
without keeping GPUs running between them. The existing head and data volumes
remain online. Provider capacity and the shared spending guard still apply.

## Inputs and comparisons

Use **Independent inputs** for a file/sequence batch or **Interacting assembly**
for chains that belong in one prediction. Type each component explicitly. Load
FASTA, sequence text, SMILES/CCD, SDF, PDB/mmCIF, or a native library record as
appropriate to the workflow. Modified nucleotides and nonstandard chemistry
must be described explicitly; native validation rejects unsupported chemistry
instead of converting it to a standard residue. Model settings are a typed
allowlist, not arbitrary command execution. Native per-model defaults are
preserved. Public MSA remains the default; private MSA is selectable and its
native support is checked before launch.

**Check compatibility** includes the text or library reference currently in the
editor; an extra **Add input** click is optional. Use **Add input/component** to
keep that entry and paste another. Unfinished drafts, names, modalities and
formats are saved locally across views and restarts. Paste and library drafts
are separate; a notice identifies saved drafts on inactive input tabs, which
are included only when you return to that tab or add them to the input list.

The comparison view bundles 3Dmol locally: rotate, zoom, change representations,
pick residues, add labels and notes, and save annotations to the head. Multiple
native samples remain available. Linked rotation shares the camera; it is not
an atomic structure alignment or RMSD calculation. Native confidence values
retain their source and scale. A high confidence prediction does not establish
binding, editing activity, or project success. RF3's native chemistry QA remains
part of result selection; failed samples are retained with their evidence.
Nonstructure workflows expose their native output files and tables.

The offline viewer example is the experimental **1UBQ** structure from RCSB,
shown in two styles; it is clearly marked as a reference and is not presented
as an output from any prediction model. Reference source:
<https://files.rcsb.org/download/1UBQ.cif>.

## Harrison in Slack

Harrison can preview and submit the same batches, read their progress, retrieve
results, and save notes. A durable watcher updates its existing Slack reply
without keeping a Codex turn alive. Ask it, for example:

> Preview this attached FASTA on RF3 and Protenix as separate proteins using
> public MSA. Show me any rejected inputs before launching.

Then ask it to launch the selected compatible pairs. Model input validation and
the cluster budget guard apply equally in Slack. Links of the form
`bio-workbench://batch/ID` open the batch in the desktop app. Slack attachments
require the bot's `files:read` scope. The bot's dedicated SSH key is restricted
on the head to the actor-scoped RPC command, with no interactive shell or port
forwarding. Bot installation/activation is documented in the robot-harrison
repository's `docs/bio-workbench.md`.

## Development and checks

```sh
nix develop
cd frontend
npm ci --ignore-scripts
npm run build
npm test
cd ..
PYTHONPATH=backend python3 -m unittest discover -s tests -v
electron desktop
```

Electron starts and owns a Python service on a random loopback port. This is
internal app transport; it never opens an external browser. The renderer has
no Node access and uses a narrow sandboxed preload for native dialogs and
batch links. SSH uses fixed arguments and the same JSON-RPC schema as Harrison.
Workbench desktop/Harrison RPC uses SSH and adds no head HTTP listener for those
requests. The separate public MSA transport uses a loopback-only proxy. The app's
local HTTP uses session cookies, CSRF tokens,
origin/host checks and a restrictive content policy; artifact downloads verify
SHA-256 before display. The head protocol is documented in
[`../../machines/head/workbench/CONTRACT.md`](../../machines/head/workbench/CONTRACT.md).

Electron's security design follows its
[security guide](https://www.electronjs.org/docs/latest/tutorial/security) and
[context isolation documentation](https://www.electronjs.org/docs/latest/tutorial/context-isolation).
