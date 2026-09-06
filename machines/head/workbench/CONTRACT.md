# Bio workbench RPC v1

The local app and Harrison use the same JSON-line RPC on bio-head. The fixed SSH
command is `/run/current-system/sw/bin/bio-workbench rpc`. The local app forwards
`POST /api/v1/rpc` unchanged. There is no head HTTP listener. Each request is
`{"id":"client-id","method":"catalog","params":{}}`; each response is
`{"id":"client-id","result":...}` or
`{"id":"client-id","error":{"code":"invalid","message":"..."}}`.
The trusted SSH command sets `BIO_WORKBENCH_ACTOR`; client parameters cannot
select an actor. Resources are scoped to that actor. The app and Slack share
resources when their trusted commands use the same actor.

Limits: 2 MiB per wire message, 512 KiB decoded upload/read chunks, 256 MiB per
upload, 128 declared inputs, 512 expanded input/model pairs, 100 list entries.
JSON numbers must be finite; duplicate keys and unknown method/parameter names
are rejected. IDs are opaque strings. Times are UTC ISO 8601. Error codes include
`invalid`, `not_found`, `conflict`, `limit`, `unavailable`, `integrity`, `internal`.

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

## Preview, select, then submit

Validation is asynchronous CPU work and does not launch paid inference.
`batch.validate` creates a durable preview and returns the Batch below. Poll
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

`batch.list {limit?:50,cursor?:string}` returns `{batches:[BatchSummary],next_cursor}`.
BatchSummary includes identifiers, name, mode, state, timestamps, models,
backend/execution, and counts; it omits inputs, pairs, and jobs.
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
