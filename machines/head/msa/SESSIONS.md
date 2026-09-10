# Managed private MSA sessions

`bio-msa prepare --model protenix --fasta query.fasta --bundle-result
prepared.json` validates its input locally, then ensures one shared private search
session. A missing session is started through the existing worker selector, fresh
quote, budget reservation and cleanup wrapper. Concurrent callers join that same
startup and wait for full database readiness. An already-ready session is reused.
RF3's verified preparation-cache lookup still runs first: a cache hit does not
start a search worker. Explicit prepared bundles likewise need no startup.

The default maximum session lifetime is 7,200 seconds, the idle timeout is 900
seconds, and index prefetch remains enabled. Existing instance selection, the
$13/hour instance-price ceiling and the current total project budget gate remain
in force. Full database storage and completed prepared bundles persist when the
worker is removed. The native GUI defaults to private MSA; the low-level legacy
`bio-submit` default remains public unless private is selected. No request falls
back to a public endpoint when private infrastructure is unavailable.

`bio-msa prepare --require-session ...` preserves the operator mode that requires
an already-ready session and never starts compute. `bio-msa session start
--timeout 7200` still starts an explicit managed session. `session status` checks
the original unit invocation, current managed provider identity, pinned SSH host
key, boot and session generation. `session stop` retains its explicit semantics.

Managed sessions retain the host key negotiated by the first successful SSH
readiness connection in a fresh, private per-job `worker-known-hosts` file.
The job receipt binds those exact bytes to its instance and IP. Registration
copies that key into the session after checking its path, ownership, mode and
hash, then verifies the worker identity over strict SSH. All later connections
use strict checking against the retained key; there is no separate key scan or
forced Ed25519 host-key requirement.

On-demand startup writes the immutable session/start intent and active registration
before asking systemd to launch anything. All starters share the existing
registration lock; readers wait out incomplete publication. A lost startup reply
can join its exact saved unit command/invocation, but never issues another launch.
A caller that starts or joins a starting generation does not replace it if startup
fails. Later requests may retire an already-ended generation after a verified
pre-allocation failure receipt, or after fresh provider evidence proves the
exact allocated worker and temporary OS disk are absent.
Replaced units, missing worker identity, unknown startup outcomes and uncertain
cleanup keep registration intact for inspection. An old closure receipt alone
never authorizes replacement. No search request is silently replayed.

Automatic capacity selection waits up to 1,800 seconds, pausing 30 seconds after
each confirmed shortage before checking regular and spot offers again. Set
`--capacity-wait-seconds 0..7200` on `session start` or `prepare` (or
`BIO_MSA_CAPACITY_WAIT_SECONDS`) to change the window; zero checks once.
`BIO_MSA_CAPACITY_POLL_SECONDS` changes the 1–300 second polling pause for direct
launches. Capacity checks do not rent compute. Invalid/provider-error responses
fail closed without retrying. The GUI shows `waiting_capacity` with a retry
window and unknown availability ETA. The request deadline still bounds waiting;
the paid worker lifetime starts with its normal allocation reservation.

Before selecting a managed session worker, `startup.py` records `attempt.json`
bound to the exact session intent, systemd invocation, frozen tools and launcher.
It fsyncs `allocation-started.json` before any `dc launch`. A failure before that
marker can publish `no-allocation.json`; after the exact unit stops, the next
request (or explicit `session stop`) revalidates that proof under the registry
lock, retains `closed.json`, and removes the active pointer. A possibly submitted
allocation or unknown old startup cannot use this path. Existing waiting callers
receive `capacity_timeout`, `preallocation_failed`, or `cancelled` without starting
a replacement in the same request.

Startup, readiness checks, queued search and execution consume the same request
`--timeout`. Each provider/systemd/SSH readiness wait is bounded by the remaining
deadline. Search deadlines are also capped by the managed session's deadline.
There is the existing maximum 45-second result-receipt transport grace after
search, followed by bounded local output validation; this does not extend native
search or worker lifetime. `--session-timeout` controls only a newly started
session's maximum lifetime and cannot extend an existing one. Cancelling a caller
stops its wait; the independently owned shared service retains idle and maximum
lifetime cleanup for other callers.

Progress is emitted as flushed stderr lines:
`BIO_MSA_SESSION_STAGE <starting|warming|ready|waiting|failed> <JSON>`.
Each event contains `message` and `timestamp_ns`, with optional `session_id` and
`code`; lines are at most 4,096 bytes and contain no credentials or command argv.
If the trusted runner supplies `BIO_MSA_PROGRESS_LOG`, the caller also appends
identical events to that existing private, regular, single-link, caller-owned
file. It never grows past 1 MiB and is never passed to the shared service. This
lets RF3 retain its nested diagnostic logs while the GUI sees startup, warm-up,
search and actionable failure messages. Readiness failures use explicit
`session_missing`, `session_starting`, `session_failed` or `session_uncertain`
codes instead of a generic missing-document message.

The official pinned CPU API runs on worker loopback. Requests travel through
pinned SSH and a worker-local spool, with one preparation at a time and the
existing per-target audit proxy. Successful output includes raw API request and
response bytes, downloaded result archives, selected template payloads,
backend job scripts and full database/source provenance. The standard model
preparers and RF3's per-chain TaxID search adapter are reused unchanged.

## Index residency

