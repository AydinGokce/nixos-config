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
fails. Later requests may retire an already-ended generation only after fresh
provider evidence proves the exact worker and temporary OS disk are absent.
Replaced units, missing worker identity, unknown startup outcomes and uncertain
cleanup keep registration intact for inspection. An old closure receipt alone
never authorizes replacement. No search request is silently replayed.

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
`--warm report` performs Linux `mincore` residency
measurement without loading missing pages. `--warm lock` additionally uses
`mlock` and fails if the worker lacks sufficient RAM or memory-lock allowance.

Readiness preserves before/after page counts and the requested mode.
Prefetch/report observations do not promise pages will remain resident under
later memory pressure. Only successful full-index locking reports a residency
guarantee for the session's lifetime. No mode reduces the databases, skips an
index or changes the search parameters. Native API settings and index identity
remain bound to the provenance namespace.

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
