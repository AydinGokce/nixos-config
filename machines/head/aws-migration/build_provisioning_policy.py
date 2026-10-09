#!/usr/bin/env python3
"""Generate an expiring migration provisioning policy locally; never calls AWS.

Fresh inventory binds the retained disk and network. The AMI, SSH key name and
KMS key ARN require separate operator review; this is not a live IAM simulator.
IAM limits each resource, not resource count or total spend. A bounded launcher
and independent guest shutdown remain required before any paid operation.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys

from build_operator_policy import ACCOUNT, INSPECTION_POLICY, INSTANCE_TYPE, REGION


def utc(value, label):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        raise ValueError(f"{label} must be an ISO timestamp in UTC")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{label} is not a valid UTC timestamp") from None


def exact(value, pattern, label):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid exact {label}")
    return value


def arn(kind, value, *, account=ACCOUNT):
    return f"arn:aws:ec2:{REGION}:{account}:{kind}/{value}"


def build_policy(inspection, *, ami_id, subnet_id, security_group_id, key_name,
                 retained_volume_id, availability_zone, kms_key_arn, run_id,
                 expires_at, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("Current time must be UTC")
    expiry = utc(expires_at, "Expiry")
    if not now < expiry <= now + timedelta(hours=24):
        raise ValueError("Expiry must be in the future and no more than 24 hours away")
    expiry_text = expiry.isoformat().replace("+00:00", "Z")
    exact(ami_id, r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})", "AMI ID")
    exact(subnet_id, r"subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})", "subnet ID")
    exact(security_group_id, r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})", "security group ID")
    exact(retained_volume_id, r"vol-(?:[0-9a-f]{8}|[0-9a-f]{17})", "retained volume ID")
    exact(key_name, r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,254}", "SSH key name")
    exact(availability_zone, r"us-east-1[a-z]", "availability zone")
    exact(run_id, r"[a-z0-9][a-z0-9-]{7,63}", "GCMigration run tag")
    exact(kms_key_arn, rf"arn:aws:kms:{REGION}:{ACCOUNT}:key/[0-9a-f]{{8}}-"
          r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "KMS key ARN")

    if (not isinstance(inspection, dict) or inspection.get("schema") != 1
            or inspection.get("status") != "complete"
            or inspection.get("account_id") != ACCOUNT or inspection.get("region") != REGION):
        raise ValueError("A completed inspection for the approved account and region is required")
    age = now - utc(inspection.get("completed_at"), "Inspection completion")
    if not timedelta(0) <= age <= timedelta(hours=1):
        raise ValueError("Inspection must have completed within the past hour")
    rows = inspection.get("instances")
    volumes = inspection.get("volumes")
    if not isinstance(rows, list) or not isinstance(volumes, list):
        raise ValueError("Inspection must contain instance and volume metadata")
    matching_network = [row for row in rows if isinstance(row, dict)
                        and row.get("availability_zone") == availability_zone
                        and row.get("subnet_id") == subnet_id
                        and security_group_id in row.get("security_group_ids", [])]
    if not matching_network:
        raise ValueError("The exact subnet, security group and AZ must match fresh inventory")
    # The inspection intentionally redacts long arbitrary key labels. An explicit
    # reviewed name is still mandatory; never substitute a stale/default name.
    observed_keys = {row.get("key_name") for row in matching_network}
    if key_name not in observed_keys and "<redacted>" not in observed_keys:
        raise ValueError("SSH key name does not match the inspected network's instances")
    matching_volumes = [row for row in volumes if isinstance(row, dict)
                        and row.get("volume_id") == retained_volume_id]
    if len(matching_volumes) != 1:
        raise ValueError("The exact retained volume must occur once in fresh inventory")
    retained = matching_volumes[0]
    if (retained.get("availability_zone") != availability_zone
            or retained.get("size_gib") != 1300 or retained.get("encrypted") is not False
            or retained.get("volume_type") != "gp3"
            or retained.get("state") not in {"available", "in-use"}):
        raise ValueError("Retained volume must be the inspected unencrypted 1300 GiB gp3 disk in the selected AZ")

    policy = json.loads(INSPECTION_POLICY.read_text())
    tags = {"GCMigration": run_id, "Project": "gc-migration"}
    request_tags = {f"aws:RequestTag/{key}": value for key, value in tags.items()}
    resource_tags = {f"aws:ResourceTag/{key}": value for key, value in tags.items()}
    instance_resource = arn("instance", "*")
    volume_resource = arn("volume", "*")
    interface_resource = arn("network-interface", "*")

    def allow(sid, action, resource, *, equals=None, **conditions):
        conditions["StringEquals"] = {"aws:RequestedRegion": REGION, **(equals or {})}
        conditions["DateLessThan"] = {"aws:CurrentTime": expiry_text}
        policy["Statement"].append({"Sid": sid, "Effect": "Allow", "Action": action,
                                    "Resource": resource, "Condition": conditions})

    allow("LaunchOnlyReviewedInputs", "ec2:RunInstances", [
        arn("image", ami_id, account=""), arn("subnet", subnet_id),
        arn("security-group", security_group_id), arn("key-pair", key_name),
    ])
    allow("LaunchTaggedTransferHost", "ec2:RunInstances", instance_resource,
          equals={**request_tags, "ec2:InstanceType": INSTANCE_TYPE,
                  "ec2:AvailabilityZone": availability_zone, "ec2:Tenancy": "default",
                  "ec2:InstanceMarketType": "on-demand", "ec2:MetadataHttpTokens": "required",
                  "ec2:MetadataHttpEndpoint": "enabled"},
          Null={"ec2:InstanceProfile": "true"},
          **{"ForAllValues:StringEquals": {"aws:TagKeys": list(tags)}})
    allow("LaunchTaggedInterfaceInReviewedSubnet", "ec2:RunInstances", interface_resource,
          equals={**request_tags, "ec2:AvailabilityZone": availability_zone},
          ArnEquals={"ec2:Subnet": arn("subnet", subnet_id)},
          **{"ForAllValues:StringEquals": {"aws:TagKeys": list(tags)}})
    # IAM cannot distinguish boot versus data by device index here. Every disk
    # created by RunInstances is capped at 20 GiB; the large disk is separate.
    allow("LaunchSmallEncryptedBootVolumes", "ec2:RunInstances", volume_resource,
          equals={**request_tags, "ec2:AvailabilityZone": availability_zone, "ec2:VolumeType": "gp3"},
          Bool={"ec2:Encrypted": "true"}, NumericEquals={"ec2:VolumeSize": 20},
          NumericLessThanEquals={"ec2:VolumeIops": 3000, "ec2:VolumeThroughput": 125},
          **{"ForAllValues:StringEquals": {"aws:TagKeys": list(tags)}})
    allow("CreateBlankEncryptedMigrationData", "ec2:CreateVolume", volume_resource,
          equals={**request_tags, "ec2:AvailabilityZone": availability_zone,
                  "ec2:VolumeType": "gp3", "ec2:KmsKeyId": kms_key_arn},
          Bool={"ec2:Encrypted": "true"}, Null={"ec2:ParentSnapshot": "true"},
          NumericEquals={"ec2:VolumeSize": 3072},
          NumericLessThanEquals={"ec2:VolumeIops": 3000, "ec2:VolumeThroughput": 125},
          **{"ForAllValues:StringEquals": {"aws:TagKeys": list(tags)}})
    allow("TagOnlyAtCreation", "ec2:CreateTags",
          [instance_resource, volume_resource, interface_resource],
          equals={**request_tags, "ec2:CreateAction": ["RunInstances", "CreateVolume"]},
          **{"ForAllValues:StringEquals": {"aws:TagKeys": list(tags)}})
    allow("AttachOnlyToMigrationHosts", "ec2:AttachVolume", instance_resource,
          equals={**resource_tags, "ec2:InstanceType": INSTANCE_TYPE,
                  "ec2:AvailabilityZone": availability_zone})
    allow("AttachOwnEncryptedVolumes", "ec2:AttachVolume", volume_resource,
          equals={**resource_tags, "ec2:AvailabilityZone": availability_zone, "ec2:VolumeType": "gp3"},
          Bool={"ec2:Encrypted": "true"})
    allow("AttachOnlyApprovedRetainedDatabase", "ec2:AttachVolume", arn("volume", retained_volume_id),
          equals={"ec2:AvailabilityZone": availability_zone, "ec2:VolumeType": "gp3"},
          Bool={"ec2:Encrypted": "false"}, NumericEquals={"ec2:VolumeSize": 1300})
    # Stop is available after type drift, but expires with the provisioning grant.
    # A separate exact-instance operator grant/guest timer handles later cleanup.
    allow("StopOnlyMigrationHosts", "ec2:StopInstances", instance_resource,
          equals={**resource_tags, "ec2:AvailabilityZone": availability_zone})
    kms_conditions = {"kms:ViaService": f"ec2.{REGION}.amazonaws.com", "kms:CallerAccount": ACCOUNT}
    allow("UseReviewedKeyOnlyThroughEC2", [
        "kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKeyWithoutPlaintext",
        "kms:ReEncryptFrom", "kms:ReEncryptTo",
    ], kms_key_arn, equals=kms_conditions)
    allow("AllowEC2GrantForReviewedKey", "kms:CreateGrant", kms_key_arn,
          equals=kms_conditions, Bool={"kms:GrantIsForAWSResource": "true"})
    return policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspection-report", type=Path, required=True)
    for field in ("ami-id", "subnet-id", "security-group-id", "key-name", "retained-volume-id",
                  "availability-zone", "kms-key-arn", "run-id", "expires-at"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New policy file; refuses to overwrite")
    args = parser.parse_args(argv)
    values = vars(args).copy()
    source = values.pop("inspection_report")
    output = values.pop("output")
    try:
        policy = build_policy(json.loads(source.read_text()), **values)
        with output.open("x") as stream:
            json.dump(policy, stream, indent=2)
            stream.write("\n")
    except (ValueError, OSError, TypeError) as error:
        # Errors name fields, never echo the inspection or credential-like values.
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
