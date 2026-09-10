# Bio workbench RPC v1

The native desktop and Harrison use the same JSON-line RPC on bio-head. The fixed SSH
command is `/run/current-system/sw/bin/bio-workbench rpc`. The native app sends
JSON lines directly over SSH. Workbench RPC has no head HTTP listener;
the separate public MSA transport uses a loopback-only HTTP CONNECT proxy.
Each request is
`{"id":"client-id","method":"catalog","params":{}}`; each response is
`{"id":"client-id","result":...}` or
`{"id":"client-id","error":{"code":"invalid","message":"..."}}`.
The trusted SSH command sets `BIO_WORKBENCH_ACTOR`; client parameters cannot
select an actor. Resources are scoped to that actor. The app and Slack share
resources when their trusted commands use the same actor.
The construct library is shared by authenticated operators. Its curation methods
publish immutable revisions; reading or editing it does not grant access to
another actor's jobs or uploads. Undo/redo history belongs to the trusted actor.

Limits: 2 MiB per wire message, 512 KiB decoded upload/read chunks, 256 MiB per
upload, 128 declared inputs, 512 expanded input/model pairs, 100 list entries.
JSON numbers must be finite; duplicate keys and unknown method/parameter names
are rejected. IDs are opaque strings. Times are UTC ISO 8601. Error codes include
`invalid`, `not_found`, `conflict`, `limit`, `unavailable`, `integrity`, `internal`.

## Construct library explorer

The authoritative registry is the operator-configured `library_root` (normally
`/var/lib/bio-library`). Clients cannot choose its path. These methods read
published records and validate their JSON and attachment hashes. Listing,
reading, archiving and editing the library never query MSA services or launch
work. Molecular model compatibility remains part of the existing run preview.

* `library.list {query?,kind?,molecule_type?,project_ref?,limit?,offset?,archived?}` returns
  `{records,projects,counts,total_count,filtered_count,next_offset,truncated,project_ref,
  archived,archived_count}`.
  Default limit is 100, maximum 500. `counts` uses singular kind keys. The ordinary
  list contains latest revisions; a project filter returns its exact pinned
  member revisions, including older revisions. Search matches all case-insensitive
  words against IDs, names, aliases, tags, notes and provenance. Summary records
  contain `ref,kind,id,name,revision,sha256,status,molecule_type,aliases,tags,sequence_length,
  molecular_form,review_status,review_reason,submission_allowed,encoded_by_ref,member_count,
  inventory_id,alt_name,verbose_name,modality,archived,derivation_kind,parent_ref`.
  Derived sequence lengths are computed from the pinned source. The boolean `archived`
  filter defaults to false; true returns archived entities. Visibility follows
  the latest archive state even when an older project pins the entity's earlier
  revision. Archiving a project does not archive its members.
* `library.get {ref}` returns `{ref,record,description,members,relations,revisions,
  submission,projects,sha256,inventory_id,alt_name,verbose_name,modality,archived,
  latest_ref,is_latest}`. Aliases resolve to an exact returned `ref`. `record` is
  the complete published record without removing user-owned provenance keys.
  `description` is null or `{text,path,sha256,incomplete}`; projects use their
  `project.md`, molecular records use `description.md`. Member summaries also
  contain `source_ref` and `role`; relations use `{relation,ref,label}`. Historical
  revisions and containing projects are summaries. Containing projects include
  historical project revisions that pin this exact member, even when the latest
  project has moved on to a newer member revision. `submission:{allowed,reason}`
  indicates whether the record may be added to the composer. Native model
  compatibility remains a separate preview check. Unresolved product candidates
  and whole double-stranded plasmids cannot be submitted as ordinary fold inputs.
  Molecular details also include `sequence_view`; it contains the translated
  peptide for derived proteins but omits duplicate explicit sequences already
  present in `record.identity.sequence`. The complete response is bounded below
  the 2 MiB RPC limit; oversized details require sequence/attachment reads.
