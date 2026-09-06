# Persistent database storage contract

The shared implementation is `../rfaa/storage.py`. It never allocates storage.
Registration verifies an existing provider volume and writes a private,
root-owned receipt. The operator must choose either persistent retention or a
UTC deadline. Persistent retention is authorized for this project; the $500
build ceiling still applies and is not a provider-enforced storage billing cap.

| Fixed profile | `rfaa` (default) | `colabfold` |
| --- | --- | --- |
| Provider name | `bio-rfaa-db-TOKEN` | `bio-colabfold-db-TOKEN` |
| `purpose` tag | `rfaa-databases` | `colabfold-databases` |
| Capacity | 3300 GB | 3000 GB |
| Type and location | `NVMe_Shared`, `FIN-02` | `NVMe_Shared`, `FIN-02` |
| Head mount | `/mnt/bio-databases` | `/mnt/bio-msa-databases` |
| Default receipt | `/var/lib/dc/rfaa-storage.json` | `/var/lib/dc/msa-storage.json` |
| Receipt override | `RFAA_STORAGE_RECEIPT` | `MSA_STORAGE_RECEIPT` |
| Head installer unit | `rfaa-database-install.service` | `msa-database-install.service` |
| Optional head service stopped during retirement | none | `msa-server.service` |
| Tracked job prefixes | `rfaa-` | `msa-`, `openfold3-`, `boltz2-`, `protenix-` |

`TOKEN` must contain 8–64 letters, digits or hyphens. `DC_STATE_DIR` changes both
default receipt directories and the shared accounting-state directory. The
profile is selected per invocation; it is never process-global mutable state.
Provider identity checks include the exact UUID, name, capacity, purpose tag,
creation time, USD pay-as-you-go contract and non-OS shared-volume type.
Registration verifies the NFS export against both provider fields.

## Invocation

The existing `bio-rfaa-storage` wrapper defaults to RFAA. The ColabFold wrapper
can dispatch the same command with `--profile colabfold`. Keep the command first
so wrappers can source credentials only for `register` and `expire`.

```bash
bio-rfaa-storage register --volume RFAA_UUID --name bio-rfaa-db-TOKEN --persistent
bio-rfaa-storage register --profile colabfold --volume MSA_UUID \
  --name bio-colabfold-db-TOKEN --persistent

bio-rfaa-storage check --profile colabfold --volume MSA_UUID
bio-rfaa-storage track --profile colabfold --volume MSA_UUID \
  --job-dir /var/lib/bio-runs/msa-JOB --pid LAUNCHER_PID
# Repeat track after the managed worker becomes ready, adding --instance UUID.

# Ordinary timers never retire an active persistent allocation.
bio-rfaa-storage expire --profile colabfold

# Explicit retirement requires the exact registered UUID.
bio-rfaa-storage expire --profile colabfold --now --volume MSA_UUID
```

`--expires-at 'YYYY-MM-DDTHH:MM:SS+00:00'` remains available instead of
`--persistent`. The two flags are mutually exclusive. **`--permanent` selects
permanent deletion when retirement occurs; it does not select persistent
retention.** Without that flag, retirement uses provider soft deletion.

## Receipt and pure launch checks

New receipts retain `version: 1` and add these fields:

```json
{
  "profile": "colabfold",
  "retention": "persistent",
  "expires_at": null,
  "status": "active"
}
```

This fragment is not a complete receipt. Registration also records the verified
volume identity, NFS source, deletion policy and tracked jobs. A persistent
receipt must contain an explicit null `expires_at`. Timed receipts require a
UTC timestamp. Legacy receipts without `profile` or `retention` are interpreted
as timed RFAA receipts; malformed or mismatched profiles are rejected.

Importable helper interfaces:

```python
receipt_path(profile="rfaa", state_root=None)  # Honors the profile's override.
validate_receipt(receipt, profile="rfaa")      # Raises Error for invalid state.
check_active(receipt, volume_id, now, profile="rfaa")
```

`check_active` validates the complete receipt, exact requested UUID, active
status and retention deadline. It performs no I/O, locking or API calls. The
budget controller must securely read the correct receipt and call it while
holding `budget.lock`, then record requested volume IDs in that same lock before
the provider create request. Retirement marks the receipt `retiring` before
reading managed reservations under that lock. Submission/orchestration checks
before and after its own lock, and `track` before launch and after readiness,
remain necessary to cover caller processes and results retrieval.

## Allocation intent and budget integration

