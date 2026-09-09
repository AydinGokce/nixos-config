# Existing construct inventory migration

`bio-library migrate` imports the already defined DNA sequences and protein
products in the schema-2 `~/constructs/constructs.sqlite` database. It performs
no translation, sequence repair, prediction, MSA request or cloud launch.

The migration preserves every inventory metadata field, all spreadsheet cells
(including nulls, formulas, hyperlinks and original colors), ordered GenBank
features and location parts, annotated translations, warnings, product evidence
and source review status. Whole plasmids and protein products are separate
constructs. The current inventory has 95 inventory entries, 96 plasmid sequences
and 87 existing product candidates. Fifty-two products are source-reference
matched; 35 remain review-required and cannot be submitted for prediction.

Names normally follow `pgc071-plasmid` and `pgc071-protein`. pGC016's two
sequence links retain the unambiguous suffixes `-g17` and `-f17` for both DNA
and products; there is no unsuffixed pGC016 alias. The original Clone 9 note is
retained without silently assigning a physical stock to the primary link.

All 183 molecular records are pinned members of
`project:gcc-germline-engineering@1`. The project holds the original main SQLite
file, a consistent SQLite backup, workbook, checksummed import mapping, source
archive and research-context archive as ordinary immutable attachments. The
source archive includes all 98 original files embedded in SQLite plus the
existing product manifest, source scripts and reports. Regular library backups
therefore include the entire shared provenance once. No separate unbacked
`imports/` directory is introduced.

Matched context descriptions require exact source-snapshot and Markdown hashes.
They describe research intent and do not change the source molecular identity,
clone ambiguity or product review status. Unmatched constructs retain inventory
descriptions/notes and explicit missing objectives for later manual work. The
original DOCX, extracted text and context review are retained with the project.

## Operator workflow

Run staging locally or on the head with the current library tools:

```bash
bio-library migrate freeze --source /path/constructs/constructs.sqlite --out /path/frozen
bio-library migrate stage --source-directory /path/constructs \
  --frozen /path/frozen --context /path/context --out /path/staged
bio-library migrate verify-stage /path/staged
```

`context/match-manifest.json` uses schema
`bio-library-context-matches.v1`, with the exact `source_snapshot.sha256`,
`source_docx` receipt, `project` (`id` and relative `markdown_path`), and `matches`.
Each match has `source_kind` (`sequence` or `protein_product`), source integer
sequence ID or string product ID in `source_id`, `construct_identifier`, relative
`markdown_path` and its SHA-256 in `markdown_sha256`. Additional matching
confidence, source paragraphs and evidence are retained verbatim.

Inspect `staged/migration.json`, the staged library, descriptions and backup
before publication. The recorded `migration_fingerprint` binds the frozen audit,
source/context bytes and migration engine. Record timestamps come from the
frozen audit, so identical frozen inputs produce identical record hashes.

After installing these tools on the destination and pausing library services,
initialize an empty target and publish with the explicit reviewed fingerprint:

```bash
bio-library --root /var/lib/bio-library init
bio-library migrate publish --stage /path/staged --root /var/lib/bio-library \
  --expected-fingerprint FINGERPRINT_FROM_MIGRATION_JSON
```

Publication restore-checks the staged backup into a private sibling directory,
takes the stable registry publication guard exclusively, and atomically exchanges
the empty target with the verified library. The previous empty root is retained
at the path returned in the receipt. A repeat recognizes exactly the same record
set and returns `already_published: true`; unrelated records, changed revisions,
unfinished staging, unknown files, missing/corrupt bytes or mismatched
fingerprints are refused. This publisher intentionally does not merge libraries.

Finally run `bio-library verify`, export and restore-check a backup, export the
project research context, and verify that context. These checks launch no models.
The migration is complete only after the head publication and local backup have
been verified; generating a stage alone does not alter production.

## Coordinate-derived protein revisions

The existing 87 imported protein products have exact, independently audited
coding footprints in their source plasmids. `migrate_derived.py` converts their
current definitions to source coordinates without changing peptide bytes.
Original immutable explicit-sequence revisions, source files, purpose documents,
Alt names, and product-review findings remain available. The migration advances
affected current project pins in the same durable transaction.

The operator first takes a verified backup, then runs `plan --root ... --audit
coordinate-audit.json --output migration-plan.json`. The plan binds every current
parent/product/project revision and source audit. `apply --root ... --audit ...
--plan ...` refuses stale or mismatching records and publishes the reviewed
revisions atomically. Repeating an applied plan verifies its immutable receipts
and makes no further revisions, including after subsequent unrelated edits.
Neither operation runs predictions. Three origin-crossing coding footprints use
ordered joined segments; all 87 translations were checked for exact equality.
