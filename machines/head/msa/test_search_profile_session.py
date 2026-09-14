import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import search_profile
import server
import session


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.database = dict(prefixes={key: str(self.root/key) for key in ("uniref30", "environmental", "pdb100")},
            pdb70=str(self.root/"templates"), pdbdivided=str(self.root/"divided"), pdbobsolete=str(self.root/"obsolete"))
        self.tools = dict(mmseqs="/pinned/mmseqs", server="/pinned/server", receipt="unchanged-tools")

    def tearDown(self): self.tmp.cleanup()

    def test_profiles_bind_execution_limits_without_altering_database_or_search_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(server.databases, "validate", return_value=self.database), \
             mock.patch.object(server.databases, "tools", return_value=self.tools):
            os.environ["MMSEQS_NUM_THREADS"] = "16"
            old_config, old = server.configuration(self.root, self.root/"results", self.root)
            self.assertNotIn("search_profile", old)
            mapped_config, mapped = server.configuration(self.root, self.root/"results", self.root,
                                                        profile=search_profile.MAPPED_PROFILE)
            resident_config, resident = server.configuration(self.root, self.root/"results", self.root,
                                                            profile=search_profile.LEGACY_PROFILE)
        self.assertEqual(mapped["database"], old["database"])
        self.assertEqual(mapped["tools"], old["tools"])
        self.assertEqual(mapped["search_settings"], old["search_settings"])
        self.assertEqual(mapped["runtime"]["mmseqs_threads"], 4)
        self.assertEqual(resident["runtime"]["mmseqs_threads"], 16)
        self.assertEqual(len({item["namespace"] for item in (old, mapped, resident)}), 3)
        for config in (old_config, mapped_config, resident_config):
            config["paths"].pop("results")
        self.assertEqual(old_config, mapped_config)
        self.assertEqual(old_config, resident_config)

    def test_inherited_profile_enforces_effective_native_environment(self):
        with mock.patch.dict(os.environ, {search_profile.ENVIRONMENT_KEY: search_profile.MAPPED_PROFILE,
                  "MMSEQS_NUM_THREADS": "128", "OMP_THREAD_LIMIT": "128"}, clear=True), \
             mock.patch.object(server.databases, "validate", return_value=self.database), \
             mock.patch.object(server.databases, "tools", return_value=self.tools):
            config, provenance = server.configuration(self.root, self.root/"results", self.root)
            self.assertEqual(provenance["runtime"]["environment"]["OMP_THREAD_LIMIT"], "4")
            self.assertEqual(search_profile.validate_configuration(config, provenance, search_profile.MAPPED_PROFILE), search_profile.resolve(search_profile.MAPPED_PROFILE))


class ProfileBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.profile = search_profile.resolve(search_profile.MAPPED_PROFILE)
        self.config = dict(app="colabfold", server=dict(address="127.0.0.1:8080"),
            local=dict(workers=1), worker=dict(paralleldatabases=1),
            paths=dict(databases=str(self.root), colabfold=dict(parallelstages=False)))
        self.database = dict(manifest_sha256="full-database-fixture")
        self.provenance = dict(search_profile=self.profile, database=self.database, tools=dict(server="/pinned/server"),
            runtime=dict(mmseqs_threads=4, environment=search_profile.configure_environment(self.profile, {})))
        self.config_path = self.root/"config.json"; self.provenance_path = self.root/"provenance.json"
        session.atomic(self.config_path, self.config); session.atomic(self.provenance_path, self.provenance)
        self.memory = dict(schema=1, kind="private-msa-cgroup-memory", profile_sha256=self.profile["profile_sha256"],
            cgroup_path="/fixture.scope", memory_max_bytes=96*1024**3, memory_swap_max_bytes=0)
        self.process = dict(pid=123, boot_id="fixture-boot", start_ticks=1)
        self.guest = search_profile.check_guest(self.profile,
            dict(MemTotal=f"{118*1024**2} kB", MemAvailable=f"{109*1024**2} kB"))

    def tearDown(self): self.tmp.cleanup()

    def adoption(self):
        value = dict(schema=1, kind="borrowed-private-msa-api", original_work_deadline_epoch=time.time()+600,
            config=dict(path=str(self.config_path), sha256=session.sha(self.config_path)),
            provenance=dict(path=str(self.provenance_path), sha256=session.sha(self.provenance_path)),
            api_argv=["/pinned/server", "-local", "-config", str(self.config_path)], api=self.process,
            panel=dict(identity=self.process, manifest="fixture", manifest_sha256="fixture"))
        path = self.root/"adoption.json"; session.atomic(path, value)
        return path

    def test_adoption_rejects_wrong_profile_or_unshared_memory_before_using_api(self):
        path = self.adoption()
        with mock.patch.object(session.databases, "validate", return_value=self.database), \
             mock.patch.object(session.panel, "manifest"), \
             mock.patch.object(search_profile, "check_process_environment") as environment, \
             mock.patch.object(search_profile, "check_cgroup", return_value=self.memory) as memory, \
             mock.patch.object(session, "BorrowedAPI", return_value="borrowed") as borrowed:
            result = session.adopted_api(path, self.root, time.time()+120, profile=self.profile)
            self.assertEqual(result[3], "borrowed")
            environment.assert_called_once_with(123, self.profile)
            memory.side_effect = [self.memory, dict(self.memory, cgroup_path="/other.scope")]
            with self.assertRaisesRegex(ValueError, "share"):
                session.adopted_api(path, self.root, time.time()+120, profile=self.profile)
            self.assertEqual(borrowed.call_count, 1)
            memory.side_effect = None
            old = copy.deepcopy(self.provenance); del old["search_profile"]
            old["runtime"]["mmseqs_threads"] = 16
            session.atomic(self.provenance_path, old); path = self.adoption()
            with self.assertRaisesRegex(ValueError, "different search profile"):
                session.adopted_api(path, self.root, time.time()+120, profile=self.profile)
            self.assertEqual(borrowed.call_count, 1)
            # Explicit legacy adoption still accepts the prior resident receipt.
            self.assertEqual(session.adopted_api(path, self.root, time.time()+120,
                profile=search_profile.LEGACY_PROFILE)[3], "borrowed")

    def publish_ready(self, ready):
        session.atomic(self.root/"ready.json", ready)
        session.atomic(self.root/"health.json", dict(session_id=ready["session_id"],
            ready_sha256=session.sha(self.root/"ready.json"), status="ready", checked_epoch=time.time()))

    def ready(self):
        session.atomic(self.root/"warm-index.json", dict(mode="report", loading=None, locked=False))
        value = dict(schema=1, kind="private-msa-session", session_id="a"*32, owner=self.process, api=self.process,
            deadline_epoch=time.time()+300, config=str(self.config_path), config_sha256=session.sha(self.config_path),
            provenance=str(self.provenance_path), provenance_sha256=session.sha(self.provenance_path),
            output=str(self.root), warm_sha256=session.sha(self.root/"warm-index.json"),
            search_profile=self.profile, guest_memory=self.guest, memory_limit=self.memory, lifecycle="owned-api")
        self.publish_ready(value)
        return value

    def test_readiness_checks_profile_memory_and_warm_receipt_without_readmitting_busy_ram(self):
        ready = self.ready()
        with mock.patch.object(session, "identity", return_value=self.process), \
             mock.patch.object(search_profile, "check_cgroup", return_value=self.memory) as memory:
            self.assertEqual(session.check_ready(self.root), ready)
            memory.return_value = dict(self.memory, cgroup_path="/escaped.scope")
            with self.assertRaisesRegex(ValueError, "memory scope changed"): session.check_ready(self.root)
            memory.return_value = self.memory
            session.atomic(self.root/"warm-index.json", dict(mode="prefetch", loading=dict(bytes=1), locked=False))
            with self.assertRaisesRegex(ValueError, "residency receipt changed"): session.check_ready(self.root)
            ready["warm_sha256"] = session.sha(self.root/"warm-index.json"); self.publish_ready(ready)
            with self.assertRaisesRegex(ValueError, "report-only"): session.check_ready(self.root)

    def test_historical_ready_without_profile_still_uses_original_binding(self):
        ready = self.ready()
        for key in ("search_profile", "guest_memory", "memory_limit"):
            ready.pop(key)
        self.publish_ready(ready)
        with mock.patch.object(session, "identity", return_value=self.process), \
             mock.patch.object(search_profile, "check_cgroup") as memory:
            self.assertEqual(session.check_ready(self.root), ready)
        memory.assert_not_called()

    def test_new_readiness_requires_exact_profile_receipt_not_null_or_only_name(self):
        ready = self.ready()
        with mock.patch.object(session, "identity", return_value=self.process), \
             mock.patch.object(search_profile, "check_cgroup", return_value=self.memory):
            for value in (None, self.profile['profile_id'], True):
                with self.subTest(value=value):
                    self.publish_ready(dict(ready, search_profile=value))
                    with self.assertRaisesRegex(ValueError, "exact search profile receipt"):
                        session.check_ready(self.root)


if __name__ == "__main__": unittest.main()