* `library.sequence {ref,min_orf_aa?,genetic_code?}` returns an exact-revision
  sequence view: `ref,molecule_type,sequence,length,available,issues,circular,
  derivation_kind,parent_ref,translation,features,orfs` and truncation/count
  metadata. Default minimum ORF length is 30 amino acids; genetic codes 1 and 11
  are supported. It scans all six frames, including circular origin crossings.
  Imported annotation segments retain strand, partial and stale-evidence flags.
  Stored coordinates are zero-based half-open in biological traversal order;
  user-facing coordinates are one-based inclusive. No model or registry write occurs.
  Derived proteins also expose their exact pinned nucleotide `source` with record
  and sequence SHA-256 values, and `codon_positions`, one genomic position triplet
  per displayed amino acid in biological order. The authoritative peptide retains
  CDS initiator overrides and residue cropping. `codon_positions_complete` and
  `source.complete` distinguish complete alignments from omitted bulk data;
  `terminal_stop_positions` optionally identifies the terminal stop triplet.
  Invalid products still expose a valid source for frame correction. Alignment
  limits are 1,000,000 source bases and 16,384 residues; an exceeded size/response
  budget omits whole alignment fields with explicit diagnostics. Annotation
  metadata has a separate 256 KiB limit, within the 2 MiB response envelope.
* `library.product_preview {parent_ref,translation}` returns the canonical
  definition and `parent_ref,parent_sha256,available,sequence,length,issues`.
  `parent_sha256` binds the parent record, not just its DNA sequence. This is
  read-only and returns diagnostics for biologically unavailable definitions.
* `library.product_create {parent_ref,expected_sha256,translation,alt_name?,request_key}`
  creates a derived protein and adds it to the current parent's projects.
  `library.create {project_ref,expected_sha256,sequence,alt_name?,request_key}`
  creates a standalone protein in that project. Both require an exact current
  parent/project record SHA and return the edit-response shape. New entities
  have `before_ref:null`; clients must not match that value against an existing
  selection. Retrying the same request is idempotent. Undo archives a creation;
  Redo restores it.
* `library.attachment {ref,name,offset?,length?}` returns
  `{ref,name,data_b64,offset,next_offset,eof,size,sha256}`. Use a pinned reference
  throughout a download. `name` can be the plain filename or its recorded
  `attachments/filename` path. Length defaults to 131072 and is at most 262144
  bytes. The size and SHA describe the complete original file; clients verify
  both before publishing an export. Arbitrary filesystem paths are rejected.
* `library.edit {ref,expected_sha256,request_key,patch}` publishes a new revision.
  The reference must be pinned and current, with its exact displayed SHA.
  Supported patch fields are `alt_name` for molecular records, `name` for
  projects, `archived` (boolean), and `sequence` for explicit protein/DNA/RNA
  constructs. `sequence_edit:{start,end,replacement}` expresses an exact splice
  instead of replacing the whole sequence. Derived proteins use `translation`
  and optionally `parent_ref`; direct peptide editing is rejected.
  `frame_offset:0|1|2` changes a derived protein's actual reading frame within its
  existing coding footprint, keeping strand, genetic code and residue crop.
  It cannot be combined with a sequence, parent or full-definition patch.
  A changed phase uses translation schema 2, literal initiation and
  `stop_policy:first_stop`: translate complete codons through the first in-frame
  stop and ignore a trailing partial codon. Empty peptides, encountered ambiguous
  codons and invalid crops remain unavailable. An unchanged phase preserves the
  entire definition, including schema-1 CDS initiation. Existing schema-1 records
  and snapshots keep their original translation behavior. Frame changes use the
  same revision checks and durable undo/redo as other edits. Parent edits
  advance every current derived product in the same transaction. Ambiguous
  remapping or an invalid coding region yields unavailable diagnostics rather
  than retaining the old peptide. The response is `{operation_id,ref,changed_refs,changed,history}`;
  each changed-ref pair contains `before_ref,after_ref`. Reusing the same request
  key with identical parameters returns the durable receipt; changing parameters
  under the same key conflicts. Current projects pinning the predecessor advance
  with the molecular edit in one recoverable registry transaction. Earlier
  revisions and source attachments remain unchanged.
* `library.history {}` returns `{undo,redo,undo_count,redo_count}` for this actor.
  Each available operation contains `operation_id,label,before_ref,after_ref`.
  `library.undo {operation_id,request_key}` and `library.redo` with the same
  parameter shape publish compensating revisions and return the edit-response
  shape. They require the applicable history entry and reject edits that would
  overwrite an intervening incompatible change. Receipts are retained in
  immutable provenance and survive ordinary library backup/restore.
