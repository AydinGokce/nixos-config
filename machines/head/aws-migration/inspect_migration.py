#!/usr/bin/env python3
"""Explicit five-request migration inspection; no writes, retries or background work."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

_spec = importlib.util.spec_from_file_location(
    "migration_aws_provider", Path(__file__).resolve().parent.parent/"msa/aws_provider.py")
aws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aws)

ACCOUNT = "147997164104"
REGION = "us-east-1"
ZONE = "us-east-1a"
INSTANCE_TYPE = "c7i.xlarge"
QUOTA = "L-1216C47A"


class InspectionError(ValueError):
    """Only fixed error codes are emitted; request/response content stays private."""


def require(condition, code="invalid-metadata"):
    if not condition:
        raise InspectionError(code)


def identifier(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix+r"-[0-9a-f]{8,17}", value))
    return value


def choice(value, allowed):
    require(isinstance(value, str) and value in allowed)
    return value


def numeric(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    return value


def sequence(value):
    require(isinstance(value, list) and len(value) <= 1000)
    require(all(isinstance(row, dict) for row in value))
    return value


def zone(value):
    require(isinstance(value, str) and re.fullmatch(r"us-east-1[a-z]", value))
    return value


def key_name(value):
    if value is None:
        return None
    # Key names are user-controlled labels. Omit credential-like or long opaque
    # names; tags, user-data, IAM session identifiers and console output are never copied.
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}", value)
            or re.search(r"(?i:secret|token|password|credential|bearer)|(?:AKIA|ASIA)[A-Z0-9]{16}", value)
            or re.search(r"[A-Za-z0-9_+=/-]{32,}", value)):
        return "<redacted>"
    return value


def permission_set(identity):
    require(identity.get("Account") == ACCOUNT, "identity-rejected")
    arn = identity.get("Arn")
    require(isinstance(arn, str) and arn.startswith(f"arn:aws:sts::{ACCOUNT}:assumed-role/"),
            "identity-rejected")
    # STS normally omits the IAM role path. Accept an optional path, but validate
    # the actual role-name component immediately before the session name.
    parts = arn.split(":", 5)[5].split("/")
    require(len(parts) >= 3 and all(parts) and bool(parts[-1]), "identity-rejected")
    match = re.fullmatch(r"AWSReservedSSO_(GCMigrationInspect|GCMigrationOperator)_[0-9a-fA-F]{16}", parts[-2])
    require(match is not None, "identity-rejected")
    return match.group(1)


def instances_metadata(value):
    result = []
    for reservation in sequence(value.get("Reservations")):
        for row in sequence(reservation.get("Instances")):
            kind = row.get("InstanceType")
            require(isinstance(kind, str) and re.fullmatch(r"[a-z0-9-]{1,32}\.[a-z0-9]{1,24}", kind))
            result.append({
                "instance_id": identifier(row.get("InstanceId"), "i"),
                "state": choice(row.get("State", {}).get("Name"),
                                {"pending", "running", "stopping", "stopped", "shutting-down", "terminated"}),
                "availability_zone": zone(row.get("Placement", {}).get("AvailabilityZone")),
                "instance_type": kind,
                "subnet_id": identifier(row.get("SubnetId"), "subnet"),
                "security_group_ids": sorted(identifier(group.get("GroupId"), "sg")
                                             for group in sequence(row.get("SecurityGroups"))),
                "key_name": key_name(row.get("KeyName")),
                "image_id": identifier(row.get("ImageId"), "ami"),
                "has_instance_profile": bool(row.get("IamInstanceProfile")),
            })
    require(len(result) <= 1000)
    return sorted(result, key=lambda row: row["instance_id"])


def volumes_metadata(value):
    result = []
    for row in sequence(value.get("Volumes")):
        require(type(row.get("Encrypted")) is bool)
        result.append({
            "volume_id": identifier(row.get("VolumeId"), "vol"),
            "state": choice(row.get("State"), {"creating", "available", "in-use", "deleting", "deleted", "error"}),
            "availability_zone": zone(row.get("AvailabilityZone")),
            "size_gib": numeric(row.get("Size")),
            "iops": numeric(row["Iops"]) if "Iops" in row else None,
            "throughput_mib_s": numeric(row["Throughput"]) if "Throughput" in row else None,
            "volume_type": choice(row.get("VolumeType"), {"standard", "gp2", "gp3", "io1", "io2", "st1", "sc1"}),
            "encrypted": row["Encrypted"],
            "attachments": sorted([
                {"instance_id": identifier(item.get("InstanceId"), "i"),
                 "state": choice(item.get("State"),
                                 {"attaching", "attached", "detaching", "detached", "busy"})}
                for item in sequence(row.get("Attachments"))
            ], key=lambda item: item["instance_id"]),
        })
    return sorted(result, key=lambda row: row["volume_id"])


def inspect(output_dir, *, runner=None, clock=time.monotonic):
    output = Path(output_dir)
    # A unique directory is mandatory. Refuse an existing directory/file/symlink
    # before any credential use; never overwrite a previous inspection.
    try:
        output.mkdir(mode=0o700)
        os.chmod(output, 0o700)
    except OSError:
        raise InspectionError("new-private-output-directory-required") from None
    grant = aws.OperationAuthorization(purpose="manual-inspect", deadline=clock()+60,
                                       call_limit=5, clock=clock)
    client = aws.AWS(runner=subprocess.run if runner is None else runner,
                     authorization=grant, audit_path=output/"actions.jsonl")
    report = {"schema": 1, "status": "failed", "profile": "gc-migration",
              "account_id": ACCOUNT, "region": REGION, "automatic_requests_enabled": False,
              "writes_enabled": False, "request_limit": 5, "deadline_seconds": 60}
    try:
        identity = client.call("sts", "get-caller-identity")
        report["permission_set"] = permission_set(identity)
        report["instances"] = instances_metadata(client.call("ec2", "describe-instances"))
        report["volumes"] = volumes_metadata(client.call("ec2", "describe-volumes"))
        quota = client.call("service-quotas", "get-service-quota",
                            {"ServiceCode": "ec2", "QuotaCode": QUOTA})
        report["standard_vcpu_quota"] = numeric(quota.get("Quota", {}).get("Value"))
        offerings = client.call("ec2", "describe-instance-type-offerings", {
            "LocationType": "availability-zone", "Filters": [
                {"Name": "instance-type", "Values": [INSTANCE_TYPE]},
                {"Name": "location", "Values": [ZONE]},
            ]})
        rows = sequence(offerings.get("InstanceTypeOfferings"))
        require(all(row.get("InstanceType") == INSTANCE_TYPE and row.get("Location") == ZONE for row in rows))
        report["transfer_offering"] = {"instance_type": INSTANCE_TYPE, "availability_zone": ZONE,
                                       "listed": bool(rows), "live_capacity_confirmed": False}
        report["status"] = "complete"
    except InspectionError as error:
        report["error"] = str(error)
    except aws.Error:
        report["error"] = "bounded-aws-inspection-failed"
    except (KeyboardInterrupt, SystemExit):
        report["error"] = "inspection-interrupted"
    except Exception:
        report["error"] = "invalid-metadata-or-local-inspection-failure"
    finally:
        grant.revoke()
        report["requests_attempted"] = grant.calls
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        aws.atomic(output/"inspection.json", report)
    return report


def main(argv=None, *, runner=None, clock=time.monotonic):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="A new directory in an existing parent; created private, never reused")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.output_dir, runner=runner, clock=clock)
    except InspectionError as error:
        report = {"schema": 1, "status": "failed", "error": str(error), "requests_attempted": 0}
    except Exception:
        report = {"schema": 1, "status": "failed", "error": "local-inspection-failure"}
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
