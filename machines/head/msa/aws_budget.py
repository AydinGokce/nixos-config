#!/usr/bin/env python3
"""AWS reservations in the same locked project ledger as the Verda workers.

This module never calls a cloud API. The provider controller supplies observed
resource identities and states; absence from a different provider's inventory
cannot stop AWS billing. Storage is charged after compute is stopped.
"""
import copy
import math
import os
from pathlib import Path
import re
import runpy
import time


DC = runpy.run_path(str(Path(__file__).resolve().parent.parent / 'dc-budget.py'))
Error = DC['Error']


def require(value, message):
    if not value:
        raise Error(message)


def finite(value, name):
    return DC['number'](value, name)


def identifier(value, kind):
    require(isinstance(value, str) and re.fullmatch(kind + r'-[0-9a-f]{8,17}', value),
            'Invalid AWS resource identifier')
    return value


class Budget:
    def __init__(self, state_root='/var/lib/dc', account_id='147997164104',
                 region='us-east-1', *, clock=time.time):
        require(re.fullmatch(r'[0-9]{12}', account_id) is not None, 'Invalid AWS account')
        require(region == 'us-east-1', 'AWS region differs from the reviewed region')
        self.root, self.account_id, self.region = Path(state_root), account_id, region
        self.store, self.clock = DC['Store'](state_root), clock
        self.ceiling = min(DC['PROJECT_BUDGET_CEILING'], finite(os.environ.get(
            'DC_BUDGET_CEILING', DC['PROJECT_BUDGET_CEILING']), 'project ceiling'))
        self.margin = max(10, finite(os.environ.get('DC_BUDGET_MARGIN', 10), 'margin'))
        self.persistent_hours = max(24, finite(os.environ.get('DC_PERSISTENT_RESERVE_HOURS', 24), 'storage reserve'))

    def aws(self, state):
        value = state.setdefault('aws', dict(version=1, account_id=self.account_id,
            region=self.region, resources={}, reservations={}, last_watchdog=0))
        require(value.get('version') == 1 and value.get('account_id') == self.account_id
                and value.get('region') == self.region, 'AWS budget account or region changed')
        # Validate all retained counters before using or changing this subledger.
        DC['aws_summary'](state, self.clock(), self.persistent_hours)
        return value

    def existing(self):
        require(self.store.path.is_file(), 'Existing project spending ledger is required; refusing to reset history')

    def job(self, aws, token):
        require(isinstance(token, str) and re.fullmatch(r'[0-9a-f]{32}', token), 'Invalid AWS budget token')
        value = aws['reservations'].get(token)
        require(value is not None, 'AWS operation has no project budget reservation')
        return value

    def receipt(self, state, job):
        return dict(token=job['token'], deadline=job['deadline'], status=job['status'],
            account_id=self.account_id, region=self.region, ceiling=self.ceiling,
            budget=DC['summary'](state, self.clock(), self.persistent_hours))

    def reserve(self, token, compute_hourly, additional_storage_hourly, hours):
        self.existing()
        require(isinstance(token, str) and re.fullmatch(r'[0-9a-f]{32}', token), 'Invalid AWS budget token')
        rate, storage, hours = (finite(v, n) for v, n in ((compute_hourly, 'AWS compute'),
            (additional_storage_hourly, 'AWS storage'), (hours, 'AWS runtime')))
        require(rate > 0 and 0 < hours <= 24, 'AWS runtime and compute quote must be positive and bounded')
        now = self.clock()
        with self.store.admission(), self.store.locked(now) as state:
            # Admission/accounting locks can wait while the watchdog publishes
            # a newer heartbeat. Compare against the clock after acquisition.
            now = self.clock()
            aws = self.aws(state)
            DC['check_storage_lifetime'](self.root, [], now)
            if token in aws['reservations']:
                prior = self.job(aws, token)
                require(prior['status'] != 'closed' and prior['deadline'] > now
                        and prior['compute_hourly'] == rate and prior['storage_quote_hourly'] == storage
                        and prior['hours'] == hours, 'AWS retry differs from its original reservation')
                return self.receipt(state, prior)
            require(0 <= now - state['last_watchdog'] <= 180, 'Project budget watchdog is stale')
            require(0 <= now - aws['last_watchdog'] <= 180, 'AWS budget watchdog is stale')
            report = DC['summary'](state, now, max(self.persistent_hours, hours))
            require(not report['uncertain'] and not report['storage_uncertain'], 'Unresolved cloud spending blocks a new AWS allocation')
            projected = (report['spent'] + report['reserved'] + report['background_reserve']
                + rate * hours + storage * max(self.persistent_hours, hours) + self.margin)
            require(projected < self.ceiling, f'BUDGET HALT: projected ${projected:.2f} >= ${self.ceiling:.2f}')
            job = dict(token=token, status='pending', created=now, deadline=now + hours * 3600,
                hours=hours, compute_hourly=rate, storage_quote_hourly=storage,
                pending_storage_hourly=storage, instance_id=None, volume_ids=[])
            aws['reservations'][token] = job
            return self.receipt(state, job)

    def check(self, token):
        self.existing()
        now = self.clock()
        with self.store.locked(now) as state:
            # Parallel transfers can wait behind a watchdog update. A timestamp
            # sampled before that wait would see its fresh heartbeat as future.
            now = self.clock()
            aws = self.aws(state)
            job = self.job(aws, token)
            require(job['status'] != 'closed' and now < job['deadline'], 'AWS reservation is closed or expired')
            project_age, aws_age = now - state['last_watchdog'], now - aws['last_watchdog']
            require(0 <= project_age <= 180 and 0 <= aws_age <= 180,
                    f'Cloud budget watchdog is stale (project age={project_age:.3f}s, AWS age={aws_age:.3f}s)')
            # The caller owns its pending operation. Other unresolved work still
            # blocks spending; do not erase its own outstanding cost reservation.
            visible = copy.deepcopy(state)
            visible['aws']['reservations'][token]['status'] = 'running'
            report = DC['summary'](visible, now, self.persistent_hours)
            require(not report['uncertain'] and not report['storage_uncertain'], 'Another cloud operation has unresolved spending')
            require(report['spent'] + report['reserved'] + report['background_reserve'] + self.margin < self.ceiling,
                    'Project spending ceiling reached')
            return self.receipt(state, job)

    def storage(self, token, volume_id, hourly, created_epoch):
        self.existing()
        ident = identifier(volume_id, 'vol')
        rate, created, now = finite(hourly, 'EBS rate'), finite(created_epoch, 'EBS creation'), self.clock()
        require(rate > 0 and created <= now + 5, 'Invalid observed EBS creation or rate')
        with self.store.locked(now) as state:
            aws = self.aws(state)
            job = self.job(aws, token)
            require(job['status'] != 'closed', 'Closed AWS reservation cannot acquire storage')
            key = 'volume:' + ident
            if key in aws['resources']:
                old = aws['resources'][key]
                # EC2 CreateVolume can return whole seconds while the later
                # DescribeVolumes value retains milliseconds. Accept that
                # precision difference only within the same UTC second.
                same_creation = (old['created'] == created or
                    math.floor(old['created']) == math.floor(created) and
                    (float(old['created']).is_integer() or float(created).is_integer()))
                require(old['rate'] == rate and same_creation and old['active'],
                        'EBS identity or rate differs from its original registration')
                if created < old['created']:
                    old.setdefault('original_created', old['created'])
                    old['cost'] += rate * (old['created'] - created) / 3600
                    old['created'] = created
                if ident not in job['volume_ids']:
                    job['volume_ids'].append(ident)
                return self.receipt(state, job)
            require(rate <= job['pending_storage_hourly'] + 1e-9, 'EBS allocation exceeds its reserved storage quote')
            aws['resources'][key] = dict(kind='volume', rate=rate, cost=rate * max(0, now - created) / 3600,
                active=True, created=created, last=now, observed_state='available', id=ident)
            job['volume_ids'].append(ident)
            job['pending_storage_hourly'] = max(0, job['pending_storage_hourly'] - rate)
            return self.receipt(state, job)

    @staticmethod
    def accrue(resource, now):
        if resource['active']:
            resource['cost'] += resource['rate'] * max(0, now - resource['last']) / 3600
        resource['last'] = now

    def running(self, token, instance_id, hourly):
        self.existing()
        ident, rate, now = identifier(instance_id, 'i'), finite(hourly, 'AWS running rate'), self.clock()
        with self.store.locked(now) as state:
            aws = self.aws(state)
            job = self.job(aws, token)
            require(job['status'] != 'closed' and rate == job['compute_hourly']
                    and job['instance_id'] in {None, ident}, 'Running AWS instance differs from its reservation')
            key = 'instance:' + ident
            if job['instance_id'] is None:
                old = aws['resources'].get(key)
                require(old is None or not old['active'], 'AWS instance already has an active reservation')
                historic = old['cost'] if old else 0
                aws['resources'][key] = dict(kind='instance', id=ident, rate=rate,
                    cost=historic + rate * max(0, now - job['created']) / 3600,
                    active=True, created=old['created'] if old else job['created'],
                    last=now, observed_state='running')
            else:
                self.accrue(aws['resources'][key], now)
                require(aws['resources'][key]['active'], 'Stopped AWS instance cannot restart under an old token')
            job.update(status='running', instance_id=ident)
            return self.receipt(state, job)

    def observe_instance(self, instance_id, observed_state):
        self.existing()
        ident, now = identifier(instance_id, 'i'), self.clock()
        require(observed_state in {'pending', 'running', 'stopping', 'stopped', 'shutting-down', 'terminated'},
                'Unsupported AWS compute observation')
        with self.store.locked(now) as state:
            aws = self.aws(state)
            resource = aws['resources'].get('instance:' + ident)
            require(resource is not None, 'Unregistered AWS compute cannot be reconciled')
            if not resource['active'] and observed_state not in {'stopped', 'terminated'}:
                # An unexpected restart may precede this observation. Charge
                # conservatively from the last confirmed stopped observation.
                resource['active'] = True
            self.accrue(resource, now)
            resource.update(active=observed_state not in {'stopped', 'terminated'}, observed_state=observed_state)
            return DC['summary'](state, now, self.persistent_hours)

    def release(self, token, instance_id, observed_state):
        self.existing()
        ident, now = identifier(instance_id, 'i'), self.clock()
        require(observed_state in {'stopped', 'terminated'}, 'A confirmed AWS compute stop is required')
        with self.store.locked(now) as state:
            aws = self.aws(state)
            job = self.job(aws, token)
            resource = aws['resources'].get('instance:' + ident)
            require(job['instance_id'] == ident and resource is not None and not resource['active']
                    and resource['observed_state'] == observed_state, 'AWS stop does not match the reserved worker')
            require(job['pending_storage_hourly'] <= 1e-9, 'Unresolved EBS creation cannot release its reservation')
            job.update(status='closed', closed_epoch=now, closure_state=observed_state)
            return dict(self.receipt(state, job), budget_released=True, compute_stopped=True)

    def cancel_unallocated(self, token, *, no_instance_confirmed,
                           no_pending_storage_confirmed):
        """Close an exact rejected create after provider reconciliation.

        The controller must retain the rejected request and verify that its
        unique client token has no instance and no unregistered volumes. API
        timeouts or a single empty inventory are not that proof. Already
        registered EBS volumes remain billed and are retained for retry.
        """
        self.existing()
        require(no_instance_confirmed is True and no_pending_storage_confirmed is True,
                'Uncertain AWS allocation cannot release spending reservations')
        now = self.clock()
        with self.store.locked(now) as state:
            aws = self.aws(state)
            job = self.job(aws, token)
            require(job['instance_id'] is None and job['status'] in {'pending', 'uncertain'},
                    'Only an unallocated AWS reservation can be rejected')
            job.update(status='closed', pending_storage_hourly=0,
                       closed_epoch=now, closure_state='allocation-rejected')
            return dict(self.receipt(state, job), budget_released=True,
                        allocation_rejected=True)

    def heartbeat(self):
        """Called by the provider watchdog after it reconciles its owned pool."""
        self.existing()
        now = self.clock()
        with self.store.locked(now) as state:
            self.aws(state)['last_watchdog'] = now
            return DC['summary'](state, now, self.persistent_hours)

    def snapshot(self):
        self.existing()
        with self.store.locked(self.clock()) as state:
            self.aws(state)
            return DC['summary'](state, self.clock(), self.persistent_hours)
