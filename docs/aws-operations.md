# AWS operations without unattended polling

## Standing constraint

On 2026-10-09 the user prohibited the recurring AWS watchdog and equivalent
background cloud polling. The account had been restricted repeatedly; causation
is not established. This policy reduces unnecessary traffic and makes authorized
operations auditable; it cannot guarantee AWS security outcomes.

The prior watchdog began on 2026-09-13 at 23:58:04 EDT. A successful cycle called
STS GetCallerIdentity, EC2 DescribeInstances, and EC2 DescribeVolumes. It kept
running while compute was stopped. Ordinary capacity/status refreshes were also
potential cloud callers. Both timer and service must remain masked.

## Controlled recovery sequence

1. Read local records and disable autonomous AWS callers before account checks.
   Inspect systemd locally over SSH; do not run the old watchdog to test it.
2. Complete offline regression tests and deploy the default-deny controls.
   Verify repeated idle/UI status and capacity reads create no AWS subprocess.
3. Perform one explicitly announced read-only inspection with a short deadline,
   small request cap, and one attempt per API call. Verify the IAM identity,
   exact region, relevant quota, and retained resource state. A failed identity
   check ends the inspection; do not continue with other API calls.
4. Check the existing combined gross-spend ceiling and outstanding commitments
   from the local ledger. Do not reset historic costs, discard reservations, or
   treat promotional credits as a budget increase.
5. Choose one bounded workload and instance. Review GPU memory per device,
   existing runtime compatibility, cost, maximum duration, and how it stops even
   if the controller disappears. Large BindCraft jobs are not ordinary folding:
   the prior full-target run requested a 346.49 GiB GPU allocation on an 80 GB
   H100. Repeating it unchanged on another 80 GB GPU is not a valid smoke test.
6. Use cached/head or pinned-worker telemetry for progress. Cloud lifecycle
   transitions may use only the operation's bounded request allowance. Do not
   introduce a permanent timer to make the old heartbeat requirement pass.
7. Confirm completion/stop once, retain the audit and scientific results, and
   end the operation. Leave no recurring AWS process behind.

## Deployment and accounting constraints

- NixOS owns the unit definitions; mask them declaratively so rebuilds and
  reboots cannot restore the original timer. Temporary `/run/systemd/system`
  guards only bridge the interval before deployment.
- Frozen session tools may contain the old implementation. Do not resume a
  frozen launcher until its actual executable and authorization boundaries
  have been reviewed. Do not alter immutable scientific provenance to hide a
  migration; record a new execution plan instead.
- Existing AWS job admission depends on a recent verified budget heartbeat.
  With the permanent watchdog removed, admission must remain fail-closed until
  a bounded job owner supplies real lifecycle observations and cleanup.
  Merely refreshing the heartbeat from an old cache is prohibited.
- AWS preparation and folding migration are distinct. Current AWS integration
  covers the retained CPU MSA worker; migrating GPU folding requires a reviewed
  worker lifecycle and runtime/storage setup.
- Never restart AWS automation to obtain an ETA, test credentials, populate a
  dashboard, or investigate an account restriction. Use one scoped inspection.

## Verification

The current `bio-aws-msa inspect` command is manual and read-only, limited to
five calls in 60 seconds. It disables retries and automatic pagination and
revokes its in-memory authorization after failure or completion. Ordinary
`capacity` and `watchdog` calls return offline; lifecycle commands fail closed.
Neither a persisted receipt nor an environment variable grants AWS access.
The manual client selects the restricted `gc-migration` profile. Follow
[the migration access setup](../machines/head/aws-migration/README.md) before
inspection; the previous `bio-aws` keys must not be used as a fallback.

Verda was also retired by the user on 2026-10-09. Its dispatchers, polling
services, and provisioning entrypoints must remain disabled. The old head's
data must be restored onto a replacement before its console can serve jobs;
restoring data must not reactivate retained queues or frozen cloud launchers.

Use fake APIs/subprocess spies for tests of default denial, expiry, exhausted
call allowances, failures, and repeated empty/stale-cache status reads. Runtime
verification must read local service and audit metadata and must not accidentally
invoke cloud helpers. Logs contain operation names, timestamps and sanitized
outcome codes; credential values and full payloads are excluded.
