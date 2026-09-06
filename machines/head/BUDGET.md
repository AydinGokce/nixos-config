# Cloud spending guard

The head's `dc` command enforces a **$500 estimated project spending ceiling**
for launches it manages. It reserves the complete requested runtime before
creating an instance, and a systemd watchdog removes workers that exceed their
deadline or reach the spending cutoff. **This is not a provider-enforced billing
cap.** The persistent head and shared data are retained and keep accruing costs
after temporary workers are removed.

The implementation is [`dc-budget.py`](dc-budget.py), invoked by
[`dc.sh`](dc.sh). [`configuration.nix`](configuration.nix) installs the helper and
watchdog. [`test_dc_budget.py`](test_dc_budget.py) exercises accounting,
reservations, provisioning failures, and confirmed cleanup without cloud access.

## Initial accounting and current resources

A read-only check on **2026-09-06 at 00:38 UTC** found one running CPU head, two
active storage volumes, and 21 trashed OS volumes. All 21 imported legacy GPU
ledger entries were closed. The new accounting estimated **$3.84 spent**, with no
open job reservation and **$0.08910/hour** of ongoing charges:

| Resource | Observed hourly price |
| --- | ---: |
| `nixos-poc`, `CPU.4V.16G` head | $0.048000 |
| Head's 50 GB OS disk | $0.013699 |
| `bio-shared`, 100 GB shared filesystem | $0.027397 |
| Total | **$0.089096** |

That snapshot predates subsequent validation jobs and any database storage
provisioning. Run `dc spend` for the current estimate. The default 24-hour
background reserve at those prices was **$2.14**, in addition to the **$10 safety
margin**. These figures describe this project's observed inventory, not an
invoice or an assertion about future prices.

