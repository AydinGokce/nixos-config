# RFAA production database storage plan

Updated 2026-09-06 UTC after production allocation and registration. The user
selected **persistent retention**. RFAA volume
`00537aea-2184-434a-84c1-1074bd1ebd58` was created at
`2026-09-06T02:52:27.347Z`, registered with `expires_at: null`, and mounted on the
head. Its name is `bio-rfaa-db-9942ea95f1bc45ee9b30434f1110d815`; its verified export is
`nfs.fin-02.datacrunch.io:/bio-rfaa-db-9942ea95f1bc45ee9b30434f1110d815-W4BWHZ3V6HHG`.

The `rfaa-database-install.service` started at **02:54:21 UTC** with a two-day
runtime limit, initial invocation `0e3ba600e859465f88a0960475c5694c`. It resumed
at 03:04:57 UTC after clean NFS 4.1 remounts, using invocation
`6fe5d072c79848589b8ccf1f72baec2a`. Read-only inspection confirmed it running and
the UniRef30 archive growing. Full installation and a
production database-backed prediction remain pending; a running service is not
an installation success receipt.

The separate 3000 GB ColabFold volume
`3ccef50a-59fe-4a5f-b7d3-ec669fe7ccef` is also allocated and registered persistent.
The [two-profile storage contract](../msa/STORAGE_CONTRACT.md) describes the
implemented allocator, persistent receipts, and explicit retirement. The $500
project ceiling remains a launch/compute guard, not a hard storage billing cap.

## Capacity, cost, and protected resources

The RFAA allocation is **3300 GB `NVMe_Shared` in `FIN-02`**. Keep all downloaded
databases beneath `/mnt/bio-databases/rfaa`; keep environments, input sequences,
predictions, alignments, and logs on the existing share and head results directory.
The [database installer](README.md) downloads and extracts serially, removing
validated archives. Check actual available bytes with `df -B1` before starting;
3300 decimal GB is approximately 3.00 TiB. Do not use `--keep-archives` at this size.

