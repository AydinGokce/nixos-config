# Head workbench service

The Electron desktop and Harrison use the same actor-scoped JSON-line RPC over
SSH. The protocol is in [CONTRACT.md](CONTRACT.md). Nix installs `bio-workbench`
and a dispatcher service; there is no new head network listener.

The shared operator workspace uses `BIO_WORKBENCH_ACTOR=harrison`. The dedicated
Harrison key has a forced RPC command and OpenSSH `restrict`. Client JSON cannot
choose an actor or a filesystem path. Desktop SSH may use the operator's existing
key; the local transport still calls only the fixed RPC command.

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

The default trusted configuration permits one model submission at a time.
`--config /absolute/operator-config.json` can override documented paths and
`max_jobs` (1–4). RPC clients cannot set this configuration. Private/public MSA,
model settings, native input constraints and runtime budget checks remain
separate from dispatcher concurrency.

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
