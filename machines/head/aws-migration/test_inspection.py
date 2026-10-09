"""Offline tests: every AWS subprocess is a fake and no credentials are loaded."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import inspect_migration as module


INSTANCE = "i-0123456789abcdef0"
VOLUME = "vol-0123456789abcdef0"
ROLE = "AWSReservedSSO_GCMigrationInspect_0123456789abcdef"
SENTINEL = "PRIVATE-RESPONSE-OR-CREDENTIAL-MUST-NOT-APPEAR"


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.output = self.root/"inspection"
        self.calls = []; self.now = 1000.0; self.after = None; self.failure = None
        self.responses = {
            "get-caller-identity": {"Account": module.ACCOUNT,
                "Arn": f"arn:aws:sts::{module.ACCOUNT}:assumed-role/{ROLE}/private-user",
                "UserId": SENTINEL},
            "describe-instances": {"Reservations": [{"Instances": [{
                "InstanceId": INSTANCE, "State": {"Name": "stopped"},
                "Placement": {"AvailabilityZone": "us-east-1a"}, "InstanceType": "r6a.32xlarge",
                "SubnetId": "subnet-0123456789abcdef0", "SecurityGroups": [{"GroupId": "sg-0123456789abcdef0", "GroupName": SENTINEL}],
                "ImageId": "ami-0123456789abcdef0", "KeyName": "gc-msa-key",
                "Tags": [{"Key": "Name", "Value": SENTINEL}], "UserData": SENTINEL,
            }]}]},
            "describe-volumes": {"Volumes": [{"VolumeId": VOLUME,
                "State": "in-use", "AvailabilityZone": "us-east-1a", "Size": 1300,
                "Iops": 8000, "Throughput": 2000, "VolumeType": "gp3", "Encrypted": False,
                "KmsKeyId": SENTINEL, "Tags": [{"Key": "Project", "Value": SENTINEL}],
                "Attachments": [{"InstanceId": INSTANCE, "State": "attached", "Device": SENTINEL}]}]},
            "get-service-quota": {"Quota": {"Value": 512, "QuotaName": SENTINEL}},
            "describe-instance-type-offerings": {"InstanceTypeOfferings": [{
                "InstanceType": "c7i.xlarge", "Location": "us-east-1a", "LocationType": "availability-zone"}]},
        }

    def runner(self, argv, **options):
        offset = argv.index("--cli-read-timeout")+2
        service, operation = argv[offset:offset+2]
        request = json.loads(argv[argv.index("--cli-input-json")+1]) if "--cli-input-json" in argv else None
        self.calls.append((service, operation, request, argv, options))
        if self.after:
            self.after(operation)
        if self.failure and self.failure[0] == operation:
            return subprocess.CompletedProcess(argv, 255, b"", self.failure[1])
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.responses[operation]).encode(), b"")

    def inspect(self, **kwargs):
        return module.inspect(self.output, runner=self.runner, clock=lambda: self.now, **kwargs)

    def test_exactly_five_ordered_reads_pinned_profile_region_and_offering(self):
        with patch.dict(os.environ, AWS_PROFILE="admin", BIO_AWS_ENABLED="1"):
            report = self.inspect()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["requests_attempted"], 5)
        self.assertEqual([row[1] for row in self.calls], ["get-caller-identity", "describe-instances",
                         "describe-volumes", "get-service-quota", "describe-instance-type-offerings"])
        self.assertEqual(self.calls[3][2], {"ServiceCode": "ec2", "QuotaCode": "L-1216C47A"})
        self.assertEqual(self.calls[4][2], {"LocationType": "availability-zone", "Filters": [
            {"Name": "instance-type", "Values": ["c7i.xlarge"]}, {"Name": "location", "Values": ["us-east-1a"]}]})
        for _, _, _, argv, options in self.calls:
            self.assertEqual(argv[argv.index("--profile")+1], "gc-migration")
            self.assertEqual(argv[argv.index("--region")+1], "us-east-1")
            self.assertIn("--no-paginate", argv)
            self.assertEqual(options["env"]["AWS_MAX_ATTEMPTS"], "1")
            self.assertTrue(0 < options["timeout"] <= 60)
        self.assertFalse(report["automatic_requests_enabled"])
        self.assertFalse(report["writes_enabled"])
        self.assertFalse(report["transfer_offering"]["live_capacity_confirmed"])
        self.assertEqual(report["instances"][0]["instance_type"], "r6a.32xlarge")

    def test_both_migration_roles_and_optional_arn_role_path_are_accepted(self):
        for role in (ROLE, ROLE.replace("Inspect", "Operator")):
            for path in ("", "aws-reserved/sso.amazonaws.com/us-east-1/"):
                identity = {"Account": module.ACCOUNT,
                            "Arn": f"arn:aws:sts::{module.ACCOUNT}:assumed-role/{path}{role}/session"}
                self.assertIn(module.permission_set(identity), {"GCMigrationInspect", "GCMigrationOperator"})

    def test_user_root_admin_foreign_and_similarly_named_roles_stop_after_identity(self):
        identities = [
            {"Account": module.ACCOUNT, "Arn": f"arn:aws:iam::{module.ACCOUNT}:root"},
            {"Account": module.ACCOUNT, "Arn": f"arn:aws:iam::{module.ACCOUNT}:user/gc-migration"},
            {"Account": module.ACCOUNT, "Arn": f"arn:aws:sts::{module.ACCOUNT}:assumed-role/AWSReservedSSO_AdministratorAccess_0123456789abcdef/user"},
            {"Account": "111122223333", "Arn": f"arn:aws:sts::111122223333:assumed-role/{ROLE}/user"},
            {"Account": module.ACCOUNT, "Arn": f"arn:aws:sts::111122223333:assumed-role/{ROLE}/user"},
            {"Account": module.ACCOUNT, "Arn": f"arn:aws:sts::{module.ACCOUNT}:assumed-role/{ROLE}_Admin/user"},
            {"Account": module.ACCOUNT},
        ]
        for index, identity in enumerate(identities):
            self.output = self.root/str(index); self.calls.clear()
            self.responses["get-caller-identity"] = identity
            report = self.inspect()
            self.assertEqual(report["error"], "identity-rejected")
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn("instances", report)

    def test_existing_directory_file_or_symlink_never_uses_credentials(self):
        directory = self.root/"existing"; directory.mkdir()
        file = self.root/"file"; file.write_text("keep")
        link = self.root/"link"; link.symlink_to(directory)
        for path in (directory, file, link, self.root/"missing-parent"/"new"):
            with self.subTest(path=path), self.assertRaisesRegex(module.InspectionError, "new-private"):
                module.inspect(path, runner=self.runner)
        self.assertEqual(self.calls, [])
        self.assertEqual(file.read_text(), "keep")

    def test_private_report_and_audit_omit_tags_response_extras_and_credential_like_key_name(self):
        self.responses["describe-instances"]["Reservations"][0]["Instances"][0]["KeyName"] = "AKIA" + "Z"*16
        report = self.inspect()
        self.assertEqual(report["instances"][0]["key_name"], "<redacted>")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for name in ("inspection.json", "actions.jsonl"):
            path = self.output/name
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            for secret in (SENTINEL, "AKIA"+"Z"*16, "private-user", "UserData", "KmsKeyId"):
                self.assertNotIn(secret, text)
        rows = [json.loads(line) for line in (self.output/"actions.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(set(row) == {"schema", "timestamp", "purpose", "service", "operation", "call_id", "outcome"}
                            for row in rows))

    def test_pagination_is_not_followed_and_no_partial_inventory_is_claimed_complete(self):
        self.responses["describe-instances"]["NextToken"] = SENTINEL
        report = self.inspect()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("instances", report)
        self.assertNotIn(SENTINEL, (self.output/"inspection.json").read_text())

    def test_access_denial_stops_immediately_and_does_not_echo_stderr(self):
        self.failure = ("describe-volumes", b"An error occurred (AccessDenied): " + SENTINEL.encode())
        report = self.inspect()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("volumes", report)
        self.assertNotIn(SENTINEL, json.dumps(report))

    def test_expiry_between_calls_prevents_next_subprocess_and_revokes_grant(self):
        self.after = lambda operation: setattr(self, "now", self.now+60)
        clients = []; original_client = module.aws.AWS
        def client(**kwargs):
            clients.append(original_client(**kwargs)); return clients[-1]
        with patch.object(module.aws, "AWS", side_effect=client):
            report = self.inspect()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(clients[0].authorization.revoked)

    def test_completion_revokes_grant_and_offering_absence_is_not_capacity_failure(self):
        self.responses["describe-instance-type-offerings"]["InstanceTypeOfferings"] = []
        clients = []; original = module.aws.AWS
        def client(**kwargs):
            clients.append(original(**kwargs)); return clients[-1]
        with patch.object(module.aws, "AWS", side_effect=client): report = self.inspect()
        self.assertEqual(report["status"], "complete")
        self.assertFalse(report["transfer_offering"]["listed"])
        self.assertTrue(clients[0].authorization.revoked)
        with self.assertRaisesRegex(module.aws.Error, "revoked"):
            clients[0].call("sts", "get-caller-identity")
        self.assertEqual(len(self.calls), 5)

    def test_cli_prints_only_selected_json_and_transport_errors_are_generic(self):
        def failed(*args, **kwargs):
            raise subprocess.TimeoutExpired(SENTINEL, 1, output=SENTINEL)
        with redirect_stdout(io.StringIO()) as output:
            status = module.main(["--output-dir", str(self.output)], runner=failed)
        value = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(value["status"], "failed")
        self.assertNotIn(SENTINEL, output.getvalue())

    def test_malformed_metadata_does_not_escape_in_error_text(self):
        self.responses["describe-instances"]["Reservations"][0]["Instances"][0]["InstanceId"] = SENTINEL
        report = self.inspect()
        self.assertEqual(report["error"], "invalid-metadata")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn(SENTINEL, json.dumps(report))

    def test_import_is_offline(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("No subprocess")) as run:
            runpy.run_path(module.__file__, run_name="migration_offline_import_test")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
