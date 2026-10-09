# Temporary access for the AWS migration

Use separate IAM Identity Center identities for routine access and provisioning.
Do not create a root key, attach administrator access, or copy AWS credentials
onto the transfer worker. Policy generation is offline. The separate inspection
command below makes only its explicitly requested, bounded reads; it does not
enable the disabled launchers or polling services.

Create two users, `gc-migration` and `gc-provision`, each using an email address
or alias you control. Accept their invitations and register MFA. Neither user
belongs to administrator groups. The routine user's assignment below is ready
now; leave the provisioning user unassigned until its resource-specific policy
has been generated after inspection. A human administrator assigns these limited
policies; the agent does not receive that administrator's credentials.

If Identity Center is not enabled, use an **organization instance** with
multi-account permissions enabled, even when assigning only this one account.
Reuse an existing organization instance if present. For a new deployment, a
single-Region instance avoids creating optional replication/KMS infrastructure.
Enabling an organization instance on a standalone account creates an AWS
organization; review the console's account-plan notice before enabling it.
[AWS's enablement instructions](https://docs.aws.amazon.com/singlesignon/latest/userguide/enable-identity-center.html)
explain the account and organization options. For the built-in Identity Center
directory, MFA enrollment is configured under **Settings → Authentication →
Multi-factor authentication**; require device registration at sign-in. If an
external identity provider is already in use, manage the users and MFA there.
[AWS's MFA instructions](https://docs.aws.amazon.com/singlesignon/latest/userguide/how-to-configure-mfa-device-enforcement.html)

## 1. Start with inspection only

An AWS administrator should create a **custom** permission set named
`GCMigrationInspect` in IAM Identity Center, set its AWS account session duration
to **1 hour**, and use [inspection-policy.json](inspection-policy.json) as its
only inline policy. Attach no other permission policies. Assign a dedicated
migration user to account `147997164104` with this permission set, with **no
administrator or other privileged account/permission-set assignments**. A stolen
cached SSO token can obtain roles assigned to its identity; choosing a limited
CLI profile does not restrict a broadly privileged SSO identity. Keep the human
administrator's provisioning login separate and do not cache it in this agent's
environment. Require MFA through the
Identity Center identity source/IdP; do not assume an IAM MFA condition represents
federated MFA.

On the local workstation, the user runs:

```sh
aws configure sso --profile gc-migration --no-browser
```

Use SSO session name `gc-migration`, the organization's actual SSO start URL, and Identity Center Region. That
Region may differ from the EC2 Region. Select account `147997164104`, role
`GCMigrationInspect`, default CLI Region `us-east-1`, and output `json`.
Open the printed authorization link in a private browser window on this same
computer and sign in as `gc-migration`, not as the administrator. Configuration
includes an SSO login; when it expires, log in again using
`aws sso login --profile gc-migration --no-browser`.
Credentials stay in the local CLI cache; do not paste them into chat or upload
the SSO cache. [AWS documents this SSO configuration flow](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).

Inspection allows session identity and eleven named EC2/quota metadata reads.
EC2 and quota reads require `us-east-1`; identity has no Region restriction.
`Resource: "*"` is required for these inventory APIs, so their results can include
other resources in that Region. There is no `Describe*` action wildcard and no
permission for console output, instance user data, snapshots, secrets, IAM,
Systems Manager, or resource writes.

Use this profile only for an explicitly reviewed, bounded inspection. SSO login
does not authorize unattended polling. A one-hour role session is not a shutdown
timer: the CLI can obtain replacement role credentials while its SSO session is
still valid. The existing combined **$1,500 gross** ceiling remains in force.

After the dedicated SSO login, run one inspection explicitly from the repository:

```sh
python3 machines/head/aws-migration/inspect_migration.py \
  --output-dir "$HOME/gc-migration-inspection-$(date -u +%Y%m%dT%H%M%SZ)"
```

This creates a new `0700` directory with a selected-metadata `inspection.json`
report and an operation-only `actions.jsonl` audit, both `0600`. It refuses to
reuse any existing output path. It uses only profile `gc-migration`, verifies
the expected account and `GCMigrationInspect`/`GCMigrationOperator` assumed SSO
role first, and refuses root, IAM-user, administrator, and other role sessions.
The role name is a scope check, not a substitute for the administrator configuring
the permission set as described above.

At most five single-page service operations run within 60 seconds: identity, instances,
volumes, Standard vCPU quota, and the `c7i.xlarge` offering in `us-east-1a`.
The CLI can also make the necessary SSO credential-exchange/refresh requests;
the five-operation counter is not a packet-level count of authentication traffic.
The process deadline still bounds the inspection, and nothing refreshes in the
background after it exits.
Failures or pagination stop the inspection; there are no retries or background
checks. An offering listing does not establish live capacity. Tags, user data,
raw responses, session identifiers, and credential values are omitted. This
command does not start the retained legacy CPU instance or any other resource.

## 2. Use the separate provisioning identity

The user authorized the agent to create migration resources through a separate
limited identity. After inspection, generate a provisioning policy for the
reviewed AMI, subnet, security group, SSH key, existing data volume, encryption key,
Availability Zone and an absolute expiry. An administrator creates the custom
`GCMigrationProvision` permission set with that policy, a one-hour role session,
and no broad managed policies, and assigns it only to the **different**
`gc-provision` user. Keep it unassigned until the generated policy is ready.

Configure `aws configure sso --profile gc-provision --no-browser` with SSO session
name `gc-provision`; open the authorization link in a separate private browser
session and sign in as that user. Use the same portal, Identity Center Region,
account and default EC2 Region, selecting `GCMigrationProvision`. Two profile or
session names for the same SSO user would not isolate its role entitlements.

The provisioning policy permits only the reviewed migration resources and
limited EC2-mediated encryption-key use. It does not grant IAM administration,
PassRole, Systems Manager commands, disk deletion, or unrestricted EC2 access.
Launching a VM does permit guest code execution through user data and access to
the disks attached to it. Revoke or let the provisioning write grant expire after
bootstrap, and use the fixed-worker role for the copy. Do not treat the lack of
SSM or snapshot permissions as protection against reading attached files.
IAM restrictions on type, size and tags do **not** cap the number of
resources or total spend. A bounded execution plan and budget still apply.
Do not reuse this migration policy for future GPU/production provisioning; review
the required resources for that separate task.

[build_provisioning_policy.py](build_provisioning_policy.py) generates the policy
offline. It requires a complete inspection from within the past hour, pins
account `147997164104` and Region `us-east-1`, and accepts these explicit inputs:

```text
--inspection-report --ami-id --subnet-id --security-group-id --key-name
--retained-volume-id --availability-zone --kms-key-arn --run-id
--expires-at --output
```

AMI, SSH key and encryption-key identity need separate review; the initial five
inspection calls do not verify all three. Use an approved existing KMS key,
including the AWS-managed EBS key if appropriate; this role cannot create keys.
All mutating permissions have an absolute UTC expiry within 24 hours of policy
generation. New instances, interfaces and disks must carry exactly
`GCMigration=<run-id>` and `Project=gc-migration` tags. Launch-created disks must
be encrypted 20 GiB gp3; the separate blank data disk must be encrypted 3,072 GiB
gp3, both capped at 3,000 IOPS and 125 MiB/s. Only this run's tagged worker may
receive the new disks or the exact approved retained database disk.

The generated policy is intended as an Identity Center **inline policy**. It is
larger than the limit for one customer-managed IAM policy, so do not use it
unchanged as a managed permissions boundary. No live IAM validation or AWS
provisioning is performed by this generator. The checks use the official
[EC2 action/resource matrix](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html),
[tagging authorization rules](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html),
and [EBS encryption requirements](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-encryption-requirements.html).

The intended
worker is **c7i.xlarge (4 vCPU, 8 GiB)** in `us-east-1`, in the existing data
volume's Availability Zone, with the approved existing security group and SSH
key. Confirm the actual instance ID and storage attachments before granting
routine operator access. This bundle does not assume provisioning is complete.
Operations absent from the generated policy, such as detaching an old volume,
remain deliberate administrator actions; do not broaden the routine identity.

Give the copy worker **no IAM instance profile**. SSH file transfer requires no
AWS API credentials. Stock Ubuntu/Amazon Linux images retrieve the launch SSH
key through IMDS during bootstrap, so require IMDSv2 and retain metadata access
until SSH is configured. Then metadata access can be disabled separately if the
runtime does not need it. Disabling IMDS at launch can prevent SSH key injection.
[AWS describes the bootstrap dependency](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/building-shared-amis.html).

Verify the retained data volume will survive worker shutdown/termination, and
preserve backups. Restricting AWS permissions does not make a mounted filesystem
read-only: a user with OS write access can still alter its data.

## 3. Generate access to the one confirmed instance

After the real instance ID is known, generate the operator policy locally:

```sh
python3 machines/head/aws-migration/build_operator_policy.py \
  --instance-id i-REPLACE_WITH_THE_REAL_17_HEX_ID \
  --output /tmp/gc-migration-operator-policy.json
```

The placeholder is deliberately rejected. The generator accepts one exact ID,
pins account `147997164104` and Region `us-east-1`, and refuses to overwrite an
existing output file. It adds only:

- `StartInstances` for that exact instance, while its type is `c7i.xlarge`.
- `StopInstances` for that exact instance, even if an administrator changes its
  type, so cleanup is not blocked by type drift.

An administrator can create a separate `GCMigrationOperator` custom permission
set with that generated policy only, a one-hour session, and the same user/account
assignment. Reconfigure `gc-migration` to select that role when operator access is
actually required. Do not stack administrator or EC2-full-access policies onto it.

The operator policy cannot launch, terminate, modify, attach, detach, or delete
resources; it has no `PassRole` or SSM command permission. The retained CPU worker
and all other instances remain outside its start/stop scope. There is no EC2
access on the file-copy worker itself. Start/stop permission is not a call-count,
duration, or spending limit: use a reviewed finite job and independent shutdown
mechanism before starting it.

An administrator may also install the **same fixed-worker operator policy as a permissions boundary**
on the corresponding role (or configure the matching customer-managed boundary
in the permission set). A boundary limits the permissions identity policies may
grant; **it grants nothing by itself**. Keep the inline permission policy as well.
The administrator must manage that boundary; the migration role receives no IAM
permissions. [AWS explains permissions boundaries](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html).

## Offline validation

```sh
cd machines/head/aws-migration
python3 -m unittest test_policy test_inspection test_provisioning_policy
```

These checks enforce the action allowlist, Region/account/instance restrictions,
type restriction for starts, and safe local generation. They do not contact AWS
or replace AWS policy validation. The action/resource model is documented in the
[EC2 service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html).

## If credentials are exposed

Use the separate administrator login to disable the affected identity/access,
revoke its sessions, and investigate the recorded operations. Deleting a local
SSO cache or enabling MFA afterwards does not neutralize an already stolen role
session. AWS documents [disabling temporary-session permissions](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_disable-perms.html).
Deactivate the old `gc-msa` long-lived keys in the console after replacement
access is confirmed; do not keep them as a fallback. A leaked migration session
still creates an availability risk for the one start/stop-enabled worker, but
these policies grant no fleet provisioning, disk deletion, snapshot export,
IAM modification, or remote command execution. Limited access reduces the
damage possible from a leak; it does not guarantee AWS will never review or
restrict the account.
