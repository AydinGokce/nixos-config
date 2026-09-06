# RFAA production database storage plan

Prepared from read-only provider inventory and official documentation on
2026-09-06 UTC. **No production database volume has been allocated or registered.**
The repository supplies the lifecycle helper and head timer described below.
The user has selected persistent retention for the upcoming private database
deployment. The existing $500 project ceiling still applies. The helper and
examples below currently implement a fixed expiry; support for persistent
receipts belongs to that deployment phase. No further retention approval is
needed.

## Capacity, cost, and protected resources

Provision one **3300 GB `NVMe_Shared` volume in `FIN-02`**. Keep all downloaded
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

These exclude compute, existing storage, taxes, and deletion delays. The existing
head and its storage add approximately $14.97 over seven days at the previously
observed $0.0891/hour. Record a fresh `dc spend` before provisioning and include
all existing job reservations, the full chosen retention, and the $10 safety
cushion. Account top-ups do not reset spending. A volume created directly through
the API bypasses `dc launch`'s reservation check.

For a seven-day allocation, configure exported
`DC_PERSISTENT_RESERVE_HOURS=168` consistently for `dc` and its watchdog before
use; this conservatively reserves another seven days of observed persistent
costs on each subsequent launch. Restore the previous value after removal.
This setting does not create an expiry timer; see [the budget guard](../BUDGET.md).

Every teardown must exclude these verified resources, regardless of names:

| Resource | Protected UUID |
| --- | --- |
| Existing head instance | `340a396b-19a1-4969-833e-2ddc80d5729b` |
| Head OS volume | `d48e5cae-cbbc-4d76-af0d-6faa275b5959` |
| Original 100 GB `bio-shared` volume | `b8b3b446-e464-44dd-9e01-6402489f8c5a` |

The original share is `NVMe_Shared`, `PAY_AS_YOU_GO`, in `FIN-02`, with export
`nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH`. Never resize or replace it to
hold the full databases. Do not use bulk volume deletion or `dc rm all` for this
storage operation.

## API requests after retention is selected

Use the existing credentials on the head; no new API key is required. All
requests below are **specifications for the later operation**, not commands that
have been executed. The [public API schema](https://api.verda.com/v1/openapi.json)
defines these request fields and endpoints. Authenticate with the existing
OAuth client-credentials flow without printing tokens or secrets.

Before creation, record a unique name, the exact UTC expiry, and the selected
delete policy in a separate root-owned allocation intent outside the new volume,
for example `/var/lib/dc/rfaa-storage-intent.json`. Do not create the operational
`rfaa-storage.json` receipt yourself: `register` creates it after verifying the
actual allocation and refuses an existing receipt. Check `GET /volumes` and
`GET /volumes/trash` for an earlier allocation with that name before retrying an
interrupted operation.

`POST /v1/volumes`:

```json
{
  "type": "NVMe_Shared",
  "location_code": "FIN-02",
  "size": 3300,
  "name": "bio-rfaa-db-<UNIQUE_TIMESTAMP>",
  "instance_ids": ["340a396b-19a1-4969-833e-2ddc80d5729b"],
  "tags": [
    {"key": "purpose", "value": "rfaa-databases"},
    {"key": "expires-at", "value": "<EXACT_UTC_EXPIRY>"}
  ]
}
```

The response is HTTP 202 with the new UUID. The official
[Python SDK](https://github.com/verda-cloud/sdk-python/blob/1d26dc83d9b26f51d2ecd87a28ffd42f73a059a3/verda/volumes/_volumes.py)
reads this response as plain text. Parse a valid UUID from either plain text or a
JSON string. The current budget helper's plain-UUID response exception applies
only to `/instances`; do not reuse its unchanged response parser for this POST.
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
mention shutdown requirements. Shared attach/detach runtime behavior has **not**
been mutation-tested in this review. Check empty-volume sharing and teardown
before starting the large download; do not shut down the head automatically if
a generic action is rejected.

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
  --expires-at 'YYYY-MM-DDTHH:MM:SS+00:00'
systemctl is-active rfaa-storage-expiry.timer
bio-rfaa-storage check --volume NEW_VOLUME_UUID
```

Registration only reads provider identity and writes the root-owned local
receipt; it never creates a volume. Compute the selected maximum lifetime from
the provider's `created_at`, including download time. Use an actual UTC date
in the command above. Registration refuses protected IDs, wrong volume identity,
and an existing receipt; preserve an earlier allocation's audit record before
registering another one.

Run the installer as a named systemd service on the existing CPU head:

```bash
systemd-run --collect --unit=rfaa-database-install --property=RuntimeMaxSec=172800 \
  /run/current-system/sw/bin/bio-rfaa-databases install
journalctl -u rfaa-database-install -f
```

Confirm its exit status and run `bio-rfaa-databases validate`. Download runtime
is uncertain; a two-day installer timeout does not extend storage retention.
Use one writer. Read-only worker mounts and a separate database-volume lifetime
avoid deleting model environments or results during database teardown.

Account NVMe and storage-item quotas include both block storage and SFS;
deleted storage still consumes quota until permanently removed. The documented
[quota view](https://docs.verda.com/welcome-to-verda/quotas/) is in the console.
No quota-read endpoint appears in the checked public OpenAPI, so capacity is not
proven by this read-only review. Subsequent live validation launches were
rejected with `Storage limit exceeded`. At the quota audit, active storage was
150 GB and trash contained 28 OS volumes totaling 1400 GB. Seven of those disks
(350 GB) matched closed new managed jobs and were subsequently confirmed
permanently removed by the narrow `dc gc` operation; the 21 older unmatched disks
were preserved. The exact account quota
limit is still unknown. Freeing those 350 GB for small validation workers does
not establish capacity for an additional 3300 GB database volume. Check quota
and request an increase if needed before production allocation; a quota error
is not a reason to delete unrelated old volumes.

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
`Persistent=true`. It does nothing before expiry or without a receipt. Detection
can take up to one minute after the recorded UTC deadline; teardown and outages
can add delay. A separate operation lock prevents concurrent timer/manual
retirements while allowing receipt checks. An `expires-at` provider tag is
descriptive; it does not activate provider-enforced expiry.

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

After validation and result verification, the selected delete-after-validation
policy can retire early without editing the receipt:

```bash
bio-rfaa-storage expire --now --volume EXACT_REGISTERED_UUID
```

This command requires the exact registered UUID and applies the same identity,
job, mount, and confirmation checks. Ordinary `expire` remains deadline-only.

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
ordering, and retry/confirmation behavior. Production volume allocation and a
real storage expiry remain untested pending the retention decision. A head-local
timer cannot guarantee a hard billing cutoff during outages; the existing GPU
watchdog only accounts for storage. The $500 estimate is still not a provider
billing cap.
