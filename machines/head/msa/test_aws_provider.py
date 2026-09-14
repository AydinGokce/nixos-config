"""Offline ownership, durable request, pricing and persistent-stop regressions."""
import copy
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import aws_provider as aws


class Budget:
    def __init__(self):
        self.calls = []; self.reservations = {}; self.denied = False

    def reserve(self, token, compute, storage, hours):
        self.calls.append(('reserve', token, compute, storage, hours))
        if self.denied: raise ValueError('budget denied')
        value = dict(token=token, deadline=100000, hours=hours)
        self.reservations[token] = value
        return value

    def check(self, token):
        if self.denied: raise ValueError('budget denied')
        return self.reservations[token]

    def storage(self, *args): self.calls.append(('storage', *args))
    def running(self, *args): self.calls.append(('running', *args))
    def observe_instance(self, *args): self.calls.append(('observe', *args))
    def release(self, *args): self.calls.append(('release', *args)); return dict(released=True)
    def heartbeat(self): self.calls.append(('heartbeat',)); return dict(spent=0)


class API:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.calls = []; self.instances = []; self.volumes = []; self.groups = []; self.keys = []
        self.fail_after = None; self.account = aws.ACCOUNT; self.fail_read = None
        self.quota = 512

    def call(self, service, operation, request=None):
        request = copy.deepcopy(request or {}); self.calls.append((operation, request))
        if operation == self.fail_read: raise aws.Error('unavailable')
        if operation in aws.WRITES:
            pool = aws.load(self.root/'pool.json')
            if operation != 'stop-instances':
                self.assert_durable(pool, operation, request)
        if operation == 'get-caller-identity': return {'Account': self.account}
        if operation == 'describe-instances': return {'Reservations': [{'Instances': copy.deepcopy(self.instances)}]}
        if operation == 'describe-volumes': return {'Volumes': copy.deepcopy(self.volumes)}
        if operation == 'describe-security-groups': return {'SecurityGroups': copy.deepcopy(self.groups)}
        if operation == 'describe-key-pairs': return {'KeyPairs': copy.deepcopy(self.keys)}
        if operation == 'get-service-quota': return {'Quota': {'Value': self.quota}}
        if operation == 'describe-images': return {'Images': [dict(ImageId=aws.AMI, OwnerId=aws.AMI_OWNER,
                State='available', Architecture='x86_64', RootDeviceName='/dev/sda1')]}
        if operation == 'describe-subnets': return {'Subnets': [dict(VpcId=self.config['vpc_id'], State='available',
                AvailabilityZone=self.config['availability_zone'])]}
        if operation == 'describe-instance-type-offerings': return {'InstanceTypeOfferings': [dict(InstanceType=aws.INSTANCE_TYPE,
                Location=self.config['availability_zone'])]}
        if operation == 'create-security-group':
            row = dict(GroupId='sg-11111111111111111', VpcId=request['VpcId'], Tags=request['TagSpecifications'][0]['Tags'], IpPermissions=[])
            self.groups.append(row); value = {'GroupId': row['GroupId']}
        elif operation == 'authorize-security-group-ingress':
            self.groups[0]['IpPermissions'] = request['IpPermissions']; value = {'Return': True}
        elif operation == 'import-key-pair':
            self.keys.append(dict(KeyName=request['KeyName'], Tags=request['TagSpecifications'][0]['Tags']))
            value = {'KeyName': request['KeyName']}
        elif operation == 'create-volume':
            if self.volumes:
                return copy.deepcopy(next(v for v in self.volumes if aws.tags(v).get('msa-role') == 'database'))
            row = dict(VolumeId='vol-11111111111111111', AvailabilityZone=request['AvailabilityZone'], Size=1300,
                VolumeType='gp3', Iops=8000, Throughput=2000, State='available', Attachments=[],
                CreateTime='2026-09-14T04:00:00+00:00', SnapshotId='', Tags=request['TagSpecifications'][0]['Tags'])
            self.volumes.append(row); value = row
        elif operation == 'run-instances':
            root = dict(VolumeId='vol-22222222222222222', AvailabilityZone=request['Placement']['AvailabilityZone'], Size=100,
                VolumeType='gp3', State='in-use', CreateTime='2026-09-14T04:01:00+00:00', Attachments=[], Tags=request['TagSpecifications'][1]['Tags'])
            self.volumes.append(root)
            instance = dict(InstanceId='i-33333333333333333', InstanceType=aws.INSTANCE_TYPE, ImageId=aws.AMI,
                State={'Name': 'running'}, Placement=request['Placement'], ClientToken=request['ClientToken'],
                VpcId=self.config['vpc_id'], SubnetId=self.config['subnet_id'], RootDeviceName='/dev/sda1',
                PublicIpAddress='192.0.2.10', Tags=request['TagSpecifications'][0]['Tags'],
                BlockDeviceMappings=[dict(DeviceName='/dev/sda1', Ebs=dict(VolumeId=root['VolumeId'], DeleteOnTermination=False, Status='attached'))])
            self.instances.append(instance); value = {'Instances': [instance]}
        elif operation == 'attach-volume':
            volume = next(v for v in self.volumes if v['VolumeId'] == request['VolumeId'])
            volume['State'] = 'in-use'; volume['Attachments'] = [dict(InstanceId=request['InstanceId'], State='attached', Device=request['Device'])]
            value = dict(State='attaching')
        elif operation == 'stop-instances':
            self.instances[0]['State']['Name'] = 'stopped'; value = {'StoppingInstances': []}
        elif operation == 'start-instances':
            self.instances[0]['State']['Name'] = 'running'; value = {'StartingInstances': []}
        else:
            raise AssertionError(operation)
        if operation == self.fail_after:
            self.fail_after = None
            raise aws.Error('lost response')
        return copy.deepcopy(value)

    def assert_durable(self, pool, operation, request):
        assert any(v['operation'] == operation and v['request'] == request
                   and v['status'] in ('pending', 'uncertain') for v in pool['requests'].values())


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)
        self.root = self.base/'provider'
        self.key = self.base/'key'; self.key.with_suffix('.pub').write_text('ssh-ed25519 QUJDRA== test\n')
        self.config = dict(account_id=aws.ACCOUNT, region=aws.REGION, availability_zone='us-east-1a',
            vpc_id='vpc-11111111111111111', subnet_id='subnet-11111111111111111', ssh_cidr='31.56.109.100/32',
            ssh_key=str(self.key))
        self.api, self.budget = API(self.root, self.config), Budget()
        self.controller = aws.Controller(self.api, self.budget, self.config, self.root, clock=lambda: 1000, sleep=lambda _: None)
        self.state = self.base/('a'*32); self.state.mkdir()

    def tearDown(self): self.temp.cleanup()

    def test_nixos_configuration_symlink_resolves_but_state_symlinks_remain_rejected(self):
        config = json.loads(Path(__file__).with_name('aws-config.json').read_text())
        target = self.base/'immutable-config.json'
        target.write_text(json.dumps(config))
        link = self.base/'etc-config.json'
        link.symlink_to(target)
        self.assertEqual(aws.configuration(link, require_assets=False), config)
        with self.assertRaises(OSError):
            aws.load(link)

    def launch(self):
        return self.controller.allocate(self.state, 2)

    def ready(self):
        lease = self.launch()
        with self.assertRaisesRegex(aws.Error, 'attaching'): self.controller.attach(self.state)
        pool = self.controller.pool(); pool.update(hostname='gc-msa-test', ready_receipt_sha256='b'*64)
        self.controller.save(pool)
        return lease

    def writes(self, name=None):
        return [op for op, req in self.api.calls if op in aws.WRITES and (name is None or op == name)]

    def test_persists_before_each_mutation_and_tags_every_resource(self):
        lease = self.ready(); pool = self.controller.pool()
        for row in self.api.instances+self.api.volumes:
            self.assertEqual(aws.tags(row)['Project'], 'gc-msa')
            self.assertEqual(aws.tags(row)['allocation-token'], pool['pool_id'])
        self.assertFalse(pool['instance_request']['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination'])
        self.assertEqual(pool['volume_request']['Iops'], 8000)
        self.assertEqual(pool['volume_request']['Throughput'], 2000)
        self.assertEqual(lease['status'], 'starting')
        self.assertEqual(self.controller.provider_check(self.state)['token'], lease['token'])

    def test_reuses_same_stopped_instance_and_both_volumes(self):
        first = self.ready(); original = self.controller.pool()
        receipt = self.controller.close(self.state)
        self.assertTrue(receipt['compute_stopped']); self.assertTrue(receipt['budget_released'])
        self.assertEqual(len(self.api.instances), 1); self.assertEqual(len(self.api.volumes), 2)
        state2 = self.base/('c'*32); state2.mkdir()
        second = self.controller.allocate(state2, 1)
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(self.controller.pool()['instance_id'], original['instance_id'])
        self.assertEqual(self.writes('run-instances'), ['run-instances'])
        self.assertEqual(self.writes('create-volume'), ['create-volume'])
        self.assertEqual(self.writes('start-instances'), ['start-instances'])
        reserve = [r for r in self.budget.calls if r[0] == 'reserve'][-1]
        self.assertEqual(reserve[3], 0)

    def test_lost_create_volume_response_recovers_exact_idempotent_response(self):
        self.api.fail_after = 'create-volume'
        with self.assertRaisesRegex(aws.Error, 'lost response'): self.launch()
        self.assertEqual(len(self.api.volumes), 1)
        self.launch()
        requests = [req for op, req in self.api.calls if op == 'create-volume']
        self.assertEqual(len(requests), 2); self.assertEqual(requests[0], requests[1])
        self.assertEqual(len(self.api.volumes), 2)  # One database plus one root.
        self.assertTrue(self.controller.pool()['volume_creation']['recovered_client_token'])

    def test_lost_run_instances_response_reconciles_without_duplicate(self):
        self.api.fail_after = 'run-instances'
        with self.assertRaisesRegex(aws.Error, 'lost response'): self.launch()
        self.launch()
        self.assertEqual(self.writes('run-instances'), ['run-instances'])
        self.assertEqual(len(self.api.instances), 1)

    def test_budget_denial_blocks_all_aws_writes(self):
        self.budget.denied = True
        with self.assertRaisesRegex(ValueError, 'budget denied'): self.launch()
        self.assertEqual(self.writes(), [])
        self.assertIsNotNone(aws.load(self.state/'aws-lease.json'))

    def test_wrong_aws_account_blocks_all_writes(self):
        self.api.account = '000000000000'
        with self.assertRaisesRegex(aws.Error, 'account differs'): self.launch()
        self.assertEqual(self.writes(), [])

    def test_foreign_managed_pool_is_not_adopted(self):
        self.api.volumes = [dict(VolumeId='vol-11111111111111111', Tags=[{'Key':'Project','Value':'gc-msa'}])]
        with self.assertRaisesRegex(aws.Error, 'Unclaimed'): self.launch()
        self.assertEqual(self.writes(), [])

    def test_another_session_cannot_start_or_stop_busy_worker(self):
        self.ready(); other = self.base/('c'*32); other.mkdir()
        with self.assertRaisesRegex(aws.Error, 'Another AWS MSA session'): self.controller.allocate(other, 1)
        with self.assertRaises(OSError): self.controller.close(other)
        self.assertEqual(self.writes('stop-instances'), [])

    def test_identity_and_attachment_drift_refuse_check_and_stop(self):
        self.ready()
        self.api.volumes[0]['Attachments'][0]['InstanceId'] = 'i-44444444444444444'
        with self.assertRaisesRegex(aws.Error, 'another worker'): self.controller.provider_check(self.state)
        with self.assertRaisesRegex(aws.Error, 'another worker'): self.controller.close(self.state)
        self.assertEqual(self.writes('stop-instances'), [])

    def test_stop_still_works_after_budget_exhaustion(self):
        self.ready(); self.budget.denied = True
        receipt = self.controller.close(self.state)
        self.assertEqual(receipt['instance_state'], 'stopped')
        self.assertEqual(self.writes('stop-instances'), ['stop-instances'])

    def test_old_close_cannot_claim_restarted_worker_is_stopped(self):
        self.ready(); self.controller.close(self.state)
        next_state = self.base/('c'*32); next_state.mkdir()
        self.controller.allocate(next_state, 1)
        with self.assertRaisesRegex(aws.Error, 'reused by a later'): self.controller.close(self.state)
        self.assertEqual(self.api.instances[0]['State']['Name'], 'running')

    def test_watchdog_requires_inventory_before_heartbeat(self):
        self.controller.watchdog(); self.assertEqual(self.budget.calls[-1], ('heartbeat',))
        self.api.fail_read = 'describe-volumes'
        before = len(self.budget.calls)
        with self.assertRaises(aws.Error): self.controller.watchdog()
        self.assertEqual(len(self.budget.calls), before)

    def test_watchdog_stops_exact_unreserved_restart(self):
        self.ready(); self.controller.close(self.state)
        self.api.instances[0]['State']['Name'] = 'running'
        receipt = self.controller.watchdog()
        self.assertEqual(receipt['status'], 'stopping_unreserved_compute')
        self.assertEqual(self.api.instances[0]['State']['Name'], 'stopped')

    def test_watchdog_recovers_lost_accepted_launch_binding(self):
        self.api.fail_after = 'run-instances'
        with self.assertRaises(aws.Error): self.launch()
        self.controller.watchdog()
        lease = self.controller.lease(self.state)
        self.assertEqual(lease['instance'], 'i-33333333333333333')
        self.assertEqual(self.writes('run-instances'), ['run-instances'])

    def test_capacity_reads_never_launch_and_do_not_claim_live_capacity(self):
        result = self.controller.capacity()
        self.assertFalse(result['msa_available']); self.assertEqual(self.writes(), [])
        self.ready(); self.controller.close(self.state)
        result = self.controller.capacity()
        self.assertEqual(result['state'], 'ready'); self.assertIsNone(result['msa_available'])
        self.assertEqual(result['cpus'][0]['availability'], 'eligible')
        self.assertEqual(result['cpus'][0]['live_capacity'], 'unknown')

    def test_capacity_essential_failure_is_unknown_not_negative(self):
        self.api.fail_read = 'get-service-quota'
        result = self.controller.capacity()
        self.assertEqual(result['state'], 'error'); self.assertIsNone(result['msa_available'])
        self.assertIsNone(result['checked_epoch'])

    def test_changed_pool_configuration_is_rejected(self):
        self.launch(); self.controller.config['availability_zone'] = 'us-east-1b'
        with self.assertRaisesRegex(aws.Error, 'configuration changed'): self.controller.pool()

    def test_pricing_includes_provisioned_gp3_and_persistent_os(self):
        self.assertAlmostEqual(aws.DATA_HOURLY*730, 204)
        self.assertAlmostEqual(aws.ROOT_HOURLY*730, 8)
        self.assertAlmostEqual(aws.COMPUTE_HOURLY, 7.2626)

    def test_cli_does_not_expose_credentials_or_retry_writes(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 1, b'', b'An error occurred (AccessDenied): secret-client-token-DO-NOT-PRINT')
        client = aws.AWS(runner=runner)
        with self.assertRaisesRegex(aws.Error, 'AccessDenied') as raised:
            client.call('ec2', 'create-volume', {'ClientToken':'a'*32})
        self.assertNotIn('DO-NOT-PRINT', str(raised.exception))
        self.assertEqual(calls[0][1]['env']['AWS_MAX_ATTEMPTS'], '1')
        self.assertIn('bio-aws', calls[0][0]); self.assertIn(aws.REGION, calls[0][0])
        with self.assertRaisesRegex(aws.Error, 'Unsupported'): client.call('ec2', 'delete-volume')

    def test_state_receipt_symlinks_rejected(self):
        destination = self.base/'foreign.json'; destination.write_text('{}')
        (self.root/'pool.json').symlink_to(destination)
        with self.assertRaises(OSError): self.controller.pool()
        self.assertEqual(destination.read_text(), '{}')

    def test_actual_combined_budget_handles_create_stop_and_retained_storage_reuse(self):
        import aws_budget
        now = aws.creation_epoch({'CreateTime':'2026-09-14T04:02:00Z'})
        ledger = self.base/'ledger'; ledger.mkdir()
        with patch.dict('os.environ', {}, clear=True):
            budget = aws_budget.Budget(ledger, clock=lambda: now)
            state = aws_budget.DC['new_state'](ledger/'ledger.tsv', now, '606')
            state['last_watchdog'] = now
            budget.store.save(state); budget.heartbeat()
            controller = aws.Controller(self.api, budget, self.config, self.root, clock=lambda: now, sleep=lambda _: None)
            first = controller.allocate(self.state, 2)
            with self.assertRaisesRegex(aws.Error, 'attaching'): controller.attach(self.state)
            pool = controller.pool(); pool['hostname'] = 'gc-msa-test'; controller.save(pool)
            controller.close(self.state)
            later = self.base/('d'*32); later.mkdir()
            second = controller.allocate(later, 1)
            controller.close(later)
            self.assertNotEqual(first['token'], second['token'])
            summary = budget.snapshot()
            self.assertGreaterEqual(summary['spent'], 606)
            self.assertAlmostEqual(summary['hourly'], aws.DATA_HOURLY+aws.ROOT_HOURLY)
            self.assertEqual(summary['reserved'], 0)
            self.assertEqual(len(self.api.instances), 1)

    def test_ebs_registration_uses_actual_creation_times(self):
        self.ready()
        rows = [call for call in self.budget.calls if call[0] == 'storage']
        self.assertEqual(rows[0][-1], aws.creation_epoch(self.api.volumes[0]))
        self.assertEqual(rows[1][-1], aws.creation_epoch(self.api.volumes[1]))

    def test_directory_upload_dereferences_only_root_and_keeps_exact_target(self):
        original = self.base/'frozen-tools'; original.mkdir(); (original/'example.py').write_text('pass\n')
        link = self.base/'tools'; link.symlink_to(original)
        transport = aws.Transport(self.state, self.config, '192.0.2.10')
        commands = []
        def command(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        with patch.object(transport, 'run') as remote, patch.object(aws.subprocess, 'run', side_effect=command):
            transport.upload(link, '/opt/frozen/tools')
        self.assertEqual(commands[0][-2], str(original)+'/')
        self.assertEqual(commands[0][-1], 'root@192.0.2.10:/opt/frozen/tools/')
        remote.assert_called_once()

    def test_only_explicit_tools_upload_dereferences_internal_nix_links(self):
        original = self.base/'tools'; original.mkdir()
        transport = aws.Transport(self.state, self.config, '192.0.2.10')
        with patch.object(transport, 'run'), patch.object(aws.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, b'', b'')) as invoked:
            transport.upload(original, '/opt/tools', dereference=True)
            self.assertIn('--copy-links', invoked.call_args.args[0])
            transport.upload(original, '/opt/regular')
            self.assertNotIn('--copy-links', invoked.call_args.args[0])

    def test_explicit_other_worker_is_rejected_before_allocation(self):
        aws.atomic(self.state/'intent.json', dict(session_id=self.state.name, provider_name='aws',
            kind='managed-private-msa-session', warm='prefetch', idle_seconds=900,
            search_profile={'profile_id':aws.PROFILE}, requested_worker='x2idn.16xlarge'))
        with self.assertRaisesRegex(aws.Error, 'no implicit fallback'): aws.session_intent(self.state)
        self.assertEqual(self.writes(), [])

    def test_invalid_transport_ip_cannot_enter_ssh_command(self):
        with self.assertRaises(ValueError): aws.Transport(self.state, self.config, '127.0.0.1; printf secret')

    def test_mirrored_readiness_preserves_exact_session_byte_hash(self):
        self.ready()
        output = self.base/'outputs'; output.mkdir()
        known = self.state/'worker-known-hosts'; known.write_text('192.0.2.10 ssh-ed25519 QUJDRA==\n')
        proof = self.controller.provider_check(self.state)
        aws.atomic(self.state/'launch.json', dict(session_id=self.state.name, remote_out=str(output),
            instance=proof['instance'], ip=proof['ip'], provider=proof, known_hosts_sha256=aws.sha(known)))
        ready = dict(schema=1, session_id=self.state.name, owner={'boot_id':'a-b'}, output=str(output))
        source_bytes = aws.canonical(ready)+b'\n'
        with patch.object(aws, 'session_intent', return_value={}), patch.object(aws, 'remote_output', return_value=str(output)), \
             patch.object(aws.Transport, 'run', return_value={'session-ready.json': ready}):
            response = aws.sync_output(self.controller, self.state)
        self.assertEqual(response['status'], 'synchronized')
        self.assertEqual((output/'session-ready.json').read_bytes(), source_bytes)
        self.assertEqual(aws.sha(output/'session-ready.json'), hashlib.sha256(source_bytes).hexdigest())

    def test_mirrored_metadata_rejects_cross_session_readiness(self):
        self.ready(); output = self.base/'outputs'; output.mkdir()
        known = self.state/'worker-known-hosts'; known.write_text('host-key')
        proof = self.controller.provider_check(self.state)
        aws.atomic(self.state/'launch.json', dict(session_id=self.state.name, remote_out=str(output),
            instance=proof['instance'], ip=proof['ip'], provider=proof, known_hosts_sha256=aws.sha(known)))
        with patch.object(aws, 'session_intent', return_value={}), patch.object(aws, 'remote_output', return_value=str(output)), \
             patch.object(aws.Transport, 'run', return_value={'session-ready.json': {'session_id':'e'*32}}):
            with self.assertRaisesRegex(aws.Error, 'another session'): aws.sync_output(self.controller, self.state)
        self.assertFalse((output/'session-ready.json').exists())

    def test_job_receipt_passes_existing_frozen_client_host_key_validation(self):
        import session_client
        self.ready()
        transport = aws.Transport(self.state, self.config, '192.0.2.10')
        key = (11).to_bytes(4, 'big')+b'ssh-ed25519'+(32).to_bytes(4, 'big')+b'x'*32
        raw = b'192.0.2.10 ssh-ed25519 '+base64.b64encode(key)+b'\n'
        transport.known.write_bytes(raw); transport.known.chmod(0o600)
        launcher = aws.Launcher.__new__(aws.Launcher)
        launcher.c, launcher.state, launcher.tools = self.controller, self.state, self.base/'tools'
        launcher.transport, launcher.out, launcher.intent = transport, '/some/out', {'search_profile':{}}
        with patch.object(aws.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')):
            launcher.register()
        job_path = self.state/'aws-job.json'
        self.assertEqual(session_client.retained_host_keys(transport.known, job_path, aws.load(job_path)), raw)

    def test_restart_uses_actual_runtime_marker_and_rejects_another_plan(self):
        launcher = aws.Launcher.__new__(aws.Launcher)
        launcher.c, launcher.state = self.controller, self.state
        launcher.remote = '/opt/bio-aws-msa/'+self.state.name
        launcher.transport = aws.Transport(self.state, self.config, '192.0.2.10')
        self.controller.config.update(runtime_archive='/source/runtime.tar.zst', runtime_plan='/source/runtime-plan.json',
                                      runtime_plan_sha256='a'*64)
        marker = dict(schema=1, plan_sha256='a'*64, archive_sha256='b'*64)
        binds = [['mount', '--bind', '/opt/bio-worker-runtime/envs/msa-tools-v1', '/mnt/bio-shared/envs/msa-tools-v1']]
        with patch.object(launcher, 'progress'), patch.object(launcher, 'local') as copy_archive, \
             patch.object(launcher, 'remote_json', return_value={'bind_commands':binds}) as restore, \
             patch.object(launcher.transport, 'json_file', return_value=marker) as read_marker, \
             patch.object(launcher.transport, 'upload') as upload, patch.object(launcher.transport, 'run') as mount:
            launcher.restore_runtime()
            read_marker.assert_called_once_with('/opt/bio-worker-runtime/.runtime-archive-receipt.json', optional=True)
            copy_archive.assert_not_called()
            restore.assert_called_once_with('restore-runtime', ['--archive', '/opt/bio-runtime.tar.zst',
                '--plan', launcher.remote+'/runtime-plan.json', '--plan-sha256', 'a'*64,
                '--tools', launcher.remote+'/tools', '--runtime-root', '/opt/bio-worker-runtime'])
            self.assertEqual(mount.call_args.args[0], binds[0])
            read_marker.return_value = dict(marker, plan_sha256='c'*64)
            upload.reset_mock(); restore.reset_mock(); mount.reset_mock()
            with self.assertRaisesRegex(aws.Error, 'another plan'):
                launcher.restore_runtime()
            copy_archive.assert_not_called(); upload.assert_not_called(); restore.assert_not_called(); mount.assert_not_called()

    def test_range_copy_keeps_budget_polling_after_other_tasks_finish(self):
        import aws_worker
        launcher = aws.Launcher.__new__(aws.Launcher)
        self.controller.root.mkdir(exist_ok=True)
        launcher.c, launcher.state, launcher.remote = self.controller, self.state, '/remote'
        launcher.transport = aws.Transport(self.state, self.config, '192.0.2.10')
        self.controller.config['source_database'] = '/source'
        owner = dict(source_manifest_sha256='a'*64, source_receipt_sha256='b'*64)
        inventory = dict(source=owner, inventory='/inventory', inventory_sha256='c'*64)
        plan = dict(commands=[['fake-rsync']]*4, ranged_entries=[{'path':'large.idx'}],
                    destination='/pending', payload_bytes=1000, range_bytes=900, rsync_bytes=100)
        ranges_started, ranges_stopped, hash_finished = threading.Event(), threading.Event(), threading.Event()
        def ranged(source, inv, selected, binding, device, cancelled, callback, sealed_future):
            ranges_started.set()
            try:
                while not cancelled.wait(.01):
                    callback(dict(path='large.idx', aggregate_bytes_sent=0))
            finally:
                ranges_stopped.set()
        def sealed(*args, **kwargs):
            hash_finished.set(); return {'source_manifest':'/unused'}
        checks = []
        def budget():
            checks.append(1)
            if len(checks) >= 2:
                self.assertTrue(ranges_started.is_set())
                self.assertTrue(hash_finished.is_set())
                raise aws.Error('budget deadline reached')
        real_open = aws.os.open
        def opened(path, *args):
            if str(path) in ('/var/lib/dc/bio-submit.lock', '/var/lib/dc/msa-submit.lock'):
                path = self.base/Path(path).name
            return real_open(path, *args)
        with patch.object(aws.os, 'open', side_effect=opened), \
             patch.object(aws_worker, 'source_inventory', return_value=inventory), \
             patch.object(aws_worker, 'transfer_plan', return_value=plan), \
             patch.object(aws_worker, 'seal_source', side_effect=sealed), \
             patch.object(launcher, 'remote_json', return_value={'population_state':'pending'}), \
             patch.object(launcher.transport, 'run'), patch.object(launcher, 'progress'), \
             patch.object(launcher, 'local'), patch.object(launcher, 'transfer_ranges', side_effect=ranged), \
             patch.object(launcher, 'check', side_effect=budget):
            with self.assertRaisesRegex(aws.Error, 'budget deadline'):
                launcher.populate(owner, '/dev/test')
        self.assertTrue(ranges_stopped.is_set())
        self.assertGreaterEqual(len(checks), 2)
        self.assertTrue((self.state/'database-transfer-progress.json').is_file())

    def test_reuse_skips_initialization_and_counts_only_fresh_network_bytes(self):
        import aws_transfer
        from concurrent.futures import Future
        launcher = aws.Launcher.__new__(aws.Launcher)
        launcher.c, launcher.state, launcher.remote = self.controller, self.state, '/remote'
        launcher.transport = aws.Transport(self.state, self.config, '192.0.2.10')
        rows = [dict(path=name, kind='file', source_metadata={'size':size}) for name,size in (('finished.idx',900),('partial.idx',100))]
        selected = dict(range_bytes=1000, range_streams=32, ranged_entries=rows)
        sealed = Future(); sealed.set_result(dict(source_manifest='/sealed', source_manifest_sha256='f'*64))
        snapshots = []
        def make_plan(worker, source, inventory, pin, owner, device, name, streams):
            self.assertEqual(streams, 32)
            return dict(entry=next(row for row in rows if row['path']==name))
        def response(action, flags, **kwargs):
            if kwargs['name'].endswith('-reuse'):
                return dict(status='reused', path='finished.idx', size=900, source_manifest_sha256='f'*64,
                            reused_verified_bytes=900, readback_bytes=900)
            name = 'finished.idx' if '-0-' in kwargs['name'] else 'partial.idx'
            return dict(status='candidate' if name=='finished.idx' else 'copy_required', path=name,
                        size=900 if name=='finished.idx' else 100)
        def transfer(source, plan, command, **kwargs):
            self.assertEqual(plan['entry']['path'], 'partial.idx')
            kwargs['callback'](dict(path='partial.idx', bytes_sent=100))
            return [{'bytes':100}]
        with patch.object(aws_transfer, 'make_plan', side_effect=make_plan), \
             patch.object(aws_transfer, 'transfer', side_effect=transfer) as sent, \
             patch.object(launcher, 'remote_json', side_effect=response), patch.object(launcher, 'local') as local:
            launcher.transfer_ranges('/source', {'inventory':'/inventory','inventory_sha256':'a'*64}, selected,
                                     {}, '/dev/test', threading.Event(), snapshots.append, sealed)
        names = [call.args[1] for call in local.call_args_list]
        self.assertNotIn('database-range-0-initialize', names)
        self.assertNotIn('database-range-0-finish', names)
        self.assertIn('database-range-1-initialize', names)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(snapshots[-1]['aggregate_bytes_sent'], 100)
        self.assertEqual(snapshots[-1]['aggregate_reused_verified_bytes'], 900)
        self.assertEqual(snapshots[-1]['aggregate_completed_bytes'], 1000)
        self.assertEqual(snapshots[-1]['aggregate_processed_bytes'], 1000)

    def test_waiting_for_sealed_source_obeys_budget_without_launching_receivers(self):
        import aws_transfer
        from concurrent.futures import Future
        launcher = aws.Launcher.__new__(aws.Launcher)
        launcher.c, launcher.state, launcher.remote = self.controller, self.state, '/remote'
        launcher.transport = aws.Transport(self.state, self.config, '192.0.2.10')
        row = dict(path='finished.idx', kind='file', source_metadata={'size':900})
        selected = dict(range_bytes=900, range_streams=32, ranged_entries=[row])
        future = Future(); snapshots=[]
        with patch.object(aws_transfer, 'make_plan', return_value={'entry':row}), \
             patch.object(aws_transfer, 'transfer') as transfer, patch.object(launcher, 'local') as local, \
             patch.object(launcher, 'remote_json', return_value=dict(status='candidate',path='finished.idx',size=900)), \
             patch.object(launcher, 'check', side_effect=aws.Error('budget deadline')):
            with self.assertRaisesRegex(aws.Error, 'budget deadline'):
                launcher.transfer_ranges('/source', {'inventory':'/inventory','inventory_sha256':'a'*64},
                                         selected, {}, '/dev/test', threading.Event(), snapshots.append, future)
        transfer.assert_not_called()
        self.assertFalse(future.done())
        self.assertEqual(snapshots[-1]['phase'], 'waiting_for_sealed_source')
        self.assertNotIn('database-range-0-initialize', [call.args[1] for call in local.call_args_list])

    def test_cancelled_remote_readback_reaps_ssh_process(self):
        import sys,time
        launcher = aws.Launcher.__new__(aws.Launcher)
        launcher.c, launcher.state, launcher.remote = self.controller, self.state, '/remote'
        launcher.transport = aws.Transport(self.state, self.config, '192.0.2.10')
        cancelled = threading.Event(); children=[]; real_popen=subprocess.Popen
        def launched(*args, **kwargs):
            child=real_popen(*args, **kwargs); children.append(child); cancelled.set(); return child
        with patch.object(launcher.transport, 'ssh', return_value=[sys.executable,'-B','-c','import time;time.sleep(60)']), \
             patch.object(launcher, 'check'), patch.object(aws.subprocess, 'Popen', side_effect=launched):
            started=time.monotonic()
            with self.assertRaisesRegex(aws.Error, 'cancelled'):
                launcher.remote_json('reuse-file', name='readback', cancelled=cancelled)
        self.assertLess(time.monotonic()-started, 3)
        self.assertTrue(children and all(child.poll() is not None for child in children))

    def test_failed_or_cancelled_source_future_never_adopts_or_initializes(self):
        import aws_transfer
        from concurrent.futures import Future
        launcher = aws.Launcher.__new__(aws.Launcher)
        launcher.c, launcher.state, launcher.remote = self.controller, self.state, '/remote'
        launcher.transport = aws.Transport(self.state, self.config, '192.0.2.10')
        row = dict(path='finished.idx', kind='file', source_metadata={'size':900})
        selected = dict(range_bytes=900, range_streams=32, ranged_entries=[row])
        for mode in ('failed', 'cancelled'):
            with self.subTest(mode=mode):
                future, cancelled = Future(), threading.Event()
                if mode == 'failed': future.set_exception(RuntimeError('source hash failed'))
                def progress(value):
                    if mode == 'cancelled' and value['phase']=='waiting_for_sealed_source': cancelled.set()
                with patch.object(aws_transfer, 'make_plan', return_value={'entry':row}), \
                     patch.object(aws_transfer, 'transfer') as transfer, patch.object(launcher, 'local') as local, \
                     patch.object(launcher, 'remote_json', return_value=dict(status='candidate',path='finished.idx',size=900)) as remote:
                    with self.assertRaises((RuntimeError, aws.Error)):
                        launcher.transfer_ranges('/source', {'inventory':'/inventory','inventory_sha256':'a'*64},
                                                 selected, {}, '/dev/test', cancelled, progress, future)
                transfer.assert_not_called()
                self.assertEqual(remote.call_count, 1, 'Only the cheap candidate inspection may run')
                self.assertNotIn('database-range-0-initialize', [call.args[1] for call in local.call_args_list])


class AWSStartupProofTests(unittest.TestCase):
    """Real pinned helper subprocesses and client retirement; no cloud calls."""
    def setUp(self):
        import session, search_profile
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.registry = self.base/'sessions'
        self.state = self.registry/('a'*32); self.state.mkdir(parents=True)
        self.tools = self.state/'tools'; source = Path(aws.__file__).parent
        shutil.copytree(source, self.tools/'msa', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('recipes/_common.sh', 'rf3/msa.py'):
            path = self.tools/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((source.parent/name).read_bytes())
        self.submit = self.base/'bio-aws-msa'; self.submit.write_text('pinned AWS wrapper fixture\n')
        self.invocation = 'b'*32; self.unit = 'bio-msa-session-'+self.state.name+'.service'
        argv = [str(self.submit), 'launch-session', '--state', str(self.state), '--tools', str(self.tools)]
        self.intent = dict(schema=1, kind='managed-private-msa-session', provider_name='aws',
            session_id=self.state.name, unit=self.unit, tools=str(self.tools), sources=session.sources(self.tools),
            submit_sha256=session.sha(self.submit), provider_helper=str(self.submit),
            provider_helper_sha256=session.sha(self.submit), argv=argv, timeout_seconds=300,
            idle_seconds=900, warm='prefetch', requested_worker=None,
            search_profile=search_profile.resolve(aws.PROFILE))
        session.atomic(self.state/'intent.json', self.intent)
        session.atomic(self.registry/'active.json', dict(session_id=self.state.name,
                       intent_sha256=session.sha(self.state/'intent.json')))
        session.atomic(self.state/'start-intent.json', dict(command=['systemd-run', *argv]))
        self.live = dict(LoadState='loaded', ActiveState='active', MainPID='1234', ControlPID='0',
            InvocationID=self.invocation, Description='Managed private MSA session '+self.state.name,
            ExecStart='{ argv[]='+shlex.join(argv)+' ; }')
        self.unit_file = self.base/'unit.json'; self.unit_file.write_text(json.dumps(self.live))
        bin_dir = self.base/'bin'; bin_dir.mkdir()
        systemctl = bin_dir/'systemctl'
        systemctl.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
            "v=json.load(open(os.environ['TEST_AWS_STARTUP_UNIT']))\n"
            "assert sys.argv[1]=='show' and sys.argv[2]==os.environ['TEST_AWS_STARTUP_NAME']\n"
            "print('\\n'.join(k+'='+v for k,v in v.items()))\n")
        systemctl.chmod(0o700)
        env = patch.dict(os.environ, BIO_MSA_SESSION_ID=self.state.name, INVOCATION_ID=self.invocation,
            PATH=str(bin_dir)+os.pathsep+os.environ.get('PATH',''),
            TEST_AWS_STARTUP_UNIT=str(self.unit_file), TEST_AWS_STARTUP_NAME=self.unit)
        env.start(); self.addCleanup(env.stop)
        self.config = json.loads(source.joinpath('aws-config.json').read_text())
        database = self.base/'database'; database.mkdir()
        self.archive = self.base/'runtime.tar.zst'; self.archive.write_bytes(b'archive fixture')
        runtime = self.base/'runtime-plan.json'; runtime.write_text('{}\n')
        key = self.base/'key'; key.with_suffix('.pub').write_text('ssh-ed25519 QUJDRA== test\n')
        self.config.update(source_database=str(database), runtime_archive=str(self.archive),
            runtime_plan=str(runtime), runtime_plan_sha256=aws.sha(runtime), ssh_key=str(key))
        self.config_file = self.base/'config.json'; self.config_file.write_text(json.dumps(self.config))
        self.root = self.base/'provider'; self.api = API(self.root, self.config); self.budget = Budget()
        self.controller = aws.Controller(self.api, self.budget, self.config, self.root,
                                         clock=lambda:1000, sleep=lambda _:None)
        pool = self.controller.new_pool(); pool['ready_receipt_sha256']='c'*64; self.controller.save(pool)

    def launch(self):
        import aws_budget
        with patch.object(aws, 'AWS', return_value=self.api), \
             patch.object(aws_budget, 'Budget', return_value=self.budget):
            aws.main(['launch-session', '--state', str(self.state), '--tools', str(self.tools),
                      '--root', str(self.root), '--config', str(self.config_file)])

    def terminal(self):
        self.live.update(ActiveState='failed', MainPID='0')
        self.unit_file.write_text(json.dumps(self.live))

    def assert_retirable(self):
        import session_client as client, lifecycle
        self.assertTrue((self.state/'no-allocation.json').is_file())
        self.assertFalse((self.state/'aws-lease.json').exists())
        self.assertFalse((self.state/'allocation-started.json').exists())
        self.assertEqual(self.budget.calls, [])
        self.assertFalse([op for op, _ in self.api.calls if op in aws.WRITES])
        self.terminal()
        observed = client.observe_session(self.registry)
        self.assertEqual(observed['state'], 'terminal'); self.assertEqual(observed['code'], 'preallocation_failed')
        with lifecycle.registration_lock(self.registry, __import__('time').monotonic()+5):
            client._retire_locked(self.registry, observed)
        self.assertFalse((self.registry/'active.json').exists())
        self.assertTrue((self.state/'attempt.json').is_file())
        self.assertTrue((self.state/'closed.json').is_file())

    def test_zero_quota_uses_real_startup_proof_and_automatic_retirement(self):
        self.api.quota = 0
        with self.assertRaisesRegex(aws.Error, 'quota is below 128'): self.launch()
        self.assert_retirable()

    def test_missing_runtime_assets_fail_before_controller_and_are_retirable(self):
        self.archive.unlink()
        with self.assertRaisesRegex(aws.Error, 'source is unavailable'): self.launch()
        self.assertEqual(self.api.calls, [])
        self.assert_retirable()

    def test_incomplete_initial_database_is_retirable_before_launcher_try(self):
        pool=self.controller.pool();pool['ready_receipt_sha256']=None;self.controller.save(pool)
        with self.assertRaisesRegex(aws.Error, 'initial preparation is incomplete'): self.launch()
        self.assertEqual(self.api.calls, [])
        self.assert_retirable()

    def test_budget_failure_after_boundary_keeps_lease_and_uncertainty(self):
        import session_client as client, lifecycle
        self.budget.denied=True
        with self.assertRaisesRegex(ValueError, 'budget denied'): self.launch()
        self.assertTrue((self.state/'allocation-started.json').is_file())
        self.assertTrue((self.state/'aws-lease.json').is_file())
        self.assertFalse((self.state/'no-allocation.json').exists())
        self.assertFalse([op for op,_ in self.api.calls if op in aws.WRITES])
        self.terminal()
        with self.assertRaises(lifecycle.SessionError): client.observe_session(self.registry)
        self.assertTrue((self.registry/'active.json').exists())

    def test_lost_create_response_never_certifies_no_allocation(self):
        self.api.fail_after='create-volume'
        with self.assertRaisesRegex(aws.Error, 'lost response'): self.launch()
        self.assertEqual(len(self.api.volumes),1)
        self.assertTrue((self.state/'allocation-started.json').is_file())
        self.assertTrue((self.state/'cleanup-pending.json').is_file())
        self.assertFalse((self.state/'no-allocation.json').exists())

    def test_changed_invocation_or_pinned_wrapper_blocks_retirement(self):
        import startup
        self.api.quota=0
        with self.assertRaises(aws.Error): self.launch()
        self.terminal();self.live['InvocationID']='c'*32
        with self.assertRaisesRegex(ValueError,'invocation changed'):
            startup.validate_no_allocation(self.state,self.intent,self.live)
        self.live['InvocationID']=self.invocation;self.submit.write_text('changed wrapper')
        with self.assertRaisesRegex(ValueError,'launcher changed'):
            startup.validate_no_allocation(self.state,self.intent,self.live)

    def test_existing_aws_evidence_including_links_blocks_false_absence(self):
        import startup
        self.api.quota=0
        with self.assertRaises(aws.Error): self.launch()
        self.terminal()
        for name in ('aws-lease.json','aws-job.json','cleanup-pending.json'):
            with self.subTest(name=name):
                path=self.state/name;path.symlink_to(self.base/'absent')
                with self.assertRaisesRegex(ValueError,'Allocation may have started'):
                    startup.validate_no_allocation(self.state,self.intent,self.live)
                path.unlink()


if __name__ == '__main__':
    unittest.main()
