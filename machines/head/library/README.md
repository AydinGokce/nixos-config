# Reusable constructs and model inputs

`bio-library` manages the authoritative library on the head. Commands from the
workstation use the existing cluster SSH key. Records live on the head's local
disk at `/var/lib/bio-library`; database volumes and temporary GPU workers are
independent of this directory. No new API key is required.

The existing plasmid/product inventory is migrated under the single
`gcc-germline-engineering` project. See [migration mapping and operator
workflow](MIGRATION.md) for original-source archives, clone identities, context
descriptions and the reviewed publication process. Protein products marked
`review_required` remain visible but are rejected for model submission; whole
double-stranded plasmids require selection of a separately defined product.

```bash
bio-library import --fasta enzyme.fasta --type protein --id enzyme --alias target
bio-library import --fasta probe.fasta --type dna --id probe
bio-library import --sdf compound.sdf --id compound
bio-library list
bio-library show target
bio-library check target --model boltz2
bio-fold boltz2 --construct target --render
```

Use JSON for modifications, monomers and assemblies. Each physical chain gets
an explicit component, including repeated copies of the same construct:

```json
{
  "kind": "assembly", "id": "enzyme-probe", "name": "Enzyme and probe",
  "identity": {
    "components": [
      {"chain_id": "A", "construct_ref": "construct:enzyme@1"},
      {"chain_id": "B", "construct_ref": "construct:probe@1"}
    ],
    "bonds": []
  }
}
```

```bash
bio-library import --json enzyme-probe.json
bio-library check enzyme-probe --model protenix
bio-fold protenix --assembly enzyme-probe
bio-library revise enzyme --json enzyme-revision.json
bio-library snapshot enzyme-probe --out exact-input.json
```

Revisions are immutable. Assemblies retain the revisions with which they were
created; revising a component does not silently update an assembly. To use a new
component, revise the assembly too. Aliases resolve to the current revision at
submission; jobs then bind that exact revision and its complete chemical record.
The [schema](SCHEMA.md) documents all fields and reference rules.

Synthetic amidites belong in the monomer library with the **final incorporated
residue chemistry**, attachment atoms, supplier notation and original documents.
A supplier code alone is insufficient to infer a modified polymer's chemistry.
The library can retain unresolved or custom chemistry as a draft. Prediction
adapters accept only representations they can preserve. They do not replace an
unknown modification with its nearest ordinary base.
At present, no adapter accepts arbitrary custom amidites as polymer residues,
custom `linkages` or explicit `termini` fields. A modification can be
submitted to Boltz, Protenix, OpenFold3 or RF3 only when a supported CCD residue
represents the incorporated chemistry. Use a pinned `monomer_ref` whose monomer
`identity` contains only `ccd`; this is their shared representation.
Keep supplier annotations in `provenance`, `notes` and attachments. RFAA rejects
all residue modifications. Storing a complete custom monomer makes it reusable
and exportable even when these models cannot predict it.

## Research objectives and construct purpose

Each newly published construct or assembly carries `attachments/description.md`.
Supply your own Markdown with `--description` at import, or use `describe` to
publish a new immutable revision. Otherwise, the library creates an explicitly
unfinished template. Existing records remain readable; missing descriptions are
reported when exporting a project context.
After filling a template, remove its opening `incomplete` comment. Supplied
descriptions remain unassessed until their claims are checked against evidence.

Describe the intended function, mechanism or hypothesis, relevant domains and
residues, experimental conditions, testable success criteria, controls and known
limitations. Identify which conclusions a structural model could support and
which require biochemical or cellular measurements. Do not use confidence alone
as a proxy for activity, affinity, specificity or project success.

```bash
bio-library import --fasta enzyme.fasta --type protein --id enzyme --description enzyme.md
bio-library describe enzyme --markdown revised-purpose.md
bio-library describe enzyme
bio-library project --id enzyme-study --brief PROJECT.md --member enzyme
bio-library context enzyme-study --out ~/bio-projects/enzyme-study-v1
bio-library context-verify ~/bio-projects/enzyme-study-v1
```

A project brief belongs to a versioned `project` record. It should define the
research objectives, measurable criteria, priorities, operating conditions,
available assays, controls and resource limits. Project members bind exact
construct or assembly revisions. A member can also have a project-specific
`role` in an imported project JSON record. Describing or revising a construct
does not silently change an existing project: revise its complete `identity`
with the intended member references. `describe PROJECT --markdown FILE` revises
the brief while retaining the project's pinned members.

The exported workspace includes the project brief, descriptions, exact source
records, resolved assemblies, original attachments, an input manifest, an
`AGENTS.md` workflow and assessment templates. It runs no models. A future Codex
session can read the workspace, map the stated criteria to suitable tests, run
supported models through `bio-fold`, and record evidence and limitations under
`analysis/`. Preserve the exported input files; export a new workspace after
changing a project or construct. `context-verify` checks the frozen input files
while allowing ongoing analysis notes.

