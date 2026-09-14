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

## Shared MSA worker and startup progress

`worker.capacity {refresh?:bool}` reads configured MSA capacity independently of
the current session and retains a separate Verda prediction-GPU inventory. It
never allocates, reserves spend or changes a session. The Console checks on
startup and every five seconds. The shared process-independent cache coalesces
operators: successful checks cache five seconds; partial/error checks back off
30 seconds. `refresh:true` bypasses age; concurrent checks return the existing
snapshot with `refreshing:true`. Configured provider changes invalidate the cache.

The response is `{schema:1,server_epoch,checked_epoch,observed_epoch,state,stale,
stale_after_seconds:120,refresh_after_seconds,refreshing,msa_available,msa_message,
msa_provider,compute_kind,cpus,gpus,region?,error?,gpu_error?}`. `state` is
`ready|partial|error`. `checked_epoch` is the last complete successful observation
or null; cache reads and failed refreshes never advance it. `observed_epoch` is
the current attempted observation. Stale evidence cannot claim availability.
Errors set `msa_available:null`, never a fabricated false result.

With `BIO_MSA_PROVIDER=aws`, `msa_provider:"aws"` and `compute_kind:"cpu"` select
AWS in `us-east-1`. `msa_available:true` requires the helper's verified running
worker and prepared private assets; incomplete assets/quota produce false,
while an eligible stopped worker produces null because offerings and quota do
not prove live EC2 capacity. That complete, successful AWS observation may have
`state:"ready"` with null availability. Actual search readiness/connection remains
separately represented by `worker.status`; an EC2 running state is not API readiness.

AWS `cpus` rows are `{instance_type,name,location,contract:"on-demand",vcpus,
ram_gib,price_hourly,msa_eligible,availability,reason}`. `availability` is
`eligible|unavailable|unknown`; price may be null if unverified. Host memory uses
AWS's GiB units directly. Eligibility is not a reserved instance or launch promise.
The AWS helper owns account/region, quota, asset and retained-pool observations;
private account identifiers and configuration are omitted from the public row.

For Verda-only MSA, the endpoint reuses `msa/worker.py` with the production
resident profile: FIN-02, supported x86 image/family, 768 GiB advertised host RAM,
regular/spot price no greater than $13/hour. CPU-only offers also count.
`mapped-128gb-v1` is an explicitly selected, unqualified experiment, never the
production default. A capacity result does not grant a budget reservation or
replace the launcher's fresh quote and guest checks. Missing essential regional
lookups keep availability unknown; partial inventory is not treated as empty.

`gpus` remains the available Verda GPU inventory across regions, independently of
which provider handles MSA. Rows are `{instance_type,name,location,contract,
gpu_count,gpu_memory_gib,ram_gib,price_hourly,msa_eligible,reason}`. `contract` is
`regular|spot`. Price is USD/hour per entire instance; VRAM is total aggregate
VRAM or null. Verda decimal GB convert to GiB (`GB * 10^9 / 2^30`); GPU count
never multiplies the already aggregate number. Under AWS, the Console labels this
as prediction inventory and omits the irrelevant Verda MSA-eligibility column.
`gpu_error` reports incomplete Verda inventory without overwriting a valid AWS
MSA observation. GPU offers are not counts of physical stock or reservations.

The trusted helper is `/run/current-system/sw/bin/bio-msa-capacity`; clients
cannot choose paths, credentials or provider endpoints. Its subprocess has a
20-second/1-MiB bound. The Verda inventory uses its four fixed GETs plus OAuth.
AWS capacity delegates only to `bio-aws-msa capacity`; allocation and control
commands are unavailable through this endpoint. Errors are fixed/redacted;
raw provider responses and credentials never enter the desktop reply.

`worker.status {}` reads the existing shared worker; it does not allocate, ensure,
extend, or retire one. Its bounded response includes `schema:1`, `shared:true`,
`state` (`absent|starting|warming|ready|busy|idle|closing|failed|uncertain`),
`message`, `server_epoch`, `checked_epoch`, `stale`, `stale_after_seconds:30`,
`target`, `shutdown_epoch`, `shutdown_reason`, `hard_deadline_epoch`,
`idle_deadline_epoch`, `active_request_id`, `queued_requests`, and optional
`idle_credit_seconds` and `progress`, plus validated `provider_name:"aws"|"verda"`
and `compute_kind:"cpu"`. Existing sessions report their frozen provider even
after configuration changes. These countdown timestamps use epoch
seconds, unlike the ordinary job ISO timestamps. Unknown deadlines are null.
The target is null or the exact four-pin object
`{session_id,invocation_id,intent_sha256,launch_sha256}`. Never substitute the
current active worker for a retained target.

