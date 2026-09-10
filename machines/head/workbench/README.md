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
Inputs preserves its exact revision and still requires the normal preview and
submission steps. Whole plasmids and unresolved protein-product candidates stay
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
A separate idempotent commit selects exactly the compatible pairs to submit.
The existing `bio-submit` and `dc` budget guard remain authoritative for all
paid work. RFAA is parked and AF3 is absent from the catalog.

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
