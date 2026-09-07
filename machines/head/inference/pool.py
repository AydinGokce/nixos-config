"""Start resident services on an existing budget-managed cloud allocation.

No rental is implicit in a prediction. The existing dc allocator/watchdog owns
the VM; this service is strictly bounded by that allocation's original deadline.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import tarfile
import time

from .common import atomic_json, configuration_id, digest, identifier, now, read, sha256
from .control_storage import ensure as ensure_control_storage


def ssh_command(target, argv, *, ssh_options=()):
    return ['ssh', '-i', target['key'], '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + target['known_hosts'],
            *ssh_options,
            'root@' + target['ip'], shlex.join([str(x) for x in argv])]


def remote(target, argv, *, input=None, timeout=60, ssh_options=()):
    return subprocess.run(ssh_command(target, argv, ssh_options=ssh_options), input=input, capture_output=True,
                          check=True, timeout=timeout).stdout


def source_assets(root):
    """Logical toolkit names and exact files to pin into a new worker archive."""
    assets = {str(path.relative_to(root)): path
              for folder in ('inference', 'msa', 'rf3', 'library')
              for path in sorted((root / folder).rglob('*.py'))
              if not path.name.startswith('test_') and '__pycache__' not in path.parts}
    for relative in ('rf3/requirements.lock', 'py/public_msa_client.py'):
        path = root / relative
        if not path.is_file():
            raise ValueError('Resident toolkit is missing required source: ' + relative)
        assets[relative] = path
    return assets


def relocate_config(value, aliases):
    if isinstance(value, dict):
        return {key: relocate_config(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate_config(item, aliases) for item in value]
    if isinstance(value, str):
        for old, new in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
            if value == old or value.startswith(old + '/'):
                return new + value[len(old):]
    return value


def admission(target, deadline):
    helper = runpy.run_path(os.environ.get('DC_HELPER', '/etc/bio-tools/dc-budget.py'))
    api = helper['API']()
    store = helper['Store'](os.environ.get('DC_STATE_DIR', '/var/lib/dc'))
    controller = helper['Controller'](api, store)
    with store.locked(now()) as state:
        inventory = controller.refresh(state)
        report = helper['summary'](state, now(), controller.persistent_hours)
        if (not 0 <= now() - state.get('last_watchdog', 0) <= 180 or report['uncertain'] or report['storage_uncertain'] or
                report['spent'] + report['reserved'] + report['background_reserve'] + controller.margin > controller.ceiling):
            raise ValueError('Resident work exceeds or cannot verify the existing cloud budget')
        rows = [row for row in inventory[0] if row['id'] == target['instance_id']]
        jobs = [(key, value) for key, value in state['jobs'].items() if value.get('id') == target['instance_id']]
        if len(rows) != 1 or len(jobs) != 1:
            raise ValueError('Existing allocation is not uniquely managed')
        token, job = jobs[0]
        if (rows[0]['status'] != 'running' or rows[0]['ip'] != target['ip'] or
                rows[0].get('os_volume_id') != target['os_id'] or job.get('os_id') != target['os_id'] or
                job['status'] != 'running' or deadline > job['deadline'] or deadline <= now() + 120):
            raise ValueError('Allocation identity/lifetime differs')
        if not any(row['id'] == target['os_id'] for row in inventory[1]) or any(row['id'] == target['os_id'] for row in inventory[2]):
            raise ValueError('Allocation OS disk is not exclusively active')
        return {'checked_epoch': now(), 'token': token, 'instance_id': target['instance_id'],
                'os_id': target['os_id'], 'deadline_epoch': deadline,
                'original_deadline_epoch': target['original_deadline_epoch'], 'reservation_deadline_epoch': job['deadline'],
                'budget': report, 'ceiling': controller.ceiling}


def service_command(session, python, worker_script, session_path, session_sha):
    resources = session.get('resources', {})
    cpus = max(1, int(resources.get('cpus', 8)))
    remaining = int(session['deadline_epoch'] - now())
    if remaining < 120:
        raise ValueError('Insufficient allocation time to start resident worker')
    work = session['config']['work_dir']
    cache = session['cache_root']
    spool = str(Path(session['spool_root']) / session['worker_id'])
    inputs = session.get('input_root', '/mnt/bio-shared/inference/jobs')
    if any(any(char.isspace() for char in path) for path in (work, cache, spool, inputs)):
        raise ValueError('Systemd runtime directories must not contain whitespace')
    properties = ['Type=exec', 'KillMode=control-group', 'TimeoutStopSec=30',
                  'CPUQuota=' + str(cpus * 100) + '%', 'TasksMax=4096',
                  'MemoryHigh=' + str(resources.get('memory_high', '48G')),
                  'MemoryMax=' + str(resources.get('memory_max', '64G')),
                  'RuntimeMaxSec=' + str(remaining), 'Restart=no', 'RemainAfterExit=yes',
                  'PrivateNetwork=yes', 'PrivateTmp=yes', 'ProtectSystem=strict',
                  'ReadWritePaths=' + ' '.join([work, cache, spool, inputs, session['gpu_lock']]),
                  'Environment=CUDA_VISIBLE_DEVICES=' + session['gpu_uuid'],
                  'Environment=PYTHONPYCACHEPREFIX=' + cache + '/python',
                  'Environment=TORCH_EXTENSIONS_DIR=' + cache + '/torch-extensions',
                  'Environment=TORCHINDUCTOR_CACHE_DIR=' + cache + '/inductor',
                  'Environment=TRITON_CACHE_DIR=' + cache + '/triton',
                  'Environment=CUDA_CACHE_PATH=' + cache + '/cuda',
                  'Environment=PATH=' + str(Path(python).parent) + ':/usr/local/cuda/bin:/usr/bin:/bin']
    for key, value in session.get('environment', {}).items():
        if key in ('CUDA_VISIBLE_DEVICES', 'PYTHONPATH', 'PYTHONPYCACHEPREFIX') or any(c.isspace() for c in key + str(value)):
            raise ValueError('Unsafe or reserved runtime environment override')
        properties.append('Environment=' + key + '=' + str(value))
    command = ['systemd-run', '--quiet', '--unit', 'bio-resident-' + session['worker_id']]
    for value in properties:
        command += ['--property', value]
    return command + [python, worker_script, '--session', session_path, '--expected-sha256', session_sha]


def start(target, config, policy, state_root):
    state_root = Path(state_root)
    worker_id = identifier(policy['worker_id'])
    deadline = min(float(policy['deadline_epoch']), float(target['original_deadline_epoch']))
    proof = admission(target, deadline)
    if sha256(target['known_hosts']) != target['known_hosts_sha256']:
        raise ValueError('Pinned worker SSH host keys changed')
    probe_code = '''import json,pathlib,socket,subprocess
print(json.dumps({'hostname':socket.gethostname(),'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
'gpus':subprocess.check_output(['nvidia-smi','--query-gpu=uuid,name','--format=csv,noheader'],text=True),
'active':subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True)}))'''
    observed = json.loads(remote(target, ['/usr/bin/python3', '-c', probe_code]))
    if observed['boot_id'] != target['boot_id'] or observed['hostname'] != target['hostname']:
        raise ValueError('Worker boot/host identity changed')
    if policy['gpu_uuid'] not in {line.split(',')[0].strip() for line in observed['gpus'].splitlines()}:
        raise ValueError('Selected physical GPU is unavailable')
    if any(line.split(',')[0].strip() == policy['gpu_uuid'] for line in observed['active'].splitlines()):
        raise ValueError('Selected physical GPU is occupied')
    root = Path(__file__).absolute().parent
    assets = source_assets(root.parent)
    sources = {name: sha256(path) for name, path in assets.items()}
    source_id = digest(sources)
    remote_source = '/opt/bio-inference/' + source_id
    remote(target, ['mkdir', '-p', remote_source])
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for name in sources:
            tar.add(assets[name], arcname=name, recursive=False)
    remote(target, ['tar', '-xzf', '-', '-C', remote_source, '--no-same-owner'], input=archive.getvalue())
    control = policy.get('control_storage', {
        'source': os.environ.get('BIO_INFERENCE_CONTROL_SOURCE', ''),
        'mountpoint': '/mnt/bio-inference-control'})
    control_head = ensure_control_storage(control['source'], control['mountpoint'])
    control_worker = json.loads(remote(target, ['/usr/bin/python3', remote_source + '/inference/control_storage.py',
        '--source', control['source'], '--mountpoint', control['mountpoint'], '--allow-mount'], timeout=75))
    spool = policy.get('spool_root', control['mountpoint'] + '/spool')
    if '..' in Path(spool).parts or not Path(spool).is_relative_to(Path(control['mountpoint'])):
        raise ValueError('Resident spool must use the dedicated control mount exclusively')
    config = json.loads(json.dumps(config))
    image_pin = policy['runtime_image']
    runtime = json.loads(remote(target, ['/usr/bin/python3', remote_source + '/inference/runtime_image.py',
                                        'validate', '--image', image_pin['path']], timeout=600))
    if runtime['sha256'] != image_pin['sha256'] or runtime['model'] != config['model']:
        raise ValueError('Local immutable model runtime differs from its declared identity')
    config = relocate_config(config, runtime['path_aliases'])
    if config['model'] == 'rf3':
        config['tools_dir'] = remote_source
    environment = dict(runtime['environment'], **policy.get('environment', {}))
    config['runtime_generation'] = {'adapter_source_sha256': source_id,
        'runtime_image_sha256': runtime['sha256'], 'runtime_specification_sha256': runtime['specification_sha256'],
        'environment': environment, 'resources': policy.get('resources', {})}
    if not Path(config['work_dir']).is_absolute():
        raise ValueError('Worker scratch must be an absolute path')
    session = dict(policy, schema=1, config=config, config_id=configuration_id(config), config_sha256=digest(config), deadline_epoch=deadline,
                   source_files={remote_source + '/' + key: value for key, value in sources.items()},
                   admission=proof, physical_worker={**observed, 'instance_id': target['instance_id'], 'os_id': target['os_id']})
    session['environment'] = environment
    session['python'] = runtime['python']
    session['runtime_image'] = image_pin
    session['tools_root'] = remote_source
    session['gpu_lock'] = '/run/bio-inference-locks/' + identifier(policy['gpu_uuid']) + '.lock'
    session['spool_root'] = spool
    session['control_storage'] = dict(control, head=control_head, worker=control_worker)
    session.setdefault('input_root', '/mnt/bio-shared/inference/jobs')
    session.setdefault('cache_root', '/var/cache/bio-inference/' + source_id)
    session.setdefault('idle_seconds', 900)
    worker_root = Path(session['spool_root']) / worker_id
    if (state_root / 'workers' / (worker_id + '.json')).exists():
        raise ValueError('Resident worker id already registered; retain its history and choose a new id')
    remote(target, ['mkdir', '-p', str(worker_root), config['work_dir'], session['cache_root'], session['input_root']])
    remote(target, ['mkdir', '-p', '/run/bio-inference-locks'])
    remote(target, ['touch', session['gpu_lock']])
    session_path = worker_root / 'session.json'
    # Both head and GPU mount the same provider export. The head is the writer
    # of the immutable session and launch intent, independently of GPU lifetime.
    atomic_json(session_path, session, exclusive=True)
    session_sha = sha256(session_path)
    code = 'import hashlib,pathlib,sys; assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()==sys.argv[2]'
    remote(target, ['/usr/bin/python3', '-c', code, str(session_path), session_sha])
    command = service_command(session, runtime['python'], remote_source + '/inference/worker.py', str(session_path), session_sha)
    record = {'session': session, 'session_sha256': session_sha, 'command': command, 'target': target,
              'unit': 'bio-resident-' + worker_id + '.service', 'created_epoch': now()}
    launch_path = state_root / 'launches' / worker_id / 'intent.json'
    atomic_json(launch_path, record, exclusive=True)
    config_path = state_root / 'configs' / (session['config_id'] + '.json')
    if config_path.exists():
        if configuration_id(read(config_path)) != session['config_id']:
            raise ValueError('Registered configuration changed')
    else:
        try:
            atomic_json(config_path, config, exclusive=True)
        except FileExistsError:
            if configuration_id(read(config_path)) != session['config_id']:
                raise ValueError('Concurrent configuration publication differs')
    remote(target, command, timeout=45)
    state = remote(target, ['systemctl', 'show', record['unit'], '-p', 'InvocationID', '-p', 'MainPID', '-p', 'ActiveState']).decode()
    atomic_json(launch_path.with_name('started.json'), {'unit_state': state, 'session_sha256': session_sha}, exclusive=True)
    atomic_json(state_root / 'workers' / (worker_id + '.json'), session, exclusive=True)
    return {'worker_id': worker_id, 'config_id': session['config_id'], 'unit_state': state,
            'deadline_epoch': deadline, 'spool': str(worker_root)}