On first use, accounting imports `/var/lib/dc/ledger.tsv`, then reconciles every
instance, active volume, and trashed volume returned by the project's public
API. Previously existing resources are estimated from creation time using their
current hourly price. Later refreshes retain accrued costs and apply observed
price changes to subsequent intervals. Restoring a trashed volume also charges
the gap spent in trash: the provider bills that interval when restoring it.
See [Verda's storage deletion documentation](https://docs.verda.com/storage/deleting-storage/).

## Launch reservations and deadlines

A launch proceeds only when this sum is **less than $500**:

```text
estimated accrued project spending
+ remaining reservations for existing managed jobs
+ complete requested runtime for the new instance and its OS disk
+ head, shared storage, and other unmanaged-resource reserve
+ $10 safety margin
```

The background reserve covers at least 24 hours, extended to the longest
reserved job duration if necessary. A locked, atomically saved state file
prevents two simultaneous launches from allocating the same remaining budget.
Unknown launch outcomes and unconfirmed cleanup block additional launches until
they are reconciled. Missing prices, unsupported currencies/contracts, corrupt
state, or unavailable inventory also prevent new paid launches.

`dc launch --max-hours HOURS` accepts a duration greater than zero and no more
than 24 hours; its default is 4 hours. The deadline starts immediately before the
create request, including provisioning time. Provisioning has its own maximum
10-minute wait. A failure during provisioning triggers cleanup, with the
watchdog retaining responsibility if cleanup cannot be confirmed.

`bio-submit --timeout SECONDS` defaults to 7,200 seconds and accepts 60–85,500.
It requests `--max-hours (SECONDS + 900) / 3600`, so the normal two-hour model
timeout reserves **2.25 hours** of instance lifetime. The extra 15 minutes covers
provisioning, SSH readiness, output transfer, and cleanup. This is a total
lifetime deadline: slow setup or transfer can consume that allowance, and the
watchdog may remove the worker before a slow transfer finishes. Recipes include
environment installation and weight downloads inside their model timeout.

Examples to run **on the head**:

```bash
dc types --gpu
dc launch 1A100.22V --max-hours 2 --volume b8b3b446-e464-44dd-9e01-6402489f8c5a
dc run 1A100.22V --max-hours 1 -- nvidia-smi
bio-submit openfold3 --fasta query.fasta --timeout 7200
```

Other launch options are `--spot`, `--name`, `--image`, `--loc`, repeatable
`--volume`, and `--os-size` (50 GB by default). Managed launches require a new
Ubuntu image rather than reusing an existing OS disk. Existing shared volumes
remain persistent; only the new worker's OS disk is selected for deletion.

## Watchdog and cleanup

`dc-budget-watchdog.timer` starts approximately 30 seconds after boot and runs
`dc watchdog` every 60 seconds, with a five-second scheduling tolerance. The
oneshot service has a 240-second systemd timeout. A launch refuses to proceed
when the last successful inventory reconciliation by the watchdog is more than
180 seconds old.

On each successful reconciliation the watchdog removes managed workers whose
deadline has elapsed, retries pending cleanup, and cleans up launches stuck in
provisioning or an uncertain state for at least 10 minutes when their instance
ID is known. It also initiates worker deletion when accrued spending plus the
background reserve and safety margin reaches the ceiling. If an inventory
request fails, it still attempts cleanup of already known overdue workers.

Deletion explicitly selects the worker's OS disk and preserves other attached
volumes. The command checks that both the instance and its OS disk have left
active inventory before reporting success. OS disks go into recoverable trash;
they are not immediately erased. Deleting resources can take time, and failed
confirmation retains the job for a later retry.

The watchdog and `dc rm all` operate on **recorded managed workers only**. The
existing CPU head and persistent shared/database volumes are not automatic
cleanup targets. Resources created outside `dc` count toward estimates when
visible in the inventory but are not automatically deleted.

Inspect status and logs:

```bash
dc spend
dc ls
systemctl status dc-budget-watchdog.timer dc-budget-watchdog.service
journalctl -u dc-budget-watchdog.service -n 50 --no-pager
jq -r '.jobs[] | select(.status != "closed") | [.id // "unknown", .status, .deadline] | @tsv' /var/lib/dc/budget.json
```

`dc ls` abbreviates instance IDs; the last command displays full managed IDs
and Unix deadlines. To stop a managed worker, use `dc rm FULL_INSTANCE_ID` or
`dc rm MANAGED_HOSTNAME`. `dc rm all` removes all recorded managed workers.
Retrieve any needed output first. `systemctl start dc-budget-watchdog.service`
runs an immediate reconciliation and applies the same deletion rules.

Budget and unresolved-operation stops return exit code **4**. `bio-submit`
recognizes that code and stops trying alternative GPU types. A capacity
rejection can still try the next compatible type without retaining a paid
reservation for the rejected launch.

## State, configuration, and limitations

State lives in `/var/lib/dc/budget.json`, with a separate `budget.lock`. Keep it
and the legacy ledger when updating or rebuilding the head. **Do not delete or
reset accounting state to unblock a job**: that discards historical costs and
the records needed to find abandoned workers. If an uncertain create has no
visible instance, inspect the provider console/API and the recorded reservation
before resolving it; the guard intentionally keeps that uncertainty blocking.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DC_BUDGET_CEILING` | `500` | Authorized estimated project spending ceiling |
| `DC_BUDGET_MARGIN` | `10` | Unallocated safety cushion in USD |
| `DC_PERSISTENT_RESERVE_HOURS` | `24` | Future head/storage/unmanaged resource allowance |
| `DC_MAX_JOB_HOURS` | `4` | Default lifetime for direct `dc launch`/`dc run` |
| `DC_PRIOR_SPEND_USD` | `0` | Known historical correction, applied only when initializing new state |
| `DC_STATE_DIR` | `/var/lib/dc` | Accounting state directory |
| `DC_CREDENTIALS_FILE` | `/root/.config/datacrunch/credentials.env` | Existing API credentials file |
| `DC_SSH_KEY` | `/root/.ssh/datacrunch_ed25519` | Head-to-worker automation key |

Keep budget settings consistent between interactive launches and the systemd
watchdog. A shell-only environment override does not configure an already
running systemd service. The credentials file is sourced by both entry points;
exported budget settings there are one way to share settings. Keep the approved
$500 ceiling unless the user changes the spending authorization. No additional
API key is required for the guard.

The estimate cannot reconstruct previously deleted resources that were never
observed or recorded, historical prices before initialization, taxes, or other
services outside the instance/volume inventory. It supports USD pay-as-you-go
and spot resources, and rejects long-term contracts rather than treating their
hourly display price as full billing. Provider balance is not used as cumulative
spending because top-ups can hide usage. The public API schema is documented at
[Verda's OpenAPI endpoint](https://api.verda.com/v1/openapi.json).

Network/provider outages, head shutdown, stalled processes, and deletion delays
can postpone enforcement. Persistent resources continue charging even after all
workers have stopped, so retaining a large RFAA database needs its own storage
and retention decision. The 24-hour reserve is an allowance for that ongoing
cost, not an automatic storage expiration or a guarantee that the total invoice
can never exceed $500.
