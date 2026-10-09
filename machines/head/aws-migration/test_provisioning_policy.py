"""Offline scope checks; deliberately not advertised as an AWS IAM simulator."""

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import fnmatch
import io
import json
from pathlib import Path
import tempfile
import unittest

import build_provisioning_policy as generator

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
VALUES = dict(ami_id="ami-0123456789abcdef0", subnet_id="subnet-0123456789abcdef0",
              security_group_id="sg-0123456789abcdef0", key_name="reviewed-migration-key",
              retained_volume_id="vol-0123456789abcdef0", availability_zone="us-east-1a",
              kms_key_arn="arn:aws:kms:us-east-1:147997164104:key/01234567-89ab-cdef-0123-456789abcdef",
              run_id="migration-20261009-a", expires_at="2026-10-09T13:00:00Z")


def inspection(now=NOW):
    return {"schema": 1, "status": "complete", "account_id": generator.ACCOUNT,
            "region": generator.REGION, "completed_at": (now-timedelta(minutes=5)).isoformat(),
            "instances": [{"availability_zone": "us-east-1a", "subnet_id": VALUES["subnet_id"],
                           "security_group_ids": [VALUES["security_group_id"]],
                           "key_name": VALUES["key_name"]}],
            "volumes": [{"volume_id": VALUES["retained_volume_id"], "availability_zone": "us-east-1a",
                         "size_gib": 1300, "encrypted": False, "volume_type": "gp3", "state": "in-use"}]}


def items(value):
    return [value] if isinstance(value, str) else value


def allowed(policy, action, resource, context):
    """Evaluate only operators generated here, failing on unknown ones.

    Tests use this for resource-level negative cases. Actual multi-resource EC2
    requests still need authorization for every required resource on AWS.
    """
    for statement in policy["Statement"]:
        if action not in items(statement["Action"]):
            continue
        if not any(fnmatch.fnmatchcase(resource, item) for item in items(statement["Resource"])):
            continue
        matches = []
        for operator, rules in statement.get("Condition", {}).items():
            for key, expected in rules.items():
                actual = context.get(key)
                if operator in {"StringEquals", "ArnEquals"}:
                    matches.append(actual is not None and actual in items(expected))
                elif operator == "Bool":
                    matches.append(actual is not None and str(actual).lower() == expected)
                elif operator == "Null":
                    matches.append((actual is None) == (expected == "true"))
                elif operator == "NumericEquals":
                    matches.append(actual is not None and float(actual) == expected)
                elif operator == "NumericLessThanEquals":
                    matches.append(actual is not None and float(actual) <= expected)
                elif operator == "DateLessThan":
                    matches.append(actual is not None and generator.utc(actual, "test") < generator.utc(expected, "test"))
                elif operator == "ForAllValues:StringEquals":
                    matches.append(all(value in expected for value in (actual or [])))
                else:
                    raise AssertionError("Unhandled policy operator: " + operator)
        if all(matches):
            return True
    return False


