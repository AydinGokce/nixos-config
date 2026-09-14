import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import block_cache_device as device


NOW = 1789344060.0
CACHE = "a" * 32
VOLUME = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OS = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
INSTANCE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
BOOT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
FS = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
OTHER = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def disk(path, major, size, *, kind="disk", **extra):
    return dict(name=Path(path).name, kname=Path(path).name, path=path, type=kind,
                size=size, **{"maj:min": major}, fstype=None, uuid=None, pttype=None,
                mountpoints=[None], pkname=None, serial="", wwn="", ro=False, **extra)


def fixture():
    ownership = dict(schema=1, cache_id=CACHE, volume_id=VOLUME, filesystem_uuid=FS,
        cache_generation="1"*64, source_manifest_sha256="2"*64, source_receipt_sha256="3"*64,
        ready_receipt_sha256=None,
        creation=dict(method="POST", path="/volumes", response_id=VOLUME,
            attempted_at="2026-09-14T00:00:00Z", request=dict(type="NVMe", size=1300,
            location_code="FIN-02", name="bio-msa-cache-"+CACHE, instance_ids=[], tags=[])))
    provider = dict(observed_epoch=NOW, managed=dict(token="b"*32, instance_id=INSTANCE,
        os_volume_id=OS, boot_id=BOOT), instances=[dict(id=INSTANCE, status="running",
        os_volume_id=OS, volume_ids=[VOLUME])], trash=[], volumes=[
            dict(id=OS, target="vda", instance_id=INSTANCE, instances=[dict(id=INSTANCE)],
                 is_os_volume=True, type="NVMe", status="attached"),
            dict(id=VOLUME, target="vdc", instance_id=INSTANCE, instances=[dict(id=INSTANCE)],
                 is_os_volume=False, type="NVMe", size=1300, status="attached",
                 location="FIN-02", name="bio-msa-cache-"+CACHE, created_at="2026-09-14T00:00:10Z")])
    osdisk = disk("/dev/vda", "252:0", 92*1024**3)
    osroot = disk("/dev/vda1", "252:1", 90*1024**3, kind="part")
    osroot.update(fstype="ext4", uuid=OTHER, pkname="vda", mountpoints=["/"])
    osdisk["children"] = [osroot]
    unrelated = disk("/dev/vdb", "252:16", device.SIZE_BYTES)
    unrelated.update(fstype="ext4", uuid=OTHER, mountpoints=["/user-data"])
    target = disk("/dev/vdc", "252:32", device.SIZE_BYTES)
    evidence = dict(schema=1, observed_epoch=NOW, boot_id=BOOT, device="/dev/vdc",
        stat=dict(is_block=True, is_symlink=False, major_minor="252:32"),
        lsblk=dict(blockdevices=[osdisk, unrelated, target]), holders=[], mounts=[
            dict(major_minor="252:1", target="/", options=["rw"], fstype="ext4", source="/dev/vda1"),
            dict(major_minor="252:16", target="/user-data", options=["rw"], fstype="ext4", source="/dev/vdb")],
        probe=dict(wipefs=dict(returncode=0, signatures=[]), blkid=dict(returncode=2, fields={})),
        mountpoint=dict(path=device.MOUNTPOINT, exists=False, is_directory=None, is_symlink=False, entries=[]))
    return ownership, provider, evidence


def filesystem(evidence):
    target = evidence["lsblk"]["blockdevices"][-1]
    target.update(fstype="ext4", uuid=FS)
    evidence["probe"] = dict(wipefs=dict(returncode=0, signatures=[dict(type="ext4", uuid=FS)]),
        blkid=dict(returncode=0, fields=dict(TYPE="ext4", UUID=FS, LABEL="bio-msa-cache")))


def ready(ownership):
    value = {k: ownership[k] for k in ("cache_id", "volume_id", "filesystem_uuid", "cache_generation",
                                       "source_manifest_sha256", "source_receipt_sha256")}
    value.update(schema=1, kind="msa-full-database-block-cache", status="ready", filesystem_type="ext4",
                 size_bytes=device.SIZE_BYTES, completion=dict(full_readback=True))
    raw = (json.dumps(value, indent=2)+"\n").encode()
    return raw, hashlib.sha256(raw).hexdigest()


