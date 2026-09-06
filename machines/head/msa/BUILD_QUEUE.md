# Resumable private database build queue

`build-queue.py` provides one bounded head-side tick for the full ColabFold
installation. It waits for completed head downloads, selects currently available
large-memory compute, and runs the existing guarded `bio-msa` wrapper. It does
not allocate storage, remove volumes, switch the prediction default, or claim
public/private scientific parity. Persistent storage continues billing under its
explicit retention policy; the existing $500 controller guards new compute and
reservations rather than deleting persistent data at that amount.

The helper is implemented and tested offline. The head configuration ships its
wrapper and 15-minute timer; `ConditionPathExists` makes ticks inert until an
operator explicitly initializes the queue. It requires the controller's
`DC_MAX_INSTANCE_HOURLY` fresh-quote guard and the `bio-msa install --json` and
`bio-msa panel` routes from the same release.

## Invocation and deployment contract

Run as root on the head through a wrapper named `bio-msa-build-queue` that executes
`python3 /etc/bio-tools/msa/build-queue.py "$@"`. Its PATH must include `python3`,
`systemctl`, `systemd-run`, `findmnt`, `bio-msa` and `bio-msa-storage`, normally
`/run/current-system/sw/bin`. For `tick`, source the existing private
`/root/.config/datacrunch/credentials.env` and export `DATACRUNCH_CLIENT_ID` and
`DATACRUNCH_CLIENT_SECRET`; do not put credentials on the command line or in
logs. Other commands do not call the cloud API. The launched wrapper obtains
its credentials using the ordinary `dc` configuration.

```sh
# Freeze the complete requested target list before unattended work:
bio-msa-build-queue init --panel-manifest /root/frozen-private-panel.json
# Omit --panel-manifest only when the requested work is database installation.
bio-msa-build-queue status
bio-msa-build-queue tick
# After investigating and resolving an explicit blocked state:
bio-msa-build-queue resume
```

`--state-root DIR` defaults to `DC_STATE_DIR` or `/var/lib/dc` and
`--database-root DIR` defaults to `MSA_DB_ROOT` or
`/mnt/bio-msa-databases/colabfold`. Global options precede the command. The state
root must already exist, be root-owned, and not be writable by other users.
`BIO_TOOLS_SRC` defaults to `/etc/bio-tools`; it locates the separately deployed
controller and lifecycle helpers. The normal production paths should be used
together with the existing wrappers and lifecycle service. Merely pointing this
helper at another database directory does not reconfigure those wrappers.

The root-private receipt is `/var/lib/dc/msa-build-queue.json`. It binds one unique
queue token, the exact registered persistent ColabFold volume, up to three
attempts, and any panel snapshot. Initialization copies the manifest into a new
0600 file `msa-build-panel-TOKEN.json`, validates that copy using `panel.py`, and
records both its byte SHA256 and canonical manifest SHA256. Later ticks reject
any changed snapshot. Each attempt retains its unit identity, boot ID, worker
quote/OS metadata, managed-job baseline, observed job tokens, unit snapshots,
result paths and final cleanup/exit state. Queue JSON rejects duplicate keys,
nonfinite values, unsafe permissions and symlinks.

A suitable timer contract is:

```ini
# bio-msa-build-queue.service
[Service]
Type=oneshot
ExecStart=/run/current-system/sw/bin/bio-msa-build-queue tick
TimeoutStartSec=600

# bio-msa-build-queue.timer
[Timer]
OnCalendar=*-*-* *:00/15:00
Persistent=true
AccuracySec=1min
```

Initialize the queue to activate work under the installed timer. A tick performs no internal
sleep/poll loop. Concurrent ticks return `busy` through a separate nonblocking
operation lock. The individual full database and panel validators have 150-second
subprocess limits; API and systemd calls have 30-second limits. A 600-second
service limit bounds a slow series of reads. On an interrupted tick after durable
dispatch, the next tick reconciles the recorded service instead of starting it
again. Detection takes up to the timer interval, plus service/API outages.

## Admission and resumption

A tick first reconciles any previous attempt. New work then requires all of:

- The exact active, explicitly persistent ColabFold storage receipt and a matching
  mounted NFS export using version 4.1. The existing lifecycle wrapper also checks
  before and after the MSA submission lock and tracks the launcher around rental.
- An inactive `msa-database-install.service`, the independent `msa-submit.lock`
  being available, and no open managed reservation using that database volume.
  Manual wrapper submissions therefore take precedence over queue admission.
- The exact pinned `manifest.json`, complete `.downloads.json`, consistent
  per-archive receipts, and each archive's exact pinned byte length. Missing
  initial download metadata waits without renting; malformed or contradictory
  metadata blocks. The scheduler does not reread hundreds of GB to recompute
  archive hashes; the installer rechecks complete source hashes before use.
- Fresh FIN-02 regular and spot availability and USD prices. Among reviewed
  Linux x86-64 worker families, choose the cheapest host advertised with at least
  768 GiB after converting decimal GB conservatively. CPU wins only at an equal
  price. Required images are `ubuntu-24.04` for CPU and
  `ubuntu-24.04-cuda-12.8-open-docker` for GPU. Unsupported images, ARM/Grace and
  confidential variants are excluded. The price must be at most $13/hour.
  Advertised memory is only admission evidence: the worker still requires
  768 GiB of actual `MemAvailable` before installation/search.

