"""Retained negotiated host-key tests; no SSH connection or key scan occurs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import session
import session_client as client
import test_session_client as fixtures

# Public keys generated for this fixture only; no corresponding private keys are retained.
KEYS = {
    'ed25519': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDHsUT5QDdMvUdc1eh6r4OYkuaxpRSIcmApPuTPFZjyB',
    'rsa': 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCl+0+yLjjMgq3tRMLgodslebA3I3QFvHGxIK/IAh/IQ/PvqQpwBX+HmvuKSo9ODJ4uA/e2aujlbQ6S6+dVLkjG2FNLtQKOzH/M6DWEO/T2BgBikavLpHWW+Rz/Bz/LnUebf5KG/6PEjB2vGp2Bc1z2+04IbpAlprMJLW4YSw9UwFAywv71kzPS92Z69cPvjIsnLz1W/4jrnrmBi3WoskI4eJu37dCJ6T5j10QMGyFT/k0UGSdN7p/11mMlAdk5MOU4HF3c2LSDHFg7K2ckD29BOeBVGZQxRcKgCgz8mXRjLXVT0jFGBlAcATTVRcooU+RXG8X6pvbIbIjgHwN+pDDJ',
    'ecdsa': 'ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEG85g4MGm2ZjicYfReZRSaY2oyNKxtk//eOC4rbVduJz60aCwUdaFbJAvK+U2CUCVy3Jd8n3Gdt77jhw7ssiTg=',
}


class HostKeyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ClientTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root
        self.job_path=self.root/'job'/'job.json';self.job_path.parent.mkdir()
        self.known=self.job_path.parent/'worker-known-hosts'
        self.job={'job':'msa-fixture','model':'msa','instance':'exact-worker','ip':'192.0.2.1',
                  'search_profile':client.search_profile.resolve(client.search_profile.MAPPED_PROFILE)}

    def key(self,algorithm='ed25519',ip='192.0.2.1'):
        raw=(ip+' '+KEYS[algorithm]+'\n').encode()
        self.known.write_bytes(raw);self.known.chmod(0o600)
        self.job['ssh_host_key']={'known_hosts':str(self.known),'sha256':hashlib.sha256(raw).hexdigest(),
                                  'instance':self.job['instance'],'ip':self.job['ip'],'trust':'first-successful-ssh'}
        session.atomic(self.job_path,self.job)
        return raw

    def test_ed25519_rsa_only_and_ecdsa_records_are_accepted_verbatim(self):
        for algorithm in KEYS:
            with self.subTest(algorithm=algorithm):
                raw=self.key(algorithm)
                self.assertEqual(client.retained_host_keys(self.known,self.job_path,self.job),raw)

    def test_host_hash_instance_and_path_mismatches_are_rejected(self):
        raw=self.key()
        for field,value in [('sha256','0'*64),('instance','other-worker'),('known_hosts','/other/job/worker-known-hosts')]:
            saved=dict(self.job['ssh_host_key']);self.job['ssh_host_key'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                client.retained_host_keys(self.known,self.job_path,self.job)
            self.job['ssh_host_key']=saved
        self.key(ip='192.0.2.2')
        with self.assertRaisesRegex(ValueError,'another host'):
            client.retained_host_keys(self.known,self.job_path,self.job)

    def test_symlink_public_file_and_invalid_key_blob_are_rejected(self):
        self.key();self.known.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'unsafe'):
            client.retained_host_keys(self.known,self.job_path,self.job)
        raw=b'192.0.2.1 ssh-ed25519 invalid!\n';self.known.write_bytes(raw);self.known.chmod(0o600)
        self.job['ssh_host_key']['sha256']=hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError,'encoding'):
            client.retained_host_keys(self.known,self.job_path,self.job)
        self.known.unlink();target=self.root/'target';target.write_text('untrusted');self.known.symlink_to(target)
        with self.assertRaises((ValueError,OSError)):
            client.retained_host_keys(self.known,self.job_path,self.job)

    def test_registration_uses_retained_rsa_and_strict_authenticated_probe_without_scan(self):
        raw=self.key('rsa')
        with mock.patch.object(client.shutil,'which',return_value=str(self.fixture.submit)), \
             mock.patch.object(client.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')):
            started=client.start(self.fixture.args)
        state=Path(started['state']);intent=session.load(state/'intent.json')
        proof={'hostname':'bio-fixture','instance':'exact-worker','os_id':'exact-os'}
        observed=[]
        def check(command,**kwargs):
            observed.append(command)
            self.assertEqual(command[0],'ssh')
            self.assertNotIn('ssh-keyscan',command)
            self.assertIn('StrictHostKeyChecking=yes',command)
            self.assertIn('GlobalKnownHostsFile=/dev/null',command)
            self.assertIn('UpdateHostKeys=no',command)
            self.assertEqual((state/'known_hosts').read_bytes(),raw)
            return json.dumps({'boot_id':'fixture-boot','hostname':'bio-fixture'})
        with mock.patch.dict(os.environ,{'INVOCATION_ID':'b'*32,'BIO_MSA_SESSION_ID':intent['session_id']}), \
             mock.patch.object(client,'unit_state',return_value={'InvocationID':'b'*32,'ActiveState':'active'}), \
             mock.patch.object(client,'provider_check',return_value=proof), \
             mock.patch.object(client.subprocess,'check_output',side_effect=check):
            launch=client.register_launch(state,self.job_path,'/mnt/bio-shared/runs/msa-fixture/out',self.known)
        self.assertEqual(len(observed),1)
        self.assertEqual(launch['known_hosts_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(launch['boot_id'],'fixture-boot')
        self.assertEqual(session.load(state/'launch.json'),launch)

    def test_registration_rejects_changed_key_before_ssh_probe(self):
        self.key();self.known.write_text('changed after successful connection')
        with mock.patch.object(client.shutil,'which',return_value=str(self.fixture.submit)), \
             mock.patch.object(client.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')):
            started=client.start(self.fixture.args)
        state=Path(started['state']);intent=session.load(state/'intent.json')
        with mock.patch.dict(os.environ,{'INVOCATION_ID':'b'*32,'BIO_MSA_SESSION_ID':intent['session_id']}), \
             mock.patch.object(client,'unit_state',return_value={'InvocationID':'b'*32,'ActiveState':'active'}), \
             mock.patch.object(client,'provider_check',return_value={}), \
             mock.patch.object(client.subprocess,'check_output') as ssh,self.assertRaisesRegex(ValueError,'bytes changed'):
            client.register_launch(state,self.job_path,'/mnt/bio-shared/runs/msa-fixture/out',self.known)
        ssh.assert_not_called();self.assertFalse((state/'launch.json').exists())

    def test_new_registration_rejects_missing_or_different_profile_before_provider_or_ssh(self):
        self.key()
        with mock.patch.object(client.shutil,'which',return_value=str(self.fixture.submit)), \
             mock.patch.object(client.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')):
            started=client.start(self.fixture.args)
        state=Path(started['state'])
        for profile in (None, client.search_profile.resolve(client.search_profile.LEGACY_PROFILE)):
            with self.subTest(profile=profile):
                if profile is None: self.job.pop('search_profile',None)
                else: self.job['search_profile']=profile
                session.atomic(self.job_path,self.job)
                with mock.patch.object(client,'provider_check') as provider, \
                     mock.patch.object(client.subprocess,'check_output') as ssh, \
                     self.assertRaisesRegex(ValueError,'search profile'):
                    client.register_launch(state,self.job_path,'/mnt/bio-shared/runs/msa-fixture/out',self.known)
                provider.assert_not_called();ssh.assert_not_called()
                self.assertFalse((state/'launch.json').exists())


if __name__=='__main__':unittest.main()
