# Construct registry schema 1

The head is the authoritative writer for reusable molecular records. Model input
files are exports of pinned records. A stored record does not imply that every
model supports its chemistry.

## Files and revisions

The default root is `/var/lib/bio-library`, on the head's local filesystem:

```text
bio-library/
  constructs/<id>/<revision>/record.json
  constructs/<id>/<revision>/attachments/<attachment-name>
  monomers/<id>/<revision>/record.json
  assemblies/<id>/<revision>/record.json
  projects/<id>/<revision>/record.json
  index.sqlite3
  .registry.lock
  .staging/
```

All four record kinds can have attachments. IDs start with a lowercase letter,
use lowercase letters, digits, hyphens or underscores, and are at most 64
characters. IDs and aliases share one case-sensitive namespace. Historical
aliases remain reserved for their original entity, and resolve to its latest
revision. The complete immutable reference is `construct:enzyme@3`,
`monomer:modified-a@1`, `assembly:enzyme-oligo@2`, or `project:binding-study@1`.
The importers retain original bytes under reserved names such as `source.fasta`,
`source.sdf` and `source.json`; custom attachments use their supplied names.

Revising creates a new directory and adds the previous revision to `parents`.
Old bytes are never changed. A revision from an explicitly stale reference is
rejected, preventing accidental overwrites of someone else's newer edit. A
revision patch replaces each supplied top-level field; in particular, supplying
`identity` replaces the entire identity object, not selected nested keys.
Attachments are carried forward unless a same-named attachment is replaced in
the new revision. Parent revisions retain their original attachments.

The SQLite index is a disposable search cache. Authoritative reads validate the
JSON records and attachment hashes; corrupt or missing indexes can be rebuilt
without affecting molecular identity. All publication uses the same file lock,
private staging directories, file/directory fsyncs and an atomic directory
rename. If index creation fails after publication, the revision remains valid;
inspect the ID and use `reindex`. Do not retry by manually removing a revision.
An abandoned `.staging` directory is never a published revision or backup input.

## Common record

Import JSON contains the following user-owned fields:

```json
{
  "schema": 1,
  "kind": "construct",
  "id": "enzyme",
  "name": "Target enzyme",
  "aliases": ["target-enzyme"],
  "tags": ["pilot"],
  "notes": "The exact original input is attached.",
  "parents": [],
  "status": "defined",
  "identity": {"molecule_type": "protein", "sequence": "ACDEFGHIK"},
  "provenance": {"source": "internal design"}
}
```

`kind`, `id` and `identity` are required. Other fields default to schema 1, an
ID-based name, empty lists/text/provenance, and `status: draft`. The registry adds
`revision`, `created_at`, `attachments` and `sha256`; imports and revision patches
cannot supply those managed fields. An attachment receipt is
`{"path":"attachments/source.sdf","bytes":1234,"sha256":"..."}`.

Provenance is an ordinary JSON object for source identifiers, attribution,
supplier notation, original design references and interpretation notes. Do not
put credentials or API keys into records. Preserve supplier notation and source
documents even when a separate molecular representation is available.

`defined` records state the intended identity under their declared conventions;
they are not a prediction-readiness flag or a chemical validation certificate.
Canonical protein/DNA/RNA FASTA imports use standard linear polymer conventions
and default to `defined`. Ambiguous or noncanonical FASTA symbols default to
`draft`. Generic JSON, SMILES and SDF imports default to `draft`, preserving
unresolved chemistry. Only a model adapter can establish which fields its
specific version supports.

## Project briefs and purpose documents

Projects use the same immutable revisions, alias rules, lock, index and backup
format as molecular records. Their membership provides research context; it
does not create a molecular assembly or authorize a model run.

```json
{
  "kind": "project",
  "id": "binding-study",
  "name": "Binding study",
  "identity": {
    "objectives_file": "attachments/project.md",
    "members": [
      {"source_ref": "construct:enzyme@2", "role": "Evaluate substrate binding."},
      {"source_ref": "assembly:enzyme-complex@1", "role": "Assess the proposed interface."}
    ]
  }
}
```

