# Head workbench service

The native Rust desktop and Harrison use the same actor-scoped JSON-line RPC over
SSH. The protocol is in [CONTRACT.md](CONTRACT.md). Nix installs `bio-workbench`
and a dispatcher service. Desktop/Harrison RPC uses SSH and adds no head HTTP
listener for those requests. The separate public MSA transport uses a
loopback-only HTTP CONNECT proxy.

The shared operator workspace uses `BIO_WORKBENCH_ACTOR=harrison`. The dedicated
Harrison key has a forced RPC command and OpenSSH `restrict`. Client JSON cannot
choose an actor or a filesystem path. Desktop SSH may use the operator's existing
key; the local transport still calls only the fixed RPC command.

The desktop's Library explorer reads the shared head registry through
`library.list`, `library.get` and `library.attachment`. It browses projects,
pinned members, molecular identities, purpose documents, revision history,
source relationships and original files. Adding a selected molecular record to
Inputs preserves its exact revision. The desktop Run action then records a
durable validation-and-execution request on the head. Whole plasmids and
unresolved protein-product candidates stay
visible with the reason they cannot be used as ordinary prediction inputs.

Library curation separates the editable Alt name from the original inventory
label, ID and modality. `library.edit` creates revisions for names, archive state
and ordinary polymer sequences; `library.history`, `library.undo` and
`library.redo` provide durable actor-owned undo/redo. Archiving hides entries
without deleting them. Current project memberships advance together with an
edited member in a recoverable transaction, while every old snapshot remains
intact. A changed sequence retains its old derivation and annotation evidence
as historical provenance. `library.runs` associates actor-visible jobs using
their original pinned library inputs; the explorer opens their retained results
through the ordinary artifact viewer. See `CONTRACT.md` for the exact API.

Plasmid-derived proteins use the same protein construct type as standalone
proteins but store source coordinates rather than a duplicate peptide.
`library.protein_domains` projects curated parent annotations through the exact
saved protein codons for 3D domain coloring. It binds protein/parent revisions and
attachment hashes, preserves frame, crop, strand and circular joins, and reports
excluded or partially covered annotations. It neither discovers ORFs nor infers
domain boundaries from names; the viewer separately verifies structure alignment.

`library.sequence` supplies bounded annotations, six-frame ORFs and exact-revision
translations. `library.product_preview` resolves a proposed definition without
writing; `library.product_create` adds a protein to its parent's projects.
`library.create` adds a standalone protein to a selected project. Parent edits
advance current derived products and project references atomically. Invalid
translations remain visible with diagnostics and cannot be submitted. Undo/redo
also covers coordinate edits and creation, with creation undone by archiving.
Derived sequence views include the exact source bases and genomic codon triplets
aligned to the current peptide. A `library.edit` with `frame_offset:0|1|2` changes
the actual protein translation, preserving its strand and coding footprint and
using a first-stop policy. Existing records retain their original behavior until
explicitly edited; frame revisions and their undo/redo survive library backups.

State lives in `/var/lib/bio-workbench`: SQLite WAL, immutable upload and artifact
bytes, isolated per-batch construct libraries, validation evidence, owned process
logs and operation receipts. Input bytes and supported chemistry are preserved.
CPU previews invoke existing native parsers without MSA searches or inference.
`batch.run` validates and automatically queues every compatible pair while
retaining rejected pairs and their reasons. It uses private MSA by default for
folding; scoring/design workflows perform no MSA. The run request, validation
receipt and atomic queue publication survive disconnects and daemon restarts.
Resending the same request key and payload returns the same run and jobs.
All-rejected runs stop visibly without submitting anything. Cancellation during
validation prevents later automatic submission.
The older `batch.validate`/`batch.create` pair remains available for clients that
need a separate preview and explicit subset selection.
The existing `bio-submit` and `dc` budget guard remain authoritative for all
paid work. RFAA remains unavailable and AF3 is absent from the catalog.

The dispatcher retains launch intent before creating an exact owned systemd
unit. Command identity, InvocationID and terminal receipts govern recovery; an
ambiguous execution becomes visible rather than being retried. An operation
captures the full Nix system's logical toolkit hierarchy so its Python sibling
imports and executable code survive subsequent deployments. Operator imports
of completed result trees explicitly record that no new inference occurred.

Cancellation of a resident request uses a private owner token and enqueue/cancel
lock. Queued requests can be cancelled atomically. A shared worker's already
claimed prediction is allowed to finish and remains visible. Ephemeral jobs
signal only their exact owned submission and retain managed cleanup. Closing a
desktop window has no effect on cloud job lifetime.

The deployed head permits up to ten model submissions at a time, admitting
queued jobs in creation order across batches. This suits occasional bursts:
slots open as jobs finish, without reserving ten workers while the queue is
empty. A slot covers a managed submission through cleanup, or its continuing
resident request if the waiting client disconnected. The client and request
consume one slot together. Native worker capacity and the existing cloud budget
guard still determine whether admitted work can obtain compute.

