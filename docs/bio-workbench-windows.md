# Install GC Protein Engineering Console on a Windows PC

The current Windows route runs the Rust desktop in **Ubuntu on WSL2**, with its
window displayed by WSLg. There is no native `.exe` installer yet. The Linux app
and Mesa software rendering have been tested; this release has not been run on
an actual Windows/WSLg machine.

## 1. Install Ubuntu and WSLg

Use Windows 11 or Windows 10 build 19044 or newer. Open **PowerShell as
Administrator** and run:

```powershell
wsl --install -d Ubuntu
```

Restart Windows if requested, open **Ubuntu** from Start, and create your Linux
username and password. If WSL is already installed, update it from PowerShell:

```powershell
wsl --update
wsl --shutdown
wsl --list --verbose
```

Ubuntu must show version `2`. If it shows `1`, run `wsl --set-version Ubuntu 2`.
Open Ubuntu again after shutdown. See Microsoft's [WSL installation
instructions](https://learn.microsoft.com/en-us/windows/wsl/install) and
[WSLg requirements](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps).

## 2. Install Nix inside Ubuntu

Run these commands in the **Ubuntu terminal**, under your regular Linux user:

```sh
sudo apt update
sudo apt install -y curl xz-utils ca-certificates openssh-client
curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --no-daemon
. "$HOME/.nix-profile/etc/profile.d/nix.sh"
```

This is Nix's official [single-user WSL
installation](https://nixos.org/download/); it does not require a systemd daemon.
The installer may request your Ubuntu password when creating `/nix`.

## 3. Copy the source archive and build

Copy `gc-protein-engineering-console-0.3.0-source.tar.gz` from the existing workstation to
your Windows **Downloads** folder. The prepared archive on that workstation is:

```text
/home/aydin/bio-runs/protein-structure-gallery-20260913/gc-protein-engineering-console-0.3.0-source.tar.gz
```

In Ubuntu, replace `YOUR_WINDOWS_USERNAME` below with your Windows profile
folder name:

```sh
mkdir -p ~/bio-workbench-rust
tar -xzf "/mnt/c/Users/YOUR_WINDOWS_USERNAME/Downloads/gc-protein-engineering-console-0.3.0-source.tar.gz" --strip-components=1 -C ~/bio-workbench-rust
cd ~/bio-workbench-rust
nix --extra-experimental-features 'nix-command flakes' build path:.
./result/bin/gc-protein-engineering-console
```

This archive includes the native **Library** explorer with project navigation,
editable Alt names and sequences, reversible archiving, undo/redo, protein run
history, pinned input selection and verified original-file downloads. Project and
construct lists are cached locally for immediate return visits and refreshed in
the background.
The sequence workspace includes collapsed parent/product hierarchies, aligned
base and amino-acid blocks, saved translation-frame edits and inline variant
creation with annotation selection.
The green Run button handles compatibility checks and submission automatically,
with live status and private MSA as the default. The plain numbered protein pane
supports copying across wrapped rows without including numbers or line breaks.
It also includes draggable viewer tabs, independent duplicate tabs, right-click
Duplicate tab / Split down / Split right, persistent splits, working quick controls
and the console resize fix. To update an existing installation, close the app,
extract over its source directory and rebuild with the same command.
Existing `bio-workbench` and `bio-workbench-rust` launch commands still work;
the renamed app uses the same connection settings, saved inputs and results.
The MSA indicators distinguish current Verda capacity from a connected worker.
Capacity refreshes at startup and every five seconds; the worker dialog includes
available GPUs, a manual Refresh button and the last-update age.

**Inputs → Binder design** opens the BindCraft workflow: load a target structure
or use a library folding result, select chains and hotspot residues in the 3D
surface or sequence, and submit with runtime and cost limits. Results include a
candidate table, target-aligned comparisons and **Save to project**. BindCraft
runs on the cloud GPU worker and does not need the shared MSA worker.

The viewer's **Protein domains** inspector colors named residue ranges and imports
curated plasmid annotations through the selected protein's saved translation.
Autodetected CDSs and ORFs are excluded. Domain colors persist independently in
each structure tab and support Undo/Redo. Plasmid selections also highlight the
full height of their map region.

Each protein's **Structures** tab now shows cached studio thumbnails for uploaded
PDB/mmCIF files and linked prediction outputs. Standalone creation supports optional
structure uploads. Cards open the viewer and offer reversible removal. Structure
views include protein/plasmid backlinks and automatic unimported-annotation lists
for linked plasmid-derived proteins. Shift-click selects inclusive residue ranges
for **Create annotation…**; a 3D click centers the residue in the sequence strip.
Hover over that strip and use the mouse wheel to scroll it. The plasmid map
supports selection in either direction, Left/Right cursor movement, Shift+arrows
to select, Ctrl for three-nucleotide steps, and right-click copying of selected
nucleotides or translation.

The first build downloads dependencies and compiles the app. Keep the extracted
source in Ubuntu's home directory for [better filesystem
performance](https://learn.microsoft.com/en-us/windows/wsl/filesystems).

If the app reports an OpenGL/Mesa driver error or cannot create its window,
close it and use the included software launcher:

```sh
cd ~/bio-workbench-rust
./result/bin/gc-protein-engineering-console-software
```

This uses the package's pinned Mesa/llvmpipe CPU renderer. Rotation can be slower
than GPU rendering, especially for large structures. It still needs a working
WSLg display. Keep WSLg's existing `DISPLAY` and `WAYLAND_DISPLAY` settings.

## 4. Connect to bio-head

The app needs an SSH key authorized on bio-head. It does not need cloud-provider
API keys, model weights, or molecular databases on the Windows PC.

Keep the private key inside Ubuntu's home directory. For example, after placing
your existing authorized `datacrunch_ed25519` key in your Windows `.ssh` folder:

```sh
install -d -m 700 ~/.ssh
install -m 600 "/mnt/c/Users/YOUR_WINDOWS_USERNAME/.ssh/datacrunch_ed25519" ~/.ssh/datacrunch_ed25519
ssh -i ~/.ssh/datacrunch_ed25519 root@31.56.109.100 true
```

Verify any first-connection host fingerprint against the trusted workstation's
record. Alternatively, create a dedicated WSL key with `ssh-keygen -t ed25519`
and have its **public** key authorized on the head; never send the private key.
For a passphrase-protected key, load it with `ssh-add` in a local SSH agent before
launching the app.

In **Connection…**, use:

| Field | Value |
| --- | --- |
| Hostname / IP | `31.56.109.100` |
| SSH user | `root` |
| Port | `22` |
| Key path | `~/.ssh/datacrunch_ed25519`, or your dedicated WSL key |

Choose **Save & connect**. Windows files are accessible through paths such as
`/mnt/c/Users/YOUR_WINDOWS_USERNAME/Downloads/construct.fasta`. Jobs continue on
the head if you close the app.

For later launches, open Ubuntu and run
`~/bio-workbench-rust/result/bin/gc-protein-engineering-console` (or the software launcher).
Slack's `bio-workbench://` links are not registered with the Windows browser by
this setup; Linux desktop registration inside WSL does not register a Windows
URL handler. Open batches through the app's run history for now.