Every project publication requires an attached `project.md` containing nonempty
UTF-8 Markdown, at most 1 MiB. Original bytes and newlines are preserved.
Membership aliases are resolved under the publication lock and stored as exact
construct or assembly revisions. Monomers and other projects cannot be members;
an empty member list is allowed while a project is being defined. Multiple
revisions of one construct can be compared in one project, but an exact member
revision cannot be repeated. Each member's `role` belongs to that project
revision. There are no mutable backlinks in construct records.

New construct and assembly publications automatically include
`attachments/description.md` when no description was supplied or inherited. The
scaffold asks for intended function, hypotheses, testable success criteria and
evidence limitations. Its explicit marker
`<!-- bio-library:purpose-scaffold:v1 incomplete -->` means it is unfinished.
A user-written document is unassessed, never automatically considered complete
or evidence of function. Criteria should specify an observation or metric,
threshold, test conditions, method and required evidence. Prediction confidence
and chemical output validation do not demonstrate biological function.

`Registry.describe(ref, markdown_path)` publishes a new immutable revision with
replacement `description.md` (construct/assembly) or `project.md` (project),
preserving the molecular identity and all other attachments. It rejects an
explicit stale revision. Chemical revisions inherit the previous purpose bytes;
their applicability must be assessed, not silently assumed. Existing records
without descriptions remain readable and unchanged. New revisions of such
legacy records receive an explicitly incomplete scaffold.

`Registry.project_snapshot(ref)` resolves a consistent project under one shared
lock. Its `resolved-project` document includes `project_ref`, the exact
`project_record`, and `members`, each with `source_ref`, `role`, a complete pinned
molecular `snapshot`, and a `description` receipt (`present: false` for a legacy
missing document). Assembly snapshots also bind the individual component
records and their description attachment receipts. The project's top-level
SHA-256 binds all nested records, molecular snapshots and document checksums.
Later project, molecule or purpose revisions do not change an older snapshot.
Assessment records should retain this exact project snapshot digest and the
source references they evaluated.

The additive `project` kind keeps schema version 1. Libraries and backups made
before the `projects/` collection existed remain readable and restorable.

## Molecular identity

Construct `identity.molecule_type` is `protein`, `dna`, `rna`, `small_molecule`,
or `mixed_polymer`. Type is never inferred from letters: `ACG` has different
meanings for a protein, DNA and RNA.

For ordinary polymers, `sequence` is a nonempty uppercase sequence in the
declared alphabet. A FASTA importer removes FASTA whitespace and uppercases
letters, records this formatting operation in provenance, and retains the
original file. Generic JSON must already have the intended case. Unknown vendor
modification codes are not stripped or mapped to ordinary bases.

Protein alphabets include the standard amino acids and explicit ambiguous or
noncanonical symbols `X B Z U O J`. DNA and RNA accept their respective standard
letters plus IUPAC ambiguity symbols. Acceptance into a draft record is separate
from prediction support.

Modifications and connections use explicit, **1-based residue positions**:

```json
{
  "molecule_type": "rna",
  "sequence": "ACGU",
  "modifications": [{"position": 2, "monomer_ref": "monomer:modified-c@1"}],
  "linkages": [{"position": 2, "monomer_ref": "monomer:custom-linkage@1"}],
  "termini": {"5_prime": {"monomer_ref": "monomer:custom-end@1"}},
  "circular": false
}
```

`linkages[].position` identifies the connection after that residue, so a linear
sequence of length four has link positions 1–3. For an explicitly circular
polymer, position four describes its closing link. Alternatively, a linkage can
specify `from_position` and `to_position`. `modifications` and `linkages` must be
lists of objects; positions are integers, not floats, booleans or strings.