class CacheDeviceTests(unittest.TestCase):
    def setUp(self):
        self.ownership, self.provider, self.evidence = fixture()

    def plan(self):
        return device.initialize_plan(self.ownership, self.provider, self.evidence, now=NOW)

    def test_provider_target_selects_vdc_and_never_first_similar_disk_or_os(self):
        with patch.object(device.subprocess, "run", side_effect=AssertionError("Plans must never execute")):
            result = self.plan()
        self.assertEqual(result["device"], "/dev/vdc")
        self.assertEqual(result["command"][-1], "/dev/vdc")
        self.assertEqual(result["filesystem_uuid"], FS)
        self.assertEqual(result["size_bytes"], 1395864371200)
        self.assertEqual(result["action"], "initialize")
        self.assertNotIn("-F", result["command"])
        self.assertEqual(result["ownership_sha256"], device.digest(self.ownership))
        self.assertEqual(result["expires_epoch"], NOW+30)

    def test_guest_nvme_name_works_only_when_provider_names_that_exact_device(self):
        self.provider["volumes"][-1]["target"] = "nvme3n1"
        self.evidence["device"] = "/dev/nvme3n1"
        self.evidence["lsblk"]["blockdevices"][-1].update(path="/dev/nvme3n1", name="nvme3n1", kname="nvme3n1")
        self.assertEqual(self.plan()["command"][-1], "/dev/nvme3n1")

    def test_different_missing_or_unsafe_provider_target_never_falls_back_by_size(self):
        for target in (None, "", "vda", "vdb", "vdc1", "../vdc", "vdc; touch /tmp/x", "/dev/vdc"):
            with self.subTest(target=target):
                self.provider["volumes"][-1]["target"] = target
                with self.assertRaises(device.InspectionRequired):
                    self.plan()

    def test_creation_must_prove_new_owned_volume_not_clone_existing_or_wrong_receipt(self):
        changes = [lambda o: o["creation"].update(method="PUT"),
                   lambda o: o["creation"].update(path="/volumes/clone"),
                   lambda o: o["creation"].update(response_id=OTHER),
                   lambda o: o["creation"]["request"].update(source_volume_id=OTHER),
                   lambda o: o["creation"]["request"].update(name="existing-user-data"),
                   lambda o: o["creation"]["request"].update(size=True),
                   lambda o: o["creation"]["request"].update(instance_ids=[INSTANCE]),
                   lambda o: o["creation"].update(attempted_at="2026-09-15T00:00:00Z"),
                   lambda o: o.update(schema=True),
                   lambda o: o.update(filesystem_uuid="random")]
        for change in changes:
            with self.subTest(change=change):
                self.ownership = fixture()[0]; change(self.ownership)
                with self.assertRaises(device.InspectionRequired): self.plan()

    def test_provider_identity_size_lifecycle_and_lease_fail_closed(self):
        changes = [lambda p: p["managed"].update(instance_id=OTHER),
                   lambda p: p["managed"].update(os_volume_id=VOLUME),
                   lambda p: p["managed"].update(token=""),
                   lambda p: p["managed"].update(boot_id=OTHER),
                   lambda p: p["volumes"][-1].update(is_os_volume=True),
                   lambda p: p["volumes"][-1].update(type="NVMe_Shared"),
                   lambda p: p["volumes"][-1].update(size=1300*1024**3/10**9),
                   lambda p: p["volumes"][-1].update(status="attaching"),
                   lambda p: p["volumes"][-1].update(created_at="2020-01-01T00:00:00Z"),
                   lambda p: p["instances"][0].update(status="stopped"),
                   lambda p: p["instances"][0].update(volume_ids=[]),
                   lambda p: p["volumes"].append(copy.deepcopy(p["volumes"][-1])),
                   lambda p: p["trash"].append(dict(id=VOLUME))]
        for change in changes:
            with self.subTest(change=change):
                self.provider = fixture()[1]; change(self.provider)
                with self.assertRaises(device.InspectionRequired): self.plan()

    def test_attachment_union_detects_other_vm_and_conflicting_device_targets(self):
        for style in ("instances", "singular", "inverse", "target"):
            self.provider = fixture()[1]
            if style == "instances":
                self.provider["volumes"][-1]["instances"].append(OTHER)
            elif style == "singular":
                self.provider["volumes"][-1]["instance_id"] = OTHER
            elif style == "inverse":
                self.provider["instances"].append(dict(id=OTHER, status="running", volume_ids=[VOLUME], os_volume_id=OTHER))
            else:
                self.provider["volumes"].append(dict(id=OTHER, target="vdc", instance_id=INSTANCE, instances=[INSTANCE]))
            with self.subTest(style=style), self.assertRaises(device.InspectionRequired): self.plan()

    def test_raw_attachment_schema_accepts_string_or_object_and_uses_inverse_refs(self):
        self.provider["volumes"][-1]["instances"] = [INSTANCE]
        self.assertEqual(self.plan()["volume_id"], VOLUME)
        self.provider["volumes"][-1]["instances"] = []
        self.assertEqual(self.plan()["volume_id"], VOLUME)

    def test_stale_future_nonfinite_observations_and_reboot_are_rejected(self):
        for value in (NOW-121, NOW+6, float("nan"), float("inf"), True, "today"):
            for key in ("provider", "evidence"):
                self.provider, self.evidence = fixture()[1:]
                getattr(self, key)["observed_epoch"] = value
                with self.subTest(value=value, key=key), self.assertRaises(device.InspectionRequired): self.plan()
        self.provider, self.evidence = fixture()[1:]
        self.evidence["boot_id"] = OTHER
        with self.assertRaises(device.InspectionRequired): self.plan()

    def test_retained_seed_clock_skew_is_tolerated_only_at_cross_host_boundary(self):
        # Actual seed01: fresh head envelope was observed at this float, while
        # its guest-written retained binding had this earlier filesystem mtime.
        head_observed, guest_now = 1789356045.0227118, 1789356044.289
        self.provider["observed_epoch"] = head_observed
        bound = device.provider_binding(self.ownership, self.provider, guest_now)
        self.assertEqual(bound["device"], "/dev/vdc")
        self.evidence["observed_epoch"] = guest_now
        plan = device.initialize_plan(self.ownership, self.provider, self.evidence, now=guest_now)
        self.assertAlmostEqual(plan["provider_age_seconds"], -0.7337117195129395)
        self.assertEqual(plan["provider_clock_skew_allowance_seconds"], 5)
        # The same future timestamp is never accepted as local device evidence.
        self.evidence["observed_epoch"] = head_observed
        with self.assertRaises(device.InspectionRequired):
            device.initialize_plan(self.ownership, self.provider, self.evidence, now=guest_now)
        for observed in (guest_now+5.001, guest_now-120.001):
            self.provider["observed_epoch"] = observed
            with self.subTest(observed=observed), self.assertRaises(device.InspectionRequired):
                device.provider_binding(self.ownership, self.provider, guest_now)

    def test_exact_guest_bytes_guard_catches_decimal_1300gb_and_bool(self):
        for value in (1300*10**9, device.SIZE_BYTES-512, device.SIZE_BYTES+512, True, str(device.SIZE_BYTES)):
            self.evidence["lsblk"]["blockdevices"][-1]["size"] = value
            with self.subTest(value=value), self.assertRaises(device.InspectionRequired): self.plan()

    def test_os_root_topology_must_match_provider_os_target(self):
        for major in ("252:32", "252:16", "0:99"):
            self.evidence["mounts"][0]["major_minor"] = major
            with self.subTest(major=major), self.assertRaises(device.InspectionRequired): self.plan()

    def test_partitions_filesystems_swap_mounts_holders_and_path_replacement_block_format(self):
        changes = [lambda e: e["lsblk"]["blockdevices"][-1].update(children=[disk("/dev/vdc1", "252:33", 100, kind="part")]),
                   lambda e: e["lsblk"]["blockdevices"][-1].update(pttype="gpt"),
                   lambda e: e["lsblk"]["blockdevices"][-1].update(fstype="LVM2_member"),
                   lambda e: e["lsblk"]["blockdevices"][-1].update(uuid=FS),
                   lambda e: e["lsblk"]["blockdevices"][-1].update(ro=True),
                   lambda e: e["lsblk"]["blockdevices"][-1].update(mountpoints=["[SWAP]"]),
                   lambda e: e["lsblk"]["blockdevices"][-1].update(mountpoints=None),
                   lambda e: e["holders"].append("dm-0"),
                   lambda e: e["stat"].update(is_symlink=True),
                   lambda e: e["stat"].update(major_minor="252:16"),
                   lambda e: e["mounts"].append(dict(major_minor="252:32", target="/hidden-use"))]
        for change in changes:
            self.evidence = fixture()[2]; change(self.evidence)
            with self.subTest(change=change), self.assertRaises(device.InspectionRequired): self.plan()

    def test_both_independent_blank_device_probes_must_succeed(self):
        for wipefs, blkid in ((dict(returncode=1, signatures=[]), dict(returncode=2, fields={})),
                             (dict(returncode=0, signatures=[dict(type="gpt")]), dict(returncode=2, fields={})),
                             (dict(returncode=0, signatures=[]), dict(returncode=4, fields={})),
                             (dict(returncode=0, signatures=[]), dict(returncode=0, fields={})),
                             (dict(returncode=0, signatures=[]), dict(returncode=2, fields=dict(PTTYPE="gpt")))):
            self.evidence["probe"] = dict(wipefs=wipefs, blkid=blkid)
            with self.subTest(probe=self.evidence["probe"]), self.assertRaises(device.InspectionRequired): self.plan()

    def test_existing_ext4_is_verified_without_ever_reformatting(self):
        filesystem(self.evidence)
        with self.assertRaises(device.InspectionRequired): self.plan()
        result, uses = device.filesystem_binding(self.ownership, self.provider, self.evidence, now=NOW)
        self.assertEqual(result["filesystem_uuid"], FS)
        self.assertEqual(uses, [])

    def test_previously_ready_cache_cannot_be_initialized_even_if_signatures_disappeared(self):
        self.ownership["ready_receipt_sha256"] = "e"*64
        with self.assertRaises(device.InspectionRequired): self.plan()

    def test_mount_plan_uses_unique_uuid_and_exact_retained_ready_bytes(self):
        filesystem(self.evidence)
        raw, pin = ready(self.ownership)
        self.ownership["ready_receipt_sha256"] = pin
        with patch.object(device.subprocess, "run", side_effect=AssertionError("Plans must never execute")):
            result = device.mount_plan(self.ownership, self.provider, self.evidence, raw, pin, now=NOW)
        self.assertEqual(result["command"], ["mount", "-t", "ext4", "-o", "ro,noload,nodev,nosuid,noexec",
                                              "UUID="+FS, device.MOUNTPOINT])
        self.assertEqual(result["ready_sha256"], pin)
        with self.assertRaises(device.InspectionRequired):
            device.mount_plan(self.ownership, self.provider, self.evidence, device.canonical(json.loads(raw)), pin, now=NOW)

    def test_ready_other_volume_generation_or_registered_pin_cannot_mount(self):
        filesystem(self.evidence)
        raw, pin = ready(self.ownership)
        for key, value in (("volume_id", OTHER), ("filesystem_uuid", OTHER), ("cache_id", "b"*32),
                           ("size_bytes", 1300*10**9), ("filesystem_type", "xfs"), ("schema", True),
                           ("status", "copying"), ("completion", dict(full_readback=False)),
                           ("cache_generation", "9"*64), ("source_manifest_sha256", "9"*64),
                           ("source_receipt_sha256", "9"*64)):
            changed = dict(json.loads(raw), **{key: value}); encoded = device.canonical(changed)
            with self.subTest(key=key), self.assertRaises(device.InspectionRequired):
                device.mount_plan(self.ownership, self.provider, self.evidence, encoded, hashlib.sha256(encoded).hexdigest(), now=NOW)
        self.ownership["ready_receipt_sha256"] = "f"*64
        with self.assertRaises(device.InspectionRequired):
            device.mount_plan(self.ownership, self.provider, self.evidence, raw, pin, now=NOW)

    def test_uuid_must_match_lsblk_blkid_wipefs_and_be_unique(self):
        changes = [lambda e: e["lsblk"]["blockdevices"][-1].update(uuid=OTHER),
                   lambda e: e["probe"]["blkid"]["fields"].update(UUID=OTHER),
                   lambda e: e["probe"]["blkid"]["fields"].update(PTTYPE="gpt"),
                   lambda e: e["probe"]["wipefs"]["signatures"].append(dict(type="gpt", uuid=FS)),
                   lambda e: e["lsblk"]["blockdevices"][1].update(uuid=FS)]
        for change in changes:
            self.evidence = fixture()[2]; filesystem(self.evidence); change(self.evidence)
            with self.subTest(change=change), self.assertRaises(device.InspectionRequired):
                device.filesystem_binding(self.ownership, self.provider, self.evidence, now=NOW)

    def test_never_mount_over_existing_data_symlink_or_another_filesystem(self):
        raw, pin = ready(self.ownership)
        changes = [lambda e: e["mountpoint"].update(entries=["user-data"]),
                   lambda e: e["mountpoint"].update(is_symlink=True),
                   lambda e: e["mountpoint"].update(exists=True, is_directory=False),
                   lambda e: e["mounts"].append(dict(major_minor="0:55", target=device.MOUNTPOINT)),
                   lambda e: e["mounts"].append(dict(major_minor="0:55", target=device.MOUNTPOINT+"/colabfold"))]
        for change in changes:
            self.evidence = fixture()[2]; filesystem(self.evidence); change(self.evidence)
            with self.subTest(change=change), self.assertRaises(device.InspectionRequired):
                device.mount_plan(self.ownership, self.provider, self.evidence, raw, pin, now=NOW)

    def mounted(self):
        filesystem(self.evidence)
        self.evidence["lsblk"]["blockdevices"][-1]["mountpoints"] = [device.MOUNTPOINT]
        self.evidence["mounts"].append(dict(major_minor="252:32", target=device.MOUNTPOINT,
            fstype="ext4", source="/dev/vdc", options=["ro", "nodev", "nosuid", "noexec", "norecovery"]))

    def test_readonly_mount_verification_requires_nonreplaying_single_exact_mount(self):
        self.mounted(); raw, pin = ready(self.ownership)
        result = device.validate_mount(self.ownership, self.provider, self.evidence, raw, pin, now=NOW)
        self.assertEqual(result["status"], "validated_mount")
        changes = [lambda e: e["mounts"][-1].update(target="/wrong"),
                   lambda e: e["mounts"][-1]["options"].append("rw"),
                   lambda e: e["mounts"][-1]["options"].remove("norecovery"),
                   lambda e: e["mounts"].append(copy.deepcopy(e["mounts"][-1]))]
        for change in changes:
            self.evidence = fixture()[2]; self.mounted(); change(self.evidence)
            with self.subTest(change=change), self.assertRaises(device.InspectionRequired):
                device.validate_mount(self.ownership, self.provider, self.evidence, raw, pin, now=NOW)

    def test_mountinfo_parser_preserves_kernel_superblock_options_and_escapes(self):
        value = device._mounts("1 0 252:32 / /mnt/name\\040with\\040space ro,nodev,nosuid,noexec - ext4 /dev/vdc ro,norecovery\n")
        self.assertEqual(value[0]["target"], "/mnt/name with space")
        self.assertIn("norecovery", value[0]["options"])

    def test_inspection_rejects_regular_file_symlink_or_generic_device_glob_before_any_command(self):
        for path in ("/dev/*", "/dev/vda1", "/tmp/test-device", "/dev/disk/by-id/unknown"):
            with self.subTest(path=path), patch.object(device.subprocess, "run", side_effect=AssertionError("No command")), \
                 self.assertRaises(device.InspectionRequired): device.inspect(path)
        for mode in (stat.S_IFREG, stat.S_IFLNK):
            with patch.object(Path, "lstat", return_value=type("Stat", (), dict(st_mode=mode))()), \
                 patch.object(device.subprocess, "run", side_effect=AssertionError("No command")), \
                 self.assertRaises(device.InspectionRequired): device.inspect("/dev/vdc")

    def collect_fixture(self, *, change_after=False, probe_error=False):
        calls = []
        observed = copy.deepcopy(self.evidence["lsblk"])
        fake_stat = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=os.makedev(252, 32), st_ino=9)
        mountinfo = "1 0 252:1 / / rw - ext4 /dev/vda1 rw\n2 1 252:16 / /user-data rw - ext4 /dev/vdb rw\n"
        def run(arguments, accepted=(0,)):
            calls.append(arguments)
            if arguments[0] == "lsblk":
                if change_after and len(calls) > 1: observed["blockdevices"][-1]["size"] -= 512
                return SimpleNamespace(stdout=json.dumps(observed), returncode=0)
            if arguments[0] == "wipefs":
                self.assertIn("--no-act", arguments)
                return SimpleNamespace(stdout='{"signatures": []}', returncode=0)
            if arguments[0] == "blkid":
                self.assertIn("--probe", arguments)
                return SimpleNamespace(stdout="", stderr="read error" if probe_error else "", returncode=2)
            self.fail("Unexpected device command: "+str(arguments))
        def read(path):
            if str(path) == "/proc/self/mountinfo": return mountinfo
            if str(path) == "/proc/sys/kernel/random/boot_id": return BOOT+"\n"
            self.fail("Unexpected file read: "+str(path))
        with patch.object(device, "_run", side_effect=run), patch.object(Path, "lstat", return_value=fake_stat), \
             patch.object(Path, "read_text", read), patch.object(Path, "iterdir", return_value=iter([])), \
             patch.object(Path, "exists", return_value=False), patch.object(Path, "resolve", lambda self, **kw: self), \
             patch.object(device.time, "time", return_value=NOW):
            result = device.inspect("/dev/vdc")
        return result, calls

    def test_collector_uses_two_stable_inventories_and_only_readonly_probes(self):
        evidence, calls = self.collect_fixture()
        self.assertEqual([call[0] for call in calls], ["lsblk", "wipefs", "blkid", "lsblk"])
        result = device.initialize_plan(self.ownership, self.provider, evidence, now=NOW)
        self.assertEqual(result["device"], "/dev/vdc")

    def test_collector_refuses_device_change_between_readonly_probes(self):
        with self.assertRaises(device.InspectionRequired): self.collect_fixture(change_after=True)

    def test_blkid_exit_two_with_read_error_is_not_blankness_proof(self):
        with self.assertRaises(device.InspectionRequired): self.collect_fixture(probe_error=True)

    def test_cli_returns_inspection_required_without_any_device_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in (("ownership", self.ownership), ("provider", self.provider), ("evidence", self.evidence)):
                (root/name).write_bytes(device.canonical(value))
            result = subprocess.run([sys.executable, str(Path(device.__file__)), "initialize-plan",
                "--ownership", str(root/"ownership"), "--provider", str(root/"provider"), "--evidence", str(root/"evidence")],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)  # Deliberately stale, retained fixture.
            self.assertEqual(json.loads(result.stdout)["status"], "inspection_required")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["evidence", "ownership", "provider"])


if __name__ == "__main__":
    unittest.main()
