"""Offline AWS bootstrap tests. Tiny fixtures do not qualify real native MSA."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aws_worker as aws
import block_cache as content
import block_cache_device as linux
import databases
import test_block_cache as cache_fixtures
from test_block_cache_device import disk

NOW = 1789344060.0
VOLUME = "vol-0123456789abcdef0"
ROOT = "vol-0123456789abcdef1"
INSTANCE = "i-0123456789abcdef0"
FS = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
BOOT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "a"*32


def fixture():
    tags = [dict(Key="Project", Value="gc-msa"), dict(Key="allocation-token", Value=TOKEN)]
    owner = dict(schema=1, provider="aws", account_id="147997164104", region="us-east-1", availability_zone="us-east-1b",
        cache_id=TOKEN, volume_id=VOLUME, filesystem_uuid=FS, source_manifest_sha256=databases.MANIFEST_SHA256,
        source_receipt_sha256="2"*64, ready_receipt_sha256=None, creation=dict(
            request=dict(VolumeType="gp3", Size=1300, AvailabilityZone="us-east-1b", Encrypted=True,
                TagSpecifications=[dict(ResourceType="volume", Tags=tags)]),
            response=dict(VolumeId=VOLUME, CreateTime="2026-09-14T00:00:10Z", SnapshotId="")))
    provider = dict(observed_epoch=NOW, account_id=owner["account_id"], region=owner["region"],
        managed=dict(instance_id=INSTANCE, boot_id=BOOT), instances=[dict(InstanceId=INSTANCE,
        State=dict(Name="running"), Placement=dict(AvailabilityZone="us-east-1b"), RootDeviceName="/dev/sda1",
        BlockDeviceMappings=[dict(DeviceName="/dev/sda1", Ebs=dict(VolumeId=ROOT, Status="attached")),
                             dict(DeviceName="/dev/sdf", Ebs=dict(VolumeId=VOLUME, Status="attached"))])],
        volumes=[dict(VolumeId=ROOT, Attachments=[dict(InstanceId=INSTANCE, State="attached", Device="/dev/sda1")]),
            dict(VolumeId=VOLUME, VolumeType="gp3", Size=1300, AvailabilityZone="us-east-1b", State="in-use",
                 MultiAttachEnabled=False, Tags=tags, CreateTime="2026-09-14T00:00:10Z", SnapshotId="",
                 Attachments=[dict(InstanceId=INSTANCE, State="attached", Device="/dev/sdf", DeleteOnTermination=False)])])
    root = disk("/dev/nvme3n1", "259:3", 100*1024**3)
    root["serial"] = ROOT.replace("-", "")
    child = disk("/dev/nvme3n1p1", "259:4", 99*1024**3, kind="part")
    child.update(pkname="nvme3n1", mountpoints=["/"], fstype="ext4", uuid="root-uuid")
    root["children"] = [child]
    target = disk("/dev/nvme1n1", "259:1", aws.SIZE_BYTES)
    target["serial"] = VOLUME.replace("-", "")
    ephemeral = disk("/dev/nvme0n1", "259:0", aws.SIZE_BYTES)
    ephemeral["serial"] = "AWSINSTANCERANDOM"
    evidence = dict(schema=1, observed_epoch=NOW, boot_id=BOOT, device="/dev/nvme1n1",
        stat=dict(is_block=True, is_symlink=False, major_minor="259:1"), lsblk=dict(blockdevices=[ephemeral, root, target]),
        holders=[], mounts=[dict(major_minor="259:4", target="/", options=["rw"], fstype="ext4", source="/dev/nvme3n1p1")],
        probe=dict(wipefs=dict(returncode=0, signatures=[]), blkid=dict(returncode=2, fields={})),
        mountpoint=dict(path=str(aws.MOUNTPOINT), exists=False, is_directory=None, is_symlink=False, entries=[]),
        nvme_identity=dict(model="Amazon Elastic Block Store    ", serial=VOLUME.replace("-", "")))
    return owner, provider, evidence


def filesystem(evidence):
    evidence["lsblk"]["blockdevices"][-1].update(fstype="ext4", uuid=FS)
    evidence["probe"] = dict(wipefs=dict(returncode=0, signatures=[dict(type="ext4", uuid=FS)]),
        blkid=dict(returncode=0, fields=dict(TYPE="ext4", UUID=FS)))


def ready(owner):
    value = dict(**aws.database_identity(owner), kind=aws.KIND, status="ready", rootrel="colabfold",
        filesystem_type="ext4", size_bytes=aws.SIZE_BYTES, source_content_manifest_sha256="3"*64,
        content_manifest_sha256="4"*64, completion=dict(full_readback=True))
    raw = aws.canonical(value)+b"\n"
    return raw, hashlib.sha256(raw).hexdigest()


class AwsDeviceTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.provider, self.evidence = fixture()

    def initialize(self):
        return aws.device_plan("initialize", self.owner, self.provider, self.evidence, now=NOW)

    def test_exact_ebs_serial_selects_nvme1_not_root_or_instance_store_without_execution(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("No plan may execute")):
            plan = self.initialize()
        self.assertEqual(plan["device"], "/dev/nvme1n1")
        self.assertEqual(plan["os_device"], "/dev/nvme3n1")
        self.assertEqual(plan["volume_id"], VOLUME)
        self.assertEqual(plan["command"][-1], "/dev/nvme1n1")
        self.assertNotIn("-F", plan["command"])
        self.assertEqual(plan["provider"], "aws")

    def test_device_reordering_does_not_change_binding(self):
        self.evidence["lsblk"]["blockdevices"].reverse()
        self.assertEqual(self.initialize()["device"], "/dev/nvme1n1")

    def test_actual_create_describe_second_precision_preserves_exact_device_plan(self):
        baseline = self.initialize()
        pairs = [
            ("2026-09-14T04:05:37+00:00", "2026-09-14T04:05:37.003000+00:00"),
            ("2026-09-14T04:05:37.003000+00:00", "2026-09-14T04:05:37+00:00"),
            ("2026-09-14T04:05:37.000Z", "2026-09-14T04:05:37.999Z"),
            ("2026-09-14T00:05:37-04:00", "2026-09-14T04:05:37.003Z"),
            ("2026-09-14T04:05:37.003Z", "2026-09-14T04:05:37.003000+00:00"),
        ]
        with patch.object(subprocess, "run", side_effect=AssertionError("No plan may execute")):
            for created, observed in pairs:
                with self.subTest(created=created, observed=observed):
                    self.owner["creation"]["response"]["CreateTime"] = created
                    self.provider["volumes"][-1]["CreateTime"] = observed
                    expected = dict(baseline, ownership_sha256=aws.digest(self.owner),
                                    provider_sha256=aws.digest(self.provider))
                    self.assertEqual(self.initialize(), expected)

    def test_creation_precision_does_not_accept_conflicting_times(self):
        pairs = [
            ("2026-09-14T04:05:37.003Z", "2026-09-14T04:05:37.004Z"),
            ("2026-09-14T04:05:37.999Z", "2026-09-14T04:05:38Z"),
            ("2026-09-14T04:05:37Z", "2026-09-14T04:05:38Z"),
            ("2026-09-14T04:05:37", "2026-09-14T04:05:37.003Z"),
            (None, "2026-09-14T04:05:37.003Z"),
        ]
        for created, observed in pairs:
            with self.subTest(created=created, observed=observed):
                self.owner["creation"]["response"]["CreateTime"] = created
                self.provider["volumes"][-1]["CreateTime"] = observed
                with self.assertRaises(linux.InspectionRequired):
                    self.initialize()

    def test_truncated_creation_time_still_requires_owned_blank_ebs(self):
        mutations = [lambda o, p, e: o["creation"]["response"].update(VolumeId=ROOT),
            lambda o, p, e: p["volumes"][-1].update(SnapshotId="snap-0123456789abcdef0"),
            lambda o, p, e: p["volumes"][-1]["Tags"].pop(),
            lambda o, p, e: filesystem(e)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.owner, self.provider, self.evidence = fixture()
                self.owner["creation"]["response"]["CreateTime"] = "2026-09-14T04:05:37Z"
                self.provider["volumes"][-1]["CreateTime"] = "2026-09-14T04:05:37.003Z"
                mutation(self.owner, self.provider, self.evidence)
                with self.assertRaises(linux.InspectionRequired):
                    self.initialize()

    def test_wrong_serial_model_missing_root_duplicate_and_boot_fail_closed(self):
        mutations = [lambda e: e["nvme_identity"].update(model="Amazon EC2 NVMe Instance Storage"),
            lambda e: e["nvme_identity"].update(serial=ROOT),
            lambda e: e["lsblk"]["blockdevices"][-1].update(serial=ROOT),
            lambda e: e["lsblk"]["blockdevices"][0].update(serial=VOLUME),
            lambda e: e["lsblk"]["blockdevices"][1].update(serial="unmapped"),
            lambda e: e.update(boot_id=FS), lambda e: e.update(holders=["dm-0"]),
            lambda e: e["mounts"][0].update(major_minor="259:1")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.evidence = fixture()[2]
                mutation(self.evidence)
                with self.assertRaises(linux.InspectionRequired):
                    self.initialize()

    def test_creation_snapshot_other_receipt_tags_geometry_and_existing_fs_never_format(self):
        mutations = [lambda o, p, e: o["creation"]["request"].update(SnapshotId="snap-0123456789abcdef0"),
            lambda o, p, e: o["creation"].update(reconciled=True),
            lambda o, p, e: o["creation"]["response"].update(VolumeId=ROOT),
            lambda o, p, e: o["creation"]["response"].update(CreateTime="2020-01-01T00:00:00Z"),
            lambda o, p, e: p["volumes"][-1]["Tags"].pop(),
            lambda o, p, e: p["volumes"][-1].update(Size=True),
            lambda o, p, e: filesystem(e),
            lambda o, p, e: e["lsblk"]["blockdevices"][-1].update(pttype="gpt"),
            lambda o, p, e: e["probe"]["wipefs"].update(signatures=[dict(type="xfs")]),
            lambda o, p, e: o.update(ready_receipt_sha256="1"*64)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.owner, self.provider, self.evidence = fixture()
                mutation(self.owner, self.provider, self.evidence)
                with self.assertRaises(linux.InspectionRequired):
                    self.initialize()

    def test_provider_account_attachment_root_and_freshness_must_match(self):
        mutations = [lambda p: p.update(account_id="000000000000"),
            lambda p: p.update(region="us-west-2"),
            lambda p: p.update(observed_epoch=NOW-121), lambda p: p.update(observed_epoch=NOW+6),
            lambda p: p["instances"][0].update(RootDeviceName="/dev/sdf"),
            lambda p: p["volumes"][-1]["Attachments"][0].update(DeleteOnTermination=True),
            lambda p: p["volumes"][-1]["Attachments"].append(dict(InstanceId="i-0123456789abcdef1")),
            lambda p: p["volumes"][-1]["Attachments"][0].update(State="attaching")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.provider = fixture()[1]
                mutation(self.provider)
                with self.assertRaises(linux.InspectionRequired):
                    self.initialize()
        self.provider = fixture()[1]
        self.provider["observed_epoch"] = NOW+0.734
        self.assertEqual(self.initialize()["device"], "/dev/nvme1n1")

    def test_readonly_mount_requires_full_ready_pin_uuid_and_clean_mountpoint(self):
        filesystem(self.evidence)
        raw, pin = ready(self.owner)
        plan = aws.device_plan("mount-serve", self.owner, self.provider, self.evidence, now=NOW, ready=raw, ready_sha256=pin)
        self.assertIn("ro,noload,nodev,nosuid,noexec", plan["command"])
        self.assertIn("UUID="+FS, plan["command"])
        for mutation in (lambda e: e["mountpoint"].update(entries=["user-file"]),
                         lambda e: e["probe"]["blkid"]["fields"].update(UUID=BOOT)):
            changed = copy.deepcopy(self.evidence)
            mutation(changed)
            with self.assertRaises(linux.InspectionRequired):
                aws.device_plan("mount-serve", self.owner, self.provider, changed, now=NOW, ready=raw, ready_sha256=pin)
        with self.assertRaises(linux.InspectionRequired):
            aws.device_plan("mount-serve", self.owner, self.provider, self.evidence, now=NOW, ready=raw, ready_sha256="0"*64)


class AwsMountTests(unittest.TestCase):
    def test_real_mountinfo_schema_uuid_nvme_and_access_are_all_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device_number = root.stat().st_dev
            major = f"{os.major(device_number)}:{os.minor(device_number)}"
            owner = fixture()[0]
            row = dict(mountpoint=str(root), filesystem_type="ext4", major_minor=major,
                       options=["ro", "nodev", "nosuid", "noexec"], super_options=["ro", "norecovery"], source="/dev/nvme1n1")
            original_lstat, original_stat = Path.lstat, Path.stat
            def lstat(path, *args, **kwargs):
                if str(path) == "/dev/nvme1n1":
                    return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=device_number)
                return original_lstat(path, *args, **kwargs)
            def path_stat(path, *args, **kwargs):
                if str(path) == "/dev/disk/by-uuid/"+FS:
                    return SimpleNamespace(st_rdev=device_number)
                return original_stat(path, *args, **kwargs)
            native = dict(mn="Amazon Elastic Block Store", sn=VOLUME.replace("-", ""))
            def run(argv):
                return SimpleNamespace(stdout=json.dumps(native) if argv[0] == "nvme" else str(aws.SIZE_BYTES))
            with patch.object(aws, "MOUNTPOINT", root), patch.object(content, "mount_record", return_value=row), \
                 patch.object(Path, "lstat", lstat), patch.object(Path, "stat", path_stat), patch.object(linux, "_run", side_effect=run), \
                 patch.object(linux, "_mounts", return_value=[dict(major_minor=major, target=str(root))]) as mounts:
                self.assertEqual(aws.mounted_cache(root, owner, "/dev/nvme1n1", True), row)
                native["sn"] = ROOT
                with self.assertRaises(linux.InspectionRequired):
                    aws.mounted_cache(root, owner, "/dev/nvme1n1", True)
                native["sn"] = VOLUME
                row["super_options"] = ["ro"]
                with self.assertRaises(linux.InspectionRequired):
                    aws.mounted_cache(root, owner, "/dev/nvme1n1", True)
                row["super_options"] = ["ro", "norecovery"]
                mounts.return_value.append(dict(major_minor=major, target="/another-rw-mount"))
                with self.assertRaises(linux.InspectionRequired):
                    aws.mounted_cache(root, owner, "/dev/nvme1n1", True)


class AwsTransferTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing realistic tiny full-component/alias/mmCIF fixture,
        # while all AWS publication and content hashing below runs unmocked.
        fixture_case = cache_fixtures.CacheTests()
        fixture_case.setUp()
        self.addCleanup(fixture_case.doCleanups)
        self.source, self.target, self.base = fixture_case.source, fixture_case.target, fixture_case.base
        self.owner = fixture()[0]
        self.owner["source_receipt_sha256"] = content.generation(self.source)["source_receipt_sha256"]
        self.out = self.base/"head-inventory"
        self.inventory = aws.source_inventory(self.source, self.out)
        self.inventory_path = self.out/"inventory.json"
        self.pin = self.inventory["inventory_sha256"]
        self.mount = patch.object(aws, "mounted_cache", return_value={"verified_fixture": True})
        self.mount.start()
        self.addCleanup(self.mount.stop)

    def seal(self):
        return aws.seal_source(self.source, self.inventory_path, self.pin, self.base/"source-hashes")

    def transfer(self):
        plan = aws.transfer_plan(self.source, self.inventory_path, self.pin, self.out,
                                 "root@192.0.2.1", self.base/"key", self.base/"known-hosts")
        pending = self.target/aws.PENDING
        pending.mkdir(exist_ok=True)
        # Exercise the exact rsync options/filelists locally; SSH transport and
        # the final remote operand alone are replaced by a local destination.
        for argv in plan["commands"]:
            command = argv.copy()
            i = command.index("-e")
            del command[i:i+2]
            command[-1] = str(pending)+"/"
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return plan

    def publish(self, sealed=None):
        sealed = sealed or self.seal()
        return aws.publish(self.target, self.owner, Path(sealed["source_manifest"]),
                           sealed["source_manifest_sha256"], "/dev/test")

    def test_complete_transfer_preserves_aliases_and_excludes_nonpublished_data(self):
        plan = self.transfer()
        self.assertEqual(plan["parallel_workers"], 4)
        for argv in plan["commands"]:
            self.assertIn("--inplace", argv)
            self.assertIn("--partial", argv)
            self.assertNotIn("--delete", argv)
            self.assertNotIn("--append", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv[argv.index("-e")+1])
        receipt = self.publish()
        self.assertEqual(receipt["ready"]["provider"], "aws")
        self.assertTrue(receipt["ready"]["completion"]["full_readback"])
        self.assertFalse((self.target/aws.PENDING).exists())
        self.assertFalse((self.target/"colabfold/.archives").exists())
        served = dict(self.owner, ready_receipt_sha256=receipt["ready_receipt_sha256"])
        result = aws.verify(self.target, served, "/dev/test")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(aws.verify(self.target, served, "/dev/test", full=True)["verification"], "full-sha256")

    def test_transfer_resume_repairs_truncated_large_file_in_place(self):
        self.transfer()
        path = next((self.target/aws.PENDING/"environmental").glob("*.idx"))
        source = self.source/path.relative_to(self.target/aws.PENDING)
        path.write_bytes(source.read_bytes()[:3])
        self.transfer()
        self.assertEqual(path.read_bytes(), source.read_bytes())
        self.publish()

    def test_source_seal_resume_reuses_only_unchanged_fully_hashed_files(self):
        first = self.seal()
        with patch.object(content, "hash_handle", side_effect=AssertionError("Completed source was rehashed")):
            second = self.seal()
        self.assertEqual(first["source_manifest_sha256"], second["source_manifest_sha256"])
        path = next((self.source/"environmental").glob("*.idx"))
        raw = path.read_bytes()
        path.write_bytes(raw[:-1]+bytes([raw[-1] ^ 1]))
        with self.assertRaises(RuntimeError):
            self.seal()

    def test_corrupt_same_size_middle_or_extra_tree_never_publish(self):
        sealed = self.seal()
        self.transfer()
        path = next((self.target/aws.PENDING/"environmental").glob("*.idx"))
        original = path.read_bytes()
        path.write_bytes(original[:-1]+bytes([original[-1]^1]))
        with self.assertRaises(linux.InspectionRequired):
            self.publish(sealed)
        self.assertFalse((self.target/"ready.json").exists())
        path.write_bytes(original)
        (self.target/aws.PENDING/"foreign.txt").write_text("user data")
        with self.assertRaises(linux.InspectionRequired):
            self.publish(sealed)
        self.assertFalse((self.target/"ready.json").exists())

    def test_source_manifest_and_shard_coverage_require_exact_pins(self):
        with self.assertRaises(linux.InspectionRequired):
            aws.seal_source(self.source, self.inventory_path, "0"*64, self.base/"state")
        (self.out/"files-1.txt0").write_bytes((self.out/"files-0.txt0").read_bytes())
        with self.assertRaises(linux.InspectionRequired):
            aws.transfer_plan(self.source, self.inventory_path, self.pin, self.out,
                              "root@192.0.2.1", self.base/"key", self.base/"known-hosts")

    def test_mixed_ranges_rsync_roundtrip_keeps_full_publication_proof(self):
        import aws_transfer
        import io
        with patch.object(aws, 'RANGE_THRESHOLD', 64):
            inventory = aws.source_inventory(self.source, self.out)
            plan = aws.transfer_plan(self.source, self.inventory_path, inventory['inventory_sha256'], self.out,
                                     'root@192.0.2.1', self.base/'key', self.base/'known-hosts')
            self.assertTrue(plan['ranged_entries'])
            self.assertEqual(plan['range_streams'], 32)
            self.assertEqual(plan['range_bytes']+plan['rsync_bytes'], plan['payload_bytes'])
            pending = self.target/aws.PENDING; pending.mkdir()
            for argv in plan['commands']:
                command = argv.copy(); i = command.index('-e'); del command[i:i+2]
                command[-1] = str(pending)+'/'
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for row in plan['ranged_entries']:
                item = aws_transfer.make_plan(aws, self.source, self.inventory_path, inventory['inventory_sha256'],
                    self.owner, '/dev/test', row['path'], 32)
                aws_transfer.initialize(pending, row['path'], row['source_metadata']['size'], item['parents'])
                with (self.source/row['path']).open('rb') as stream:
                    for span in item['ranges']:
                        stream.seek(span['offset'])
                        aws_transfer.receive(pending, row['path'], row['source_metadata']['size'], span,
                                             io.BytesIO(stream.read(span['length'])))
                content.preserve(pending/row['path'], row['source_metadata'])
            self.assertFalse((self.target/'ready.json').exists())
            receipt = self.publish()
            self.assertTrue(receipt['ready']['completion']['full_readback'])
            # A ranged file cannot also be delegated to rsync.
            with (self.out/'files-0.txt0').open('ab') as stream:
                stream.write(plan['ranged_entries'][0]['path'].encode()+b'\0')
            with self.assertRaisesRegex(linux.InspectionRequired, 'exclusively cover'):
                aws.transfer_plan(self.source, self.inventory_path, inventory['inventory_sha256'], self.out,
                                  'root@192.0.2.1', self.base/'key', self.base/'known-hosts')

    def reuse_fixture(self):
        import aws_transfer
        self.transfer()
        sealed = self.seal()
        path = next((self.target/aws.PENDING/'environmental').glob('*.idx'))
        name = str(path.relative_to(self.target/aws.PENDING))
        plan = aws_transfer.make_plan(aws, self.source, self.inventory_path, self.pin,
                                     self.owner, '/dev/test', name)
        plan_path = self.out/'reuse-plan.json'; pin = aws.write_json(plan_path, plan)
        return path, sealed, plan_path, pin

    def test_completed_file_reuse_is_fresh_and_checkpoint_matches_full_publish(self):
        path, sealed, plan, pin = self.reuse_fixture()
        call = lambda **kw: aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin, **kw)
        self.assertEqual(call()['status'], 'candidate')
        before = content.metadata(path)
        actual_hash = content.hash_handle
        with patch.object(content, 'hash_handle', wraps=actual_hash) as reads:
            for _ in range(2):
                result = call(manifest=sealed['source_manifest'], manifest_sha256=sealed['source_manifest_sha256'])
                self.assertEqual(result['status'], 'reused')
                self.assertEqual(result['readback_bytes'], path.stat().st_size)
            self.assertEqual(reads.call_count, 2, 'Admission must ignore older cached readbacks')
        self.assertEqual(content.metadata(path), before, 'Adoption must not touch metadata')
        def no_second_read(handle, *args):
            self.assertNotEqual(os.fstat(handle.fileno()).st_ino, before['inode'], 'Full-plan checkpoint was missed')
            return actual_hash(handle, *args)
        with patch.object(content, 'hash_handle', side_effect=no_second_read):
            published = self.publish(sealed)
        self.assertTrue(published['ready']['completion']['full_readback'])

    def test_partial_and_false_finished_metadata_never_skip_content_verification(self):
        path, sealed, plan, pin = self.reuse_fixture()
        before = content.metadata(path)
        path.write_bytes(b'X'*before['size'])
        with patch.object(content, 'hash_handle', side_effect=AssertionError('Obvious partial should not be hashed')):
            self.assertEqual(aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin)['reason'], 'unfinished_metadata')
        content.preserve(path, before)
        result = aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin,
                               manifest=sealed['source_manifest'], manifest_sha256=sealed['source_manifest_sha256'])
        self.assertEqual(result['reason'], 'checksum_mismatch')
        self.assertEqual(result['reused_verified_bytes'], 0)
        self.assertEqual(result['readback_bytes'], before['size'])
        self.assertFalse((self.target/'ready.json').exists())

    def test_reuse_rejects_wrong_source_volume_links_and_posthash_mutation(self):
        path, sealed, plan, pin = self.reuse_fixture()
        with self.assertRaises(linux.InspectionRequired):
            aws.reuse_file(self.target, dict(self.owner, volume_id=ROOT), '/dev/test', plan, pin)
        changed = aws.read_pinned(sealed['source_manifest'], sealed['source_manifest_sha256'])
        changed['inventory_sha256'] = '0'*64
        wrong = self.out/'wrong-sealed.json'; wrong_pin = aws.write_json(wrong, changed)
        with self.assertRaisesRegex(linux.InspectionRequired, 'source identity'):
            aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin, manifest=wrong, manifest_sha256=wrong_pin)
        link = path.parent/'linked'; os.link(path, link)
        with self.assertRaisesRegex(RuntimeError, 'private regular'):
            aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin)
        link.unlink()
        result = aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin,
                               manifest=sealed['source_manifest'], manifest_sha256=sealed['source_manifest_sha256'])
        self.assertEqual(result['status'], 'reused')
        path.write_bytes(b'X'*path.stat().st_size)
        content.preserve(path, result['metadata'])
        with self.assertRaises(linux.InspectionRequired):
            self.publish(sealed)

    def test_cancelled_existing_readback_reaps_reader_without_checkpoint(self):
        path, sealed, plan, pin = self.reuse_fixture()
        import time
        ended = threading.Event()
        def reading(handle, progress):
            try:
                while not progress.cancelled.wait(.01):
                    pass
                raise RuntimeError('reader cancelled')
            finally:
                ended.set()
        def cancel(_):
            raise RuntimeError('coordinator cancelled')
        started = time.monotonic()
        with patch.object(content, 'hash_handle', side_effect=reading):
            with self.assertRaisesRegex(RuntimeError, 'coordinator cancelled'):
                aws.reuse_file(self.target, self.owner, '/dev/test', plan, pin,
                    manifest=sealed['source_manifest'], manifest_sha256=sealed['source_manifest_sha256'], callback=cancel)
        self.assertTrue(ended.is_set())
        self.assertLess(time.monotonic()-started, 3)
        self.assertFalse((self.target/aws.STATE/'destination-hashes.sqlite').exists())

    def test_publish_recovery_after_final_rename_retains_full_file_proof(self):
        self.transfer()
        sealed = self.seal()
        original = aws.write_json
        def interrupted(path, value):
            if Path(path).name == "ready.json":
                raise OSError("simulated process loss before ready")
            return original(path, value)
        with patch.object(aws, "write_json", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.publish(sealed)
        self.assertTrue((self.target/"colabfold").exists())
        self.assertFalse((self.target/"ready.json").exists())
        self.assertEqual(aws.population_state(self.target, self.owner, "/dev/test")["population_state"], "renamed")
        with patch.object(content, "hash_handle", side_effect=AssertionError("Unchanged destination rehashed")):
            result = self.publish(sealed)
        self.assertTrue(result["ready"]["completion"]["full_readback"])

    def test_lost_publish_reply_reverifies_exact_sealed_source_without_recopy(self):
        self.assertEqual(aws.population_state(self.target, self.owner, "/dev/test")["population_state"], "absent")
        self.transfer()
        self.assertEqual(aws.population_state(self.target, self.owner, "/dev/test")["population_state"], "pending")
        sealed = self.seal()
        result = self.publish(sealed)
        state = aws.population_state(self.target, self.owner, "/dev/test")
        self.assertEqual(state["population_state"], "published")
        self.assertFalse(state["publication_verified"])
        with patch.object(content, "hash_handle", side_effect=AssertionError("Unchanged verified payload was rehashed")):
            resumed = self.publish(sealed)
        self.assertTrue(resumed["already_ready"])
        self.assertEqual(resumed["ready_receipt_sha256"], result["ready_receipt_sha256"])
        path = next((self.target/"colabfold/environmental").glob("*.idx"))
        path.write_bytes(path.read_bytes()[:-1]+b"X")
        with self.assertRaises(linux.InspectionRequired):
            self.publish(sealed)
        (self.target/aws.PENDING).mkdir()
        with self.assertRaises(linux.InspectionRequired):
            aws.population_state(self.target, self.owner, "/dev/test")

    def test_ready_and_published_content_mutation_prevent_later_serve(self):
        self.transfer()
        result = self.publish()
        owner = dict(self.owner, ready_receipt_sha256=result["ready_receipt_sha256"])
        path = next((self.target/"colabfold/uniref30").glob("*.idx"))
        path.write_bytes(path.read_bytes()+b"corruption")
        with self.assertRaises(RuntimeError):
            aws.verify(self.target, owner, "/dev/test")
        wrong = dict(owner, volume_id=ROOT)
        with self.assertRaises(linux.InspectionRequired):
            aws.verify(self.target, wrong, "/dev/test")


class AwsRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base/"bio-worker-runtime"
        self.tools = Path(__file__).resolve().parents[3]/"modules/bio"
        stage = self.base/"original"
        native = stage/"envs/msa-tools-v1/bin"
        native.mkdir(parents=True)
        programs = (("mmseqs", databases.MMSEQS_COMMIT, "MMSEQS_SHA256"),
                    ("mmseqs-server", databases.BACKEND_COMMIT, "BACKEND_SHA256"))
        for name, version, key in programs:
            path = native/name
            path.write_text("#!/bin/sh\nprintf '%s\\n' '"+version+"'\n")
            path.chmod(0o755)
            patched = patch.object(databases, key, hashlib.sha256(path.read_bytes()).hexdigest())
            patched.start()
            self.addCleanup(patched.stop)
        archive = self.base/"runtime.tar"
        with tarfile.open(archive, "w") as stream:
            stream.add(stage/"envs/msa-tools-v1", arcname="envs/msa-tools-v1")
        self.archive = self.base/"runtime.tar.zst"
        subprocess.run(["zstd", "-q", str(archive), "-o", str(self.archive)], check=True)
        plan = dict(schema=1, recipe="msa", paths=["envs/msa-tools-v1"], source_fingerprint="1"*64,
            source_bytes=sum(p.stat().st_size for p in native.iterdir()), source_files=2)
        plan["package"] = dict(plan, format="bio-runtime-tar-zstd-v1", archive_bytes=self.archive.stat().st_size,
                               archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.plan = self.base/"plan.json"
        self.pin = aws.write_json(self.plan, plan)
        patched = patch.object(aws, "RUNTIME_ROOT", self.root)
        patched.start()
        self.addCleanup(patched.stop)

    def restore(self):
        return aws.restore_runtime(self.archive, self.plan, self.pin, self.tools, self.root)

    def test_existing_verified_extractor_restores_exact_native_binaries_and_repeat(self):
        first = self.restore()
        self.assertEqual(first["search_profile"]["profile_id"], "resident-768gib-v1")
        self.assertEqual(first["search_profile"]["warm_mode"], "prefetch")
        self.assertEqual(first["search_profile"]["mmseqs_threads"], 16)
        self.assertEqual(first["bind_commands"], [["mount", "--bind", str(self.root/"envs/msa-tools-v1"),
                                                  "/mnt/bio-shared/envs/msa-tools-v1"]])
        inode = (self.root/"envs/msa-tools-v1/bin/mmseqs").stat().st_ino
        self.assertEqual(self.restore()["receipt"], first["receipt"])
        self.assertEqual((self.root/"envs/msa-tools-v1/bin/mmseqs").stat().st_ino, inode)

    def test_corrupt_archive_or_plan_rejected_before_extraction(self):
        raw = self.archive.read_bytes()
        self.archive.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with self.assertRaises(linux.InspectionRequired):
            self.restore()
        self.assertFalse(self.root.exists())
        self.archive.write_bytes(raw)
        self.pin = "0"*64
        with self.assertRaises(linux.InspectionRequired):
            self.restore()

    def test_unowned_existing_runtime_and_modified_native_binary_fail_closed(self):
        self.root.mkdir()
        (self.root/"user.txt").write_text("do not remove")
        with self.assertRaises(FileNotFoundError):
            self.restore()
        self.assertEqual((self.root/"user.txt").read_text(), "do not remove")
        shutil.rmtree(self.root)
        self.restore()
        (self.root/"envs/msa-tools-v1/bin/mmseqs").write_text("changed binary")
        with self.assertRaises(RuntimeError):
            self.restore()


if __name__ == "__main__":
    unittest.main()
