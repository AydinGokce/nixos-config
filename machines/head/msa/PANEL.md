# Private panel preparation

`bio-msa panel` is a standalone **Verda** operator route; it is not redirected by
`BIO_MSA_PROVIDER=aws`. It prepares a frozen list on one temporary worker with the
resident profile by default. For ordinary AWS preparation, use the shared
`bio-msa prepare` session described in [SESSIONS.md](SESSIONS.md).

The panel starts the pinned official API once, uses the complete read-only
snapshot, then runs model clients serially without inference. The Console's MSA
default is private; low-level `bio-submit` defaults to public. Successful private
preparation alone does not establish public-server or prediction equivalence.

```json
{
  "version": 1,
  "targets": [
    {"name": "case_A", "model": "openfold3", "sequence": "ACDEFGHIKLMNPQRSTVWY"},
    {"name": "case_A", "model": "boltz2", "sequence": "ACDEFGHIKLMNPQRSTVWY"},
    {"name": "case_A", "model": "protenix", "sequence": "ACDEFGHIKLMNPQRSTVWY"}
  ]
}
```

Use a real frozen target sequence in place of the illustration. Each entry is one
standard protein chain. Names use 1–96 letters, digits, dots, underscores or
hyphens and start with a letter or digit. `(model, name)` must be unique; repeated
case names across models must have identical sequences. Unknown fields, duplicate
JSON keys, nonstandard residues, empty panels and manifests above 16 MiB are
rejected. The complete manifest is checked before rental, again after waiting for
the MSA lock, and on the worker with its canonical SHA256.

```sh
BIO_MSA_SEARCH_PROFILE=resident-768gib-v1 bio-msa panel --json frozen-panel.json --timeout 21600
# Explicitly select a sufficiently large host if the default CPU type is unavailable:
bio-msa panel --json frozen-panel.json --worker TYPE --spot --timeout 21600
# Install a complete corpus, then prepare this panel on the same worker:
bio-msa install --json frozen-panel.json --worker TYPE --spot --timeout 21600
```

Automatic selection uses FIN-02 x86 hosts meeting the resident profile's
768 GiB advertised and actual available-RAM requirement, at no more than
$13/hour. `--spot` restricts offers; `--worker TYPE` selects a host explicitly.
The launcher still rechecks price, storage identity and the combined project
budget. Installation always retains the resident build profile.

The `mapped-128gb-v1` profile is explicit opt-in and **unqualified**. Its complete
NFS-backed editor trial exceeded the native one-hour limit. Its four-thread,
96 GiB/no-swap and report-only mechanics remain available for separately reviewed
experiments; they are not a production performance claim. Neither profile
reduces databases, indexes, sensitivity, templates or requested targets.

The full `.msa-databases.json` receipt and configured active storage are required
before rental. Pinned model environments must exist on shared storage. Panel jobs
retain the independent MSA submission lock, managed budget, storage tracking and
temporary-worker cleanup.

Combined `install --json` validates the entire manifest before rental even when
the full database receipt does not exist yet. It retains the install worker's
read/write mount, completes installation and validation, then starts the same
panel runner. The panel uses the time remaining in the original worker deadline;
there is no second rental or extra timeout. Installation without `--json` still
stops after database validation. Interrupted combined jobs retain completed
database stages for a later installer and any panel statuses already written.

Results are fetched to `/var/lib/bio-runs/JOB` on the head:

- `panel-manifest.json` retains the submitted manifest.
- `panel/panel.json` contains every requested target, status, input and output
  hashes, database provenance identity, timestamps and errors.
- `panel/targets/MODEL/NAME/prepared/` is a validated portable native bundle,
  suitable for `bio-submit MODEL --fasta .../input.fasta --msa-bundle .../prepared`.
- Each target also retains `input.fasta`, `prepared.native-work/`, preparation and
  validation logs, `status.json`, and its isolated `api-audit/` and `api-jobs/`.
  These include raw request/response bytes, template responses when requested,
  result archives and generated backend scripts for that target's actual tickets.
- The shared official server configuration, provenance and log remain at the job
  root. Backend cache identity includes database/tool versions and search runtime.

Targets continue after an individual failure. Any failed, timed-out or interrupted
target makes the overall job fail; incomplete native output is retained for
diagnosis. A worker-wide deadline starts before bootstrap and reserves the final
60 seconds for cleanup. Remaining targets receive explicit timeout/interruption
statuses when no usable time remains. Native child process groups and audit
proxies are stopped before advancing, and ticket export is bounded. Abrupt VM
loss can leave honest `pending`/`running` statuses in the latest durable receipt;
it cannot produce a successful panel receipt.

Successful panels are revalidated after fetching, including native input hashes,
query identity, private database provenance and retained API evidence, before
`DONE`. Use a fresh job directory for retries; the original panel keeps every
case and failure. To retry selected failures, retain the original manifest and
receipt and explicitly submit a new manifest containing those cases.

Local validation uses `python3 -m unittest test_panel.py` from this directory and
`python3 -m unittest test_bio_submit.py` from its parent. Tests use synthetic
portable bundles, real localhost audit proxies and bounded child processes; they
do not claim a production database or scientific-quality validation.