`--config /absolute/operator-config.json` can override documented paths and
`max_jobs` (1–32); the generic default is one. RPC clients cannot set this
configuration. Queued jobs report their FIFO position and occupied/configured
execution slots. Decreasing the limit lets existing work finish before further
admission. A recovered, exactly bound terminal receipt releases a completed
operation even if the daemon or runner restarted before its final database
update. Invalid receipts retain their slots and report an integrity error.
Private/public MSA, model settings, native input constraints and runtime budget
checks remain separate from dispatcher concurrency.

The native worker panel uses `worker.status` to inspect the shared private MSA
worker without starting it. It shows observed startup stages and the verified
idle/hard deadlines. Keep warm adds up to 15 minutes inside the existing budgeted
lifetime; Shutdown drains accepted searches and stops new admissions. Controls
retain exact generation pins and a durable command ID before returning. The
dispatcher reconciles that same command after a lost response or restart, so an
extension cannot be applied twice. `worker.control_get` exposes the retained
outcome without changing the worker. Trusted configuration can set
`msa_sessions_root`; clients cannot choose it.

`worker.capacity` separately observes available Verda CPU/GPU capacity on
startup and every five seconds. The head shares a durable five-second cache
between operators, with a 30-second backoff after incomplete or failed checks.
Manual Refresh bypasses the cache age; overlapping requests share one provider
check. The read-only credential wrapper `bio-msa-capacity` uses the existing dc
API client and fixed catalog/location/capacity GETs. Its process is bounded to
20 seconds and it never touches the budget ledger, launches a worker, or changes
a session. Trusted configuration may override `capacity_helper`.

The Console distinguishes capacity to launch an MSA worker from a connected
worker. Eligibility reuses the current FIN-02/RAM/image/$13-hour policy, includes
CPU-only offers, and reports failed essential lookups as unknown. Its shared
worker dialogue lists available GPU offers across locations, with prices,
aggregate VRAM, conservatively converted host RAM and eligibility reasons.
The last-complete-update timestamp survives failed refreshes and cache reads;
partial/error evidence is visible. See the protocol for row units and freshness.

Per-job startup telemetry exposes actual stages, optional byte/item/step
counters, and explicitly scoped ETAs. Transfer estimates use observed throughput;
unmeasured phases remain unknown, and observations older than 30 seconds lose
their numeric ETA. A stage estimate is not an estimate of total model runtime.
Private progress files and mirrored job logs retain the evidence without changing
RF3 preparation code or its cache key. See the protocol for emitter fields and
the difference between startup time, stage time, and whole-job time.

The dedicated BindCraft workflow accepts actor-owned PDB/mmCIF uploads or retained
structure artifacts. It validates exact chain/crop/hotspot identities and queues
through the same durable dispatcher with no review step. Its campaign seed and
GPU-plus-OS spending cap are bound to the request; the per-run cap includes prior
fallback attempts. Scientific defaults, native filters and existing global
budget/cancellation machinery are preserved. Native stages and observed counts
appear in normal job progress, while final candidate tables retain both accepted
and rejected outcomes. Candidates can become standalone project proteins with
structure/settings provenance and normal undo/redo.

Original and submitted target structures are sealed before allocation. The
`binder.context` endpoint supplies hash-bound residue correspondences for viewing
cropped candidates against the full original target; it validates actual output
residue identifiers instead of assuming numbering offsets. See `CONTRACT.md`
for the RPC payloads, metric units and distinction between native filter counts
and experimental binding evidence. CPU-only tests use temporary stores and fake
owned systemd units, never cloud allocations.

Useful operator commands:

```sh
systemctl status bio-workbench
journalctl -u bio-workbench
printf '%s\n' '{"id":"catalog","method":"catalog","params":{}}' |
  env BIO_WORKBENCH_ACTOR=harrison bio-workbench rpc
```

Import a known retained result tree for viewing, without rerunning a model:

```sh
bio-workbench import-retained \
  --source /absolute/retained/completed-output-directory \
  --model rf3 --actor harrison \
  --name 'Retained validation — no new prediction'
```

Imports retain source hashes and native metadata. A completed import means the
archive was ingested; inspect original runtime/provenance and chemical QA to
assess the prediction. RF3 selection requires matching canonical/raw coordinates,
sidecars, chemistry audit, and completion records. Failed or unverified raw
samples remain available and are not promoted to the selected result. Native
confidence metric names and scales are preserved across all models.

For isolated CPU fixtures, use a separate `--state` and a trusted test `--config`.
The normal test suite does not rent GPUs or send Slack messages:

```sh
PYTHONPATH=machines/head python3 -m unittest discover \
  -s machines/head/workbench -p 'test_*.py'
PYTHONPATH=machines/head python3 -m unittest discover \
  -s machines/head/inference -p 'test_*.py'
```

Actual CPU preview and desktop verification evidence is kept locally under
`~/bio-runs/desktop-workbench-20260906` and the related frontend validation
folder. The deployed head also retains isolated native preview receipts under
`/var/lib/bio-workbench-smoke-20260906`.