class ProvisioningScopeTests(unittest.TestCase):
    def setUp(self):
        self.policy = generator.build_policy(inspection(), now=NOW, **VALUES)
        self.context = {"aws:RequestedRegion": "us-east-1", "aws:CurrentTime": NOW.isoformat()}
        self.request_tags = {"aws:RequestTag/GCMigration": VALUES["run_id"],
                             "aws:RequestTag/Project": "gc-migration",
                             "aws:TagKeys": ["GCMigration", "Project"]}
        self.resource_tags = {"aws:ResourceTag/GCMigration": VALUES["run_id"],
                              "aws:ResourceTag/Project": "gc-migration"}
        self.volume = {**self.context, **self.request_tags, "ec2:AvailabilityZone": "us-east-1a",
                       "ec2:VolumeType": "gp3", "ec2:Encrypted": True,
                       "ec2:VolumeSize": 20, "ec2:VolumeIops": 3000, "ec2:VolumeThroughput": 125}

    def permits(self, action, resource, context):
        return allowed(self.policy, action, resource, context)

    def test_only_reviewed_actions_and_all_mutations_expire(self):
        read = json.loads(generator.INSPECTION_POLICY.read_text())
        self.assertEqual(self.policy["Statement"][:2], read["Statement"])
        writes = self.policy["Statement"][2:]
        observed = {action for statement in writes for action in items(statement["Action"])}
        self.assertEqual(observed, {"ec2:RunInstances", "ec2:CreateVolume", "ec2:CreateTags",
                                   "ec2:AttachVolume", "ec2:StopInstances", "kms:Decrypt", "kms:DescribeKey",
                                   "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncryptFrom", "kms:ReEncryptTo",
                                   "kms:CreateGrant"})
        for statement in writes:
            self.assertEqual(statement["Effect"], "Allow")
            self.assertNotIn("*", items(statement["Action"]))
            self.assertEqual(statement["Condition"]["DateLessThan"], {"aws:CurrentTime": VALUES["expires_at"]})
            self.assertEqual(statement["Condition"]["StringEquals"]["aws:RequestedRegion"], "us-east-1")
        self.assertLess(len(json.dumps(self.policy, separators=(",", ":"))), 10240)

    def test_boot_size_encryption_and_performance_cannot_expand(self):
        resource = generator.arn("volume", "vol-1123456789abcdef0")
        self.assertTrue(self.permits("ec2:RunInstances", resource, self.volume))
        for key, value in [("ec2:VolumeSize", 3072), ("ec2:Encrypted", False), ("ec2:VolumeType", "io2"),
                           ("ec2:VolumeIops", 3001), ("ec2:VolumeThroughput", 126),
                           ("aws:RequestTag/GCMigration", "different-run"), ("aws:TagKeys", ["GCMigration", "Project", "Admin"]),
                           ("aws:CurrentTime", VALUES["expires_at"]), ("aws:RequestedRegion", "us-west-2")]:
            with self.subTest(key=key):
                self.assertFalse(self.permits("ec2:RunInstances", resource, {**self.volume, key: value}))

    def test_large_data_requires_exact_size_key_blank_source_and_tags(self):
        resource = generator.arn("volume", "vol-1123456789abcdef0")
        context = {**self.volume, "ec2:VolumeSize": 3072, "ec2:KmsKeyId": VALUES["kms_key_arn"]}
        self.assertTrue(self.permits("ec2:CreateVolume", resource, context))
        for key, value in [("ec2:VolumeSize", 3073), ("ec2:VolumeSize", 20), ("ec2:Encrypted", False),
                           ("ec2:KmsKeyId", "other-key"), ("ec2:ParentSnapshot", "some-snapshot"),
                           ("ec2:VolumeThroughput", 126), ("ec2:AvailabilityZone", "us-east-1b")]:
            with self.subTest(key=key, value=value):
                self.assertFalse(self.permits("ec2:CreateVolume", resource, {**context, key: value}))
        del context["aws:RequestTag/GCMigration"]
        self.assertFalse(self.permits("ec2:CreateVolume", resource, context))

    def test_instance_type_profile_metadata_and_reviewed_inputs(self):
        resource = generator.arn("instance", "i-1123456789abcdef0")
        context = {**self.context, **self.request_tags, "ec2:InstanceType": "c7i.xlarge",
                   "ec2:AvailabilityZone": "us-east-1a", "ec2:Tenancy": "default",
                   "ec2:InstanceMarketType": "on-demand", "ec2:MetadataHttpTokens": "required",
                   "ec2:MetadataHttpEndpoint": "enabled"}
        self.assertTrue(self.permits("ec2:RunInstances", resource, context))
        for key, value in [("ec2:InstanceType", "r6a.32xlarge"), ("ec2:InstanceProfile", "admin-profile"),
                           ("ec2:MetadataHttpTokens", "optional"), ("ec2:InstanceMarketType", "spot")]:
            self.assertFalse(self.permits("ec2:RunInstances", resource, {**context, key: value}))
        self.assertTrue(self.permits("ec2:RunInstances", generator.arn("image", VALUES["ami_id"], account=""), self.context))
        for kind, value in [("image", "ami-1123456789abcdef0"), ("subnet", "subnet-1123456789abcdef0"),
                            ("security-group", "sg-1123456789abcdef0"), ("key-pair", "other-key"),
                            ("snapshot", "snap-0123456789abcdef0")]:
            self.assertFalse(self.permits("ec2:RunInstances", generator.arn(kind, value, account="" if kind in {"image", "snapshot"} else generator.ACCOUNT), self.context))

    def test_existing_resources_cannot_be_retagged_to_take_ownership(self):
        resource = generator.arn("instance", "i-1123456789abcdef0")
        context = {**self.context, **self.request_tags}
        self.assertFalse(self.permits("ec2:CreateTags", resource, context))
        self.assertTrue(self.permits("ec2:CreateTags", resource, {**context, "ec2:CreateAction": "RunInstances"}))
        self.assertFalse(self.permits("ec2:CreateTags", resource, {**context, "ec2:CreateAction": "CreateSnapshot"}))

    def test_attach_requires_scoped_host_and_exact_retained_disk_exception(self):
        host = generator.arn("instance", "i-1123456789abcdef0")
        context = {**self.context, **self.resource_tags, "ec2:InstanceType": "c7i.xlarge", "ec2:AvailabilityZone": "us-east-1a"}
        self.assertTrue(self.permits("ec2:AttachVolume", host, context))
        del context["aws:ResourceTag/GCMigration"]
        self.assertFalse(self.permits("ec2:AttachVolume", host, context))
        old = generator.arn("volume", VALUES["retained_volume_id"])
        context = {**self.context, "ec2:AvailabilityZone": "us-east-1a", "ec2:VolumeType": "gp3",
                   "ec2:VolumeSize": 1300, "ec2:Encrypted": False}
        self.assertTrue(self.permits("ec2:AttachVolume", old, context))
        other = generator.arn("volume", "vol-1123456789abcdef0")
        self.assertFalse(self.permits("ec2:AttachVolume", other, context))
        self.assertFalse(self.permits("ec2:AttachVolume", other, {**context, **self.resource_tags}))
        self.assertTrue(self.permits("ec2:AttachVolume", other, {**context, **self.resource_tags, "ec2:Encrypted": True}))

    def test_stop_works_after_type_change_but_only_for_this_run(self):
        host = generator.arn("instance", "i-1123456789abcdef0")
        context = {**self.context, **self.resource_tags, "ec2:AvailabilityZone": "us-east-1a", "ec2:InstanceType": "different"}
        self.assertTrue(self.permits("ec2:StopInstances", host, context))
        self.assertFalse(self.permits("ec2:StopInstances", host, {**context, "aws:ResourceTag/GCMigration": "old-run"}))
        for action in ["ec2:StartInstances", "ec2:TerminateInstances", "ec2:DeleteVolume", "ec2:ModifyVolume",
                       "ec2:DetachVolume", "ec2:CreateSnapshot", "iam:PassRole", "ssm:SendCommand"]:
            self.assertFalse(self.permits(action, host, context))

    def test_kms_is_exact_key_ec2_mediated_and_grants_only_aws_resources(self):
        key = VALUES["kms_key_arn"]
        context = {**self.context, "kms:ViaService": "ec2.us-east-1.amazonaws.com", "kms:CallerAccount": generator.ACCOUNT}
        self.assertTrue(self.permits("kms:Decrypt", key, context))
        self.assertFalse(self.permits("kms:Decrypt", key, self.context))
        self.assertFalse(self.permits("kms:Decrypt", key.replace("01234567-", "11234567-"), context))
        self.assertFalse(self.permits("kms:CreateGrant", key, context))
        self.assertTrue(self.permits("kms:CreateGrant", key, {**context, "kms:GrantIsForAWSResource": True}))
        for action in ["kms:CreateKey", "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion"]:
            self.assertFalse(self.permits(action, key, context))

    def test_fresh_inventory_and_retained_volume_are_mandatory(self):
        for field, value in [("status", "failed"), ("account_id", "000000000000"), ("region", "us-west-2"),
                             ("completed_at", "2026-10-09T10:00:00Z"), ("completed_at", "2026-10-09T12:01:00Z")]:
            report = inspection()
            report[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                generator.build_policy(report, now=NOW, **VALUES)
        for field, value in [("volume_id", "vol-1123456789abcdef0"), ("availability_zone", "us-east-1b"),
                             ("encrypted", True), ("size_gib", 20), ("state", "deleted")]:
            report = inspection()
            report["volumes"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                generator.build_policy(report, now=NOW, **VALUES)

    def test_invalid_values_and_unbounded_expiry_are_rejected(self):
        for field, value in [("expires_at", "2026-10-09T11:00:00Z"), ("expires_at", "2026-10-10T12:00:01Z"),
                             ("expires_at", "2026-10-09T13:00:00"), ("ami_id", "ami-*"), ("subnet_id", "subnet-*"),
                             ("security_group_id", "sg-*"), ("retained_volume_id", "vol-*"), ("key_name", "key/*"),
                             ("run_id", "*"), ("run_id", "${aws:username}"), ("availability_zone", "us-west-2a"),
                             ("kms_key_arn", VALUES["kms_key_arn"].replace(generator.ACCOUNT, "000000000000"))]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                generator.build_policy(inspection(), now=NOW, **{**VALUES, field: value})

    def test_redacted_inventory_key_still_requires_explicit_reviewed_name(self):
        report = inspection()
        report["instances"][0]["key_name"] = "<redacted>"
        policy = generator.build_policy(report, now=NOW, **VALUES)
        self.assertIn(generator.arn("key-pair", VALUES["key_name"]), json.dumps(policy))
        self.assertNotIn("<redacted>", json.dumps(policy))

    def test_cli_only_writes_new_local_policy_and_preserves_input(self):
        now = datetime.now(timezone.utc)
        values = {**VALUES, "expires_at": (now+timedelta(hours=1)).isoformat()}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"inspection.json"
            source.write_text(json.dumps(inspection(now)))
            original = source.read_bytes()
            output = Path(directory)/"provision.json"
            args = ["--inspection-report", str(source), "--output", str(output)]
            for name, value in values.items():
                args.extend(["--"+name.replace("_", "-"), value])
            self.assertEqual(generator.main(args), 0)
            saved = output.read_bytes()
            self.assertEqual(source.read_bytes(), original)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                generator.main(args)
            self.assertEqual(output.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