`controls.extend` and `controls.shutdown` contain `enabled` and `reason`.
Extend also contains `seconds:900`; shutdown contains `mode:"drain"`.
Missing, stale, legacy, or unverified generations cannot expose active controls.
The returned deadline is authoritative; the desktop must not infer a hard
deadline from a worker's creation time. Idle keep-warm credit stays within the
existing budgeted hard lifetime and does not renew that reservation. Shutdown
stops admitting searches and drains accepted work; it does not cancel other
Workbench runs or signal unrelated workers. AWS completion stops the exact EC2
instance and releases its compute reservation while retaining the original OS
and database volumes. Verda keeps its original temporary-worker removal policy.

`worker.extend {request_key,target}` and `worker.shutdown {request_key,target}`
immediately retain an actor-owned intent. They return
`{control_id,request_key,action,target,state,result,error,created_at,updated_at}`,
where state is `pending` or `complete`. The dispatcher applies the same command
ID to the exact generation. Transport failures retain pending uncertainty and
retry that ID with backoff; the MSA control journal deduplicates the side effect.
No replacement worker or new command is created during recovery. The completed
result includes `status:"applied"|"rejected"`, `reason`, `applied_seconds`,
the four identity pins, the command ID, and any verified control revision and
deadlines. A hard cap may reject a requested increment or report its smaller
effective value. Reusing a request key with different parameters conflicts.

`worker.control_get {control_id}` reads that actor's durable command receipt,
including after disconnect, daemon restart, or an unrelated worker generation.
Polling is read-only. If the initial response is lost, retry the exact original
write request/key to recover its control ID. Closing the desktop does not
abandon accepted control intent. Up to 64 pending controls are retained at once.

`job.progress` and optional `worker.status.progress` include real measured
`stage`, `scope` (`msa|gpu`), `stage_state` (`running|complete|failed`), `message`,
`timestamp_ns`, and optional integer `completed,total,unit` (`bytes|items|steps`).
Stage names are `runtime_package`, `waiting_capacity`, `allocating`, `base_setup`,
`runtime_download`, `runtime_extract`, `database_check`, `index_warm`, `ready`,
`search`, `gpu_allocation`, `model_setup`, `inference`, `result_transfer`,
and `cleanup`. Counters refer only to their named stage; no synthetic overall
percentage is supplied.

`waiting_capacity` keeps the shared worker in `starting` while no qualifying
worker is available. Its message reports the remaining retry window;
availability ETA stays unknown and completion counters are omitted. The retry
window is not a promise of capacity. A later `allocating` stage starts its own
estimate when the provider launch begins.

`eta` contains `state:"estimate"|"range"|"unknown"|"stale"`, a human `basis`,
and `scope:"stage"|"startup"|"job"`. An estimate supplies `seconds`, a range
supplies `lower_seconds,upper_seconds`. The response normalizes durations to
its `server_epoch` (also recorded as `eta.as_of_epoch`); the client may subtract
its own elapsed monotonic time after receipt. Stage ETA must never be presented
as the whole run's remaining time. Unmeasured work is explicitly unknown.
Observations older than 30 seconds or more than 5 seconds in the future are stale;
stale estimates omit durations. Job reads recompute age even if the runner has
stopped updating. Throughput-based estimates use real, monotonic samples of the
same stage identity, total and unit. Resets, stalls and missing measurements do
not produce a fabricated rate.

