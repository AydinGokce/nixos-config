#!/usr/bin/env python3
"""One persistent, budgeted AWS CPU MSA worker; credentials remain on the head.

Allocation requests are retained before AWS calls and use fixed client tokens.
Closing a session stops compute and retains both EBS volumes. Nothing here
terminates an instance, deletes a volume, or changes native search settings.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import copy
from datetime import datetime
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION
import threading
import time
import uuid

ACCOUNT = '147997164104'
REGION = 'us-east-1'
INSTANCE_TYPE = 'r6a.32xlarge'
AMI = 'ami-025d99823a4caad37'
AMI_OWNER = '099720109477'
COMPUTE_HOURLY = 7.2576 + .005  # On-demand Linux plus public IPv4.
DATA_HOURLY = (1300*.08 + (8000-3000)*.005 + (2000-125)*.04)/730
ROOT_HOURLY = 100*.08/730
ROOT = Path('/var/lib/dc/aws-msa')
CONFIG = '/etc/bio-aws-msa.json'
KEY = '/root/.ssh/datacrunch_ed25519'
PROFILE = 'resident-768gib-v1'
METADATA = ('session-starting.json', 'session-ready.json', 'session-closed.json',
            'startup-progress.json', 'worker-status.json', 'worker-controls.json',
            'msa-server.json', 'msa-server.provenance.json', 'warm-index.json')
WRITES = {'create-volume', 'run-instances', 'start-instances', 'stop-instances',
          'attach-volume', 'create-security-group', 'authorize-security-group-ingress', 'import-key-pair'}


class Error(ValueError):
    pass


def require(value, message):
    if not value:
        raise Error(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(8*1024*1024):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def ident(value, prefix=None):
    pattern = r'[0-9a-f]{32}' if prefix is None else re.escape(prefix)+r'-[0-9a-f]{8,17}'
    require(isinstance(value, str) and re.fullmatch(pattern, value), 'Invalid AWS resource or lease identity')
    return value


def load(path, optional=False):
    path = Path(path)
    if optional and not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 16*1024*1024, 'Invalid AWS receipt file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            result = json.load(stream)
    finally:
        os.close(fd)
    require(isinstance(result, dict), 'AWS receipt must be an object')
    return result


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink(), 'AWS receipt cannot be a symlink')
    temporary = path.parent/('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(canonical(value)+b'\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def tags(row):
    result = {}
    require(isinstance(row.get('Tags'), list), 'AWS ownership tags are missing')
    for item in row['Tags']:
        require(isinstance(item, dict) and isinstance(item.get('Key'), str)
                and isinstance(item.get('Value'), str) and item['Key'] not in result,
                'Ambiguous AWS ownership tags')
        result[item['Key']] = item['Value']
    return result


def tag_spec(kind, token, role):
    return {'ResourceType': kind, 'Tags': [
        {'Key': 'Project', 'Value': 'gc-msa'}, {'Key': 'allocation-token', 'Value': token},
        {'Key': 'msa-role', 'Value': role}, {'Key': 'Name', 'Value': 'gc-msa-'+role+'-'+token[:12]}]}


def creation_epoch(row):
    value = row.get('CreateTime')
    require(isinstance(value, str), 'AWS EBS creation timestamp is missing')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'AWS EBS creation timestamp has no timezone')
    return parsed.timestamp()


class AWS:
    """Fixed official CLI/profile/region; no arbitrary endpoints or shell calls."""
    def __init__(self, executable='aws', runner=subprocess.run):
        self.executable, self.runner = executable, runner

    def call(self, service, operation, request=None):
        require(service in ('ec2', 'sts', 'service-quotas'), 'Unsupported AWS service')
        allowed = operation.startswith('describe-') or (service, operation) in {
            ('sts', 'get-caller-identity'), ('service-quotas', 'get-service-quota')}
        require(allowed or service == 'ec2' and operation in WRITES, 'Unsupported AWS operation')
        argv = [self.executable, '--profile', 'bio-aws', '--region', REGION, '--output', 'json',
                '--no-cli-pager', '--cli-connect-timeout', '10', '--cli-read-timeout', '30', service, operation]
        if request:
            argv += ['--cli-input-json', canonical(request).decode()]
        environment = dict(os.environ, AWS_MAX_ATTEMPTS='1' if operation in WRITES else '3', AWS_PAGER='')
        result = self.runner(argv, capture_output=True, timeout=50, env=environment)
        if result.returncode:
            # AWS stderr may contain request details. Never echo credentials or complete argv.
            error = re.search(rb'\(([A-Za-z][A-Za-z0-9.]+)\)', result.stderr or b'')
            code = error.group(1).decode() if error else 'RequestFailed'
            raise Error('AWS '+operation+' failed or is uncertain ('+code+'); retained intent prevents duplicate allocation')
        require(len(result.stdout) <= 16*1024*1024, 'Oversized AWS response')
        value = json.loads(result.stdout)
        require(isinstance(value, dict), 'Invalid AWS response')
        return value


def configuration(path=None, *, require_assets=True):
    # NixOS installs /etc configuration through immutable store symlinks.
    # Resolve this configuration entry; mutable pool/lease receipts still use
    # load() directly and retain its no-symlink protection.
    result = load(Path(path or os.environ.get('BIO_AWS_MSA_CONFIG', CONFIG)).resolve(strict=True))
    require(result.get('account_id') == ACCOUNT and result.get('region') == REGION,
            'AWS configuration must pin the authorized account and region')
    require(result.get('instance_type', INSTANCE_TYPE) == INSTANCE_TYPE and result.get('ami', AMI) == AMI,
            'AWS worker type or image differs from the approved configuration')
    require(result.get('availability_zone') in {REGION+x for x in 'abcdf'}, 'Unsupported AWS availability zone')
    ident(result.get('subnet_id'), 'subnet'); ident(result.get('vpc_id'), 'vpc')
    require(str(ipaddress.ip_network(result.get('ssh_cidr', ''), strict=True)) == '31.56.109.100/32',
            'AWS SSH access must be restricted to bio-head')
    for key in ('source_manifest_sha256', 'source_receipt_sha256', 'runtime_plan_sha256'):
        require(isinstance(result.get(key), str) and re.fullmatch('[a-f0-9]{64}', result[key]), 'Missing pinned AWS source receipt')
    for key in ('source_database', 'runtime_plan', 'runtime_archive'):
        require(isinstance(result.get(key), str) and Path(result[key]).is_absolute(), 'Missing AWS source asset path')
    if require_assets:
        require(Path(result['source_database']).is_dir() and Path(result['runtime_archive']).is_file()
                and sha(result['runtime_plan']) == result['runtime_plan_sha256'], 'AWS runtime or database source is unavailable/changed')
    return result


class Controller:
    def __init__(self, api, budget, config, root=ROOT, clock=time.time, sleep=time.sleep):
        self.api, self.budget, self.config, self.root = api, budget, copy.deepcopy(config), Path(root)
        self.clock, self.sleep = clock, sleep
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not self.root.is_symlink(), 'AWS state root cannot be a symlink')

    @contextmanager
    def lock(self):
        fd = os.open(self.root/'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def pool(self, optional=False):
        value = load(self.root/'pool.json', optional=optional)
        if value is not None:
            require(value.get('schema') == 1 and value.get('provider') == 'aws'
                    and value.get('account_id') == ACCOUNT and value.get('region') == REGION,
                    'AWS pool scope differs')
            ident(value.get('pool_id'))
            require(value.get('configuration_sha256') == hashlib.sha256(canonical(self.config)).hexdigest(),
                    'AWS pool configuration changed; explicit migration is required')
        return value

    def save(self, pool):
        pool['updated_epoch'] = self.clock()
        atomic(self.root/'pool.json', pool)

    def inventory(self):
        identity = self.api.call('sts', 'get-caller-identity')
        require(identity.get('Account') == ACCOUNT, 'AWS credential account differs from authorized account')
        rows = self.api.call('ec2', 'describe-instances', {'Filters': [{'Name': 'tag:Project', 'Values': ['gc-msa']}]})
        instances = [i for r in rows.get('Reservations', []) for i in r.get('Instances', [])]
        volumes = self.api.call('ec2', 'describe-volumes', {'Filters': [{'Name': 'tag:Project', 'Values': ['gc-msa']}]})['Volumes']
        require(isinstance(volumes, list), 'AWS volume inventory is incomplete')
        return dict(observed_epoch=self.clock(), account_id=ACCOUNT, region=REGION, instances=instances, volumes=volumes)

    @staticmethod
    def owned(row, pool, role):
        values = tags(row)
        require(values.get('Project') == 'gc-msa' and values.get('allocation-token') == pool['pool_id']
                and values.get('msa-role') == role, 'AWS resource ownership differs from retained allocation intent')

    def reconcile(self, pool, inventory):
        instances = [r for r in inventory['instances'] if r.get('State', {}).get('Name') != 'terminated']
        volumes = [r for r in inventory['volumes'] if r.get('State') != 'deleted']
        require(all(tags(r).get('allocation-token') == pool['pool_id'] for r in instances+volumes),
                'Another managed AWS pool exists; refusing duplicate allocation')
        require(len(instances) <= 1, 'Multiple managed AWS instances require explicit reconciliation')
        database = [r for r in volumes if tags(r).get('msa-role') == 'database']
        roots = [r for r in volumes if tags(r).get('msa-role') == 'root']
        require(len(database) <= 1 and len(roots) <= 1 and len(database)+len(roots) == len(volumes),
                'Managed AWS storage inventory is ambiguous')
        for rows, key, role, size in ((database, 'db_volume_id', 'database', 1300), (roots, 'os_id', 'root', 100)):
            if not rows:
                require(not pool.get(key), 'Retained AWS volume is missing from fresh inventory')
                continue
            row = rows[0]; self.owned(row, pool, role)
            require(row.get('AvailabilityZone') == self.config['availability_zone'] and row.get('Size') == size
                    and row.get('VolumeType') == 'gp3', 'AWS volume geometry or zone differs')
            if role == 'database':
                require(row.get('Iops') == 8000 and row.get('Throughput') == 2000, 'AWS database performance specification differs')
            value = ident(row.get('VolumeId'), 'vol')
            require(pool.get(key) in (None, value), 'AWS volume identity changed')
            pool[key] = value
        if instances:
            row = instances[0]; self.owned(row, pool, 'worker')
            value = ident(row.get('InstanceId'), 'i')
            require(pool.get('instance_id') in (None, value) and row.get('InstanceType') == INSTANCE_TYPE
                    and row.get('ImageId') == AMI and row.get('Placement', {}).get('AvailabilityZone') == self.config['availability_zone']
                    and row.get('ClientToken') == pool['instance_request']['ClientToken'], 'AWS instance identity or launch parameters changed')
            require(row.get('SubnetId') == self.config['subnet_id'] and row.get('VpcId') == self.config['vpc_id'],
                    'AWS managed instance network differs')
            pool['instance_id'] = value
            root = [b['Ebs'] for b in row.get('BlockDeviceMappings', []) if b.get('DeviceName') == row.get('RootDeviceName')]
            require(len(root) == 1 and root[0].get('VolumeId') == pool.get('os_id')
                    and root[0].get('DeleteOnTermination') is False, 'AWS retained root-volume binding differs')
        else:
            require(not pool.get('instance_id'), 'Retained AWS instance is missing from fresh inventory')
        return instances[0] if instances else None

    def request(self, pool, name, operation, payload, token):
        """Persist exact mutation before calling AWS; retry only same idempotent request."""
        previous = pool.setdefault('requests', {}).get(name)
        require(previous is None or previous['operation'] == operation and previous['request'] == payload,
                'Retained AWS operation changed')
        if previous and previous.get('response') is not None:
            return previous['response']
        if previous and operation not in {'run-instances', 'create-volume', 'start-instances', 'stop-instances', 'attach-volume'}:
            raise Error('AWS '+operation+' is uncertain; reconcile its tagged resource before any retry')
        self.budget.check(token)
        entry = previous or dict(operation=operation, request=payload, started_epoch=self.clock(), status='pending')
        pool['requests'][name] = entry; self.save(pool)
        try:
            response = self.api.call('ec2', operation, payload)
        except Exception:
            entry['status'] = 'uncertain'; self.save(pool)
            raise
        entry.update(status='observed', response=response, finished_epoch=self.clock()); self.save(pool)
        return response

    def new_pool(self):
        pool_id = uuid.uuid4().hex
        return dict(schema=1, provider='aws', account_id=ACCOUNT, region=REGION,
                    pool_id=pool_id, filesystem_uuid=str(uuid.uuid4()), created_epoch=self.clock(),
                    configuration_sha256=hashlib.sha256(canonical(self.config)).hexdigest(), requests={},
                    status='planned', instance_id=None, os_id=None, db_volume_id=None,
                    active_session_id=None, ready_receipt_sha256=None)

    def access(self, pool, token):
        filters = [{'Name': 'tag:allocation-token', 'Values': [pool['pool_id']]}]
        groups = self.api.call('ec2', 'describe-security-groups', {'Filters': filters})['SecurityGroups']
        require(len(groups) <= 1, 'AWS security group ownership is ambiguous')
        if groups:
            group = groups[0]; self.owned(group, pool, 'ssh')
            require(group.get('VpcId') == self.config['vpc_id'], 'AWS SSH security group VPC differs')
            group_id = group['GroupId']
        else:
            response = self.request(pool, 'security-group', 'create-security-group', dict(
                GroupName='gc-msa-'+pool['pool_id'], Description='GC MSA SSH from bio-head only',
                VpcId=self.config['vpc_id'], TagSpecifications=[tag_spec('security-group', pool['pool_id'], 'ssh')]), token)
            group_id = response['GroupId']
        ident(group_id, 'sg'); pool['security_group_id'] = group_id
        permission = dict(IpProtocol='tcp', FromPort=22, ToPort=22, IpRanges=[{'CidrIp': self.config['ssh_cidr']}])
        if groups:
            normalized = [{k: p.get(k) for k in ('IpProtocol', 'FromPort', 'ToPort', 'IpRanges')}
                          for p in group.get('IpPermissions', [])]
            require(all(p == permission for p in normalized)
                    and all(not p.get(k) for p in group.get('IpPermissions', [])
                            for k in ('Ipv6Ranges', 'PrefixListIds', 'UserIdGroupPairs')),
                    'AWS SSH security group has unexpected ingress')
        if not groups or not group.get('IpPermissions'):
            self.request(pool, 'ssh-ingress', 'authorize-security-group-ingress',
                         dict(GroupId=group_id, IpPermissions=[permission]), token)
        keys = self.api.call('ec2', 'describe-key-pairs', {'Filters': filters})['KeyPairs']
        require(len(keys) <= 1, 'AWS SSH key ownership is ambiguous')
        key_name = 'gc-msa-'+pool['pool_id']
        public = Path(self.config.get('ssh_key', KEY)+'.pub').read_text().strip()
        require(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public), 'Expected existing Ed25519 head public key')
        public_bytes = (' '.join(public.split()[:2])+'\n').encode()
        if keys:
            self.owned(keys[0], pool, 'ssh')
            require(keys[0].get('KeyName') == key_name and pool.get('public_key_sha256') in
                    (None, hashlib.sha256(public_bytes).hexdigest()), 'AWS SSH key name or local public key differs')
        else:
            self.request(pool, 'ssh-key', 'import-key-pair', dict(KeyName=key_name,
                         PublicKeyMaterial=base64.b64encode(public_bytes).decode(),
                         TagSpecifications=[tag_spec('key-pair', pool['pool_id'], 'ssh')]), token)
        pool['key_name'] = key_name; pool['public_key_sha256'] = hashlib.sha256(public_bytes).hexdigest()
        self.save(pool)

    def allocation_requests(self, pool):
        volume = dict(AvailabilityZone=self.config['availability_zone'], Size=1300, VolumeType='gp3',
                      Iops=8000, Throughput=2000, ClientToken=pool['pool_id']+'-database',
                      TagSpecifications=[tag_spec('volume', pool['pool_id'], 'database')])
        instance = dict(ImageId=AMI, InstanceType=INSTANCE_TYPE, MinCount=1, MaxCount=1,
                        ClientToken=pool['pool_id']+'-instance', KeyName=pool['key_name'],
                        NetworkInterfaces=[dict(DeviceIndex=0, SubnetId=self.config['subnet_id'],
                            Groups=[pool['security_group_id']], AssociatePublicIpAddress=True, DeleteOnTermination=True)],
                        Placement={'AvailabilityZone': self.config['availability_zone']},
                        BlockDeviceMappings=[dict(DeviceName='/dev/sda1', Ebs=dict(VolumeSize=100,
                            VolumeType='gp3', DeleteOnTermination=False))],
                        MetadataOptions=dict(HttpTokens='required', HttpEndpoint='enabled', HttpPutResponseHopLimit=1),
                        TagSpecifications=[tag_spec('instance', pool['pool_id'], 'worker'), tag_spec('volume', pool['pool_id'], 'root')])
        pool.update(volume_request=volume, instance_request=instance)
        self.save(pool)
        return volume, instance

    def launch_preflight(self):
        images = self.api.call('ec2', 'describe-images', dict(ImageIds=[AMI], Owners=[AMI_OWNER]))['Images']
        require(len(images) == 1 and images[0].get('ImageId') == AMI and images[0].get('OwnerId') == AMI_OWNER
                and images[0].get('State') == 'available' and images[0].get('Architecture') == 'x86_64'
                and images[0].get('RootDeviceName') == '/dev/sda1', 'AWS approved Canonical image is unavailable or differs')
        subnets = self.api.call('ec2', 'describe-subnets', {'SubnetIds': [self.config['subnet_id']]})['Subnets']
        require(len(subnets) == 1 and subnets[0].get('VpcId') == self.config['vpc_id']
                and subnets[0].get('AvailabilityZone') == self.config['availability_zone']
                and subnets[0].get('State') == 'available', 'AWS subnet is unavailable or in another VPC/zone')
        quota = self.api.call('service-quotas', 'get-service-quota',
                             {'ServiceCode': 'ec2', 'QuotaCode': 'L-1216C47A'})['Quota']['Value']
        require(type(quota) in (int, float) and math.isfinite(quota) and quota >= 128, 'AWS Standard vCPU quota is below 128')

    def lease(self, state):
        state = Path(state).absolute(); ident(state.name)
        value = load(state/'aws-lease.json')
        require(value.get('session_id') == state.name and value.get('state') == str(state), 'AWS lease state binding differs')
        ident(value.get('token'))
        return value

    def allocate(self, state, hours, *, before_reservation=None):
        state = Path(state).absolute(); ident(state.name)
        require(math.isfinite(hours) and 0 < hours <= 24, 'Invalid AWS reservation lifetime')
        with self.lock():
            pool = self.pool(optional=True)
            if pool is None:
                inventory = self.inventory()
                require(not inventory['instances'] and not inventory['volumes'], 'Unclaimed managed AWS resources block a new pool')
                pool = self.new_pool(); self.save(pool)
            require(pool.get('active_session_id') in (None, state.name), 'Another AWS MSA session owns the persistent worker')
            self.launch_preflight()
            # Read-only admission may fail without renting anything. Once this
            # boundary is crossed, even an interrupted reservation is uncertain.
            if before_reservation is not None:
                before_reservation()
            prior = load(state/'aws-lease.json', optional=True)
            if prior:
                require(prior.get('status') != 'closed', 'Closed AWS session cannot allocate again')
                token = ident(prior['token'])
                reservation = (self.budget.reserve(token, COMPUTE_HOURLY,
                    (0 if pool.get('db_volume_id') else DATA_HOURLY)+(0 if pool.get('os_id') else ROOT_HOURLY), hours)
                    if prior.get('status') == 'reserving' else self.budget.check(token))
            else:
                token = uuid.uuid4().hex
                # Lease and pool ownership are durable even if reservation outcome is uncertain.
                prior = dict(schema=1, session_id=state.name, state=str(state), token=token,
                             pool_id=pool['pool_id'], status='reserving', created_epoch=self.clock())
                atomic(state/'aws-lease.json', prior)
                pool['active_session_id'] = state.name; pool['active_state'] = str(state); self.save(pool)
                storage = (0 if pool.get('db_volume_id') else DATA_HOURLY)+(0 if pool.get('os_id') else ROOT_HOURLY)
                reservation = self.budget.reserve(token, COMPUTE_HOURLY, storage, hours)
            prior.update(status='allocating', reservation=reservation); atomic(state/'aws-lease.json', prior)
            self.access(pool, token)
            volume_request, instance_request = self.allocation_requests(pool)
            inventory = self.inventory(); instance = self.reconcile(pool, inventory); self.save(pool)
            if pool.get('db_volume_id') and not pool.get('volume_creation'):
                # Reconcile the exact tagged ID, then recover the real response
                # with the SAME idempotent ClientToken (never a new allocation).
                request = pool.get('requests', {}).get('database')
                require(request and request['request'] == volume_request, 'Reconciled AWS data volume lacks its allocation intent')
                response = self.request(pool, 'database', 'create-volume', volume_request, token)
                require(response.get('VolumeId') == pool['db_volume_id'], 'AWS idempotent response differs from observed volume')
                pool['volume_creation'] = dict(request=volume_request, response=response, recovered_client_token=True)
                self.save(pool)
            if not pool.get('db_volume_id'):
                response = self.request(pool, 'database', 'create-volume', volume_request, token)
                pool['db_volume_id'] = ident(response.get('VolumeId'), 'vol')
                pool['volume_creation'] = dict(request=volume_request, response=response); self.save(pool)
            created = pool['volume_creation']['response']
            self.budget.storage(token, pool['db_volume_id'], DATA_HOURLY, creation_epoch(created))
            if not instance:
                self.request(pool, 'instance', 'run-instances', instance_request, token)
                inventory = self.inventory(); instance = self.reconcile(pool, inventory); self.save(pool)
                require(instance is not None, 'AWS launch accepted but instance is not yet visible; retained token must be reconciled')
            root_row = next(v for v in inventory['volumes'] if v['VolumeId'] == pool['os_id'])
            self.budget.storage(token, pool['os_id'], ROOT_HOURLY, creation_epoch(root_row))
            self.budget.running(token, pool['instance_id'], COMPUTE_HOURLY)
            state_name = instance['State']['Name']
            require(state_name in ('pending', 'running', 'stopped', 'stopping'), 'Unexpected managed AWS instance state')
            if state_name == 'stopping':
                raise Error('AWS worker is still stopping; lease retained until the exact state is resolved')
            if state_name == 'stopped':
                self.request(pool, 'start-'+token, 'start-instances', {'InstanceIds': [pool['instance_id']]}, token)
            prior.update(status='starting', instance=pool['instance_id'], os_id=pool['os_id'], db_volume_id=pool['db_volume_id'])
            atomic(state/'aws-lease.json', prior)
            return prior

    def observe(self, state, *, running=True, expected=None):
        with self.lock():
            pool, lease = self.pool(), self.lease(state)
            require(lease['pool_id'] == pool['pool_id'] and pool.get('active_session_id') == lease['session_id'],
                    'AWS session no longer owns the persistent worker')
            inventory = self.inventory(); row = self.reconcile(pool, inventory)
            require(row is not None, 'Managed AWS worker is unavailable')
            actual = row['State']['Name']; self.budget.observe_instance(row['InstanceId'], actual)
            if running:
                require(actual == 'running', 'AWS managed worker is not running')
            reservation = self.budget.check(lease['token']) if running else lease['reservation']
            ip = row.get('PublicIpAddress')
            if running:
                ipaddress.IPv4Address(ip)
            if expected:
                for key, value in (('instance', row['InstanceId']), ('ip', ip), ('os_id', pool['os_id'])):
                    require(expected.get(key) in (None, value), 'AWS requested worker identity differs')
            database = next(v for v in inventory['volumes'] if v['VolumeId'] == pool['db_volume_id'])
            attachments = database.get('Attachments')
            require(isinstance(attachments, list) and len(attachments) <= 1, 'AWS database attachments are ambiguous')
            if attachments:
                require(attachments[0].get('InstanceId') == row['InstanceId'], 'AWS database is attached to another worker')
            proof = dict(provider='aws', account=ACCOUNT, region=REGION, instance=row['InstanceId'], ip=ip,
                         os_id=pool['os_id'], db_volume_id=pool['db_volume_id'], token=lease['token'],
                         reservation_deadline=reservation['deadline'], checked_epoch=self.clock(),
                         hostname=pool.get('hostname'), budget=reservation, instance_state=actual)
            return pool, lease, inventory, proof

    def attach(self, state):
        pool, lease, inventory, proof = self.observe(state)
        database = next(v for v in inventory['volumes'] if v['VolumeId'] == pool['db_volume_id'])
        attachments = database.get('Attachments', [])
        if attachments:
            require(attachments[0].get('State') == 'attached', 'AWS database attachment has not completed')
            return proof
        require(database.get('State') == 'available', 'AWS database is not available for exclusive attachment')
        with self.lock():
            latest = self.pool()
            require(latest.get('active_session_id') == lease['session_id'], 'AWS lease changed before attachment')
            self.request(latest, 'attach-'+lease['token'], 'attach-volume', dict(
                VolumeId=pool['db_volume_id'], InstanceId=pool['instance_id'], Device='/dev/sdf'), lease['token'])
        raise Error('AWS database is attaching; retry observation')

    def provider_check(self, state, expected=None):
        pool, lease, inventory, proof = self.observe(state, expected=expected)
        require(isinstance(proof['hostname'], str) and proof['hostname'], 'AWS SSH host identity is not registered')
        database = next(v for v in inventory['volumes'] if v['VolumeId'] == pool['db_volume_id'])
        attached = database.get('Attachments', [])
        require(database.get('State') == 'in-use' and len(attached) == 1
                and attached[0].get('State') == 'attached', 'AWS database is not attached and ready')
        return proof

    def close(self, state, timeout=180):
        state = Path(state)
        closed = load(state/'aws-closed.json', optional=True)
        if closed:
            # A former lease must neither stop a reused worker nor fabricate a
            # fresh "stopped" proof after another session has restarted it.
            with self.lock():
                pool = self.pool()
                require(pool.get('active_session_id') in (None, state.name), 'AWS worker has been reused by a later session')
                row = self.reconcile(pool, self.inventory())
                require(row and row['State']['Name'] == 'stopped', 'AWS previous close is historical; compute is not currently stopped')
                return dict(closed, checked_epoch=self.clock())
        deadline = self.clock()+timeout
        while True:
            pool, lease, inventory, proof = self.observe(state, running=False)
            actual = proof['instance_state']
            if actual == 'stopped':
                released = self.budget.release(lease['token'], pool['instance_id'], 'stopped')
                receipt = dict(proof, status='closed', compute_stopped=True, instance_state='stopped',
                               budget_released=True, budget= released, checked_epoch=self.clock())
                with self.lock():
                    pool = self.pool(); require(pool.get('active_session_id') == lease['session_id'], 'AWS lease changed before close')
                    lease.update(status='closed', closed_epoch=self.clock()); atomic(state/'aws-lease.json', lease)
                    atomic(state/'aws-closed.json', receipt)
                    pool.update(active_session_id=None, active_state=None, last_closed_state=str(state), status='stopped'); self.save(pool)
                return receipt
            require(actual in ('running', 'pending', 'stopping'), 'Unexpected AWS stop state; storage and lease retained')
            if actual in ('running', 'pending'):
                # Cleanup remains allowed after the spending/deadline gate has closed.
                with self.lock():
                    pool = self.pool()
                    request = dict(InstanceIds=[pool['instance_id']])
                    atomic(state/'aws-stop-intent.json', dict(request=request, token=lease['token'], created_epoch=self.clock()))
                    response = self.api.call('ec2', 'stop-instances', request)
                    atomic(state/'aws-stop-response.json', response)
            require(self.clock() < deadline, 'AWS stop is not yet confirmed; watchdog retains cleanup ownership')
            self.sleep(5)

    def watchdog(self):
        """Inventory must succeed before refreshing the shared budget heartbeat."""
        with self.lock():
            pool = self.pool(optional=True)
            inventory = self.inventory()
            if pool is None:
                require(not inventory['instances'] and not inventory['volumes'], 'Unclaimed AWS resources require reconciliation')
                report = self.budget.heartbeat()
                return dict(provider='aws', status='idle', budget=report, checked_epoch=self.clock())
            row = self.reconcile(pool, inventory); self.save(pool)
            state_path = pool.get('active_state')
            if state_path:
                lease = self.lease(state_path)
                # Recover the exact accepted allocation after a lost launcher
                # response before trusting a healthy watchdog heartbeat.
                for key, rate in (('db_volume_id', DATA_HOURLY), ('os_id', ROOT_HOURLY)):
                    if pool.get(key):
                        volume = next(v for v in inventory['volumes'] if v['VolumeId'] == pool[key])
                        self.budget.storage(lease['token'], volume['VolumeId'], rate, creation_epoch(volume))
                if row and lease.get('instance') is None:
                    self.budget.running(lease['token'], row['InstanceId'], COMPUTE_HOURLY)
                    lease.update(instance=row['InstanceId'], os_id=pool['os_id'], db_volume_id=pool['db_volume_id'], status='starting')
                    atomic(Path(state_path)/'aws-lease.json', lease)
            if row:
                self.budget.observe_instance(row['InstanceId'], row['State']['Name'])
            report = self.budget.heartbeat()
            if not state_path:
                if row and row['State']['Name'] in ('running', 'pending'):
                    request = dict(InstanceIds=[row['InstanceId']])
                    atomic(self.root/'unexpected-restart-stop-intent.json', dict(request=request, checked_epoch=self.clock()))
                    response = self.api.call('ec2', 'stop-instances', request)
                    atomic(self.root/'unexpected-restart-stop-response.json', response)
                    return dict(provider='aws', status='stopping_unreserved_compute', instance=row['InstanceId'], budget=report)
                return dict(provider='aws', status='stopped', budget=report, checked_epoch=self.clock())
        try:
            self.budget.check(self.lease(state_path)['token'])
        except Exception:
            if row:
                return self.close(Path(state_path))
            raise Error('AWS unresolved allocation has exceeded its guard; exact allocation intent is retained') from None
        intent = load(Path(state_path)/'intent.json', optional=True)
        if intent and isinstance(intent.get('unit'), str):
            require(re.fullmatch(r'bio-[a-zA-Z0-9-]+\.service', intent['unit']), 'Invalid AWS owner unit')
            observed = subprocess.run(['systemctl', 'show', intent['unit'], '--no-pager',
                '--property=ActiveState,MainPID,LoadState,InvocationID'], capture_output=True, text=True, timeout=15)
            require(observed.returncode == 0, 'AWS owner unit could not be observed')
            fields = dict(line.split('=', 1) for line in observed.stdout.splitlines() if '=' in line)
            if (fields.get('LoadState') == 'not-found' or fields.get('ActiveState') in ('inactive', 'failed')) and fields.get('MainPID') == '0':
                if row:
                    return self.close(Path(state_path))
                raise Error('AWS launcher ended with an unresolved allocation; retained client-token reconciliation is required')
        return dict(provider='aws', status='within_budget', budget=report, checked_epoch=self.clock())

    def capacity(self):
        observed = self.clock()
        result = dict(schema=1, provider='aws', msa_provider='aws', compute='cpu', compute_kind='cpu', region=REGION, account=ACCOUNT,
                      server_epoch=observed, observed_epoch=observed, checked_epoch=None,
                      stale_after_seconds=120, state='error', msa_available=None, gpus=[], cpus=[])
        try:
            inventory = self.inventory()
            quota = self.api.call('service-quotas', 'get-service-quota',
                                 {'ServiceCode': 'ec2', 'QuotaCode': 'L-1216C47A'})['Quota']['Value']
            require(type(quota) in (int, float) and math.isfinite(quota) and quota >= 0, 'AWS quota is unavailable')
            offers = self.api.call('ec2', 'describe-instance-type-offerings', dict(LocationType='availability-zone',
                Filters=[{'Name': 'instance-type', 'Values': [INSTANCE_TYPE]}]))['InstanceTypeOfferings']
            offered = self.config['availability_zone'] in {r['Location'] for r in offers}
            pool = self.pool(optional=True)
            ready = bool(pool and pool.get('ready_receipt_sha256'))
            known_running = False
            if pool:
                row = self.reconcile(pool, inventory)
                known_running = bool(row and row.get('State', {}).get('Name') == 'running')
            eligible = bool(quota >= 128 and offered)
            reason = ('Shared AWS worker is running' if eligible and ready and known_running else
                      'AWS database/runtime preparation is incomplete' if not ready else
                      'AWS vCPU quota or regional offering is insufficient' if not eligible else
                      'Configured and quota eligible; EC2 live launch capacity is known only when starting')
            result.update(state='ready', checked_epoch=observed, msa_available=(True if eligible and ready and known_running else
                          False if not eligible or not ready else None), msa_message=reason,
                          cpus=[dict(instance_type=INSTANCE_TYPE, name='AMD EPYC CPU', region=REGION,
                            location=self.config['availability_zone'], contract='on-demand', vcpus=128,
                            ram_gib=1024, price_hourly=COMPUTE_HOURLY, msa_eligible=eligible and ready,
                            status='running' if known_running else 'offered' if offered else 'not-offered',
                            availability='eligible' if eligible and ready else 'unavailable',
                            quota_vcpus=quota, live_capacity='unknown', reason=reason)])
        except (Error, OSError, KeyError, TypeError, ValueError):
            result.update(msa_message='AWS availability could not be verified', error='AWS account, inventory, quota or configuration check failed')
        return result


class Transport:
    def __init__(self, state, config, ip):
        self.state, self.config = Path(state), config
        self.ip = str(ipaddress.IPv4Address(ip))
        self.known = self.state/'worker-known-hosts'
        self.key = config.get('ssh_key', KEY)

    def ssh(self, user='root', first=False):
        return ['ssh', '-i', self.key, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'StrictHostKeyChecking='+('accept-new' if first else 'yes'),
                '-o', 'UserKnownHostsFile='+str(self.known), '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', user+'@'+self.ip]

    def run(self, argv, *, user='root', first=False, timeout=120, input=None, json_output=False):
        result = subprocess.run(self.ssh(user, first)+[shlex.join([str(x) for x in argv])],
                                input=input, capture_output=True, timeout=timeout)
        require(result.returncode == 0, 'AWS worker command failed (exit '+str(result.returncode)+')')
        if json_output:
            require(len(result.stdout) <= 16*1024*1024, 'AWS worker response is oversized')
            return json.loads(result.stdout)
        return result.stdout

    def upload(self, source, destination, timeout=120, *, dereference=False):
        source = Path(source)
        if source.is_dir():
            source = source.resolve(strict=True)
            self.run(['mkdir', '-p', str(destination)], timeout=min(120, timeout))
            source_arg, destination = str(source)+'/', str(destination).rstrip('/')+'/'
        else:
            source_arg = str(source)
        argv = ['rsync', '--archive', *(['--copy-links'] if dereference else []), '--protect-args', '--partial', '-e', shlex.join(self.ssh()[:-1]),
                source_arg, 'root@'+self.ip+':'+str(destination)]
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        require(result.returncode == 0, 'AWS worker asset upload failed')

    def json_file(self, path, optional=False):
        script = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
if not p.exists() and sys.argv[2]=='optional': print('null');sys.exit()
assert p.is_file() and not p.is_symlink() and p.stat().st_size<=16777216
print(json.dumps(json.loads(p.read_bytes())))
'''
        return self.run(['python3', '-B', '-c', script, path, 'optional' if optional else 'required'], json_output=True)


def session_intent(state, tools=None):
    state = Path(state).absolute(); ident(state.name)
    value = load(state/'intent.json')
    require(value.get('session_id') == state.name and value.get('provider_name') == 'aws'
            and value.get('kind') == 'managed-private-msa-session', 'Not an exact managed AWS MSA session')
    if tools is not None:
        require(Path(value['tools']).resolve() == Path(tools).resolve(), 'AWS launch tools differ from the frozen session')
    require(value.get('warm') == 'prefetch' and value.get('idle_seconds') == 900
            and value.get('search_profile', {}).get('profile_id') == PROFILE,
            'AWS session must use the existing resident profile and 15-minute idle timer')
    require(value.get('requested_worker') in (None, INSTANCE_TYPE), 'Requested AWS worker differs from the configured R6A worker; no implicit fallback')
    import session
    require(value.get('sources') == session.sources(Path(value['tools'])), 'AWS frozen session source changed')
    return value


def remote_output(state):
    ident(Path(state).name)
    return '/mnt/bio-shared/runs/msa-aws-'+Path(state).name+'/out'


def sync_output(controller, state, request_id=None):
    state = Path(state).absolute(); intent = session_intent(state)
    if request_id is not None:
        ident(request_id)
    launch = load(state/'launch.json')
    require(launch.get('session_id') == state.name and launch.get('remote_out') == remote_output(state),
            'AWS output path is not bound to this session')
    proof = controller.provider_check(state, dict(instance=launch['instance'], ip=launch['ip'],
                                                 os_id=launch['provider']['os_id']))
    require(proof['token'] == launch['provider']['token'] and proof['db_volume_id'] == launch['provider']['db_volume_id'],
            'AWS output worker lease changed')
    transport = Transport(state, controller.config, proof['ip'])
    require(sha(transport.known) == launch['known_hosts_sha256'], 'AWS output SSH host key changed')
    target = Path(remote_output(state)); target.mkdir(parents=True, exist_ok=True)
    require(target.resolve() == target, 'AWS output directory is symlinked')
    script = '''import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); out={}
for name in json.loads(sys.argv[2]):
 p=root/name
 if p.exists():
  assert p.is_file() and not p.is_symlink() and p.stat().st_size<=4194304
  out[name]=json.loads(p.read_bytes())
print(json.dumps(out))
'''
    observed = transport.run(['python3', '-B', '-c', script, str(target), json.dumps(METADATA)], json_output=True)
    require(isinstance(observed, dict) and set(observed) <= set(METADATA), 'Unexpected AWS metadata path')
    for name, document in observed.items():
        require(isinstance(document, dict), 'Invalid AWS session metadata')
        if name in ('session-starting.json', 'session-ready.json', 'session-closed.json', 'worker-status.json'):
            require(document.get('session_id') == state.name, 'AWS mirrored metadata belongs to another session')
        prior = load(target/name, optional=True)
        if name in ('session-ready.json', 'session-closed.json') and prior:
            require(prior == document, 'Immutable AWS session metadata changed')
        atomic(target/name, document)
    if request_id is not None:
        output = target/'requests'/request_id
        terminal = transport.json_file(str(output/'status.json'))
        require(terminal.get('request_id') == request_id and terminal.get('session_id') == state.name
                and terminal.get('status') in ('complete', 'failed'), 'AWS request is not terminal and owned by this session')
        existing = load(output/'status.json', optional=True)
        if existing is not None:
            require(existing == terminal, 'Retained AWS terminal request changed')
        else:
            staging = output.parent/('.'+request_id+'.'+uuid.uuid4().hex)
            staging.mkdir(parents=True, mode=0o700)
            try:
                argv = ['rsync', '--archive', '--protect-args', '--safe-links', '-e', shlex.join(transport.ssh()[:-1]),
                        'root@'+transport.ip+':'+str(output)+'/', str(staging)+'/']
                result = subprocess.run(argv, capture_output=True, timeout=600)
                require(result.returncode == 0, 'AWS terminal request synchronization failed')
                files, size = 0, 0
                for path in staging.rglob('*'):
                    require(not path.is_symlink(), 'AWS request contains an unsupported symlink')
                    if path.is_file():
                        files += 1; size += path.stat().st_size
                    require(files <= 100000 and size <= 32*1024**3, 'AWS request exceeds retained-output bounds')
                require(load(staging/'status.json') == terminal, 'AWS request changed while synchronizing')
                require(not output.exists(), 'Concurrent AWS request publication must be reconciled')
                os.rename(staging, output)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
    return dict(provider='aws', status='synchronized', session_id=state.name, request_id=request_id)


class Launcher:
    """Head-side SSH and source transfer, bounded by the same paid reservation."""
    def __init__(self, controller, state, tools, *, prepare=False, before_reservation=None):
        self.c, self.state, self.tools = controller, Path(state).absolute(), Path(tools).resolve()
        self.prepare_only = prepare
        self.before_reservation = before_reservation
        if prepare:
            self.intent = load(self.state/'intent.json')
            require(self.intent.get('kind') == 'managed-aws-msa-preparation' and self.intent.get('session_id') == self.state.name
                    and self.intent.get('tools') == str(self.tools), 'AWS preparation identity differs')
            import session
            require(self.intent.get('sources') == session.sources(self.tools), 'AWS preparation frozen sources changed')
        else:
            self.intent = session_intent(self.state, self.tools)
        self.transport = None
        self.remote = '/opt/bio-aws-msa/'+self.state.name
        self.out = remote_output(self.state)
        self.deadline = None

    def progress(self, stage, message, **extra):
        value = dict(schema=1, scope='msa', stage=stage, state='running', message=message,
                     stage_id=self.state.name+':'+stage, timestamp_ns=time.time_ns(), **extra)
        atomic(self.state/'startup-progress.json', value)
        print('BIO_WORKER_STAGE '+json.dumps(value, sort_keys=True), flush=True)

    def check(self):
        lease = self.c.lease(self.state)
        receipt = self.c.budget.check(lease['token'])
        require(time.time() < receipt['deadline']-60, 'AWS worker reached its cleanup reserve')
        self.deadline = receipt['deadline']
        return receipt

    def local(self, argv, name, *, timeout=None, cancelled=None):
        self.check()
        log = self.state/(name+'.log')
        with log.open('ab') as stream:
            child = subprocess.Popen([str(x) for x in argv], stdout=stream, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            started = time.monotonic()
            try:
                while child.poll() is None:
                    self.check()
                    require(cancelled is None or not cancelled.is_set(), 'AWS '+name+' cancelled after another transfer failed')
                    require(timeout is None or time.monotonic()-started < timeout, 'AWS '+name+' exceeded its bounded timeout')
                    time.sleep(2)
                require(child.returncode == 0, 'AWS '+name+' failed; inspect its retained log')
            finally:
                if child.poll() is None:
                    try: os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try: os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        child.wait()

    def remote_json(self, action, args=(), *, name=None, cancelled=None):
        argv = self.transport.ssh()+[shlex.join(['python3', '-B', self.remote+'/tools/msa/aws_worker.py', action, *map(str, args)])]
        filename = self.state/((name or action)+'.json')
        self.check()
        with filename.open('wb') as output, (self.state/((name or action)+'.log')).open('ab') as errors:
            child = subprocess.Popen(argv, stdout=output, stderr=errors, start_new_session=True)
            try:
                while child.poll() is None:
                    self.check()
                    require(cancelled is None or not cancelled.is_set(), 'AWS '+action+' cancelled after another transfer failed')
                    time.sleep(1)
                require(child.returncode == 0, 'AWS '+action+' failed; inspect retained worker receipt/log')
            finally:
                if child.poll() is None:
                    try: os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try: os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        child.wait()
        return load(filename)

    def upload_json(self, value, name):
        path = self.state/name; atomic(path, value)
        self.transport.upload(path, self.remote+'/'+name)
        return self.remote+'/'+name

    def wait_worker(self):
        self.progress('worker_start', 'Starting the persistent AWS CPU worker')
        while True:
            self.check()
            try:
                pool, lease, inventory, proof = self.c.observe(self.state)
                self.transport = Transport(self.state, self.c.config, proof['ip'])
                # First successful authentication retains the key; every later command pins it.
                user = 'root' if pool.get('hostname') else 'ubuntu'
                self.transport.run(['true'], user=user, first=True, timeout=20)
                os.chmod(self.transport.known, 0o600)
                if user == 'ubuntu':
                    script = ('install -d -m700 /root/.ssh; install -m600 /home/ubuntu/.ssh/authorized_keys /root/.ssh/authorized_keys; '
                              'hostnamectl set-hostname gc-msa-'+pool['pool_id'][:12])
                    self.transport.run(['sudo', 'bash', '-ceu', script], user='ubuntu')
                identity = self.transport.run(['python3', '-B', '-c',
                    "import json,pathlib,socket;print(json.dumps({'hostname':socket.gethostname(),'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))"], json_output=True)
                with self.c.lock():
                    pool = self.c.pool(); pool.update(hostname=identity['hostname'], boot_id=identity['boot_id']); self.c.save(pool)
                break
            except (Error, OSError, subprocess.SubprocessError):
                time.sleep(5)
        while True:
            self.check()
            try:
                self.c.attach(self.state); break
            except Error as exc:
                require('attaching' in str(exc) or 'attachment has not completed' in str(exc), str(exc))
                time.sleep(3)

    def bootstrap(self):
        self.progress('worker_setup', 'Installing system dependencies and transferring the pinned tools')
        self.transport.run(['mkdir', '-p', self.remote, '/mnt/bio-msa-databases', '/mnt/bio-shared/cache/msa-api', self.out])
        self.local(self.transport.ssh()+[shlex.join(['bash', '-ceu',
            'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq python3 rsync zstd e2fsprogs util-linux nvme-cli udev libgomp1 ca-certificates libstdc++6 libgl1 libglib2.0-0 libgfortran5'])], 'system-dependencies', timeout=1200)
        # /etc/bio-tools contains Nix-store symlink directories. Only this frozen
        # source upload dereferences them; runtime/database aliases stay intact.
        self.transport.upload(self.tools, self.remote+'/tools', timeout=120, dereference=True)
        pool, lease, inventory, proof = self.c.observe(self.state)
        inventory['managed'] = dict(instance_id=pool['instance_id'], boot_id=pool['boot_id'])
        creation = pool.get('volume_creation')
        require(creation, 'AWS volume creation receipt is unavailable')
        owner = dict(schema=1, provider='aws', account_id=ACCOUNT, region=REGION,
            availability_zone=self.c.config['availability_zone'], cache_id=pool['pool_id'],
            volume_id=pool['db_volume_id'], filesystem_uuid=pool['filesystem_uuid'],
            source_manifest_sha256=self.c.config['source_manifest_sha256'],
            source_receipt_sha256=self.c.config['source_receipt_sha256'],
            ready_receipt_sha256=pool.get('ready_receipt_sha256'), creation=creation)
        binding_path = self.upload_json(owner, 'binding.json')
        evidence_path = self.upload_json(inventory, 'provider-evidence.json')
        self.remote_json('inspect-device', ['--volume-id', pool['db_volume_id']], name='device-evidence')
        self.transport.upload(self.state/'device-evidence.json', self.remote+'/device-evidence.json')
        flags = ['--binding', binding_path, '--provider-evidence', evidence_path,
                 '--evidence', self.remote+'/device-evidence.json']
        device = load(self.state/'device-evidence.json')
        # Only a previously uninitialized allocation may reach mkfs. An uncertain
        # formatting attempt is retained for explicit inspection, never replayed.
        if not pool.get('filesystem_initialized'):
            require(not pool.get('filesystem_initialize_issued'), 'Previous AWS filesystem initialization is uncertain; inspect exact device before continuing')
            plan = self.remote_json('initialize-plan', flags)
            with self.c.lock():
                pool = self.c.pool(); pool['filesystem_initialize_issued'] = dict(plan=plan, epoch=time.time()); self.c.save(pool)
            self.local(self.transport.ssh()+[shlex.join(plan['command'])], 'format-new-owned-volume', timeout=600)
            self.transport.run(['udevadm', 'trigger', '--action=change', '--subsystem-match=block'])
            self.transport.run(['udevadm', 'settle'])
            with self.c.lock():
                pool = self.c.pool(); pool['filesystem_initialized'] = True; self.c.save(pool)
            self.remote_json('inspect-device', ['--volume-id', pool['db_volume_id']], name='device-evidence')
            self.transport.upload(self.state/'device-evidence.json', self.remote+'/device-evidence.json')
        # Refresh both observations after formatting to avoid stale device plans.
        pool, lease, inventory, proof = self.c.observe(self.state)
        inventory['managed'] = dict(instance_id=pool['instance_id'], boot_id=pool['boot_id'])
        self.upload_json(inventory, 'provider-evidence.json')
        self.remote_json('inspect-device', ['--volume-id', pool['db_volume_id']], name='device-evidence')
        self.transport.upload(self.state/'device-evidence.json', self.remote+'/device-evidence.json')
        if pool.get('ready_receipt_sha256'):
            self.transport.upload(self.c.root/'database-ready.json', self.remote+'/database-ready.json')
            plan = self.remote_json('mount-plan', flags+['--mode', 'serve', '--ready', self.remote+'/database-ready.json',
                                                        '--ready-sha256', pool['ready_receipt_sha256']])
        else:
            plan = self.remote_json('mount-plan', flags+['--mode', 'populate'])
        self.transport.run(plan['command'])
        database_device = plan['device']
        if not pool.get('ready_receipt_sha256'):
            self.populate(owner, database_device)
            pool = self.c.pool(); owner['ready_receipt_sha256'] = pool['ready_receipt_sha256']
            binding_path = self.upload_json(owner, 'binding.json')
            self.transport.run(['sync'])
            self.transport.run(['umount', '/mnt/bio-msa-databases'])
            pool, lease, inventory, proof = self.c.observe(self.state)
            inventory['managed'] = dict(instance_id=pool['instance_id'], boot_id=pool['boot_id'])
            self.upload_json(inventory, 'provider-evidence.json')
            self.remote_json('inspect-device', ['--volume-id', pool['db_volume_id']], name='device-evidence')
            self.transport.upload(self.state/'device-evidence.json', self.remote+'/device-evidence.json')
            self.transport.upload(self.c.root/'database-ready.json', self.remote+'/database-ready.json')
            plan = self.remote_json('mount-plan', flags+['--mode', 'serve', '--ready', self.remote+'/database-ready.json',
                                                        '--ready-sha256', pool['ready_receipt_sha256']])
            self.transport.run(plan['command'])
        self.remote_json('verify', ['--cache', '/mnt/bio-msa-databases', '--binding', binding_path, '--device', database_device])
        self.restore_runtime()

    def populate(self, owner, device):
        self.progress('database_copy', 'Copying the complete pinned MSA database to AWS EBS')
        source = self.c.config['source_database']; work = self.c.root/'source'
        work.mkdir(exist_ok=True)
        # Hold both existing read leases so no installer can replace source bytes
        # while its hashes and transferred snapshot are being bound together.
        locks = []
        try:
            for path in ('/var/lib/dc/bio-submit.lock', '/var/lib/dc/msa-submit.lock'):
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(fd, fcntl.LOCK_SH); locks.append(fd)
            import aws_worker
            inventory = aws_worker.source_inventory(source, work)
            require(inventory['source']['source_manifest_sha256'] == owner['source_manifest_sha256']
                    and inventory['source']['source_receipt_sha256'] == owner['source_receipt_sha256'], 'AWS database source generation changed')
            plan = aws_worker.transfer_plan(source, inventory['inventory'], inventory['inventory_sha256'], work,
                                            'root@'+self.transport.ip, self.transport.key, self.transport.known)
            population = self.remote_json('population-state', ['--cache', '/mnt/bio-msa-databases',
                '--binding', self.remote+'/binding.json', '--device', device])
            stage = population.get('population_state')
            require(stage in ('absent', 'pending', 'renamed', 'published'), 'Unknown AWS database publication state')
            commands = plan['commands'] if stage in ('absent', 'pending') else []
            if commands:
                self.transport.run(['mkdir', '-p', plan['destination']])
            cancelled = threading.Event()
            telemetry, telemetry_lock = {}, threading.Lock()
            def observe(kind, row):
                require(not cancelled.is_set(), 'AWS database copy cancelled')
                with telemetry_lock:
                    telemetry[kind] = row
            copy_started = time.monotonic()
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(self.local, command, 'database-copy-'+str(index), cancelled=cancelled)
                           for index, command in enumerate(commands)]
                sealed_future = executor.submit(aws_worker.seal_source, source, inventory['inventory'],
                                                inventory['inventory_sha256'], work, callback=lambda row: observe('source_hash', row))
                if commands and plan['ranged_entries']:
                    futures.append(executor.submit(self.transfer_ranges, source, inventory, plan, owner, device,
                                                   cancelled, lambda row: observe('ranges', row), sealed_future))
                futures.append(sealed_future)
                try:
                    while True:
                        # The coordinator keeps the budget/deadline check alive
                        # after source hashing or the smaller rsync files finish.
                        self.check()
                        with telemetry_lock:
                            current = copy.deepcopy(telemetry)
                        current.update(schema=1, observed_epoch=time.time(), elapsed_seconds=time.monotonic()-copy_started,
                            payload_bytes=plan['payload_bytes'], range_bytes=plan['range_bytes'], rsync_bytes=plan['rsync_bytes'],
                            small_rsync_completed=sum(f.done() and f.exception() is None for f in futures[:len(commands)]),
                            small_rsync_jobs=len(commands))
                        atomic(self.state/'database-transfer-progress.json', current)
                        ranged = current.get('ranges', {})
                        self.progress('database_copy', 'Streaming or checksum-verifying existing database files; full tree verification follows',
                            completed=ranged.get('aggregate_processed_bytes', ranged.get('aggregate_bytes_sent', 0)),
                            total=plan['range_bytes'], unit='ranged bytes streamed or checksum-verified',
                            fresh_bytes_sent=ranged.get('aggregate_bytes_sent', 0),
                            reused_verified_bytes=ranged.get('aggregate_reused_verified_bytes', 0),
                            completed_file_bytes=ranged.get('aggregate_completed_bytes', 0),
                            transfer_phase=ranged.get('phase'),
                            transfer_bytes_per_second=ranged.get('aggregate_bytes_per_second'),
                            range_transfer_eta_seconds=ranged.get('aggregate_eta_seconds'),
                            active_file=ranged.get('path'), source_hash_completed_bytes=current.get('source_hash', {}).get('completed_bytes'))
                        done, pending = wait(futures, timeout=1, return_when=FIRST_EXCEPTION)
                        for future in done:
                            future.result()
                        if not pending:
                            break
                    sealed = sealed_future.result()
                except BaseException:
                    cancelled.set()
                    for future in futures:
                        future.cancel()
                    raise
            atomic(self.state/'source-sealed.json', sealed)
            self.transport.upload(sealed['source_manifest'], self.remote+'/source-manifest.json')
            self.progress('database_verify', 'Verifying every transferred database byte against the source SHA256 manifest')
            result = self.remote_json('publish', ['--cache', '/mnt/bio-msa-databases', '--binding', self.remote+'/binding.json',
                '--source-manifest', self.remote+'/source-manifest.json', '--source-manifest-sha256', sealed['source_manifest_sha256'],
                '--device', device], name='database-publication')
            raw = self.transport.run(['cat', '/mnt/bio-msa-databases/ready.json'])
            require(hashlib.sha256(raw).hexdigest() == result['ready_receipt_sha256'], 'AWS ready receipt byte hash differs')
            destination = self.c.root/'database-ready.json'
            if destination.exists():
                require(destination.read_bytes() == raw, 'Previously retained AWS database readiness changed')
            else:
                with destination.open('xb') as stream:
                    os.chmod(destination, 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            with self.c.lock():
                pool = self.c.pool(); pool['ready_receipt_sha256'] = result['ready_receipt_sha256']; self.c.save(pool)
        finally:
            for fd in reversed(locks):
                os.close(fd)

    def transfer_ranges(self, source, inventory, transfer_plan, owner, device, cancelled, callback, sealed_future):
        """Reuse fully checked files or send one file with at most 32 SSH streams."""
        import aws_transfer
        import aws_worker
        total = transfer_plan['range_bytes']
        completed = sent_completed = reused = 0
        started = time.monotonic()
        receipts = []
        sealed = None
        remote_helper = self.remote+'/tools/msa/aws_transfer.py'
        for index, entry in enumerate(transfer_plan['ranged_entries']):
            require(not cancelled.is_set(), 'AWS ranged transfer cancelled')
            plan = aws_transfer.make_plan(aws_worker, source, inventory['inventory'], inventory['inventory_sha256'],
                                          owner, device, entry['path'], transfer_plan['range_streams'])
            stem = 'database-range-'+str(index)
            path = self.state/(stem+'.json'); atomic(path, plan)
            remote_plan = self.remote+'/'+path.name
            self.local(['rsync', '--archive', '--protect-args', '-e', shlex.join(self.transport.ssh()[:-1]),
                        str(path), 'root@'+self.transport.ip+':'+remote_plan], stem+'-plan', timeout=120, cancelled=cancelled)
            base = ['python3', '-B', remote_helper, '--tools', self.remote+'/tools',
                    '--plan', remote_plan, '--plan-sha256', sha(path)]
            ssh = aws_transfer.ssh_argv('root@'+self.transport.ip, self.transport.key, self.transport.known)
            def observe(row=None, phase='streaming'):
                row = row or dict(path=entry['path'], bytes_sent=0, total_bytes=entry['source_metadata']['size'])
                elapsed = max(.001, time.monotonic()-started)
                sent = sent_completed+row['bytes_sent']
                rate = sent/elapsed
                callback(dict(row, phase=phase, aggregate_bytes_sent=sent, aggregate_total_bytes=total,
                              aggregate_reused_verified_bytes=reused, aggregate_completed_bytes=completed,
                              aggregate_processed_bytes=completed+row['bytes_sent'],
                              aggregate_bytes_per_second=rate, aggregate_eta_seconds=max(0, total-reused-sent)/rate if rate else None))
            flags = ['--cache', '/mnt/bio-msa-databases', '--binding', self.remote+'/binding.json', '--device', device,
                     '--range-plan', remote_plan, '--range-plan-sha256', sha(path)]
            candidate = self.remote_json('reuse-file', flags, name=stem+'-candidate', cancelled=cancelled)
            require(candidate.get('path') == entry['path'] and candidate.get('size') == entry['source_metadata']['size']
                    and candidate.get('status') in ('candidate', 'copy_required'), 'Invalid existing-file candidate receipt')
            if candidate['status'] == 'candidate':
                observe(phase='waiting_for_sealed_source')
                if sealed is None:
                    while not sealed_future.done():
                        require(not cancelled.is_set(), 'AWS existing-file verification cancelled')
                        self.check(); cancelled.wait(1)
                    sealed = sealed_future.result()
                    atomic(self.state/'source-sealed.json', sealed)
                    self.local(['rsync', '--archive', '--protect-args', '-e', shlex.join(self.transport.ssh()[:-1]),
                        sealed['source_manifest'], 'root@'+self.transport.ip+':'+self.remote+'/source-manifest.json'],
                        'reuse-source-manifest', timeout=600, cancelled=cancelled)
                observe(phase='existing_file_readback')
                adoption = self.remote_json('reuse-file', flags+['--source-manifest', self.remote+'/source-manifest.json',
                    '--source-manifest-sha256', sealed['source_manifest_sha256']], name=stem+'-reuse', cancelled=cancelled)
                require(adoption.get('path') == entry['path'] and adoption.get('size') == entry['source_metadata']['size']
                        and adoption.get('status') in ('reused', 'copy_required'), 'Invalid existing-file readback receipt')
                if adoption['status'] == 'reused':
                    size = entry['source_metadata']['size']
                    require(adoption.get('source_manifest_sha256') == sealed['source_manifest_sha256']
                            and adoption.get('reused_verified_bytes') == size and adoption.get('readback_bytes') == size,
                            'Existing-file reuse did not fully read the pinned source size')
                    completed += size; reused += size
                    receipt = dict(path=entry['path'], size=size, plan_sha256=sha(path), method='verified-reuse', readback=adoption)
                    atomic(self.state/(stem+'-complete.json'), receipt); receipts.append(receipt)
                    observe(phase='file_reused')
                    continue
            self.local(ssh+[shlex.join([*base, 'initialize'])], stem+'-initialize', cancelled=cancelled)
            result = aws_transfer.transfer(source, plan,
                lambda slot: ssh+[shlex.join([*base, 'receive', '--slot', str(slot)])],
                cancelled=cancelled, callback=observe, log_root=self.state/(stem+'-logs'))
            self.local(ssh+[shlex.join([*base, 'finish'])], stem+'-finish', cancelled=cancelled)
            completed += entry['source_metadata']['size']
            sent_completed += entry['source_metadata']['size']
            observe(phase='file_transferred')
            receipt = dict(path=entry['path'], size=entry['source_metadata']['size'], plan_sha256=sha(path), method='transferred', ranges=result)
            atomic(self.state/(stem+'-complete.json'), receipt)
            receipts.append(receipt)
        require(completed == total, 'Ranged transfer byte count differs from inventory')
        atomic(self.state/'database-ranges-complete.json', dict(schema=1, bytes=completed, files=receipts,
               fresh_bytes_sent=sent_completed, reused_verified_bytes=reused,
               elapsed_seconds=time.monotonic()-started, publication='unpublished; full source and destination SHA verification required'))
        return receipts

    def restore_runtime(self):
        self.progress('runtime_restore', 'Restoring the pinned MSA model runtimes on the persistent OS disk')
        archive = self.c.config['runtime_archive']; plan = self.c.config['runtime_plan']
        present = self.transport.json_file('/opt/bio-worker-runtime/.runtime-archive-receipt.json', optional=True)
        if present is None:
            self.local(['rsync', '--archive', '--partial', '--protect-args', '-e', shlex.join(self.transport.ssh()[:-1]),
                archive, 'root@'+self.transport.ip+':/opt/bio-runtime.tar.zst'], 'runtime-copy')
        else:
            require(present.get('plan_sha256') == self.c.config['runtime_plan_sha256'], 'AWS persistent runtime belongs to another plan')
        self.transport.upload(plan, self.remote+'/runtime-plan.json')
        result = self.remote_json('restore-runtime', ['--archive', '/opt/bio-runtime.tar.zst',
            '--plan', self.remote+'/runtime-plan.json', '--plan-sha256', self.c.config['runtime_plan_sha256'],
            '--tools', self.remote+'/tools', '--runtime-root', '/opt/bio-worker-runtime'])
        for command in result['bind_commands']:
            self.transport.run(['mkdir', '-p', command[-1]])
            self.transport.run(command)

    def register(self):
        proof = self.c.provider_check(self.state)
        job = dict(schema=1, provider_name='aws', model='msa', job='msa-aws-'+self.state.name,
            instance=proof['instance'], ip=proof['ip'], search_profile=self.intent['search_profile'],
            ssh_host_key=dict(known_hosts=str(self.transport.known), sha256=sha(self.transport.known),
                instance=proof['instance'], ip=proof['ip'], trust='first-successful-ssh'))
        path = self.state/'aws-job.json'; atomic(path, job)
        # Call the frozen client executable in its original managed unit context.
        command = [sys.executable, '-B', str(self.tools/'msa/session_client.py'), 'register-launch',
                   '--state', str(self.state), '--job', str(path), '--remote-out', self.out,
                   '--known-hosts', str(self.transport.known)]
        result = subprocess.run(command, capture_output=True, timeout=90)
        require(result.returncode == 0, 'AWS managed session registration failed; exact lease remains retained')

    def run(self):
        require((self.prepare_only or os.environ.get('BIO_MSA_SESSION_ID') == self.state.name)
                and re.fullmatch('[a-f0-9]{32}', os.environ.get('INVOCATION_ID', '')),
                'AWS sessions must run inside their exact managed systemd invocation')
        if self.prepare_only:
            require(self.intent.get('invocation_id') == os.environ.get('INVOCATION_ID'), 'AWS preparation invocation changed')
        else:
            pool = self.c.pool(optional=True)
            require(pool and pool.get('ready_receipt_sha256'), 'AWS database initial preparation is incomplete; normal sessions cannot perform the first copy')
        child = None
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        def interrupted(signum, frame):
            raise InterruptedError('AWS managed launcher interrupted; stopping its exact owned compute')
        for s in previous:
            signal.signal(s, interrupted)
        try:
            self.c.allocate(self.state, float(self.intent['timeout_seconds'])/3600,
                            before_reservation=self.before_reservation)
            self.wait_worker(); self.bootstrap()
            if self.prepare_only:
                receipt = dict(provider='aws', status='prepared', checked_epoch=time.time(),
                    instance=self.c.pool()['instance_id'], db_volume_id=self.c.pool()['db_volume_id'],
                    ready_receipt_sha256=self.c.pool()['ready_receipt_sha256'],
                    runtime_plan_sha256=self.c.config['runtime_plan_sha256'])
                atomic(self.state/'preparation-ready.json', receipt)
                return receipt
            self.register()
            self.progress('index_warm', 'Loading the full private MSA indexes into AWS RAM')
            command = ['env', 'BIO_TOOLS_DIR='+self.remote+'/tools', 'BIO_MSA_SEARCH_PROFILE='+PROFILE,
                'python3', '-B', self.remote+'/tools/msa/session.py', 'serve',
                '--session-id', self.state.name, '--state', '/tmp/bio-msa-session-'+self.state.name,
                '--out', self.out, '--database', '/mnt/bio-msa-databases/colabfold',
                '--tools', self.remote+'/tools', '--tools-root', '/mnt/bio-shared/envs/msa-tools-v1',
                '--results', '/mnt/bio-shared/cache/msa-api', '--deadline', str(self.deadline),
                '--idle-seconds', '900', '--search-profile', PROFILE, '--warm', 'prefetch']
            # The supervised SSH command remains the owner of the native session.
            with (self.state/'aws-session.log').open('ab') as log:
                child = subprocess.Popen(self.transport.ssh()+[shlex.join(command)], stdout=log, stderr=subprocess.STDOUT)
                while child.poll() is None:
                    self.check()
                    sync_output(self.c, self.state)
                    time.sleep(5)
                sync_output(self.c, self.state)
                require(child.returncode == 0, 'Native AWS MSA session failed; retained native log contains its cause')
        finally:
            for s in previous:
                signal.signal(s, signal.SIG_IGN)
            if child is not None and child.poll() is None:
                child.terminate()
                try: child.wait(timeout=15)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
            # Stop exact compute even after bootstrap, native or metadata failures.
            try:
                lease = load(self.state/'aws-lease.json', optional=True)
                pool = self.c.pool(optional=True)
                if lease and pool and pool.get('instance_id'):
                    self.c.close(self.state)
                elif lease:
                    atomic(self.state/'cleanup-pending.json', dict(provider='aws', status='uncertain',
                        reason='Allocation is not yet bound to a visible instance; reconciliation is required', checked_epoch=time.time()))
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)


def prepare_intent(state, tools, timeout, owner_unit):
    state, tools = Path(state).absolute(), Path(tools).resolve()
    ident(state.name)
    require(type(timeout) in (int, float) and math.isfinite(timeout) and 120 <= timeout <= 21600,
            'AWS initial preparation must be bounded to at most six hours')
    require(re.fullmatch('[a-f0-9]{32}', os.environ.get('INVOCATION_ID', '')), 'AWS preparation requires a supervised systemd unit')
    require(isinstance(owner_unit, str) and re.fullmatch(r'bio-[a-zA-Z0-9-]+\.service', owner_unit),
            'AWS preparation requires its exact managed owner unit')
    import session
    proposed = dict(schema=1, kind='managed-aws-msa-preparation', provider_name='aws',
        session_id=state.name, tools=str(tools), sources=session.sources(tools),
        timeout_seconds=timeout, invocation_id=os.environ['INVOCATION_ID'], unit=owner_unit)
    existing = load(state/'intent.json', optional=True)
    if existing:
        require(existing == proposed, 'AWS preparation retry differs from its frozen managed invocation')
    else:
        atomic(state/'intent.json', proposed)
    return proposed


@contextmanager
def startup_proof(state, tools):
    """Use the session's existing proof protocol before any allocation path."""
    state, tools = Path(state).absolute(), Path(tools).resolve()
    session_intent(state, tools)
    helper = tools/'msa/startup.py'
    run = state/('msa-'+time.strftime('%Y%m%d-%H%M%S', time.gmtime())+'-'+str(os.getpid()))
    run.mkdir(mode=0o700)

    def invoke(action, *extra):
        result = subprocess.run([sys.executable, '-B', str(helper), action,
            '--state', str(state), *extra], capture_output=True, timeout=45)
        require(result.returncode == 0,
                'AWS startup '+action+' proof failed; exact registration remains fenced')

    invoke('begin', '--run-dir', str(run))
    try:
        yield lambda: invoke('mark-allocation')
    except BaseException as exc:
        status = 130 if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else 2
        invoke('finish', '--exit-status', str(status))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capacity', 'prepare', 'launch-session', 'provider-check', 'provider-close', 'sync-output', 'watchdog'))
    parser.add_argument('--state', type=Path); parser.add_argument('--tools', type=Path)
    parser.add_argument('--instance'); parser.add_argument('--ip'); parser.add_argument('--os-id'); parser.add_argument('--request-id')
    parser.add_argument('--config'); parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--timeout', type=float, default=21600)
    parser.add_argument('--owner-unit')
    args = parser.parse_args(argv)
    if args.action == 'launch-session':
        require(args.state and args.tools, 'AWS launch requires the frozen state and tools paths')
        with startup_proof(args.state, args.tools) as before_reservation:
            dispatch(args, before_reservation=before_reservation)
    else:
        dispatch(args)