* `library.runs {ref,limit?,cursor?,include_revisions?}` returns
  `{ref,records,next_cursor,include_revisions,scope,match}`. Default limit is 30,
  maximum 100; `cursor` is the prior page's last job ID. It includes the construct's
  other revisions by default. Each row has job/batch IDs, input and batch names,
  model, state, timestamps, exact `source_refs`, `selected_revision`,
  `artifact_count` and `structure_count`. Associations require an explicit pinned
  library input or a pinned assembly containing the construct. Names, sequence
  similarity and floating aliases are not used to assign old jobs. Both job and
  batch must belong to the requesting actor. Existing job/artifact methods open
  results with their existing access controls.

Imported Alt names come from `provenance.inventory.alt_orf_name`; a curated
`provenance.workbench.alt_name`, including an explicit empty string, overrides
that display field. The verbose source name and inventory identifier remain
separate. Archive state is retained in `provenance.workbench.archived` and never
deletes data. Ordinary sequence edits require uppercase symbols in the declared
alphabet, without whitespace or a FASTA header, and at most 1,000,000 symbols.
Explicit modifications, linkages, bonds or structural chemistry require a
complete remapped molecular definition and cannot be discarded by this editor.
A changed sequence preserves prior derivation/reference claims as historical
provenance and becomes an explicit user-defined input; its original source
annotations and purpose text require reassessment. A no-op edit does not clear
source review findings.

Projects and purpose documents provide research context. They do not constitute
findings or authorize an engineering campaign. The original source database,
context document and import evidence are retained as project attachments.

## Catalog and uploads

`catalog {}` returns `{version:1,limits:{...},models:[...],input_formats:[...],
msa_backends:["public","private"],executions:["auto","resident","ephemeral"]}`.
A model has `id`, `name`, `workflow`, `enabled`, `disabled_reason`,
`molecule_types`, `input_formats`, `settings` (typed allowed options), and
`description`. Four folding models are Boltz2, Protenix, OpenFold3, and RF3.
Other installed workflows are listed with their actual input constraints. RFAA
is visibly disabled; AF3 is absent. Catalog compatibility is only guidance:
actual native CPU validation decides every submitted pair.

* `upload.begin {name,size,sha256?}` returns
  `{upload_id,name,size,offset:0,chunk_bytes:524288,state:"uploading"}`.
* `upload.chunk {upload_id,offset,data_base64}` returns `{upload_id,offset}`.
  `offset` in the response is the next byte. An exact repeated chunk is
  idempotent; divergent or gapped bytes conflict.
* `upload.get {upload_id}` returns `{upload_id,name,size,offset,state,sha256?}`.
* `upload.finish {upload_id,sha256}` requires SHA-256 and exact declared size;
  returns `{upload_id,name,size,sha256,state:"complete"}`. Completed bytes are
  immutable. Filenames are display labels, never paths.

## One-click runs and optional manual previews

`batch.run` accepts the same parameters as `batch.validate` below and records a
durable request to validate and execute all compatible input/model pairs. It
returns Batch immediately with `auto_run:true` and `state:"validating"`. No
subsequent `batch.create` call or confirmation is needed. The head completes
native CPU checks, retains each rejected pair and its reasons, then atomically
queues every compatible pair after successful validation completion is recorded.
Closing or reconnecting the GUI does not interrupt this continuation.

The `request_key` is actor-scoped. Resending the same normalized request returns
the original batch at its current state; changing its payload conflicts. After
an uncertain or lost response, retry the identical request and key rather than
creating a new key. Daemon restart recovers the recorded validation receipt and
continues the same run; queue publication and pair/job links commit together, so
recovery cannot duplicate jobs. Native input/source checks and the existing
execution, chemistry and budget gates remain in force; failed or ambiguous model
executions are never automatically retried.

An omitted `msa_backend` defaults to `private` for `batch.run`; explicit `public`
and `private` selections are honored. Workflows without MSA (ESM, EVOLVEpro,
ProteinMPNN and RFdiffusion) treat that selection as inapplicable and perform no
public or private search. Their job provenance records `msa_applicable:false`,
`msa_backend:"not_applicable"` and the original `msa_backend_requested`; folding
models retain the selected backend and its compatibility checks.

All-rejected runs finish as `validation_failed` with no jobs. Mixed runs keep
rejected pairs visible while compatible jobs execute; if those jobs succeed,
the final batch state is `partial`. `batch.cancel` also applies during validation
or while awaiting automatic queue publication. Its transaction either prevents
publication or cancels the newly queued jobs through the existing job controls.
A known disabled model is rejected per pair and cannot block supported models;
unknown model IDs or malformed requests/settings remain request errors.

