# Managed private MSA sessions

`bio-msa session start --timeout 14400` requests one managed search worker using
the existing worker selector, fresh quote, budget reservation and cleanup
wrapper. Its API remains alive across preparation requests. `bio-msa prepare
--model protenix --fasta query.fasta --bundle-result prepared.json` submits a
durable request to that session; it does not rent another worker. Public MSA
remains the folding default unless private preparation is explicitly selected.

`bio-msa session status` checks the original unit invocation, current managed
provider identity, pinned SSH host key, boot and session generation before
reporting readiness. `bio-msa session stop` stops the original managed
submission and clears its registration only after fresh inventory confirms the
exact worker and temporary OS are absent. Natural idle/deadline shutdown can
be reconciled with the same stop command, including a collected systemd unit.
Uncertain starts, replaced units and unresolved cleanup retain their records
and prevent automatic repeat allocation.

The default idle timeout is 900 seconds after requests finish. The maximum
lifetime and every request's absolute deadline are bounded by the managed
worker deadline. Queued time counts toward a request timeout. Failed or
uncertain requests retain their IDs, inputs, logs and partial outputs; the
client never silently re-submits them. Full database storage and completed
prepared bundles persist independently of compute.

The official pinned CPU API runs on worker loopback. Requests travel through
pinned SSH and a worker-local spool, with one preparation at a time and the
existing per-target audit proxy. Successful output includes raw API request and
response bytes, downloaded result archives, selected template payloads,
backend job scripts and full database/source provenance. The standard model
preparers and RF3's per-chain TaxID search adapter are reused unchanged.

## Index residency

The three full CPU indexes occupy about 700 GB. `--warm prefetch` is the default
and reads all existing index pages, with a memory-headroom preflight and a
bounded warm-up timeout. `--warm report` performs Linux `mincore` residency
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
invocation; stopping the registration targets only the latter. No public
default is changed and no new allocation occurs.

If the original owner has already removed the borrowed worker, a failed SSH
observation clears registration only after fresh managed-provider evidence proves
the exact worker and OS disk are permanently absent. An unreachable live worker
or uncertain cleanup keeps registration intact.

Adoption is an operator workflow requiring explicit ready, owner-unit,
spool-unit, source and known-host bindings; ordinary preparation uses
`bio-msa prepare`. Production independent sessions use `session start`.

## Validation

The local session suites exercise actual file page-residency calls and owned
HTTP subprocesses as well as request generation, expiry, source identity,
uncertain launch and provider-cleanup refusal. The borrowed API cancellation
test verifies that its actual server remains responsive after spool shutdown.
Local fixtures do not qualify a production database or establish prediction
quality. Actual A3 adoption/preparation evidence is retained separately under
`bio-runs/architecture-session-20260906/msa-adopt-v1`.