def dispatch(args, *, before_reservation=None):
    try:
        config = configuration(args.config, require_assets=args.action in ('prepare', 'launch-session', 'capacity'))
    except (ValueError, OSError, KeyError):
        if args.action != 'capacity':
            raise
        result = dict(schema=1, provider='aws', msa_provider='aws', compute='cpu', compute_kind='cpu',
            account=ACCOUNT, region=REGION, server_epoch=time.time(), observed_epoch=time.time(), checked_epoch=None, state='error',
            stale_after_seconds=120, msa_available=None, msa_message='AWS MSA configuration/assets are not ready', gpus=[], cpus=[])
        print(json.dumps(result, sort_keys=True)); return
    from aws_budget import Budget
    controller = Controller(AWS(), Budget(state_root='/var/lib/dc', account_id=ACCOUNT, region=REGION), config, args.root)
    if args.action == 'capacity':
        result = controller.capacity()
    elif args.action in ('launch-session', 'prepare'):
        require(args.state and args.tools, 'AWS launch requires the frozen state and tools paths')
        if args.action == 'prepare':
            prepare_intent(args.state, args.tools, args.timeout, args.owner_unit)
        result = Launcher(controller, args.state, args.tools, prepare=args.action == 'prepare',
                          before_reservation=before_reservation).run()
    elif args.action == 'watchdog':
        result = controller.watchdog()
    else:
        require(args.state, 'AWS operation requires the exact session state')
        if args.action == 'provider-check':
            result = controller.provider_check(args.state, dict(instance=args.instance, ip=args.ip, os_id=args.os_id))
        elif args.action == 'provider-close':
            result = controller.close(args.state)
        else:
            result = sync_output(controller, args.state, args.request_id)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, KeyError, TypeError) as error:
        print(json.dumps(dict(provider='aws', status='failed', error=str(error))), file=sys.stderr)
        sys.exit(2)