Mixed polymers can use an explicit `residues` list of symbols or residue objects
containing `monomer_ref`, retaining type information in those objects. If a
record contains both `sequence` and `residues`, their lengths must agree.
Unknown identity fields are retained for future adapters; they are never
silently interpreted by the registry. Adapters must reject unsupported semantic
fields rather than discarding modifications.

Monomer records describe the **incorporated chemical group**, its attachment
sites and known stereochemistry. They may retain a CCD identifier, a structure
attachment, an exact SMILES string, or an unresolved descriptive definition.
The synthesis reagent, protecting groups and supplier product name are separate
provenance, not automatically the final incorporated residue. This registry
does not infer a deprotected structure from an amidite name.
Storing that definition does not enable a model to use a custom amidite. Current
Boltz, Protenix and OpenFold3 modification adapters resolve monomers whose
`identity` contains only `ccd`; other chemical identity fields, custom `linkages`
and explicit `termini` are rejected. Put annotations for a CCD monomer
in `notes`, `provenance` or attachments. RFAA currently rejects all modified
residues. See the [adapter limits](README.md#current-adapters) before submission.

Recognized chemical references are `monomer_ref`, `monomer_refs`,
`construct_ref` and `construct_refs`, including nested terminal definitions.
These references are resolved and pinned when a revision is published; stored
records never contain a floating `latest` reference. Construct references belong
to assemblies. Parent references describe lineage and do not silently add
molecular components.

For a CCD substitution, use the existing `modifications` shape with a pinned
`monomer_ref`. Boltz and OpenFold3 also accept an inline `ccd` instead of
`monomer_ref`; Protenix requires the monomer reference. A `status: defined` value
does not bypass any native compatibility check.

Small molecules can use exact `smiles` text, `ccd`, or a source structure:

```json
{
  "molecule_type": "small_molecule",
  "structure_format": "sdf",
  "structure_file": "attachments/source.sdf"
}
```

The referenced attachment must exist and its bytes are hashed. The core does
not sanitize a graph, neutralize charges, select tautomers, remove counterions,
choose stereoisomers, infer unspecified stereochemistry, or extract one member
of a multirecord SDF. SDF/SMILES syntax and model compatibility are checked by an
adapter before a prediction. Preserve separately supplied chemical forms as
separate records or revisions.

## Assemblies and bonds

Assemblies identify the intended molecular components, not physical stock
solutions. Each component has a distinct chain ID and a pinned construct
revision; repeat a construct with a new chain ID to specify multiple copies.
There is no implicit component `count` expansion.

```json
{
  "kind": "assembly",
  "id": "enzyme-complex",
  "identity": {
    "components": [
      {"chain_id": "A", "construct_ref": "construct:enzyme@1"},
      {"chain_id": "B", "construct_ref": "construct:enzyme@1"},
      {"chain_id": "L", "construct_ref": "construct:ligand@1"}
    ],
    "bonds": []
  }
}
```

An explicit bond uses `from` and `to` endpoints. Each endpoint has `chain_id`, an
explicit `atom` name or index, and a 1-based `position` for polymer residues.
For example:

```json
{
  "from": {"chain_id": "A", "position": 2, "atom": "SG"},
  "to": {"chain_id": "L", "atom": "C7"},
  "order": "single"
}
```

Atom naming/index conventions must be explicit in the source chemistry or
adapter; the registry does not guess how a SMILES atom corresponds to an SDF
atom. Endpoint chain and residue bounds are validated. Construct-local `bonds`
and `crosslinks` use the same endpoint shape, with optional chain ID `A`; they
remain in that construct's identity when included in a resolved snapshot.

Physical lots, vendor orders, concentration, storage locations and measurements
can be linked through provenance or separate experiment/sample records. They
should not silently change the reusable molecule's identity.

## Resolved prediction snapshots

`Registry(root).snapshot(reference)` or `snapshot REF` returns:

```text
{
  schema: 1,
  kind: "resolved-assembly",
  name: ...,
  source_ref: "assembly:enzyme-complex@1",
  components: [{chain_id: "A", construct_ref: "construct:enzyme@1", record: {...}}],
  bonds: [...],
  monomers: {"monomer:modified-c@1": {...}},
  provenance: {registry_source_ref: ..., source_record_sha256: ...},
  assembly_record: {...},
  sha256: ...
}
```

`assembly_record` is present for assembly sources so unrecognized assembly
semantics remain visible to adapters. A single construct becomes chain `A`.
The snapshot contains all referenced monomer revisions, including transitive
monomer definitions and terminal modifications. Snapshot digests remain stable
when unrelated or newer revisions are added to the registry. The adapter can
find an original file with `Registry.attachment_path(ref, relative_path)`.

For records, resolved snapshots and backup manifests, `sha256` is SHA-256 of
UTF-8 `json.dumps(document_without_top_level_sha256, sort_keys=True,
separators=(",", ":"), ensure_ascii=False, allow_nan=False)`. Nested receipts
remain part of the digest. Original file checksums hash the exact file bytes.

## Commands

```bash
bio-library init
bio-library import --fasta enzyme.fasta --type protein --id enzyme --alias target
bio-library import --fasta aptamer.fasta --type rna --id aptamer
bio-library import --smiles 'C[C@H](O)Cl' --id candidate
bio-library import --sdf compound.sdf --id ligand
bio-library import --json construct.json --attachment notes.txt=source-notes.txt
bio-library list --type protein --tag pilot
bio-library show target
bio-library revise target --json revised-identity.json
bio-library snapshot enzyme --out frozen-input.json
bio-library verify
bio-library reindex
```

On the workstation, file imports are uploaded and `snapshot --out` saves its
result locally. Set `BIO_LIBRARY_REMOTE_ROOT=/absolute/head/path` to select
another head library. On the head itself, registry commands accept `--root`
before the command and default to `BIO_LIBRARY_ROOT` or `/var/lib/bio-library`;
the separate `check` command accepts `--root` after `check REF`. For example:

```bash
# Run these two commands on the head.
bio-library --root /var/lib/test-library list
bio-library check target --root /var/lib/test-library --model boltz2
```

FASTA import accepts exactly one record; use an assembly for multiple molecular
chains. JSON/SDF/FASTA imports retain their original input as an attachment.
Revision JSON is also retained. Use `list --all-revisions` to inspect history.
Most commands return JSON; workstation `snapshot --out` prints its saved path.

## Backups and restore

These lower-level commands run on the head, and all paths in this block are
head paths:

```bash
bio-library export-snapshot --out /some/backup/path/library.tar.gz
bio-library verify-backup /some/backup/path/library.tar.gz
bio-library restore /some/backup/path/library.tar.gz --destination /some/empty/path
```

The workstation wrapper downloads `export-snapshot --out` to a local path.
Use `backup` for the periodic-backup workflow, and `restore-local` to test or
recover a local copy without modifying the head:

```bash
bio-library backup
bio-library restore-local --from ~/bio-library-backups/library-TIMESTAMP.tar.gz --to ~/restored-library
```

The exporter holds a shared registry lock, hashes authoritative record files
and attachments, creates a tar.gz beside its destination, and verifies every
archived byte against its manifest before atomic publication. It excludes the
SQLite cache, lock, staging files and model output caches. `manifest.json` binds
each relative path, size and SHA-256; the export receipt also hashes the complete
archive. A failed export does not replace the previous backup.

Restoration requires an absent or empty destination. It rejects traversal,
duplicate members, symlinks, hard links and special files. It copies validated
regular files into a private sibling directory, rebuilds the SQLite index,
verifies all records, attachments and reference closure, then publishes the
restored registry. It never merges into an existing nonempty registry. A restore
test therefore checks chemistry references and original bytes as well as archive
integrity. These archives contain the molecular data in plaintext; backup
transport and destination permissions belong to the workstation backup wrapper.
