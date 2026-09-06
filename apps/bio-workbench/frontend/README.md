This directory contains the React/TypeScript renderer for the **Bio Workbench
Electron desktop app**. The native launcher and Nix packages are described in
[the app README](../README.md). The renderer is not offered as an external
browser interface.

All dependencies are pinned in `package-lock.json`. The production build bundles
React and [3Dmol 2.5.5](https://github.com/3dmol/3Dmol.js/releases/tag/2.5.5),
including the viewer's code and styles. No runtime CDN or external font request
is needed. 3Dmol uses the BSD-3-Clause license; its license notice is retained in
the generated bundle. The local viewer example uses the public experimental
[1UBQ reference](https://www.rcsb.org/structure/1UBQ), SHA-256
`056f98710cb2b36f633c45e41902a02eb446e82871da21ff2dd44f74a56ca0f6`.
It is labeled as reference data and does not submit predictions.

The transport uses the canonical [head RPC contract](../../../machines/head/workbench/CONTRACT.md)
through the app's local proxy. A persistent native CPU preview lists every
input/model pair and its compatibility result. Launch requires a separate user
action selecting compatible pairs. Lost responses reuse the existing request
key; they do not implicitly retry inference. Public MSA remains the default.
The installed catalog also exposes sequence analysis, variant ranking, and
backbone/sequence design with their actual constraints. RFAA is visibly parked.

Structure downloads are verified against their exact artifact SHA-256 before
parsing. Views support rotation, residue picking, labels, chain/element coloring,
cartoon/stick/sphere styles, linked cameras, and local PNG export. Camera linking
does not align coordinates. Native scalar confidence fields keep their original
names; pLDDT coloring is only enabled when the artifact explicitly declares
pLDDT in the atom B-factor field. Automatic opening uses verified QA-selected
outputs. Every raw sample remains manually inspectable with its QA state.
Chemical QA is separate from fold accuracy or demonstrated function.

Residue annotations bind to the structure hash, chain, residue number,
insertion code, and selected atom. Electron's native state stores added inputs
and local annotations across changing internal ports. Head annotation updates
use only explicitly viewed revisions or revisions from the user's own writes.
On reopen, current head notes are shown and divergent local copies remain
exportable. Local additions can be synced explicitly; pending offline deletions
are shown and exported, never automatically replayed. Late responses cannot
advance the visible revision baseline. Removing a residue annotation records a
hidden revision, preserving its head history. Shared text notes from
Harrison appear separately without assuming a coordinate selection. Annotation
JSON export/import retains the artifact binding. Imports for another structure
are refused. CSV/TSV and text/JSON/FASTA outputs have bounded previews plus the
unchanged full download. Interactive structures are limited to 64 MB and text
previews to 2 MB.

Development verification:

```sh
npm ci --ignore-scripts
npm run build
npm test
BIO_TEST_CHROMIUM=/path/to/chromium npm run test:browser
BIO_TEST_ELECTRON=/path/to/electron \
  BIO_DESKTOP_PYTHON=/path/to/python3 node tests/desktop-smoke.mjs
```

The Chromium tests exercise the renderer with a mocked head and real WebGL;
they do not launch the product in an external browser. The Electron test uses
isolated native state and a disabled SSH command, checks restart persistence,
the native File menu, and an actual PNG download. It requires Xvfb on Linux.
`tests/head-preview.mjs` is an explicitly invoked integration check against the
configured head. It only performs upload and CPU validation, and blocks every
`batch.create` request. It requires explicit evidence/runtime environment paths.
`tests/head-retained.mjs` inspects existing retained Protenix outputs and verifies
an exact structure download plus a labeled shared annotation across two fresh
native profiles. It hides only its own test note afterward and blocks all
upload, validation and inference requests.
No normal test starts paid inference.

The app enforces its CSP without `unsafe-eval`. Upstream 3Dmol contains an
optional string-callback evaluation path, which produces a bundler warning;
this renderer supplies only function callbacks and does not use that path.