Assessment should distinguish a supported prediction, a contradictory result,
an inconclusive test and an unresolved need for experiment. Record the exact
project/construct revisions, model and checkpoint, MSA source, parameters,
seed, outputs, metrics and comparison controls for each conclusion. A completed
run or a well-folded prediction does not establish that a construct meets its
functional goals. Unfinished descriptions are a request for missing intent,
not permission to invent an objective.

Project briefs and all description revisions remain on the head and are
included in the existing verified local backups. An exported workspace is a
working copy; its later `analysis/` notes are local until explicitly saved back
as record attachments or backed up separately.

## Current adapters

| Input | Boltz 2.2.1 | Protenix 2.0.0 | OpenFold3 0.5.0 | RFAA pinned source | RF3 pinned source |
|---|---|---|---|---|---|
| Canonical protein / DNA / RNA, multiple chains | Yes | Yes | Yes | Yes | Yes; each polymer needs at least 4 residues |
| CCD residue substitutions | Native checks required | Native checks required | Native checks required | Rejected | Native chemical and feature checks required |
| Circular polymers | Yes | Explicit terminal bond | Yes | Rejected | Peptides, through native cyclic-chain conditioning |
| Small molecules | CCD / SMILES / SDF→SMILES | CCD / SMILES / single 3D SDF | CCD / SMILES / SDF→SMILES | SMILES / single V2000 SDF | CCD / exact SMILES / exact single SDF |
| Explicit covalent bonds | Named atoms, single bonds; CCD ligands | Named atoms, single bonds | Rejected: pinned pipeline ignores them | Rejected pending reliable atom mapping | Named single bonds only when retained in model token features; ordinary polymer crosslinks rejected |

These are supported input representations, subject to the selected model's
compatibility check. Protenix rejects MSE because its native loader converts it
to MET. Boltz rejects assemblies containing the same base sequence with different
modifications or circularity; OpenFold3 also rejects identical sequence strings
assigned to different polymer types. Those native entity-grouping cases would
otherwise merge distinct identities.

RF3 uses Foundry commit `b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c` and
AtomWorks 2.2.1. `bio-library check REF --model rf3 --msa-backend private`
checks the complete native assembly; it does not flatten it to one FASTA.
Both public and private RF3 preparation search every protein chain separately,
with canonical amino acids mapped to their letters and other CCD residues to
`X` in the MSA query. The native structural input retains the exact CCD code.
Server-paired rows retain their partners through explicit synthetic pairing
keys; these are not biological taxonomy IDs. Raw responses remain in the job.
There is no automatic single-sequence fallback.

RF3 compatibility checks execute the pinned native parser and actual chemical,
reference-conformer, chiral and bond transforms on CPU, without model weights
or MSA queries. They compare heavy atom identities, formal charges, ligand
connectivity/stereochemistry, valid reference masks and requested token bonds.
They retain native atom names and residue mappings in `preflight.json`.
For a custom SDF/SMILES covalent attachment, inspect those exact names first;
the adapter never guesses a supplied atom index. The original SDF bytes remain
in the immutable bundle. Ligand coordinates encode chemistry, not a requested
structural restraint.

RF3 also checks every predicted sample against the native atom inventory,
elements, formal charges, declared bonds and assigned tetrahedral/E/Z stereo,
using the predicted coordinates. It retains all raw samples and the original
ranking, then publishes the highest native-ranked sample that passes. If none
passes, the job fails with retained diagnostics. CPU input compatibility alone
does not guarantee output stereochemistry; these output checks also do not
establish folding or binding accuracy.

An ordinary protein-to-protein disulfide or other crosslink can survive RF3's
parser but be absent from the model's token bond features. The compatibility
check rejects that input. Circular peptides use RF3's explicit `cyclic_chains`
option and must pass the token-feature check. Reactions that cause the parser
to change heavy atoms, formal charges or existing bond orders are also rejected,
apart from the native peptide cyclization leaving oxygen. Arbitrary custom
polymer monomers, backbone/terminal chemistry, isotope/radical labels, enhanced
stereochemistry and special bond orders remain unsupported. Exact records stay
in the library for export or later model support.

The pinned RF3 model omits standard polymer terminal `OXT`/`OP3` atoms; the
preflight records these omissions explicitly. Its AtomWorks parser can retain a
temporary `atom_id` annotation for ligand-only input that conflicts with the
model's fresh indexing transform. The same source-verified compatibility helper
runs at preflight and inference, removing only that annotation from a private
copy and checking that every molecular array remains unchanged.