Trusted emitters write newline-terminated
`BIO_WORKER_STAGE {"schema":1,"stage":...,"scope":...,"state":...,"message":...,"timestamp_ns":...}`
to stdout/stderr or the runner-created private `BIO_WORKER_PROGRESS_LOG`.
Optional fields are `stage_id`, counters and `eta` (raw ETA durations are relative
to the event timestamp). Each line is at most 4096 bytes. Unknown fields,
nonfinite numbers, duplicate keys, malformed types and unfinished lines are
ignored. A bounded tail is read only from the runner's own known paths; nested
RF3 logs are not discovered and frontend source identity does not change.
The last accepted sidechannel event is mirrored into the retained job log.
Later native output supersedes waiting infrastructure, and a final failed event
preserves the concrete cause even when its process exits between polls.

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
* `library.protein_domains {ref}` projects retained parent annotations onto the
  exact coordinate-derived protein, without ORF discovery or library writes.
  It returns `schema:1,ref,sha256,available,sequence,sequence_sha256,length`,
  `coordinate_system:"protein_0based_half_open"`, `source`, `features`, `excluded`,
  `issues`, `complete`, and `counts:{total,mapped,excluded,omitted}`. `sha256`
  binds the protein record; `source` binds the pinned parent record/sequence,
  translation definition, and `annotations_receipt` / `sequence_receipt`
  (`path,bytes,sha256`). Source status is `current`, `stale`, `missing`, or
  `unsupported`. Standalone proteins have `source:null` and no inferred domains.
  Mapped features contain `id,label,kind,segments:[{start,end}],source_feature_id,
  source_feature_index,source_ref,source_strand,source_segments,status,issues`.
  `source_segments` are nucleotide coordinates; `segments` are protein coordinates.
  Optional `color` is an explicit retained `#RRGGBB` color. `status:mapped|clipped`
  distinguishes complete coverage from a reliable subset; partial codons are
  always omitted, with diagnostics. Only fully covered, same-strand codons from
  the actual saved frame/crop/join/origin-wrap map. Feature `CDS` types are allowed;
  explicitly autogenerated suggestions are excluded. Historical protein
  `derivation.json` coordinates and domain guesses from names are never used.
  `excluded` rows retain the same source identity plus `reason` and diagnostics:
  stale/missing binding, unsupported/fuzzy coordinates, opposite strand,
  autogenerated provenance, ambiguous source ID, a whole-sequence `source`
  feature, or no complete codon inside the product. `complete` means the source
  list was fully assessed, including exclusions; omitted metadata makes it false.
  Source/codon limits match `library.sequence`; annotation responses are limited
  to 1,000 features and 256 KiB of metadata. Oversized peptides fail explicitly
  rather than returning a truncated sequence with its full digest. Clients must
  independently verify sequence-to-structure correspondence before coloring.
