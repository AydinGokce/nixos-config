"""Small actor-scoped JSON RPC surface; launch work belongs to the daemon."""
from __future__ import annotations

import base64
from copy import deepcopy
import os
from pathlib import Path

from . import catalog, inputs
from .common import (CHUNK, UPLOAD, TERMINAL, Error, atomic, canonical, decode_chunk,
                     file_sha, identifier, keys, now, number, require, safe_file,
                     sha, string, uid)


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith('_')}
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


class API:
    def __init__(self, store, actor, *, library_config=None, worker_config=None):
        self.store, self.actor = store, string(actor, 'trusted actor', 128)
        self.library_config = library_config
        self.worker_config = worker_config

    def call(self, method, params):
        require(isinstance(method, str), 'Method must be a string')
        function = {
            'catalog': self.catalog, 'upload.begin': self.upload_begin, 'upload.get': self.upload_get,
            'worker.status': self.worker_status, 'worker.capacity': self.worker_capacity,
            'worker.extend': self.worker_extend,
            'worker.shutdown': self.worker_shutdown, 'worker.control_get': self.worker_control_get,
            'library.list': self.library_list, 'library.get': self.library_get,
            'library.attachment': self.library_attachment,
            'library.edit': self.library_edit, 'library.history': self.library_history,
            'library.undo': self.library_undo, 'library.redo': self.library_redo,
            'library.runs': self.library_runs,
            'library.sequence': self.library_sequence,
            'library.protein_domains': self.library_protein_domains,
            'library.product_preview': self.library_product_preview,
            'library.product_create': self.library_product_create,
            'library.create': self.library_create,
            'md.catalog': self.md_catalog, 'md.validate': self.md_validate, 'md.plan': self.md_plan,
            'md.resume': self.md_resume,
            'md.compare': self.md_compare,
            'binder.catalog': self.binder_catalog, 'binder.inspect': self.binder_inspect,
            'binder.run': self.binder_run, 'binder.candidates': self.binder_candidates,
            'binder.save': self.binder_save, 'binder.context': self.binder_context,
            'upload.chunk': self.upload_chunk, 'upload.finish': self.upload_finish,
            'batch.validate': self.batch_validate, 'batch.create': self.batch_create,
            'batch.run': self.batch_run,
            'batch.get': self.batch_get, 'batch.list': self.batch_list, 'batch.cancel': self.batch_cancel,
            'job.get': self.job_get, 'job.logs': self.job_logs, 'job.artifacts': self.job_artifacts,
            'job.cancel': self.job_cancel, 'artifact.read': self.artifact_read,
            'annotation.put': self.annotation_put, 'annotation.list': self.annotation_list,
        }.get(method)
        require(function is not None, 'Unknown method')
        if method in {'library.list', 'library.get', 'library.attachment',
                      'library.edit', 'library.history', 'library.undo', 'library.redo',
                      'library.runs', 'library.sequence', 'library.protein_domains', 'library.product_preview',
                      'library.product_create', 'library.create'}:
            # The explorer returns published user records, including their
            # exact identity/provenance keys and record hashes. These methods
            # explicitly construct their public envelopes and have no private
            # Workbench job fields to strip.
            return function(params)
        return public(function(params))

    def binder_catalog(self, params):
        from .binder_api import catalog
        return catalog(self, params)

    def binder_inspect(self, params):
        from .binder_api import inspect_target
        return inspect_target(self, params)

    def binder_run(self, params):
        from .binder_api import request
        return request(self, params)

    def binder_candidates(self, params):
        from .binder_results import candidates
        return candidates(self, params)

    def binder_save(self, params):
        from .binder_results import save
        return save(self, params)

    def binder_context(self, params):
        from .binder_results import context
        return context(self, params)

    def worker_status(self, params):
        from .worker_api import status
        return status(self, params)

    def worker_capacity(self, params):
        from .capacity_api import capacity
        return capacity(self, params)

    def worker_extend(self, params):
        from .worker_api import submit
        return submit(self, params, 'extend')

    def worker_shutdown(self, params):
        from .worker_api import submit
        return submit(self, params, 'shutdown')

    def worker_control_get(self, params):
        from .worker_api import get
        return get(self, params)

    def library_list(self, params):
        from .library_api import list_records
        return list_records(self, params)

    def library_get(self, params):
        from .library_api import get_record
        return get_record(self, params)

    def library_sequence(self, params):
        from .library_sequence import get_view
        return get_view(self, params)

    def library_protein_domains(self, params):
        from .library_domains import get_domains
        return get_domains(self, params)

    def library_product_preview(self, params):
        from .library_sequence import preview
        return preview(self, params)

    def library_product_create(self, params):
        from .library_edits import create_product
        return create_product(self, params)

    def library_create(self, params):
        from .library_edits import create_protein
        return create_protein(self, params)

    def library_attachment(self, params):
        from .library_api import attachment
        return attachment(self, params)

    def library_edit(self, params):
        from .library_edits import edit
        return edit(self, params)

    def library_history(self, params):
        from .library_edits import history
        return history(self, params)

    def library_undo(self, params):
        from .library_edits import undo
        return undo(self, params)

    def library_redo(self, params):
        from .library_edits import redo
        return redo(self, params)

    def library_runs(self, params):
        from .library_history import list_runs
        return list_runs(self, params)

    def catalog(self, params):
        keys(params)
        from md.gateway import catalog as md_catalog
        return {**catalog.catalog(), 'molecular_dynamics': md_catalog(),
                'workflows': {'bindcraft': {'enabled': True, 'catalog_method': 'binder.catalog',
                                           'submit_method': 'binder.run'}}}

    def md_catalog(self, params):
        keys(params)
        from md.gateway import catalog as md_catalog
        return md_catalog()

    def md_validate(self, params):
        from md.gateway import request
        return request(self, params)

    def md_plan(self, params):
        from md.gateway import inspect_plan
        return inspect_plan(self, params)

    def md_resume(self, params):
        from md.gateway import resume_request
        return resume_request(self, params)

    def md_compare(self, params):
        from md.gateway import compare_results
        return compare_results(self, params)

    def upload_begin(self, params):
        keys(params, ('name', 'size'), ('sha256',))
        string(params['name'], 'name', 200); number(params['size'], 'size', 1, UPLOAD)
        if 'sha256' in params:
            sha(params['sha256'])
        ident = uid()
        folder = self.store.directory('uploads', ident)
        folder.mkdir(parents=True, mode=0o700)
        atomic(folder / 'content.part', b'', exclusive=True)
        data = {'upload_id': ident, 'name': params['name'], 'size': params['size'], 'offset': 0,
                'state': 'uploading', 'chunk_bytes': CHUNK, '_expected_sha256': params.get('sha256')}
        with self.store.transaction() as db:
            self.store.put(db, 'upload', data, self.actor)
        return data

    def upload_get(self, params):
        keys(params, ('upload_id',))
        return self.store.read('upload', params['upload_id'], self.actor)

    def upload_chunk(self, params):
        keys(params, ('upload_id', 'offset', 'data_base64'))
        offset = number(params['offset'], 'offset', 0, UPLOAD)
        chunk = decode_chunk(params['data_base64'])
        with self.store.transaction() as db:
            data = self.store.get(db, 'upload', params['upload_id'], self.actor)
            require(offset + len(chunk) <= data['size'], 'Chunk exceeds declared file size', 'limit')
            folder = self.store.directory('uploads', data['upload_id'])
            path = folder / ('content' if data['state'] == 'complete' or (folder / 'content').exists() else 'content.part')
            safe_file(path)
            with path.open('r+b' if path.name == 'content.part' else 'rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                require(offset <= data['offset'] and offset <= size, 'Upload gap or incorrect offset', 'conflict')
                if offset + len(chunk) <= size:
                    stream.seek(offset)
                    require(stream.read(len(chunk)) == chunk, 'Repeated chunk differs from retained bytes', 'conflict')
                else:
                    require(data['state'] == 'uploading' and offset == data['offset'] == size,
                            'Chunk overlaps existing bytes', 'conflict')
                    stream.seek(offset); stream.write(chunk); stream.flush(); os.fsync(stream.fileno())
                data['offset'] = max(data['offset'], offset + len(chunk))
            self.store.put(db, 'upload', data)
        return {'upload_id': data['upload_id'], 'offset': data['offset']}

    def upload_finish(self, params):
        keys(params, ('upload_id', 'sha256')); sha(params['sha256'])
        with self.store.transaction() as db:
            data = self.store.get(db, 'upload', params['upload_id'], self.actor)
            folder = self.store.directory('uploads', data['upload_id'])
            path = folder / ('content' if (folder / 'content').exists() else 'content.part')
            require(safe_file(path).stat().st_size == data['offset'] == data['size'], 'Upload is incomplete', 'conflict')
            value = file_sha(path)
            require(value == params['sha256'] and data['_expected_sha256'] in (None, value), 'Upload SHA-256 mismatch', 'integrity')
            if path.name != 'content':
                os.rename(path, folder / 'content')
                os.chmod(folder / 'content', 0o400)
                fd = os.open(folder, os.O_DIRECTORY); os.fsync(fd); os.close(fd)
            data.update(state='complete', sha256=value)
            self.store.put(db, 'upload', data)
        return data

    def batch_validate(self, params):
        return self._batch_request(params)

    def batch_run(self, params):
        # This endpoint records execution intent before returning. The daemon,
        # rather than a connected client, commits the compatible results.
        if isinstance(params, dict):
            params = {**params, 'msa_backend': params.get('msa_backend', 'private')}
        return self._batch_request(params, auto_run=True)

    def _batch_request(self, params, auto_run=False):
        document = inputs.request(params)
        method = 'batch.run' if auto_run else 'batch.validate'
        # Ownership and completeness are checked before a preview can refer to
        # any upload. Expensive chemistry and FASTA expansion run asynchronously.
        for item in document['inputs']:
            source = item['source']
            uploads = list(source.get('attachments', {}).values())
            if source['kind'] == 'upload':
                uploads.append(source['upload_id'])
            for ident in uploads:
                data = self.store.read('upload', ident, self.actor)
                require(data['state'] == 'complete', 'An input upload is incomplete', 'conflict')
        for settings in document['settings'].values():
            if 'labels_upload_id' in settings:
                data = self.store.read('upload', settings['labels_upload_id'], self.actor)
                require(data['state'] == 'complete', 'Labels upload is incomplete', 'conflict')
        with self.store.transaction() as db:
            old = self.store.idem(db, self.actor, method, document['request_key'], document)
            if old:
                return self._batch(old, db)
            ident = uid()
            data = {'batch_id': ident, 'name': document['name'], 'mode': document['mode'],
                    'state': 'validating', 'msa_backend': document['msa_backend'], 'execution': document['execution'],
                    'inputs': [{k: v for k, v in item.items() if k != 'source'} for item in document['inputs']],
                    'models': document['models'], 'pairs': [], 'errors': [], '_request': document, '_committed': False}
            if auto_run:
                data['auto_run'] = True
            base = [{'id': 'assembly', 'name': document['name']}] if document['mode'] == 'assembly' else document['inputs']
            data['pairs'] = [{'pair_id': uid(), 'input_id': item['id'], 'input_name': item['name'],
                              'model': model, 'state': 'pending', 'reasons': [], 'job_id': None}
                             for item in base for model in document['models']]
            require(len(data['pairs']) <= 512, 'Too many input/model combinations', 'limit')
            self.store.put(db, 'batch', data, self.actor)
            self.store.idem(db, self.actor, method, document['request_key'], document, ident)
            self.store.event(db, ident, 'validation_requested', {'models': document['models']})
            if auto_run:
                self.store.event(db, ident, 'run_requested', {'models': document['models'], 'msa_backend': document['msa_backend']})
            return self._batch(ident, db)

    def batch_create(self, params):
        keys(params, ('batch_id', 'request_key', 'pair_ids'))
        require(isinstance(params['pair_ids'], list) and 0 < len(params['pair_ids']) <= 512,
                'Select at least one validated pair')
        require(all(isinstance(x, str) for x in params['pair_ids']) and len(set(params['pair_ids'])) == len(params['pair_ids']), 'Repeated or invalid pair IDs')
        with self.store.transaction() as db:
            old = self.store.idem(db, self.actor, 'batch.create', params['request_key'], params)
            if old:
                return self._batch(old, db)
            batch = self.store.get(db, 'batch', params['batch_id'], self.actor)
            require(not batch.get('auto_run'), 'This run automatically submits its compatible pairs', 'conflict')
            require(batch['state'] == 'validated' and not batch['_committed'], 'Preview is not ready or already submitted', 'conflict')
            selected = {p['pair_id']: p for p in batch['pairs']}
            require(all(ident in selected and selected[ident]['state'] == 'compatible' for ident in params['pair_ids']),
                    'Every selected pair must have completed compatible validation', 'conflict')
            require(all(selected[ident]['_prepared'].get('tools_dir') for ident in params['pair_ids']),
                    'This preview predates the current execution contract; validate a new preview before submitting', 'conflict')
            self._enqueue_pairs(db, batch, [selected[ident] for ident in params['pair_ids']])
            self.store.idem(db, self.actor, 'batch.create', params['request_key'], params, batch['batch_id'])
            return self._batch(batch['batch_id'], db)

    def _enqueue_pairs(self, db, batch, pairs):
        """Publish jobs and their batch links in the caller's one transaction."""
        for pair in pairs:
            ident = pair['pair_id']
            job_id = uid(); pair['job_id'] = job_id
            job = {'job_id': job_id, 'batch_id': batch['batch_id'], 'pair_id': ident,
                   'input_id': pair['input_id'], 'input_name': pair['input_name'], 'model': pair['model'],
                   'state': 'queued', 'phase': 'queued', 'started_at': None, 'finished_at': None,
                   'exit_code': None, 'error': None, 'progress': {'message': 'Waiting for the head dispatcher', 'observed_at': now()},
                   'provenance': {'settings': pair['_prepared']['settings'],
                                  'msa_backend': pair['_prepared'].get('msa_backend', batch['msa_backend']),
                                  'execution_requested': batch['execution'], 'automatic_retry': False},
                   '_prepared': pair['_prepared']}
            if 'msa_applicable' in pair['_prepared']:
                job['provenance'].update(msa_applicable=pair['_prepared']['msa_applicable'],
                                         msa_backend_requested=batch['msa_backend'])
            self.store.put(db, 'job', job, self.actor)
            self.store.event(db, job_id, 'enqueued', {'batch_id': batch['batch_id'], 'pair_id': ident})
        batch.update(state='queued', _committed=True)
        self.store.put(db, 'batch', batch)

    def _automatic_run(self, batch_id):
        """Daemon-only continuation; cancellation and retries share this CAS."""
        with self.store.transaction() as db:
            batch = self.store.get(db, 'batch', batch_id, self.actor)
            if not batch.get('auto_run') or batch['_committed'] or batch['state'] != 'validated':
                return False
            operation = db.execute('SELECT kind,state FROM operations WHERE object_id=?', (batch_id,)).fetchone()
            if operation and operation['kind'] == 'validation' and operation['state'] in {'intent', 'running'}:
                return False  # The runner has not durably completed its receipt yet.
            compatible = [pair for pair in batch['pairs'] if pair['state'] == 'compatible']
            failure = None
            if not operation or operation['kind'] != 'validation' or operation['state'] != 'complete':
                failure = 'Validation did not retain successful completion evidence; no model jobs were queued.'
            elif any(pair['state'] not in {'compatible', 'rejected'} for pair in batch['pairs']):
                failure = 'Validation ended before every input/model pair was checked; no model jobs were queued.'
            elif any(not pair.get('_prepared', {}).get('tools_dir') for pair in compatible):
                failure = 'Validation results do not match the current execution contract; start a new run.'
            elif not compatible:
                failure = 'No compatible input/model pairs; see each pair\'s rejection reasons.'
            if failure:
                batch['state'] = 'validation_failed'
                batch['errors'].append(failure)
                self.store.put(db, 'batch', batch)
                self.store.event(db, batch_id, 'run_not_queued', {'message': failure})
                return False
            self._enqueue_pairs(db, batch, compatible)
            self.store.event(db, batch_id, 'run_queued', {'pairs': len(compatible), 'rejected': len(batch['pairs']) - len(compatible)})
            return True

    def _job(self, job, db, with_artifacts=True):
        result = deepcopy(job)
        from .progress import freshness
        result['progress'] = freshness(result.get('progress', {}))
        from .common import parse
        where = "kind='artifact' AND actor=? AND json_extract(data,'$.job_id')=?"
        args = (self.actor, job['job_id'])
        result['artifacts'] = [parse(row['data']) for row in db.execute('SELECT data FROM objects WHERE ' + where + ' ORDER BY created,id LIMIT 100', args)] if with_artifacts else []
        result['artifact_count'] = db.execute('SELECT COUNT(*) FROM objects WHERE ' + where, args).fetchone()[0]
        return result

    def _batch(self, ident, db):
        batch = deepcopy(self.store.get(db, 'batch', ident, self.actor))
        jobs = [self._job(self.store.get(db, 'job', p['job_id'], self.actor), db, False) for p in batch['pairs'] if p.get('job_id')]
        for job in jobs:
            job.pop('_prepared', None)
            job['provenance'] = {k: v for k, v in job['provenance'].items() if k in {'msa_backend', 'msa_applicable', 'msa_backend_requested', 'execution_requested', 'automatic_retry', 'resident_job_id'}}
            if job.get('error'):
                job['error'] = {'message': job['error'].get('message', '')[:500]}
        for pair in batch['pairs']:
            pair['reasons'] = [reason[:500] for reason in pair['reasons'][:4]]
        counts = {key: 0 for key in ('pairs', 'compatible', 'rejected', 'jobs', 'queued', 'running', 'complete', 'failed', 'cancelled', 'interrupted')}
        counts['pairs'] = len(batch['pairs']); counts['jobs'] = len(jobs)
        for pair in batch['pairs']:
            if pair['state'] in {'compatible', 'rejected'}:
                counts[pair['state']] += 1
        for job in jobs:
            state = job['state']; counts['running' if state in {'running', 'starting', 'cancel_requested'} else state] += 1
        batch['jobs'], batch['counts'] = jobs, counts
        if jobs:
            states = {j['state'] for j in jobs}
            if states <= TERMINAL:
                batch['state'] = 'complete' if states == {'complete'} else 'cancelled' if states == {'cancelled'} else 'failed' if not counts['complete'] else 'partial'
                if batch.get('auto_run') and counts['rejected'] and batch['state'] == 'complete':
                    batch['state'] = 'partial'
            elif 'cancel_requested' in states:
                batch['state'] = 'cancel_requested'
            elif states & {'running', 'starting'}:
                batch['state'] = 'running'
            else:
                batch['state'] = 'queued'
        return batch

    def batch_get(self, params):
        keys(params, ('batch_id',))
        with self.store.connection() as db:
            return self._batch(params['batch_id'], db)

    def batch_list(self, params):
        keys(params, (), ('limit', 'cursor'))
        limit = number(params.get('limit', 50), 'limit', 1, 100)
        all_items = self.store.listing('batch', self.actor)
        all_items.reverse()
        cursor = params.get('cursor')
        if cursor is not None:
            identifier(cursor)
            indexes = [n for n, b in enumerate(all_items) if b['batch_id'] == cursor]
            require(indexes, 'Unknown pagination cursor')
            all_items = all_items[indexes[0] + 1:]
        page = all_items[:limit]
        with self.store.connection() as db:
            batches = [{k: v for k, v in self._batch(item['batch_id'], db).items()
                        if k in {'batch_id', 'name', 'mode', 'workflow', 'state', 'created_at', 'updated_at', 'msa_backend', 'execution', 'models', 'counts', 'auto_run'}} for item in page]
        return {'batches': batches, 'next_cursor': page[-1]['batch_id'] if len(all_items) > limit else None}

    def job_get(self, params):
        keys(params, ('job_id',))
        with self.store.connection() as db:
            return self._job(self.store.get(db, 'job', params['job_id'], self.actor), db)

    def job_cancel(self, params):
        keys(params, ('job_id',))
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', params['job_id'], self.actor)
            self._cancel_job(db, job)
            return self._job(job, db)

    def _cancel_job(self, db, job):
        if job['state'] not in TERMINAL:
            operation = db.execute('SELECT 1 FROM operations WHERE object_id=?', (job['job_id'],)).fetchone()
            if job['state'] == 'queued' and operation is None:
                job.update(state='cancelled', phase='cancelled', finished_at=now())
            else:
                job.update(state='cancel_requested', phase='cancellation requested')
            job['progress'] = {'message': 'Cancellation requested; any owned execution is being reconciled', 'observed_at': now()}
            self.store.put(db, 'job', job)
            self.store.event(db, job['job_id'], 'cancel_requested', {})

    def batch_cancel(self, params):
        keys(params, ('batch_id',))
        with self.store.transaction() as db:
            batch = self.store.get(db, 'batch', params['batch_id'], self.actor)
            for pair in batch['pairs']:
                if pair.get('job_id'):
                    self._cancel_job(db, self.store.get(db, 'job', pair['job_id'], self.actor))
            if not batch['_committed']:
                batch['state'] = 'cancelled'
                self.store.put(db, 'batch', batch)
        return self.batch_get(params)

    def job_logs(self, params):
        keys(params, ('job_id',), ('offset', 'max_bytes'))
        job = self.store.read('job', params['job_id'], self.actor)
        offset = number(params.get('offset', 0), 'offset', 0, 2**63 - 1)
        maximum = number(params.get('max_bytes', 65536), 'max_bytes', 1, CHUNK)
        path = self.store.directory('jobs', job['job_id']) / 'run.log'
        chunk = b''; size = 0
        if path.exists():
            with safe_file(path).open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                require(offset <= size, 'Log offset exceeds size')
                stream.seek(offset); chunk = stream.read(maximum)
        else:
            require(offset == 0, 'Log offset exceeds size')
        return {'job_id': job['job_id'], 'offset': offset, 'next_offset': offset + len(chunk),
                'text': chunk.decode('utf-8', errors='replace'), 'eof': offset + len(chunk) >= size}

    def job_artifacts(self, params):
        keys(params, ('job_id',), ('limit', 'cursor'))
        job = self.store.read('job', params['job_id'], self.actor)
        limit = number(params.get('limit', 100), 'limit', 1, 100)
        from .common import parse
        with self.store.connection() as db:
            artifacts = [parse(row['data']) for row in db.execute("SELECT data FROM objects WHERE kind='artifact' AND actor=? AND json_extract(data,'$.job_id')=? ORDER BY created,id", (self.actor, job['job_id']))]
        if params.get('cursor'):
            cursor = identifier(params['cursor'])
            indexes = [n for n, a in enumerate(artifacts) if a['artifact_id'] == cursor]
            require(indexes, 'Unknown pagination cursor')
            artifacts = artifacts[indexes[0] + 1:]
        page = artifacts[:limit]
        return {'job_id': job['job_id'], 'artifacts': page,
                'next_cursor': page[-1]['artifact_id'] if len(artifacts) > limit else None}

    def artifact_read(self, params):
        keys(params, ('artifact_id',), ('offset', 'max_bytes'))
        data = self.store.read('artifact', params['artifact_id'], self.actor)
        path = self.store.directory('artifacts', data['artifact_id']) / 'content'
        require(path.stat().st_size == data['size'] and file_sha(path) == data['sha256'], 'Artifact integrity check failed', 'integrity')
        offset = number(params.get('offset', 0), 'offset', 0, data['size'])
        maximum = number(params.get('max_bytes', CHUNK), 'max_bytes', 1, CHUNK)
        with safe_file(path).open('rb') as stream:
            stream.seek(offset); chunk = stream.read(maximum)
        return {'artifact_id': data['artifact_id'], 'offset': offset, 'data_base64': base64.b64encode(chunk).decode(),
                'next_offset': offset + len(chunk), 'eof': offset + len(chunk) == data['size'],
                'size': data['size'], 'sha256': data['sha256'], 'name': data['name'], 'media_type': data['media_type']}

    def annotation_put(self, params):
        keys(params, ('artifact_id', 'text'), ('annotation_id', 'expected_revision', 'selection'))
        self.store.read('artifact', params['artifact_id'], self.actor)
        require(isinstance(params['text'], str) and len(params['text'].encode()) <= 16384, 'Annotation text exceeds 16 KiB', 'limit')
        selection = params.get('selection')
        require(len(canonical(selection)) <= 16384, 'Annotation selection exceeds 16 KiB', 'limit')
        with self.store.transaction() as db:
            if 'annotation_id' in params:
                data = self.store.get(db, 'annotation', params['annotation_id'], self.actor)
                require(data['artifact_id'] == params['artifact_id'] and params.get('expected_revision') == data['revision'], 'Annotation revision conflicts', 'conflict')
                data['revision'] += 1
            else:
                require('expected_revision' not in params, 'New annotations have no expected revision')
                data = {'annotation_id': uid(), 'artifact_id': params['artifact_id'], 'revision': 1, 'author': self.actor}
            data.update(text=params['text'], selection=selection)
            self.store.put(db, 'annotation', data, self.actor)
            self.store.event(db, data['annotation_id'], 'annotation_revision', data)
        return data

    def annotation_list(self, params):
        keys(params, ('artifact_id',))
        self.store.read('artifact', params['artifact_id'], self.actor)
        return {'artifact_id': params['artifact_id'], 'annotations': [a for a in self.store.listing('annotation', self.actor) if a['artifact_id'] == params['artifact_id']]}