ESM accepts one canonical, linear protein from the library for its existing
scoring, embedding, logits and mutation commands. EVOLVEpro's library path also
exports only one protein and is useful for `--sub embed`. Variant ranking needs
a batch of candidate sequences; a molecular assembly is not a variant set.
Use the existing multirecord FASTA path for EVOLVEpro ranking, with measured
activities in a CSV containing `variant,activity`. FASTA IDs and CSV IDs must
match, and at least one candidate must remain unmeasured. Without labels, ranking
selects diverse candidates rather than fitting measured activity.

```bash
bio-fold esm --construct target --sub score
bio-fold evolvepro --construct target --sub embed
bio-fold evolvepro --fasta variants.fasta --labels activity.csv --sub rank
```

ProteinMPNN and RFdiffusion still require their structure/design inputs. AF3 is
outside this integration.

For the folding models' native inputs, `check` executes the installed model's
real CPU parser without inference or MSA queries. ESM, EVOLVEpro and the private
Boltz/Protenix/OpenFold3 path instead check that the record can be exported as
one canonical protein; their report says `native_parser: false`. Private RF3
checks the complete native assembly. Those checks do not
validate EVOLVEpro label files or execute its ranking pipeline. Checks are
serialized and limited to 6 GiB and ten minutes on the head. A passing check does
not prove that inference will succeed, fit GPU memory, or produce an accurate
structure. RFAA checks its input parsers and ligand features without constructing
the full model's assembly tensors.

Use canonical letters for portable inputs. Protenix can retain native `X` protein
or `N` nucleotide placeholders, but these remain unspecified residues; the other
folding adapters reject sequence ambiguity. Isotopes, radicals, unsupported
stereochemical features and unresolved attachment chemistry have additional
model-specific limits. Models can also reject particular CCD residues.
Input conformers may be regenerated by the model; retaining the original SDF
does not mean its coordinates constrain the prediction.
RFAA's ligand features omit explicit formal charges and alkene E/Z stereo;
its adapter therefore rejects those ligands while retaining their library records.

Native mixed assemblies use each model's established public MSA path where
protein MSAs are needed. RFAA uses its separate local HHsuite preparation. The
private ColabFold preparation path currently supports one canonical, linear,
unmodified protein for Boltz, Protenix and OpenFold3; use `--msa-backend private`
for that case. RFAA does not accept that flag. Unsupported private mixed
assemblies fail
without falling back or inventing chain pairing. Default prediction settings are
unchanged. Full private/public prediction-quality parity is a separate ongoing
validation.

Each job retains `library-input/` with the complete resolved source, original
attachments, native input, CPU parser report and checksummed manifest. `job.json`
records the source revision, source hash and bundle hash. The worker verifies
the bundle and compiler source before using it. Rejected compilations cannot
rent a GPU. Boltz, Protenix, OpenFold3 and RFAA refuse raw multirecord FASTA; use
an assembly to preserve chain boundaries. RF3 also accepts multirecord canonical
protein FASTA, assigning a separate chain to each record. Raw folding JSON must first be imported
as typed library records instead of passed through `bio-fold --json`.

Use recorded chain mappings when interpreting output. `library-input/bundle.json`
retains the requested chains and any model-specific mapping; a single-protein
FASTA export uses native chain `A` and records its source chain in `chain_mapping`.
RFAA groups proteins, nucleic acids and ligands in native order and may give
disconnected ligand fragments separate output chains. Its
`library-input/preflight.json` and prediction `native-runtime.json` contain
`output_chain_map`. Output labels need not equal the original assembly labels.
Choose single-character chain IDs for portability: RFAA requires them, Boltz
allows up to five characters, and Protenix/OpenFold3 allow up to 32.

## Operator setup and RFAA recovery

Native CPU checks require the deployed head configuration, its
`/etc/bio-tools/library-runtime.json`, and the existing `/mnt/bio-shared` mount.
Boltz, Protenix and OpenFold3 use their pinned packages in
`envs/{boltz,protenix,openfold3}/lib/python3.12/site-packages` below that mount,
including their chemistry/CCD data. Boltz needs `cache/boltz/mols`; Protenix uses
`protenix/release_data`. These checks do not install missing packages or data.
The canonical FASTA checks for ESM, EVOLVEpro and private MSA inputs do not load
those model environments. Restoring the molecular library alone does not restore
model environments, caches or the private RFAA runtime.

RFAA additionally needs a head-private CPython 3.10/RDKit runtime. Keep its inputs
with operator recovery files; they are outside `bio-library` backups. The verified
inputs are retained on the workstation in
`/home/aydin/bio-runs/rfaa-private-runtime-20260906/`:

| Artifact | SHA256 |
|---|---|
| `cpython-3.10.20-build20260610-dereferenced.tar.gz` | `750057aa4d6fd3e8842c46dc2dead272bc042b51cbf7fee9846499a2bc42fd12` |
| `rdkit-2024.9.6-cp310-cp310-manylinux_2_28_x86_64.whl` | `2b5573055c8defbad7ce25db10786a56e0699faf3282daea21100c59f7af7298` |

