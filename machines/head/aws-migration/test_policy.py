"""Offline policy scope contracts, not an AWS IAM policy simulator."""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

import build_operator_policy as generator


READ_ACTIONS = {
    "sts:GetCallerIdentity",
    "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeVolumesModifications",
    "ec2:DescribeImages", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
    "ec2:DescribeKeyPairs", "ec2:DescribeAvailabilityZones", "ec2:DescribeInstanceTypes",
    "ec2:DescribeInstanceTypeOfferings", "servicequotas:GetServiceQuota",
}
INSTANCE = "i-0123456789abcdef0"
INSTANCE_ARN = "arn:aws:ec2:us-east-1:147997164104:instance/" + INSTANCE


def actions(statement):
    value = statement["Action"]
    return [value] if isinstance(value, str) else value


class ScopeTests(unittest.TestCase):
    def inspection(self):
        return json.loads(generator.INSPECTION_POLICY.read_text())

    def assert_contract(self, policy, operator=False):
        self.assertEqual(policy["Version"], "2012-10-17")
        self.assertEqual(set(policy), {"Version", "Statement"})
        self.assertEqual(len(policy["Statement"]), 4 if operator else 2)
        observed = set()
        for statement in policy["Statement"]:
            self.assertEqual(statement["Effect"], "Allow")
            self.assertNotIn("NotAction", statement)
            self.assertNotIn("NotResource", statement)
            self.assertNotIn("Principal", statement)
            for action in actions(statement):
                self.assertNotIn("*", action)
                self.assertNotIn("?", action)
                self.assertNotIn(action, observed, "Each action must have one authorization statement")
                observed.add(action)
            if actions(statement) == ["sts:GetCallerIdentity"]:
                self.assertEqual(statement["Resource"], "*")
                self.assertNotIn("Condition", statement)
            else:
                conditions = statement["Condition"]
                self.assertEqual(set(conditions), {"StringEquals"})
                self.assertEqual(conditions["StringEquals"]["aws:RequestedRegion"], "us-east-1")
                if all(action in READ_ACTIONS for action in actions(statement)):
                    self.assertEqual(statement["Resource"], "*")
                    self.assertEqual(conditions["StringEquals"], {"aws:RequestedRegion": "us-east-1"})
                else:
                    self.assertEqual(statement["Resource"], INSTANCE_ARN)
        self.assertEqual(observed, READ_ACTIONS | ({"ec2:StartInstances", "ec2:StopInstances"} if operator else set()))

    def test_inspection_is_only_explicit_regional_metadata(self):
        self.assert_contract(self.inspection())

    def test_operator_adds_only_exact_instance_start_and_stop(self):
        self.assert_contract(generator.build_policy(INSTANCE), operator=True)

    def test_start_requires_c7i_xlarge_but_stop_remains_possible_after_type_drift(self):
        statements = {s["Action"]: s for s in generator.build_policy(INSTANCE)["Statement"]
                      if isinstance(s["Action"], str)}
        self.assertEqual(statements["ec2:StartInstances"]["Condition"], {"StringEquals": {
            "aws:RequestedRegion": "us-east-1", "ec2:InstanceType": "c7i.xlarge"}})
        self.assertEqual(statements["ec2:StopInstances"]["Condition"], {"StringEquals": {
            "aws:RequestedRegion": "us-east-1"}})

    def test_missing_wildcard_foreign_arn_and_multiple_ids_are_rejected(self):
        for value in (None, "", "*", "i-*", "i-01234567", INSTANCE_ARN,
                      INSTANCE+",i-1123456789abcdef0", INSTANCE+"\n", "i-0123456789ABCDEF0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                generator.build_policy(value)

    def test_separate_generations_preserve_inspection_and_do_not_accumulate_grants(self):
        before = generator.INSPECTION_POLICY.read_bytes()
        first = generator.build_policy(INSTANCE)
        second = generator.build_policy("i-1123456789abcdef0")
        self.assertEqual(len(first["Statement"]), 4)
        self.assertEqual(len(second["Statement"]), 4)
        self.assertNotIn(INSTANCE_ARN, json.dumps(second))
        self.assertEqual(generator.INSPECTION_POLICY.read_bytes(), before)

    def test_cli_creates_valid_policy_and_refuses_to_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/"policy.json"
            args = ["--instance-id", INSTANCE, "--output", str(output)]
            self.assertEqual(generator.main(args), 0)
            self.assert_contract(json.loads(output.read_text()), operator=True)
            original = output.read_bytes()
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                generator.main(args)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