* `library.structures {ref,include_revisions?,include_hidden?,limit?,cursor?}`
  returns `schema:1,ref,sha256,sequence_sha256,entries,next_cursor,scope`.
  Defaults include all revisions of the selected protein family, omit hidden
  cards, and return 50 entries (maximum 100). Order is creation time descending,
  then opaque stable `entry_id`. Manual cards are immutable library attachments;
  automatic cards are retained `role:structure` PDB/mmCIF artifacts whose original
  batch explicitly selected this pinned protein or an assembly containing it.
  Names, equal sequences, floating references, templates and input files do not
  create associations. Same-job identical-byte aliases share one card. Shared
  prediction access requires artifact/job/batch ownership to agree and every
  relevant molecular input to be a pinned shared library reference. Complexes
  with private text/upload partners remain visible only to their original actor.
  Generic job/artifact APIs retain their existing actor boundaries.
  Previously saved BindCraft proteins also expose their existing
  `predicted-complex.pdb` when its immutable structure/sequence/provenance receipts
  agree. Their original saved protein revision is retained across later edits and
  library-only backup/restore. The separately retained design target is excluded.
  These cards add `association:saved_bindcraft_candidate,candidate_status`;
  `job_state` is null because an archived library receipt does not establish the
  current state or availability of its originating job.
  Each card has `entry_id,origin:manual|prediction,label,format,size,sha256,
  source_ref,source_sequence_sha256,sequence_relation,hidden,created_at,protein`.
  Prediction cards additionally retain `source_refs,job_id,job_state,model,
  confidence,qa`. `protein` binds the exact associated protein's `ref,sha256,
  sequence_sha256,derivation_kind` and, when derived, `parent:{ref,sha256,
  molecule_type,molecular_form}`; otherwise parent is null. This is the original
  source revision, never a guessed current protein/plasmid. `sequence_relation`
  is `same_library_sequence`, `historical_library_sequence`, or `unresolved`.
  This compares library sequences, not coordinate-chain identity;
  `coordinate_sequence_match:unverified` makes that distinction explicit.
* `library.structure_read {ref,entry_id,offset?,length?}` reauthorizes the exact
  association on every read and verifies the complete source digest. It returns
  `ref,entry_id,source_ref,source_sequence_sha256,protein,sha256,size,format,name,
  offset,next_offset,eof,data_base64`. Length defaults to 131072, maximum 262144.
  Hidden entries remain readable by their exact ID; trash does not destroy or
  revoke historical evidence. No filesystem paths are accepted or returned.
  `library.structure_links {artifact_id}` supports structures opened from ordinary
  run history: it requires the existing actor-owned `role:structure` artifact,
  job and original batch, verifies the structure bytes, and returns
  `schema:1,artifact_id,sha256,proteins:[...]` with the same exact source context
  objects. Only explicitly referenced library proteins are returned; equal
  sequences and names do not establish backlinks. This method grants no shared
  artifact access beyond the separate verified gallery association.
* `library.structure_attach {ref,expected_sha256,structures,request_key}` accepts
  1–16 `{source:{kind:upload|artifact,id,sha256},label?}` entries from completed,
  actor-owned uploads or artifacts. PDB/mmCIF files are bounded to 32 MiB each;
  manual assets are bounded to 128 MiB / 256 associations per protein. Exact
  original bytes are copied atomically into hash-named library attachments and
  included in normal backups. Basic text/format checks preserve alternate
  conformers, incomplete structures and multiple models; actual render/parser
  issues remain visible in thumbnail/viewer diagnostics. No structure is made
  a molecular input template via `identity.structure_file`.
  `library.create` accepts the same optional `structures` list and creates the
  protein, attachments and project membership in one recoverable transaction.
* `library.structure_visibility {ref,expected_sha256,entry_id,hidden,request_key}`
  hides/restores one shared card. Both structure mutations return the ordinary
  library edit response and actor-owned Undo/Redo history. Upload undo hides the
  association while retaining its append-only catalog and asset bytes. Ordinary
  molecular/name undo preserves that independent structure catalog; only verified
  managed structure attachments are exempted from strict source-attachment
  equality. Other users' conflicting changes to the same visibility state are
  rejected. No prediction completion writes to the library: automatic cards are
  derived from retained exact associations, so existing runs appear immediately.
  Thumbnail queue/read RPCs and their independent CPU service are documented in
  `STRUCTURE_THUMBNAILS.md`; caches never bypass association authorization.
* `library.product_preview {parent_ref,translation}` returns the canonical
  definition and `parent_ref,parent_sha256,available,sequence,length,issues`.
  `parent_sha256` binds the parent record, not just its DNA sequence. This is
  read-only and returns diagnostics for biologically unavailable definitions.
* `library.product_create {parent_ref,expected_sha256,translation,alt_name?,request_key}`
  creates a derived protein and adds it to the current parent's projects.
  `library.create {project_ref,expected_sha256,sequence,alt_name?,structures?,request_key}`
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
  A project `description` patch replaces `attachments/project.md` in that new
  revision, preserving exact UTF-8 text up to 1 MiB. Empty text is allowed for an
  existing project and marks its brief incomplete. Current reference/SHA checks
  prevent stale writes; undo/redo restores the prior brief while preserving
  independently changed project names and member references.
  The reference must be pinned and current, with its exact displayed SHA.
  Supported patch fields are `alt_name` for molecular records, `name` and
  `description` for projects, `archived` (boolean), and `sequence` for explicit protein/DNA/RNA
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

## Binder design

`catalog.workflows.bindcraft` advertises the dedicated workflow; it is not a
folding-model checkbox. `binder.catalog {}` returns `schema:1`, `enabled`,
`runtime_ready`, `runtime_status`, `defaults`, `limits`, `formats`,
`msa_required:false`, `license_scope`, and descriptions of count/cost scope.
Readiness here is an installation receipt observation; asynchronous submission
performs complete native installation and input integrity checks before rental.
Defaults are lengths `[65,150]`, `designs:100`, `timeout_seconds:7200`,
`max_cost_usd:10.0`, and `seed:null` (upstream random campaign state).

A Target is `{kind:"upload"|"artifact",id,sha256,source_ref?,project_ref?}`.
Sources must be complete, owned by the trusted actor, and match the exact hash.
Optional library references must be exact pinned revisions; their existence and
record hashes are verified and retained as user-selected context. They do not
silently assert that an uploaded structure was produced from that construct.
A Residue is `{chain,number,insertion_code}`; blank original chain `""` and
insertion code `""` are valid. Never substitute a viewer's array index for the
original residue identifier.

`binder.inspect {target:Target}` returns `{schema:1,target,target_name,format,
chains:[{chain,residue_count,sequence,supported,residues:[{chain,number,
insertion_code,name,amino_acid,position,supported,issues}]}],warnings,context}`.
The strict PDB/mmCIF parser preserves author identities. Unsupported chemistry
and incomplete backbone atoms are explicit; ambiguous models/conformers reject.

`binder.run {request_key,name,target,chains:[string],hotspots?:[Residue],
crop?:[Residue],lengths?,designs?,timeout_seconds?,max_cost_usd?,seed?}` immediately
returns the ordinary durable auto-run batch envelope with `workflow:"bindcraft"`
and `model:"bindcraft"`. No review/commit request follows. Missing crop means
all selected chains; an explicit crop is an exact nonempty residue selection.
Every selected residue requires canonical protein chemistry and N/CA/C/O atoms.
Disjoint crop segments and true chain breaks become separate submitted fragments.
The original-to-submitted map and selected hotspot correspondence are immutable.
All native scientific defaults and production filters remain unchanged.
`designs` is an accepted-design goal, not an attempt limit. The campaign seed
controls Python/NumPy random state; native trajectory seeds remain in CSVs and
bitwise GPU/Rosetta reproducibility is not claimed.

The existing dispatcher owns validation, queueing, logs, cancellation and exact
unit/receipt recovery. Use `batch.get`, `job.get`, `job.logs`, `job.cancel` and
`batch.cancel`. Native stdout adds `job.progress.binder` observational counters:
`attempts_started`, `trajectories_completed`, `candidates_accepted`,
`candidates_rejected`, `current_trajectory`, `log_caught_up`, `counts_scope`.
Exact base-AF2-screen and final-filter rejection messages contribute to the
deduplicated rejected count; `rejection_screens` records their observed classes.
Terminal jobs preserve these observations with the sealed native log artifact
and SHA-256 in `provenance.binder_observations`. These counts can include screened
candidates that upstream does not retain as complete CSV/structure rows.
Counts can become `state:"unavailable"` when the log cannot support them. They
never control execution or replace final native tables. No percentage is invented.
Periodic worker inference heartbeats preserve the last observed native BindCraft
substage only within the same active stage identity and with fresh telemetry.
Stage transitions, completion/failure, stale heartbeats or unavailable log
evidence suppress that retained substage.

`max_cost_usd` bounds the freshly quoted GPU plus disposable OS reservation for
the requested runtime plus the launcher's existing 900-second allowance. Earlier
paid fallback attempts share one durable cost scope and consume the same cap.
The exact quote, scope and cap enter the managed ledger before allocation; a
later attempt cannot raise the original scoped cap. Shared head/storage costs
remain covered by the separate authorized project budget. Provider teardown
remains governed by the existing managed watchdog and cleanup machinery.

`binder.candidates {job_id,limit?:100,cursor?}` returns `{schema:1,job_id,state,
candidates,next_cursor,summary,warnings}`. Each candidate has `candidate_id`,
`name`, `status` (`trajectory`, `accepted`, `rejected`, or `unclassified` when
native acceptance evidence is missing), `sequence`, `sequence_sha256`, `length`,
`seed`, `metrics`, `native_metrics`, `structure_artifacts`, and `provenance`.
Normalized metric keys are `plddt`, `iptm`, `pae`, `interface_pae`, `rosetta_dg`,
`interface_hbonds`, `interface_unsatisfied_hbonds`, `interface_sasa`, and
`binder_rmsd`; missing/nonfinite values are null. `summary.filter_failures` is
run-level native failure counts, not a guessed explanation for each candidate.
Candidates appear after managed result transfer and archival. Every native CSV,
PDB and log remains downloadable through the standard artifact RPCs.

`binder.context {job_id,artifact_id?}` returns original/submitted structure
receipts, `output_structure_artifact`, `target`, `target_context`, `residue_map`,
ordered `submitted_chains:[{chain,sequence,residue_count}]`, and
`output_mapping:{status:"available"|"unavailable",pairs:[{original,submitted,
output}],reason?,artifact_id?,sha256?}`. Inputs are archived with role
`target_structure` before allocation. For an available output map, the requested
actor-owned output artifact SHA is verified, its native input manifest matches,
and its actual target-chain sequence/order matches the submitted chain order.
Pinned ColabDesign concatenates target chains into output A and writes binder B;
output residue numbers can retain gaps and are read from the actual PDB.
The viewer must bind both structure hashes and use these explicit residue pairs;
it must not invent offsets or fall back to positional alignment on failure.
Oversized mapping responses fail clearly; the complete map is also retained in
the downloadable `binder-provenance.json` input artifact.

`binder.save {job_id,candidate_id,project_ref,expected_sha256,request_key,
alt_name?}` returns the ordinary library operation envelope. `expected_sha256`
is the current PROJECT record hash. The backend derives the protein sequence
from the sealed candidate CSV, checks it against output chain B, validates the
output target mapping, and attaches the predicted complex, submitted target,
description and complete settings/target/hotspot/mapping provenance. The write
is idempotent, revision-preserving, and supports existing library undo/redo.
Named hotspot patches are initially frontend-session state keyed by the exact
original structure SHA; reusing a name never authorizes applying it to new bytes.

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