The CPython archive was made from the workstation's uv-managed
`/home/aydin/.local/share/uv/python/cpython-3.10.20-linux-x86_64-gnu`, build
`20260610`, with internal symlinks dereferenced and bytecode excluded. Use the
retained archive to reproduce that hash. RDKit is the unmodified PyPI wheel;
its exact download URL is recorded in `artifacts.json` and
[`rfaa_runtime.py`](rfaa_runtime.py). Keep `artifacts.json`, `receipt.json` and
`rfaa.json` with the two archives as provenance.

For a replacement x86_64 NixOS head, first deploy the head configuration and
restore/mount the existing shared packages and RFAA source. The source must retain
commit `d69ab3a73f8ede31a4cc005fbc076a341d848469` and the deployed recipe's
protein/template parser patches; preserve the source tree as well as its Git
commit. RFAA's shared Torch must be `2.0.1+cu118`. The provisioner reads this tree
and `/mnt/bio-shared/envs/rfaa/lib/python3.10/site-packages` without modifying them.
It does not use or repair the worker-specific shared `bin/python` symlink.

Transfer the retained input files to `/root/rfaa-runtime-inputs` on the replacement
head. Run the following as root there. The loader shown is from the verified
deployment; it must exist in the restored Nix closure. If deploying a different
Nix revision, select that revision's glibc loader and retain its generated
`library-runtime.json` library paths instead of copying the old `rfaa.json`.

```bash
RFAA_INPUTS=/root/rfaa-runtime-inputs
RFAA_LOADER=/nix/store/8kvxvr3pmsypxiypq4g8zy13glnfr7nx-glibc-2.42-67/lib/ld-linux-x86-64.so.2
RFAA_LIBRARY_ARGS=()
while IFS= read -r RFAA_LIBRARY; do
  RFAA_LIBRARY_ARGS+=(--library "$RFAA_LIBRARY")
done < <(jq -r '.library_paths[]' /etc/bio-tools/library-runtime.json)
test -f "$RFAA_LOADER" || exit 1
python3 /etc/bio-tools/library/rfaa_runtime.py \
  --python-archive "$RFAA_INPUTS/cpython-3.10.20-build20260610-dereferenced.tar.gz" \
  --python-sha256 750057aa4d6fd3e8842c46dc2dead272bc042b51cbf7fee9846499a2bc42fd12 \
  --rdkit-wheel "$RFAA_INPUTS/rdkit-2024.9.6-cp310-cp310-manylinux_2_28_x86_64.whl" \
  --loader "$RFAA_LOADER" "${RFAA_LIBRARY_ARGS[@]}"
bio-library check target --model rfaa
```

Use an existing restored protein or assembly reference instead of `target`.
The provisioner verifies artifact hashes, probes CPU imports, creates an immutable
generation and atomically publishes `/var/lib/bio-library-runtime/rfaa.json` only
after success. The runtime root must be a real directory accessible only to root
(`0700`). Nix loader/library dependencies receive GC roots under
`/nix/var/nix/gcroots/bio-library-rfaa`. Each later check verifies the private
generation inventory and its bound configuration. A matching intact generation
can be reprobed by rerunning the command; a damaged generation fails verification
and must be restored from trusted inputs before use. Do not edit its receipt or
configuration to bypass that failure.

## Local backups

The workstation's `bio-library-backup.timer` pulls hourly snapshots, with a
persistent catch-up after downtime. The workstation and SSH connection must be
available. Backups live at `~/bio-library-backups` with private permissions.
Every successful automatic or `backup` snapshot is checksum-verified and
restored into an isolated directory before it becomes `latest.json` or old
snapshots are pruned. Retention
keeps the newest snapshot in 24 hourly, 30 daily and 12 monthly buckets.
Each backup directory is bound to its head and library root in `origin.json`,
and export receipts record the same origin. Use a separate
`bio-library backup --destination DIR` for each head or project library;
reusing a bound directory for another origin fails before transfer or pruning.
Legacy archives without a known origin are preserved rather than assigned an
origin from their molecular contents.

```bash
bio-library backup
systemctl --user status bio-library-backup.timer
cat ~/bio-library-backups/latest.json
bio-library restore-local --from ~/bio-library-backups/library-TIMESTAMP.tar.gz --to ~/restored-library
```

A restore destination must be absent or empty. Restoration does not overwrite
the head library. The index is rebuilt from authoritative JSON and attachments.
Backup archives include all molecular data; keep them with your other private
project files. A separately named test/project library can be selected with
`BIO_LIBRARY_REMOTE_ROOT=/absolute/head/path`; the default stays the authoritative
production library.