The three full CPU indexes occupy about 700 GB. `--warm prefetch` is the default
and reads all existing index pages, with a memory-headroom preflight and a
deadline bounded by the existing session lifetime minus its cleanup reserve.
Runtime setup and index warm-up consume that lifetime; they never extend it.
An explicit worker `--warm-seconds` can impose a shorter warm-up cap. The
default no longer imposes an independent 30-minute limit on the full indexes.
Loading uses four independent buffered readers over disjoint, page-aligned
ranges, with 16 MiB read buffers. Each reader opens its own read-only descriptor
so the kernel can maintain a separate sequential read-ahead stream. Index
identity and byte counts are checked, and the existing final `mincore` check
still requires every page of every full index to be resident before readiness.

Where writable BDI controls are available, loading temporarily raises the
worker device's read-ahead to at least 15,360 KiB. A private claim records its
boot, device, original value and exact temporary setting before changing it.
The loader restores it on completion, failure or cancellation, before waiting
for outstanding reads. Enclosing worker cleanup repeats restoration from
`/tmp/bio-msa-prefetch-<session-id>` after abnormal termination. Repeated cleanup
is harmless; a missing claim is a no-op, and an unrelated later BDI change is
never overwritten. Unsupported BDI controls are reported in the warm receipt;
parallel reads and complete residency verification still run.

`--warm report` performs Linux `mincore` residency
measurement without loading missing pages. `--warm lock` additionally uses
`mlock` and fails if the worker lacks sufficient RAM or memory-lock allowance.

Readiness preserves before/after page counts and the requested mode.
Prefetch/report observations do not promise pages will remain resident under
later memory pressure. Only successful full-index locking reports a residency
guarantee for the session's lifetime. No mode reduces the databases, skips an
index or changes the search parameters. Native API settings and index identity
remain bound to the provenance namespace.

## Shared-worker observation and controls

`session_client.py worker-status --root ...` returns bounded JSON without
starting compute or sending a worker command. It distinguishes absent,
starting, warming, ready, busy, idle, closing, failed and uncertain workers.
The exact session, unit invocation, immutable intent hash and launch hash form
the control target. New workers publish a heartbeat and an actual idle deadline;
startup and older frozen workers never receive an invented idle countdown.
Stale heartbeats disable controls. Startup snapshots and the last bounded
progress lines from the registered job provide stage details and measured
index-load progress. The waiting caller forwards those events to its private
`BIO_WORKER_PROGRESS_LOG`; RF3's preparation-cache identity is unchanged.

`worker-control --command-id <32-hex-id> --action extend|shutdown` requires all
four target fields (`--session-id`, `--invocation-id`, `--intent-sha256`,
`--launch-sha256`). The head journals the exact command before contacting the
worker. The worker changes its lease and saves the command receipt in one
atomic, locked ledger update on persistent shared storage. Exact retries return
that same receipt, including after a lost reply or worker removal; reuse with a
different payload fails. Known stale, unsupported or exhausted targets return
a durable rejected receipt. Transport uncertainty requires retrying that exact
command, never inventing a new ID.

Extend adds 900 seconds to an idle lease. While searches are active or queued,
it grants 900 seconds of credit to the next idle period. The original absolute
worker lifetime and its cleanup reserve always prevail; the button is disabled
when a full additional 15 minutes cannot currently fit. A search that consumes
the remaining lifetime can shorten later usable idle credit. This operation
does not renew the cloud reservation or change the two-hour default lifetime.

Graceful shutdown stops admitting new requests under the same lock used by
submission. Previously accepted current and queued searches drain normally,
then the enclosing managed worker cleanup runs. An idle worker closes at once.
During a drain the shutdown epoch is unknown until accepted work finishes; the
hard lifetime remains visible and enforced. Shared GUI controls are unavailable
for borrowed APIs and older frozen sessions. The existing explicit manual
`session stop` command retains its original operator semantics.

## Existing-worker operational adoption

The separately labeled `adopt` path supports an explicit transition on an
already managed worker. A new spool observes the exact original API PID,
start ticks, boot, native command and configuration/provenance hashes. It
validates the full database receipts. It owns neither the borrowed API nor
the original worker cleanup and never stops that API when cancelled.

For the A3 transition, requests remain durably queued until the original
42-target panel has exited successfully with its exact complete receipt and
the original audit-proxy port is free. The new spool's own earlier deadline
and original worker deadline both remain enforced. Head registration binds
the original managed head unit and the separately supervised worker spool
invocation; stopping the registration targets only the latter. Adoption itself changes no defaults and allocates no resources.

If the original owner has already removed the borrowed worker, a failed SSH
observation clears registration only after fresh managed-provider evidence proves
the exact worker and OS disk are permanently absent. An unreachable live worker
or uncertain cleanup keeps registration intact.

Adoption is an operator workflow requiring explicit ready, owner-unit,
spool-unit, source and known-host bindings; ordinary preparation uses
`bio-msa prepare`. On-demand startup and explicit `session start` both use
independent managed sessions; adoption never transfers ownership of its original
worker to an ordinary waiting preparation.

## Validation

The local session suites exercise actual file page-residency calls and owned
HTTP subprocesses as well as request generation, expiry, source identity,
uncertain launch and provider-cleanup refusal. The borrowed API cancellation
test verifies that its actual server remains responsive after spool shutdown.
Local fixtures do not qualify a production database or establish prediction
quality. Actual A3 adoption/preparation evidence is retained separately under
`bio-runs/architecture-session-20260906/msa-adopt-v1`.

The on-demand suites use isolated files and fake units/providers only. They cover
concurrent callers, a real multiprocess barrier during registration publication,
lost replies, generation replacement refusal, exact cleanup proofs, cancellation,
request deadlines, malformed input before allocation and bounded progress logs.
They do not start cloud instances or invoke native model/search execution.
