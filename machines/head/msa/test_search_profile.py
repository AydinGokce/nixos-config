import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import search_profile as profiles


class ProfileTests(unittest.TestCase):
    def test_resident_default_requires_explicit_opt_in_for_mapped_experiment(self):
        self.assertEqual(profiles.DEFAULT_PROFILE, profiles.LEGACY_PROFILE)
        self.assertEqual(profiles.resolve(), profiles.resolve(profiles.LEGACY_PROFILE))
        self.assertEqual(profiles.warm_mode(), 'prefetch')
        self.assertEqual(profiles.configure_environment(environ={})['MMSEQS_NUM_THREADS'], '16')
        self.assertIsNone(profiles.check_cgroup())
        self.assertEqual(profiles.resolve(profiles.MAPPED_PROFILE)['profile_sha256'],
                         'e3dfc3af6e0669fb7cc0ba39bcf472c7e896e38e6aeb45dbb9fa784a668f27a8')

    def setUp(self):
        self.mapped = profiles.resolve(profiles.MAPPED_PROFILE)
        self.legacy = profiles.resolve(profiles.LEGACY_PROFILE)

    def test_exact_definition_hash_and_copies_cannot_change_shared_policy(self):
        definition = dict(self.mapped)
        digest = definition.pop("profile_sha256")
        self.assertEqual(digest, hashlib.sha256(profiles.canonical(definition)).hexdigest())
        self.assertEqual(profiles.resolve(self.mapped), self.mapped)
        self.mapped["mmseqs_threads"] = 16
        self.assertEqual(profiles.resolve(profiles.MAPPED_PROFILE)["mmseqs_threads"], 4)
        for changed in (self.mapped, dict(profiles.resolve(profiles.MAPPED_PROFILE), api_workers=True),
                        dict(profiles.resolve(profiles.MAPPED_PROFILE), extra=1), dict(profiles.resolve(profiles.MAPPED_PROFILE), minimum_total_gib=float("nan"))):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                profiles.resolve(changed)
        for unknown in ("unknown", {}, [], 128, True):
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                profiles.resolve(unknown)

    def test_nominal_128gb_and_legacy_bytes_are_distinct_from_gib(self):
        self.assertEqual(self.mapped["minimum_advertised_bytes"], 128*10**9)
        self.assertAlmostEqual(self.mapped["minimum_advertised_bytes"]/profiles.GIB, 119.20928955078125)
        self.assertEqual(self.legacy["minimum_advertised_bytes"], 768*profiles.GIB)
        self.assertEqual(self.mapped["memory_max_gib"], 96)
        self.assertEqual(self.mapped["memory_swap_max_bytes"], 0)
        self.assertIsNone(self.legacy["memory_max_gib"])
        self.assertIsNone(profiles.check_cgroup(self.legacy))

    def test_guest_admission_accepts_nominal_128gb_with_guest_overhead(self):
        meminfo = dict(MemTotal=str(118*1024**2)+" kB", MemAvailable=str(109*1024**2)+" kB")
        receipt = profiles.check_guest(self.mapped, meminfo)
        self.assertEqual(receipt["total_bytes"], 118*profiles.GIB)
        self.assertEqual(profiles.validate_guest_receipt(receipt, self.mapped), receipt)
        for total, available in ((109, 100), (118, 99), (110, 111)):
            with self.subTest(total=total, available=available), self.assertRaises(ValueError):
                profiles.check_guest(self.mapped, dict(MemTotal=f"{total*1024**2} kB", MemAvailable=f"{available*1024**2} kB"))
        for value in ("128 GB", "-1 kB", "nan kB", "Infinity kB", "118000000", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profiles.check_guest(self.mapped, dict(meminfo, MemTotal=value))
        for changed in (dict(receipt, profile_sha256="0"*64), dict(receipt, available_bytes=True),
                        dict(receipt, minimum_total_bytes=1), dict(receipt, extra="wrong")):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                profiles.validate_guest_receipt(changed, self.mapped)
        with self.assertRaises(ValueError):
            profiles.check_guest(self.legacy, meminfo)

    def test_profile_warm_modes_preserve_legacy_and_reject_full_mapped_load(self):
        self.assertEqual(profiles.warm_mode(self.mapped), "report")
        for mode in ("prefetch", "lock", "unknown"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                profiles.warm_mode(self.mapped, mode)
        self.assertEqual(profiles.warm_mode(self.legacy), "prefetch")
        self.assertEqual(profiles.warm_mode(self.legacy, "lock"), "lock")
        self.assertEqual(profiles.warm_mode(self.legacy, "report"), "report")

    def test_environment_overrides_all_native_thread_caps_before_child_launch(self):
        environ = dict(MMSEQS_NUM_THREADS="192", OMP_NUM_THREADS="192", OMP_THREAD_LIMIT="192",
                       OMP_DYNAMIC="TRUE", UNRELATED="preserved")
        expected = profiles.configure_environment(self.mapped, environ)
        self.assertEqual(expected, dict(MMSEQS_NUM_THREADS="4", OMP_NUM_THREADS="4",
                                       OMP_THREAD_LIMIT="4", OMP_DYNAMIC="FALSE"))
        self.assertEqual(environ["UNRELATED"], "preserved")
        self.assertEqual(environ[profiles.ENVIRONMENT_KEY], profiles.MAPPED_PROFILE)
        child_environment = dict(os.environ, **environ)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], env=child_environment)
        try:
            self.assertEqual(profiles.check_process_environment(child.pid, self.mapped), expected)
            with self.assertRaises(ValueError):
                profiles.check_process_environment(child.pid, self.legacy)
        finally:
            child.terminate(); child.wait(timeout=5)

    def fixture_configuration(self, profile=None):
        profile = self.mapped if profile is None else profile
        config = dict(app="colabfold", local=dict(workers=1), worker=dict(paralleldatabases=1),
                      paths=dict(colabfold=dict(parallelstages=False)))
        provenance = dict(search_profile=profile, runtime=dict(mmseqs_threads=profile["mmseqs_threads"],
            environment=profiles.configure_environment(profile, {})))
        return config, provenance

    def test_configuration_rejects_concurrency_or_pipeline_changes(self):
        config, provenance = self.fixture_configuration()
        self.assertEqual(profiles.validate_configuration(config, provenance, self.mapped), self.mapped)
        for section, key, value in (("local", "workers", 2), ("local", "workers", True),
                                    ("worker", "paralleldatabases", 2)):
            changed = copy.deepcopy(config); changed[section][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                profiles.validate_configuration(changed, provenance, self.mapped)
        for key, value in (("parallelstages", True), ("parallelstages", 0), ("gpu", {}),
                           ("environmentalpair", "changed-db")):
            changed = copy.deepcopy(config); changed["paths"]["colabfold"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                profiles.validate_configuration(changed, provenance, self.mapped)
        for field in profiles.ENVIRONMENT_FIELDS:
            changed = copy.deepcopy(provenance); changed["runtime"]["environment"].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                profiles.validate_configuration(config, changed, self.mapped)
        with self.assertRaises(ValueError):
            profiles.validate_configuration(config, dict(provenance, search_profile=self.legacy), self.mapped)
        for incomplete in (None, profiles.MAPPED_PROFILE, profiles.LEGACY_PROFILE):
            with self.subTest(incomplete=incomplete), self.assertRaises(ValueError):
                profiles.validate_configuration(config, dict(provenance, search_profile=incomplete), self.mapped)
        old = copy.deepcopy(provenance); del old["search_profile"]
        with self.assertRaises(ValueError):
            profiles.validate_configuration(config, old, self.mapped, allow_legacy=True)
        old["runtime"]["mmseqs_threads"] = 16
        self.assertEqual(profiles.validate_configuration(config, old, self.legacy, allow_legacy=True), self.legacy)


class CgroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.proc = self.root/"proc"; (self.proc/"123").mkdir(parents=True)
        self.cgroups = self.root/"cgroup"; self.scope = self.cgroups/"system.slice"/"fixture.scope"
        self.scope.mkdir(parents=True)
        (self.proc/"123"/"cgroup").write_text("0::/system.slice/fixture.scope\n")
        for directory in (self.scope, self.scope.parent):
            (directory/"memory.max").write_text("max")
            (directory/"memory.swap.max").write_text("max")
        (self.scope/"memory.max").write_text(str(96*profiles.GIB))
        (self.scope/"memory.swap.max").write_text("0")

    def tearDown(self): self.tmp.cleanup()

    def check(self):
        return profiles.check_cgroup(profiles.MAPPED_PROFILE, pid=123, proc_root=self.proc, cgroup_root=self.cgroups)

    def test_effective_limits_and_membership_are_bound(self):
        receipt = self.check()
        self.assertEqual(receipt["memory_max_bytes"], 96*profiles.GIB)
        self.assertEqual(receipt["memory_swap_max_bytes"], 0)
        self.assertEqual(receipt["cgroup_path"], "/system.slice/fixture.scope")
        self.assertEqual(receipt["profile_sha256"], profiles.resolve(profiles.MAPPED_PROFILE)["profile_sha256"])
        (self.scope/"memory.max").write_text("max")
        (self.scope.parent/"memory.max").write_text(str(96*profiles.GIB))
        self.assertEqual(self.check(), receipt)  # Effective ancestor limit is sufficient.

    def test_uncapped_larger_smaller_and_swap_limits_are_rejected(self):
        for value in ("max", str(110*profiles.GIB), str(90*profiles.GIB), "nan", "-1"):
            (self.scope/"memory.max").write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError): self.check()
        (self.scope/"memory.max").write_text(str(96*profiles.GIB))
        (self.scope.parent/"memory.max").write_text(str(80*profiles.GIB))
        with self.assertRaises(ValueError): self.check()
        (self.scope.parent/"memory.max").write_text("max")
        (self.scope/"memory.swap.max").write_text("max")
        with self.assertRaises(ValueError): self.check()

    def test_missing_controller_ambiguous_and_escaping_paths_fail_closed(self):
        for membership in ("0::/../../escape\n", "0::/system.slice/fixture.scope\n0::/other\n", "5:memory:/legacy\n"):
            (self.proc/"123"/"cgroup").write_text(membership)
            with self.subTest(membership=membership), self.assertRaises(ValueError): self.check()
        (self.proc/"123"/"cgroup").write_text("0::/system.slice/fixture.scope\n")
        (self.scope/"memory.max").unlink()
        with self.assertRaises(ValueError): self.check()


if __name__ == "__main__": unittest.main()