The live [volume price endpoint](https://api.verda.com/v1/volume-types?currency=usd)
returns `$0.20/GB/month` and `7.6103500761035e-8 USD/GB/second` for `NVMe_Shared`.
Multiplying the second-based rate gives:

| Retention | Dedicated 3300 GB storage estimate |
| --- | ---: |
| One hour | $0.9041 |
| One day | $21.70 |
| Seven days | $151.89 |
| Provider monthly equivalent | $660.00 |

These exclude compute, existing storage, taxes, and deletion delays. The two new
database volumes together cost approximately $41.42/day; with the existing head
and original storage at $0.0891/hour, background costs are approximately
**$43.56/day**. A fresh $500 allowance would cover at most 11.48 days before
previous spending, safety margin, or additional compute. Account top-ups do not
reset project spending.

`bio-database-volume` checks current spending, existing job reservations, at least
24 hours of background/new-storage cost, and at least $10 margin before creating
a volume. Subsequent launches reserve ongoing background costs too. This rolling
allowance does not expire persistent storage: it continues charging after the
compute guard halts workers. Read `dc spend` regularly and update the ceiling only
with the user's authorization. `DC_PERSISTENT_RESERVE_HOURS` can increase the
look-ahead consistently for `dc` and its watchdog, but does not schedule deletion;
see [the budget guard](../BUDGET.md).

Every teardown must exclude these verified resources, regardless of names:

| Resource | Protected UUID |
| --- | --- |
| Existing head instance | `340a396b-19a1-4969-833e-2ddc80d5729b` |
| Head OS volume | `d48e5cae-cbbc-4d76-af0d-6faa275b5959` |
| 300 GiB `bio-shared` runtime volume | `b8b3b446-e464-44dd-9e01-6402489f8c5a` |

The original share is `NVMe_Shared`, `PAY_AS_YOU_GO`, in `FIN-02`, with export
`nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH`. It was expanded from 200 to
300 GiB on September 14 to retain the MSA, BindCraft and RF3 runtime archives;
its ID, export, mounted root and checked files were preserved. Never resize or
replace it to hold the full databases. Do not use bulk volume deletion or `dc rm all` for this
storage operation.

## Allocation and registration procedure

Use the existing credentials on the head; no new API key is required. All
requests below document the implemented operation. Both current database
allocations have already completed this step; do not create replacements or
rerun registration over their receipts. The [public API schema](https://api.verda.com/v1/openapi.json)
defines these request fields and endpoints. Authenticate with the existing
OAuth client-credentials flow without printing tokens or secrets.

`bio-database-volume plan rfaa`, `create rfaa`, and `reconcile rfaa` manage the
unique name/token and root-owned `/var/lib/dc/rfaa-storage-intent.json` outside the
new volume. They serialize allocation and budget checks and reconcile active and
trashed inventory before any retry. An ambiguous accepted request is never
blindly repeated. Do not create the operational `rfaa-storage.json` yourself:
`register` creates it after verifying the actual allocation and refuses an
existing receipt.

`POST /v1/volumes`:

```json
{
  "type": "NVMe_Shared",
  "location_code": "FIN-02",
  "size": 3300,
  "name": "bio-rfaa-db-<UNIQUE_TOKEN>",
  "instance_ids": ["340a396b-19a1-4969-833e-2ddc80d5729b"],
  "tags": [
    {"key": "purpose", "value": "rfaa-databases"},
    {"key": "allocation-token", "value": "<UNIQUE_TOKEN>"},
    {"key": "retention", "value": "persistent"}
  ]
}
```

The response is HTTP 202 with the new UUID. The official
[Python SDK](https://github.com/verda-cloud/sdk-python/blob/1d26dc83d9b26f51d2ecd87a28ffd42f73a059a3/verda/volumes/_volumes.py)
reads this response as plain text. Parse a valid UUID from either plain text or a
JSON string. The budget API helper accepts this strict UUID shape at both
`/instances` and `/volumes` create endpoints.
If the response is ambiguous, reconcile inventory using the unique allocation
name/tag before another POST; otherwise a retry could create a second paid volume.

Persist the returned UUID in the allocation intent immediately. Poll `GET /v1/volumes/<NEW_VOLUME_ID>`
until its requested sharing is visible. Verify `type`, `size`, `location`,
`is_os_volume=false`, `currency=usd`, and `contract=PAY_AS_YOU_GO`. Record
`created_at`, `base_hourly_cost`, `monthly_price`, `pseudo_path`, and
`mount_command`. An exported shared filesystem can report `status=exported`;
requiring block-volume status `attached` would reject the current working share.
The live API returns objects in `instances`, despite its schema's string-array
annotation. Match their IDs, not the singular `instance_id`, which can retain an
old attachment after a worker is removed.

Additional sharing uses `PUT /v1/volumes`, for example:

```json
{
  "action": "attach",
  "id": "<NEW_VOLUME_ID>",
  "instance_id": "<SAME_LOCATION_INSTANCE_ID>"
}
```

Normal RFAA workers already pass both existing volume IDs during `dc launch`.
The [SFS sharing guide](https://docs.verda.com/storage/shared-filesystems-sfs/editing-share-settings/)
requires matching locations and warns that sharing to long-term instances can
incur an upfront contract payment. Use pay-as-you-go instances here. The SFS
guide supports sharing to running instances; generic block-volume API/SDK notes
mention shutdown requirements. Creation-time sharing and head NFS access are now
verified on the allocated volumes. Shared detach/deletion has not been exercised
on these persistent production volumes. Do not shut down the head automatically
if a generic action is rejected.

Read the NFS source token from `mount_command` and compare it with `pseudo_path`;
do not execute a provider-returned shell command with `eval`. Use its actual host
name: current live exports still use `datacrunch.io`, while the
[mounting guide](https://docs.verda.com/storage/shared-filesystems-sfs/mounting-a-shared-filesystem/)
also documents `verda.com`. Put the verified new UUID and export in
`machines/head/rfaa-storage.nix`, deploy, and check the head mount:

```bash
findmnt /mnt/bio-databases
df -B1 /mnt/bio-databases
bio-rfaa-databases plan
```

Register the actual allocation and verify the timer **before starting downloads**:

```bash
bio-rfaa-storage register --volume NEW_VOLUME_UUID --name ACTUAL_VOLUME_NAME \
  --persistent
systemctl is-active rfaa-storage-expiry.timer
bio-rfaa-storage check --volume NEW_VOLUME_UUID
```

Registration only reads provider identity and writes the root-owned local
receipt; it never creates a volume. The alternative
`--expires-at 'YYYY-MM-DDTHH:MM:SS+00:00'` selects a timed allocation with an actual
future UTC deadline, including download time; it is mutually exclusive with
`--persistent`. Registration refuses protected IDs, wrong volume identity,
and an existing receipt; preserve an earlier allocation's audit record before
registering another one.

Run the installer as a named systemd service on the existing CPU head:

```bash
systemd-run --collect --unit=rfaa-database-install --property=RuntimeMaxSec=172800 \
  /run/current-system/sw/bin/bio-rfaa-databases install
journalctl -u rfaa-database-install -f
```

Confirm its exit status and run `bio-rfaa-databases validate`. Download runtime
is uncertain; the two-day installer timeout stops the writer, not storage billing.
Use one writer. Read-only worker mounts and a separate database-volume lifetime
avoid deleting model environments or results during database teardown.

Account NVMe and storage-item quotas include both block storage and SFS;
deleted storage still consumes quota until permanently removed. The documented
[quota view](https://docs.verda.com/welcome-to-verda/quotas/) is in the console.
No quota-read endpoint appears in the checked public OpenAPI. Earlier live validation launches were
rejected with `Storage limit exceeded`. At the quota audit, active storage was
150 GB and trash contained 28 OS volumes totaling 1400 GB. Seven of those disks
(350 GB) matched closed new managed jobs and were subsequently confirmed
permanently removed by the narrow `dc gc` operation; the 21 older unmatched disks
were preserved. The exact account quota
limit is still unknown. Both production allocations subsequently succeeded,
establishing capacity for the new 6300 GB at that time. This does not establish
remaining headroom for worker OS disks. A later quota error is a reason to inspect
the account quota, not to delete unrelated old volumes.

## Full-mode worker and validation

The current [instance type endpoint](https://api.verda.com/v1/instance-types)
reports the following host memory. GPU VRAM is a separate limit:

| Instance type | Host RAM | GPU VRAM | Pay-as-you-go/hour |
| --- | ---: | ---: | ---: |
| `1A100.22V` | 120 GB | 80 GB | $1.79 |
| `1A100.40S.22V` | 120 GB | 40 GB | $1.29 |
| `1A6000.10V` | 60 GB | 48 GB | $0.61 |

Full mode's 64 GiB HHsuite allowance exceeds A6000 host RAM. Default full-mode
candidates are the two A100 types; single-sequence mode retains the A6000.
The recipe checks available memory against the requested limit plus 8 GiB before
searching. An explicitly chosen smaller worker can use head-side
`RFAA_MEM_GB=32`, but the full BFD search still needs validation at that setting;
HHsuite's limit is not a cap on every process's total resident memory.

For the first production validation, choose the A100 80 GB explicitly:

```bash
bio-submit rfaa --fasta /tmp/bio-validation.fasta --sub full \
  --gpu 1A100.22V --name rfaa-full-validation --timeout 21600
```

Verify the worker sees all database receipts, the preparation receipt records
completed searches, the output PDB has the expected sequence/residues, and
`job.json` reports success. A valid query may have no significant template hits.
Copy predictions, alignments, logs, `job.json`, preparation receipts, database
installation receipts, and the allocation receipt to head-local storage and the
workstation. Check copied file sizes/hashes before ending validation.

## Implemented expiry and deletion workflow

[`storage.py`](storage.py), exposed as `bio-rfaa-storage` on the head, operates
only on the exact UUID in `/var/lib/dc/rfaa-storage.json`. The receipt has root
ownership and private permissions; symlinks and insecure state are rejected.
`DC_STATE_DIR` changes the default state directory and `RFAA_STORAGE_RECEIPT`
can select a different receipt consistently across submission and budget tools.

`rfaa-storage-expiry.timer` invokes the helper at each minute boundary with
`Persistent=true`. It does nothing without a receipt or for an active persistent
allocation. For a timed receipt, detection can take up to one minute after the
recorded UTC deadline; teardown and outages
can add delay. A separate operation lock prevents concurrent timer/manual
retirements while allowing receipt checks. Retiring persistent allocations are
retried too. Provider tags describe retention; they do not enforce it.

Full submissions check the receipt before and after the submission lock and
record their PID, kernel boot ID, and process start ticks. The launcher checks
the same receipt again while holding `budget.lock`, then records its requested
volume IDs before sending the create request. Retirement first marks the receipt
`retiring`, releases its receipt lock, and acquires `budget.lock` before reading
reservations. An in-flight launcher therefore either commits a visible
reservation or is refused. Single-sequence and unrelated model jobs continue.

The service then verifies provider identity before process or cloud cleanup,
stops the named database installer, copies small installation receipts, and
retrieves partial outputs from tracked active jobs before signaling them.
Successful head-local results with `job.json` exit status zero are not overwritten
from the shared filesystem. Signals require matching boot/start identity and use
Linux process handles to avoid targeting a reused PID; no process groups are
killed. Only exact managed worker IDs recorded as using this database are passed
to `dc rm`. Results on the original share and head remain outside the database
volume. Copy failures are retained in the receipt's `copy_warnings`; an offline
workstation does not extend storage retention.

Any unresolved managed reservation blocks volume teardown. In particular, an
uncertain create with no known instance ID may need manual provider/accounting
reconciliation; repeated timer runs cannot necessarily resolve it. Do not reset
the budget ledger to force deletion. Unknown live attachments, identity changes,
wrong or busy mounts, and unsuccessful API actions also keep the receipt
`retiring` for later retry, so actual retention can exceed the intended deadline.

After workers are confirmed closed, the helper checks the database mount's
source, stops its automount, and unmounts without force. It requests removal of
the head's sharing only for the new database UUID and waits for provider
confirmation on a subsequent timer run. For shared storage it uses the volume's
`instances` list and live instances' `volume_ids` arrays together; either can
reveal a remaining attachment. It ignores the legacy singular `instance_id`,
which can still name the head after confirmed unsharing. Missing or malformed
attachment arrays defer teardown. It then sends
`DELETE /v1/volumes/<REGISTERED_UUID>`. The receipt becomes `complete` only after
the volume leaves active inventory and a deleted timestamp or complete absence
is confirmed; a GET 404 with an identity-checked trash record is also supported.
The head and original shared volume are never deletion targets. Clear retired
values from `rfaa-storage.nix` in a later normal deployment; expiry itself does
not depend on the workstation being online.

Persistent retention is the selected policy. If the operator later chooses to
retire this allocation after verifying retained results, the explicit command is:

```bash
bio-rfaa-storage expire --now --volume EXACT_REGISTERED_UUID
```

This command requires the exact registered UUID and applies the same identity,
job, mount, and confirmation checks. Ordinary `expire` starts timed retirement
only at the deadline and retries any already-retiring allocation; it never starts
retirement of an active persistent receipt.

The default is soft deletion: provider documentation gives a 96-hour
recovery window, then permanent removal. Restoring incurs the pay-as-you-go
charge for the time in trash. `register --permanent` selects immediate permanent
deletion and is irreversible; use it only if the chosen policy expressly
includes permanent removal or releasing quota immediately.
See [Verda deletion semantics](https://docs.verda.com/storage/deleting-storage/).

Inspect `systemctl status rfaa-storage-expiry.timer`,
`journalctl -u rfaa-storage-expiry.service`, and the receipt for current state.
The offline lifecycle tests cover identity/permission failures, exact scope,
launcher/expiry races, concurrent retirement, PID/boot identity, output retrieval
ordering, persistence/profile isolation, and retry/confirmation behavior.
Production allocation, registration and mounting are verified; a real production
storage retirement remains untested because these databases are being retained.
A head-local timer cannot guarantee a hard billing cutoff during outages. The
existing compute watchdog accounts for storage and fences new paid work after an
unexplained allocation inventory omission, while preserving its estimated costs.
It does not delete persistent volumes at $500. The estimate is not a provider
billing cap.
