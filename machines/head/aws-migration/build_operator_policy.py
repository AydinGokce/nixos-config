#!/usr/bin/env python3
"""Build an exact-instance migration policy locally; never contacts AWS."""

import argparse
import json
from pathlib import Path
import re
import sys

ACCOUNT = "147997164104"
REGION = "us-east-1"
INSTANCE_TYPE = "c7i.xlarge"
INSPECTION_POLICY = Path(__file__).with_name("inspection-policy.json")


def build_policy(instance_id):
    if not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]{17}", instance_id):
        raise ValueError("Expected one exact EC2 instance ID (i- followed by 17 lowercase hex digits)")
    policy = json.loads(INSPECTION_POLICY.read_text())
    resource = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}"
    policy["Statement"].extend([
        {
            "Sid": "StartOnlyTheApprovedTransferInstance",
            "Effect": "Allow",
            "Action": "ec2:StartInstances",
            "Resource": resource,
            "Condition": {"StringEquals": {
                "aws:RequestedRegion": REGION,
                "ec2:InstanceType": INSTANCE_TYPE,
            }},
        },
        {
            "Sid": "StopOnlyTheApprovedTransferInstance",
            "Effect": "Allow",
            "Action": "ec2:StopInstances",
            "Resource": resource,
            # Cleanup must remain possible if an administrator changes its type.
            "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
        },
    ])
    return policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output", type=Path, help="Create a new JSON file; refuses to overwrite")
    args = parser.parse_args(argv)
    try:
        value = json.dumps(build_policy(args.instance_id), indent=2) + "\n"
        if args.output:
            with args.output.open("x") as stream:
                stream.write(value)
        else:
            sys.stdout.write(value)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