`../database-volume.py` provides `plan PROFILE`, `create PROFILE` and
`reconcile PROFILE` for these two fixed persistent allocations. It uses the
existing budget API credentials. Its default dependencies are sibling
`dc-budget.py` and `rfaa/storage.py`; `DC_HELPER` and `DATABASE_STORAGE_HELPER`
can select their deployed locations.

The private intents are `/var/lib/dc/rfaa-storage-intent.json` and
`/var/lib/dc/msa-storage-intent.json`. `DC_STATE_DIR` changes their default
directory; `RFAA_STORAGE_INTENT` and `MSA_STORAGE_INTENT` provide explicit
overrides. Intents must be separate from operational receipts and outside both
database volumes. Each records a unique token/name and the fixed create payload.
Provider tags are `purpose`, `allocation-token` and `retention=persistent`.

`plan` writes only a local `planned` intent and returns the current quote.
`create` reconciles exact names/tokens across active and trashed volumes before
considering a POST. A shared operation lock and `budget.lock` protect inventory
refresh, the quote, the durable `creating` marker and the POST. The quote includes
existing spending/reservations, at least 24 hours of background and new-storage
charges, and at least $10 margin below the configured ceiling. A successful
watchdog reconciliation within 180 seconds is required.

Intents have states `planned`, `creating`, `uncertain`, `allocated` and
`rejected`. Paid worker launches must reject unknown/corrupt intents and block
while either profile is `creating` or `uncertain`. The creation helper also
blocks a second allocation during that uncertainty. Accepted IDs and ambiguous
requests are reconciled without another POST, even after restart. A definite
HTTP 400 `Storage limit exceeded` rejection is recorded as `rejected`; explicit
`create` may retry it after fresh inventory and budget checks. Matching trash,
multiple matches, identity conflicts or disappearance do not authorize a new
allocation or any deletion.

After allocation, the budget controller retains the exact UUID and its verified
creation time/rate. An unexplained later inventory omission keeps its estimated
costs active and blocks further paid compute or allocation. Reappearance clears
that fence. Previously observed provider deletion or a private completed lifecycle
receipt matching UUID, profile, name and creation time confirms retirement;
cleanup remains available while accounting is unresolved.

Commands print JSON. `create` and `reconcile` return exit 0 only for an
`allocated` result, which includes verified UUID, identity, pricing and NFS
metadata. Other unresolved states return exit 4. `plan` returns exit 0 with a
`quote.allowed` field and any blockers; `create` enforces that quote again under
the lock. Registration of the operational receipt remains a separate verified
step after allocation. The helper does not download data or modify an existing
volume.

## Retirement isolation and preservation

Each profile has its own receipt and operation lock. Active persistent receipts
are timer no-ops. Once explicit retirement marks one `retiring`, later timer
runs retry unfinished cleanup regardless of its retention mode. Check and track
refuse retiring or completed allocations.

Only managed jobs whose recorded `volumes` contain this exact database UUID are
eligible for direct worker cleanup. A tracked client alone does not establish
worker ownership. Pending or uncertain reservations without a resolved worker
ID block volume teardown and may require manual reconciliation. Recorded boot
ID and process start time protect tracked callers against PID reuse or reboot.
Partial results are collected before signaling callers; completed head-local
results are preserved. No broad process-group or volume deletion is used.

Retirement stops the profile's named head units and collects small database
receipts before removing workers, unmounting its verified NFS export and
requesting exact-volume unshare/deletion. Unknown live attachments or a busy or
mismatched mount block teardown. Original head/shared-storage IDs remain
protected. ColabFold retirement cannot validate or act on an RFAA receipt.

ColabFold installation metadata retained under
`/var/lib/bio-runs/colabfold-database-receipts-VOLUME_UUID/` includes the allocation
receipt and these files beneath `/mnt/bio-msa-databases/colabfold`:

- `.msa-databases.json` and `manifest.json`;
- `.components/uniref30.json`, `environmental.json`, `pdb100.json`,
  `templates.json` and `mmcif.json`;
- `.conversions.json` and `.conversions/uniref30.json`, `environmental.json`
  and `pdb100.json`, retained as `conversions.json` and `conversion-NAME.json`;
- `mmcif/mmcif-content.jsonl.gz`, the compressed per-structure content hash manifest.

Copy failures remain recorded as warnings; model outputs on the original share
remain outside database-volume deletion. This metadata export does not preserve
database blobs or substitute for per-target MSA/template-coordinate bundles.
