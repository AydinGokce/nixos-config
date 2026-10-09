# Temporary access for the AWS migration

Use a dedicated IAM Identity Center identity, permission set, and local profile.
Do not create a root key, attach administrator access, or copy AWS credentials
onto the transfer worker. Policy generation is offline. The separate inspection
command below makes only its explicitly requested, bounded reads; it does not
enable the disabled launchers or polling services.

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
aws configure sso --profile gc-migration
aws sso login --profile gc-migration
```

Use the organization's actual SSO start URL and Identity Center Region. That
Region may differ from the EC2 Region. Select account `147997164104`, role
`GCMigrationInspect`, default CLI Region `us-east-1`, and output `json`.
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

## 2. Provision through the human-controlled console

After inspection, an administrator provisions the approved transfer worker and
handles any required EBS enlargement or attachment outside this role. The intended
worker is **c7i.xlarge (4 vCPU, 8 GiB)** in `us-east-1`, in the existing data
volume's Availability Zone, with the approved existing security group and SSH
key. Confirm the actual instance ID and storage attachments before granting
operator access. This bundle neither assumes provisioning is complete nor grants
permission to perform it.

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

An administrator may also install the **same policy as a permissions boundary**
on the corresponding role (or configure the matching customer-managed boundary
in the permission set). A boundary limits the permissions identity policies may
grant; **it grants nothing by itself**. Keep the inline permission policy as well.
The administrator must manage that boundary; the migration role receives no IAM
permissions. [AWS explains permissions boundaries](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html).

## Offline validation

```sh
cd machines/head/aws-migration
python3 -m unittest test_policy test_inspection
```

These checks enforce the action allowlist, Region/account/instance restrictions,
type restriction for starts, and safe local generation. They do not contact AWS
or replace AWS policy validation. The action/resource model is documented in the
[EC2 service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html).
