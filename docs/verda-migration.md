# Verda retirement and exact-data migration

The user authorized retiring Verda on 2026-10-09, then explicitly required
copying the full data onto AWS storage before deletion. Do not discard and
redownload the reference datasets instead. AWS access must use least privilege;
the old recurring AWS watchdog is prohibited.

## Current state

- Both providers' recurring timers and the head's dispatchers were stopped.
  Declarative masks and default-deny provider clients are in this repository.
- No Verda resources have been deleted. No AWS account requests or resource
  changes have been made during this retirement work yet.
- The latest independently verified library archive contains 287 immutable
  revisions and 1,422 files. A larger private local head backup is also underway;
  that archive is not complete or restore-verified until its receipt says so.
- The previous AWS MSA transfer is incomplete. Source hashing completed, but
  copying was only roughly 53% at the last retained observation. The separate
  21.61 GB runtime archive completed. Neither proves a complete MSA installation.
- Scientific results, project/construct revisions, custom source, environments,
  exact reference data, metadata and historical cost accounting must survive.

Private inventory and preservation receipts are under
`/home/aydin/bio-runs/verda-decommission-20261009/`. Retained AWS transfer evidence
is under `bio-runs/msa-128gb-loader-20260913/aws-backup/prepare04-retained/`.
These directories are not git artifacts; they may contain private configuration.

## Proposed destination

Retain the existing 1,300 GiB EBS MSA disk in `us-east-1a` and add a 3,072 GiB
encrypted gp3 disk for RFAA, the shared runtime/results filesystem, and head data.
The original MSA disk was created unencrypted and contains public reference data;
do not put private project data on it. Keep existing filesystem contents intact.
Format only the positively identified new disk after checking its volume ID and
that it has no filesystem. Attach both disks to one fixed `c7i.xlarge` transfer
host (4 vCPU, 8 GiB), subject to a fresh quota/resource inspection.

The host needs no GPU, IAM instance profile, AWS access keys, or SSO cache. Use
SSH with a dedicated migration key and restrict port 22 to the required source
addresses. Require IMDSv2 during SSH-key bootstrap. Keep AWS credentials from
the old head in the restricted local backup, outside the data copied to the
transfer worker; preserve other private configuration with appropriate access.
Set instance-initiated shutdown behavior to stop and install an absolute,
guest-local shutdown deadline before beginning the copy. No API polling timer.

At baseline gp3 performance, the two data disks total about **$350/month**,
excluding boot disks/taxes. Keeping the old MSA disk's elevated IOPS/throughput
adds roughly **$100/month** using the retained regional rates. These settings
are a separate deliberate console change. The copy host is budgeted at roughly
**$0.185/hour including IPv4**, pending a current regional quote.

Allow **16–30 hours** for about 3.5 TB of source data plus complete destination
verification, with an absolute **48-hour stop**. This is a planning range, not
a measured ETA. Project from actual source reads, SSH transfer bytes, disk writes
and verification progress; never wait out a stalled timer without diagnosis.
The c7i.xlarge sustained EBS baseline is 156.25 MB/s; advertised burst bandwidth
is not a sustained guarantee. See [AWS's EBS throughput table](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-optimized.html)
and [storage pricing](https://aws.amazon.com/ebs/pricing/).

The old combined $1,500 gross ceiling has already been exceeded by retained
infrastructure. A proposed **$50 AWS migration allowance** covers about two days
of the planned AWS resources, excluding Verda overlap/egress/taxes and ongoing
retention. This proposal does not itself change the user's ceiling. Preserve the
ledger and obtain a specific budget exception or updated ceiling before paid work.

## Permission phases

1. Configure the dedicated [inspection-only SSO identity](../machines/head/aws-migration/README.md).
   Its identity must have no administrator assignments. Do not fall back to the
   legacy static credentials. Make one bounded inspection, not a polling loop.
2. Review the exact existing disks, attachments, quota, network, AMI and quote.
   A human administrator performs one-time provisioning separately; the agent's
   migration role does not receive creation, resize, IAM or command-execution powers.
3. Generate the fixed-instance operator policy only after the actual ID is known.
   It permits only inspection and start/stop of that worker, not other instances.
4. Transfer and inspect progress over SSH. An expiring SSO session need not stop
   an already-running file transfer; the independent guest deadline still applies.

## Preservation and deletion gate

Record a source inventory including regular-file byte counts/hashes, symlink
targets, permissions, ownership and required hard-link relationships. Quiesce
writes for the final copy. Preserve all three shared exports plus head state,
including `/var/lib/bio-library`, `bio-workbench`, `bio-runs`, `bio-inference`,
`bio-md`, `bio-library-runtime` and `dc`. The inference-control mount is a subtree
of the shared export, not an additional independent dataset. The detached MSA
block cache must also be inventoried; do not assume it has no unique files.
Its retained receipt proves a complete historical copy of the ColabFold payload,
but its publication metadata and any later additions still need preservation.
Mount it separately read-only with journal replay disabled. Deduplicate against
the migrated dataset only after exact path/hash comparison; preserve all other
entries. This may also provide a faster local-disk source for the MSA transfer.

Use resumable transfers and independently verify destination contents. Sparse
apparent length, completed source hashes, accepted API calls and transfer exit
codes alone are not sufficient. Restore-test the library and check Workbench's
SQLite state and referenced artifacts. Capture source code modifications and
environment manifests. Do not activate restored launch queues or old credentials.

Only then delete the exact inventoried Verda instance and disks. Confirm active
and trash inventories no longer contain billable/recoverable target resources,
record deletion times, and reconcile the preserved ledger. Keep any unrelated
resources out of the deletion list. Revoke retired Verda credentials after cleanup.
The console's replacement head and GPU folding lifecycle are separate migration
steps; backing up the disks alone does not make the AWS folding cluster ready.