The manual preview flow remains available for clients that explicitly need to
choose a subset:

Validation is asynchronous CPU work and does not launch paid inference.
`batch.validate` creates a durable preview and returns the Batch below. Its
omitted MSA default remains `public`. Poll
`batch.get {batch_id}` until its state is `validated` or `validation_failed`.
Every expanded input/model pair appears in `pairs`, including rejected pairs
with specific reasons. Nothing silently drops an incompatible selection.

Example parameters:

```json
{
  "request_key":"preview-client-uuid",
  "name":"Ubiquitin comparison",
  "mode":"batch",
  "inputs":[{"id":"sequence-1","name":"ubiquitin","molecule_type":"protein",
    "source":{"kind":"text","format":"sequence","text":"MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"}}],
  "models":["protenix","openfold3"],
  "msa_backend":"public",
  "execution":"auto",
  "settings":{"protenix":{},"openfold3":{}}
}
```

Input shape: `{id,name,molecule_type,chain_id?,source}`. Molecule types are
`protein`, `dna`, `rna`, `ligand`, `assembly`, `structure`. Source is one of:
`{kind:"text",format,text}`, `{kind:"upload",format,upload_id}`, or
`{kind:"library",ref}`. Formats: `sequence`, `fasta`, `smiles`, `ccd`, `sdf`,
`pdb`, `mmcif`, `library-json`. An uploaded `library-json` contains native
registry records, not arbitrary model JSON or filesystem paths. Attachments use
`attachments:{"source.sdf":"completed-upload-id"}` on the source (plain filenames;
the record's `structure_file` is `attachments/source.sdf`). A library-json may be
one registry record or `{records:[...],entrypoint:"assembly:example@1"}` with
referenced monomers/constructs declared before their users.
Library refs are resolved to immutable revisions during validation.

Batch mode expands FASTA records for folding and sequence scoring. Workflows
that require a sequence set, such as EVOLVEpro ranking, keep that declared FASTA
together. Assembly mode combines typed components in order, with unique
`chain_id` values (otherwise A, B, ...); it preserves declared chemistry and
explicit registry assembly bonds. Raw coordinate workflows keep their uploaded
structure. Model settings are typed catalog options; arbitrary CLI flags are
not accepted. Native defaults are preserved unless a permitted option was
explicitly supplied. Private MSA and native chemistry limitations are checked
before submission. No preview performs MSA searches or model inference.

`batch.create {batch_id,request_key,pair_ids:["pair-id",...]}` commits exactly
those **compatible, fully validated** pairs, all or nothing, and returns Batch.
Selection is required and cannot include rejected/pending pairs. Validation and
creation keys are independent and actor-scoped: identical requests return the
same resource, changed payloads with the same key conflict. A preview can be
committed once; retries never create extra jobs. To change inputs/settings or
retry a failed inference, create an explicit new preview/batch.
`batch.create` rejects batches created by `batch.run`, whose compatible pairs
are already owned by the automatic continuation.

`batch.list {limit?:50,cursor?:string}` returns `{batches:[BatchSummary],next_cursor}`.
BatchSummary includes identifiers, name, mode, state, timestamps, models,
backend/execution, `auto_run:true` for automatic runs, and counts; it omits
inputs, pairs, and jobs.
`batch.get {batch_id}` returns Batch. `batch.cancel {batch_id}` requests
cancellation and returns Batch. Completed jobs and artifacts are retained.

Batch:
```json
{
  "batch_id":"opaque","name":"example","mode":"batch","state":"validated",
  "created_at":"...","updated_at":"...","msa_backend":"public","execution":"auto",
  "inputs":[{"id":"sequence-1","name":"ubiquitin","molecule_type":"protein"}],
  "models":["protenix","openfold3"],
  "pairs":[{"pair_id":"opaque","input_id":"sequence-1","input_name":"ubiquitin",
    "model":"protenix","state":"compatible","reasons":[],"job_id":null}],
  "jobs":[],
  "counts":{"pairs":2,"compatible":2,"rejected":0,"jobs":0,"queued":0,
    "running":0,"complete":0,"failed":0,"cancelled":0,"interrupted":0},
  "errors":[]
}
```
Batch states: `validating`, `validated`, `validation_failed`, `queued`,
`running`, `complete`, `partial`, `failed`, `cancel_requested`, `cancelled`.
Pair states: `pending`, `validating`, `compatible`, `rejected`.

## Jobs, progress, and artifacts

`job.get {job_id}` returns Job. `job.cancel {job_id}` returns the updated Job.
Job: `{job_id,batch_id,pair_id,input_id,input_name,model,state,phase,created_at,
updated_at,started_at,finished_at,exit_code,error,artifacts:[Artifact],
progress:{message,observed_at},provenance:{...}}`. States: `queued`, `starting`,
`running`, `cancel_requested`, `complete`, `failed`, `cancelled`, `interrupted`.
Progress is observed phase/log evidence; there is no invented percentage.
Errors and cancellation do not hide prior output. No automatic inference retry.
For queued jobs, dispatcher observations also include
`progress:{message,observed_at,queue_position,active_jobs,max_jobs}`. Position is
one-based in the shared FIFO queue; counts describe occupied execution slots,
not a promise of available GPU instances. A continuing native resident request
and its client occupy one slot together. Capacity comes from trusted head
configuration and is not a submission parameter.

Batch.get includes every pair and lightweight jobs (`artifacts:[]`,
`artifact_count`); use job.get for artifact details and complete provenance.
Job.get embeds the first 100 artifacts and `artifact_count`.

`job.logs {job_id,offset?:0,max_bytes?:65536}` returns
`{job_id,offset,next_offset,text,eof}` (byte offsets; UTF-8 decoding replaces an
incomplete boundary). `job.artifacts {job_id,limit?:100,cursor?:string}` returns
`{job_id,artifacts,next_cursor}` with at most 100 artifacts.
Artifact: `{artifact_id,job_id,name,size,sha256,media_type,format,role,model,
sample_id,confidence,qa}`. `role` is `structure`, `confidence`, `log`, `data`, or
`provenance`. `confidence` and `qa` are null unless verified native evidence
exists; they are never invented or pooled across unlike confidence metrics.
All native structure samples are retained, with stable opaque IDs. No head
filesystem paths are sent to the browser. Failed partial artifacts, if sealed
after the owned process stops, are labeled by the failed job state.

`artifact.read {artifact_id,offset?:0,max_bytes?:524288}` returns
`{artifact_id,offset,data_base64,next_offset,eof,size,sha256,name,media_type}`.
The server verifies the sealed artifact before serving. The local proxy's
`GET /api/v1/artifacts/{artifact_id}` uses these chunks and verifies the final
SHA-256 before serving its cached download.

## Shared annotations

`annotation.put {artifact_id,annotation_id?,expected_revision?,text,selection?}`
returns Annotation. New annotations omit both ID/revision; updates require the
current revision, or conflict. `text` is plain text (maximum 16 KiB).
`selection` is optional viewer state as finite JSON (maximum 16 KiB), e.g.
`{chain:"A",residues:[12,13],atoms:["CA"]}`. Selection does not change structure
bytes and must not contain scripts/HTML. Annotation:
`{annotation_id,artifact_id,revision,text,selection,author,created_at,updated_at}`.
`annotation.list {artifact_id}` returns `{artifact_id,annotations:[...]}`.
The author comes from trusted actor identity, never client parameters.

## Durability and operation

State is `/var/lib/bio-workbench` (SQLite WAL plus immutable bytes). The head
service runs `bio-workbench daemon`; RPC only persists/retrieves work. CPU
validation and inference are exact-owned systemd units. Durable intent precedes
launch; restart reconciles exact unit/InvocationID and terminal receipts.
Uncertain execution becomes visible `interrupted`, never an implicit retry.
Inference uses the existing guarded `bio-submit` argv, no shell interpolation,
and the existing estimated total-budget guard. Cancellation requests terminate
only the exact owned head process/service and allow its normal cloud cleanup;
they do not remove unrelated workers. Validation, inference logs, failures,
input pins, and terminal artifacts survive client disconnects.

Each admitted request pins the complete validated Nix toolkit generation.
Previews from older execution contracts must be validated again before commit.
If a resident waiting client ends early, its owned unclaimed request is
cancelled; an already running request remains tracked by the durable daemon,
which retrieves its verified eventual outputs without another inference.
An operator may import retained outputs with `import-retained`; those jobs
explicitly record `imported_retained_result:true,inference_performed:false`.
