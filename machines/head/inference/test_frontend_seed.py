"""A fixed Boltz worker must not redefine an omitted native CLI seed."""
from pathlib import Path
import tempfile
import unittest

from inference import frontend
from inference.common import atomic_json,configuration_id,inventory,now,sha256


class BoltzSeedProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.config={'model':'boltz2','native_config':{'seed':42},'work_dir':'/worker'}
        self.config_id=configuration_id(self.config)
        proof=self.root/'proof';proof.mkdir();atomic_json(proof/'qualified.json',{'status':'passed'})
        self.policy={'model':'boltz2','interface':'bio-submit-explicit-seed-v1','native_seed':42,'seeds':[42],
            'config_id':self.config_id,'validation_root':str(proof),'validation_files':inventory(proof)}
        atomic_json(self.root/'profiles/boltz2.json',self.policy)
        atomic_json(self.root/'configs'/(self.config_id+'.json'),self.config)
        source=Path(frontend.__file__).parent.parent
        worker={'worker_id':'boltz','config_id':self.config_id,'spool_root':str(self.root/'spool'),
            'deadline_epoch':now()+1000,'tools_root':'/tools','source_files':{
                '/tools/'+name:sha256(source/name) for name in ('inference/adapters/boltz2.py','msa/prepared.py')}}
        atomic_json(self.root/'workers/boltz.json',worker)
        self.status={'state':'ready','config_id':self.config_id,'heartbeat_epoch':now(),'deadline_epoch':now()+1000}
        atomic_json(self.root/'spool/boltz/status.json',self.status)

    def test_bare_profile_refuses_fixed_seed_but_explicit_42_matches(self):
        with self.assertRaises(frontend.Unavailable):frontend.profile(self.root,'boltz2')
        self.assertEqual(frontend.profile(self.root,'boltz2',42)[0],self.policy)
        for seed in (43,True,'42'):
            with self.subTest(seed=seed),self.assertRaises(frontend.Unavailable):
                frontend.profile(self.root,'boltz2',seed)

    def test_profile_seed_list_and_loaded_config_must_match_explicit_seed(self):
        atomic_json(self.root/'profiles/boltz2.json',dict(self.policy,seeds=[43]))
        with self.assertRaisesRegex(ValueError,'inconsistent'):frontend.profile(self.root,'boltz2',42)
        config=dict(self.config,native_config={'seed':43});config_id=configuration_id(config)
        atomic_json(self.root/'profiles/boltz2.json',dict(self.policy,config_id=config_id))
        atomic_json(self.root/'configs'/(config_id+'.json'),config)
        with self.assertRaisesRegex(ValueError,'loaded native'):frontend.profile(self.root,'boltz2',42)

    def test_mislabeling_as_defaults_or_expired_worker_cannot_select_fixed_seed(self):
        atomic_json(self.root/'profiles/boltz2.json',dict(self.policy,interface='bio-submit-native-defaults-v1'))
        with self.assertRaises(frontend.Unavailable):frontend.profile(self.root,'boltz2')
        atomic_json(self.root/'profiles/boltz2.json',self.policy)
        atomic_json(self.root/'spool/boltz/status.json',dict(self.status,deadline_epoch=now()-1))
        with self.assertRaises(frontend.Unavailable):frontend.profile(self.root,'boltz2',42)


if __name__=='__main__':unittest.main()
