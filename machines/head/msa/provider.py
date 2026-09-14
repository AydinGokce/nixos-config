"""Explicit head-provider routing; native search and session protocols stay shared.

Existing immutable sessions without a provider binding remain Verda sessions.
AWS checks/synchronization delegate to the independently budgeted head helper;
this module never imports credentials or manages a cloud resource itself.
"""
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selected(value=None):
    value = os.environ.get('BIO_MSA_PROVIDER', 'verda') if value is None else value
    require(value in ('verda', 'aws'), 'Unknown configured private MSA provider')
    return value


def name(intent):
    return selected(intent.get('provider_name', 'verda'))


def launch_binding(value=None):
    value = selected(value)
    if value == 'verda':
        return dict(provider_name=value)
    helper = shutil.which('bio-aws-msa')
    require(helper is not None, 'Configured AWS MSA helper is unavailable')
    helper = Path(helper).resolve(strict=True)
    require(helper.is_file(), 'Configured AWS MSA helper is not a file')
    return dict(provider_name=value, provider_helper=str(helper), provider_helper_sha256=sha(helper))


def helper(intent):
    require(name(intent) == 'aws', 'AWS routing requires a frozen AWS session')
    path = Path(intent.get('provider_helper', ''))
    require(path.is_absolute() and path.is_file() and sha(path) == intent.get('provider_helper_sha256'),
            'Frozen AWS session helper changed or is unavailable')
    return str(path)


def invoke(intent, state, command, *, timeout=60, request_id=None, job=None):
    require(command in ('provider-check', 'provider-close', 'sync-output'), 'Unsupported AWS session operation')
    argv = [helper(intent), command, '--state', str(state)]
    if command == 'provider-check':
        require(isinstance(job, dict), 'AWS provider check needs its exact managed worker')
        argv += ['--tools', intent['tools'], '--instance', job['instance'], '--ip', job['ip']]
        if job.get('os_id'):
            argv += ['--os-id', job['os_id']]
    if request_id is not None:
        require(command == 'sync-output' and re.fullmatch(r'[a-f0-9]{32}', request_id), 'Invalid AWS request synchronization')
        argv += ['--request-id', request_id]
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)
        require(len(result.stdout) <= 1024*1024, 'Oversized AWS session helper response')
        value = json.loads(result.stdout)
        require(isinstance(value, dict) and value.get('provider') == 'aws', 'AWS helper returned another provider')
        return value
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise ValueError('AWS session operation failed or is uncertain; its exact registration is retained') from None


def check_proof(value, job):
    require(value.get('provider') == 'aws' and value.get('region') == 'us-east-1'
            and isinstance(value.get('account'), str) and re.fullmatch(r'[0-9]{12}', value['account']),
            'AWS session provider scope differs')
    require(isinstance(value.get('instance'), str) and value.get('instance') == job.get('instance')
            and re.fullmatch(r'i-(?:[a-f0-9]{8}|[a-f0-9]{17})', value['instance'])
            and value.get('ip') == job.get('ip'), 'AWS managed worker identity differs')
    ipaddress.IPv4Address(value['ip'])
    for key in ('os_id', 'db_volume_id'):
        require(isinstance(value.get(key), str) and re.fullmatch(r'vol-(?:[a-f0-9]{8}|[a-f0-9]{17})', value[key]),
                'AWS persistent storage identity is missing')
    require(value['os_id'] != value['db_volume_id'], 'AWS OS and database volumes conflict')
    prior = job.get('provider', {})
    require(all(value.get(key) == prior[key] for key in
                ('os_id', 'db_volume_id', 'token', 'region', 'account') if key in prior),
            'AWS registered storage or reservation changed')
    require(not job.get('os_id') or value['os_id'] == job['os_id'], 'AWS registered OS volume changed')
    require(isinstance(value.get('token'), str) and re.fullmatch(r'[a-f0-9]{32}', value['token']),
            'AWS managed reservation identity is missing')
    require(type(value.get('checked_epoch')) in (int, float) and -5 <= time.time()-value['checked_epoch'] <= 120,
            'AWS provider receipt is stale')
    require(type(value.get('reservation_deadline')) in (int, float) and math.isfinite(value['reservation_deadline'])
            and value['reservation_deadline'] > time.time()+60
            and isinstance(value.get('hostname'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', value['hostname'])
            and isinstance(value.get('budget'), dict), 'AWS managed budget/host receipt is incomplete')
    return value


def closed(value, launch):
    prior = launch['provider']
    require(prior.get('provider') == 'aws' and prior.get('region') == 'us-east-1'
            and isinstance(prior.get('account'), str) and re.fullmatch(r'[0-9]{12}', prior['account'])
            and isinstance(prior.get('token'), str) and re.fullmatch(r'[a-f0-9]{32}', prior['token'])
            and all(isinstance(prior.get(key), str) and re.fullmatch(r'vol-(?:[a-f0-9]{8}|[a-f0-9]{17})', prior[key])
                    for key in ('os_id', 'db_volume_id'))
            and prior['os_id'] != prior['db_volume_id'], 'AWS original ownership receipt is incomplete')
    require(type(value.get('checked_epoch')) in (int, float) and -5 <= time.time()-value['checked_epoch'] <= 120,
            'AWS stop receipt is stale')
    require(value.get('provider') == 'aws' and value.get('status') == 'closed'
            and value.get('compute_stopped') is True and value.get('instance_state') == 'stopped'
            and value.get('budget_released') is True
            and all(value.get(key) == prior.get(key) for key in ('instance', 'os_id', 'db_volume_id', 'token', 'region', 'account'))
            and value.get('instance') == launch['instance'],
            'AWS compute stop and retained-storage ownership are not confirmed')
    return value