A missing source archive is allowed **only** if its exact owning promoted
component passes the existing complete structural/file validator, its internal
and external component receipts agree, and its source receipt exactly matches
`.downloads.json`. Taxonomy belongs to the promoted UniRef30 component. Each
such source and owner is recorded in `input_validation`. Incomplete staging,
receipt presence alone, an altered source history or corrupt component cannot
qualify. This permits the installer's intentional deletion of validated source
archives after promotion. Structural/file validation checks file inventories,
index bounds and recorded fingerprints; it is not a full rehash of every large
installed file.

If the final `.msa-databases.json` exists, the full existing database validator
must pass. No removed archive is needed for that path. The remaining current
mmCIF mirror download, conversion, full index creation and validation still run
as part of full installation; the scheduler does not change their scope.

Each paid attempt uses the ordinary wrapper with `--worker TYPE`, optional
`--spot`, and `--timeout 21600`. The fresh controller quote independently enforces
`DC_MAX_INSTANCE_HOURLY=13` before any reservation or POST; a changed price cannot
bypass the ceiling between selection and launch. The controller also applies
its existing watchdog, storage-lifetime, allocation-intent and aggregate budget
fences. The six-hour work timeout reserves **6.25 hours**, including the existing
15-minute launch/cleanup allowance: at the maximum instance rate, that is $81.25
of instance reservation per attempt, plus applicable OS/storage costs.

## Exact attempts, cleanup and requested panels

Services are named `bio-msa-build-QUEUETOKEN-a1.service` through `a3.service`.
The queue durably records each attempt **before** calling `systemd-run`. Units
use `Type=exec`, `RemainAfterExit=yes`, a 22,500-second service runtime limit and
180-second stop allowance. Do not use `--collect`, reset failed units or restart
these unit names while the queue is active. Their retained status is essential
reconciliation evidence. Logs append to root-private state-directory files;
normal worker output and every retrieved partial panel stay under
`/var/lib/bio-runs/JOB`.

A missing service after submission, changed invocation/description, or head reboot
never triggers automatic re-dispatch. A running service is observed without
starting another. After it exits, every recorded managed job must be closed,
the exact worker must be absent from live inventory, and its managed OS disk must
be absent from active/trash inventory or explicitly marked permanently deleted.
An uncertain reservation with no instance ID still blocks. The queue calls no
cloud delete API: ordinary submission cleanup and the existing watchdog own
resource removal. Successful results must match the tracked launcher PID and
boot ID, database volume, managed worker ID, and final `job.json` exit status.

With a panel manifest, `ready` means the final database validates **and the entire
frozen panel validates**, including every native bundle and its retained private
API evidence, after exact worker cleanup. Initial installation runs
`bio-msa install --json SNAPSHOT`, preparing targets on the same worker using the
remaining original timeout. If installation promotes the full database before a
later failure/interruption, the next eligible attempt runs
`bio-msa panel --json SNAPSHOT` using the read-only database mount. Starting a new
queue when the database is already ready uses the same panel-only route. It does
not rebuild indexes or require deleted archives. Whole-panel retries keep the
same complete target list and preserve every previous partial/failed artifact;
there is no automatic target selection or cross-attempt merging.

Without a panel, a valid final database and reconciled prior attempts are enough
for `ready`; no additional host is rented. Three total dispatch attempts are the
maximum across install and panel stages. Failed provider/preflight attempts also
consume an attempt. Exit 4 from the guarded wrapper is terminal `blocked`, pending
an explicit operator `resume`; it never silently falls through to another host.
`resume` preserves the attempt count and all outstanding identities.

Other states explain waiting for downloads, manual work, capacity or cleanup.
`exhausted` needs operator review. Missing-unit/reboot/no-ID ambiguity may require
manual reconciliation and cannot be cleared by waiting alone. Inspect the exact
retained unit, queue receipt, budget ledger, lifecycle tracking and provider
inventory; do not delete/reset state to make a retry appear new. The queue never
stops unrelated jobs or deletes persistent volumes to force progress.

## Local checks

When an existing prediction baseline must survive later deployments, set
`BIO_MSA_QUEUE_TOOLS_PIN` to its retained `pin.json`. The optional pin binds an
immutable Nix source tree, complete file manifest, exact `bio-submit` executable
and cluster configuration. The queue verifies these before recording intent and
again before dispatch, records their hashes in the attempt, and invokes that
exact executable with `BIO_TOOLS_SRC` and `BIO_CLUSTER_CONFIG`. A changed or
missing pin stops dispatch; it never silently selects newly deployed recipes.
The current head configuration pins the September 6 comparison artifacts.

Mount admission ignores systemd's `autofs` placeholder while still requiring
exactly one matching data export using NFS 4.1. Additional or wrong data mounts
remain errors.

```sh
cd machines/head/msa
python3 -m unittest -v test_build_queue.py
python3 build-queue.py --database-root /path/to/candidate/colabfold check-inputs
```

The tests use fake cloud inventory and miniature real template FFDB validators.
They cover dispatch ambiguity, reboot/invocation fencing, actual archive sizes,
fully validated promoted-source resumption, corrupt components, price/RAM/image
selection, exact worker and OS cleanup, panel-only retries and the three-attempt
limit. They do not allocate a worker or establish production build readiness.

A separate head-only transient service probe confirmed that `Type=exec` with
`RemainAfterExit=yes` retains `ExecMainPID`, exit status, invocation ID and the
exact description after completion. Its process-reported PID matched
`ExecMainPID`; `MainPID` correctly became zero. The probe was then removed.
