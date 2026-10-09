# Working in this repository

Preserve unrelated user changes, including staged changes. Read the existing
implementation before editing. Test changed behavior with offline fixtures;
never use a cloud account as an automatic test target.

## Verda is retired

On 2026-10-09 the user authorized decommissioning Verda and moving to AWS.
Never recreate Verda workers, disks, exports, or polling/dispatch timers. Keep
the retired head's dispatchers masked until an AWS replacement has a reviewed
bounded lifecycle. Changing the MSA provider does not migrate GPU launchers.
Preserve scientific data, custom code, and historical accounting before any
destructive cleanup. Use verified migration receipts; do not treat a partial
AWS database transfer as a completed copy. Future paid AWS work remains subject
to the existing budget, not an inferred unlimited migration allowance.

## AWS: no unattended polling

The user explicitly prohibited recurring AWS requests on 2026-10-09. The old
`bio-aws-msa-watchdog` performed cloud reads every minute while compute was
stopped. Other status/capacity paths could also reach AWS every few seconds.
The cause of AWS's account restrictions remains unconfirmed.

- Keep both `bio-aws-msa-watchdog.timer` and `.service` declaratively masked.
  Do not unmask them, create replacement timers/cron jobs, or put an explicit
  inspection command into a loop. This survives quota restoration and reboots.
- Ordinary GUI/CLI status and capacity reads must be local-only, even when their
  caches are empty or expired. Return stale/unknown with the observation time.
  UI refresh timers must never create AWS traffic, even during an authorized job.
- Real AWS calls are denied by default. An authorized operation needs an explicit
  purpose, short expiry, allowed operations, and hard request-count limit.
  Credentials and an inherited environment flag are not authorization scopes.
  Set CLI/SDK attempts to one; bound pagination and lifecycle checks explicitly.
- Record each permitted call's time, operation, and outcome without credential
  values or complete request/response payloads. Stop the operation on denied or
  invalid credentials, account restrictions, or quota errors; report the blocker
  instead of repeatedly retrying it.
- Keep local budget accrual, resource identities, deadlines, and retained disks
  intact. Do not fabricate a fresh AWS budget heartbeat from cached inventory.
  New jobs must have a reviewed bounded lifecycle and an independent shutdown
  mechanism before launch. Disabling polling must not silently remove safeguards.
- Review frozen helper copies and existing services during deployment; changing
  current source alone does not update previously frozen launchers. Verify idle
  and ordinary status paths make zero AWS requests after deployment.
- Make only explicitly scoped compute changes. The existing combined project
  ceiling is $1,500 gross unless the user explicitly changes it. Credits and quota
  increases do not change the ceiling. Preserve unrelated infrastructure.
- Do not print AWS keys, tokens, credential files, or unredacted session logs.
  No root AWS credentials or unrelated IAM/support/account changes.
- For migration use the dedicated, limited `gc-migration` SSO profile, not the
  legacy `bio-aws` static credentials. The SSO identity must have no administrator
  assignments that a stolen cached SSO token could assume. Provisioning is
  separate from the inspection/fixed-worker operator role; never broaden that
  role to AdministratorAccess, EC2FullAccess, IAM writes, PassRole, or SSM commands.

See [the AWS operations runbook](docs/aws-operations.md) for the recovery sequence
and the current implementation's limitations. User instructions in the session
take precedence; do not infer an exception to the no-polling rule.
