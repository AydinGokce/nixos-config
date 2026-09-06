"""Operator-only ingestion of completed, retained results; never launches work."""
from pathlib import Path

from .catalog import specification
from .common import digest, inventory, now, string, uid, write_json
from .runner import seal_results


def import_retained(store, source, model, actor, name):
    specification(model); string(actor, 'actor', 128); string(name, 'name', 200)
    source = Path(source).absolute()
    files = inventory(source)
    fingerprint = digest({'source_inventory': files, 'model': model})
    key = 'retained-' + fingerprint
    with store.transaction() as db:
        existing = store.idem(db, actor, 'operator.import-retained', key, {'sha256': fingerprint})
        if existing:
            return {'batch_id': existing, 'already_imported': True, 'inference_performed': False}
        batch_id, job_id, pair_id = uid(), uid(), uid()
        pair = {'pair_id': pair_id, 'input_id': 'retained', 'input_name': name, 'model': model,
                'state': 'compatible', 'reasons': [], 'job_id': job_id}
        batch = {'batch_id': batch_id, 'name': name, 'mode': 'batch', 'state': 'running',
                 'msa_backend': 'retained', 'execution': 'retained', 'inputs': [], 'models': [model],
                 'pairs': [pair], 'errors': [], '_committed': True,
                 '_request': {'operator_import': True, 'source_inventory_sha256': fingerprint}}
        job = {'job_id': job_id, 'batch_id': batch_id, 'pair_id': pair_id, 'input_id': 'retained',
               'input_name': name, 'model': model, 'state': 'running', 'phase': 'archiving retained results',
               'started_at': now(), 'finished_at': None, 'exit_code': None, 'error': None,
               'progress': {'message': 'Importing previously retained artifacts; no inference launch', 'observed_at': now()},
               'provenance': {'imported_retained_result': True, 'inference_performed': False,
                              'source_inventory_sha256': fingerprint, 'automatic_retry': False}}
        store.put(db, 'batch', batch, actor); store.put(db, 'job', job, actor)
        store.idem(db, actor, 'operator.import-retained', key, {'sha256': fingerprint}, batch_id)
    try:
        artifacts = seal_results(store, job, source, 'operator-retained')
        evidence = {'version': 1, 'batch_id': batch_id, 'job_id': job_id, 'source_inventory': files,
                    'source_inventory_sha256': fingerprint, 'artifacts': artifacts,
                    'inference_performed': False, 'created_at': now()}
        write_json(store.directory('jobs', job_id) / 'import-receipt.json', evidence, exclusive=True)
        with store.transaction() as db:
            current = store.get(db, 'job', job_id)
            current.update(state='complete', phase='complete', finished_at=now(), exit_code=0)
            current['provenance']['artifacts_sha256'] = digest(artifacts)
            store.put(db, 'job', current)
            store.event(db, job_id, 'retained_result_imported', {'inference_performed': False, 'artifacts': len(artifacts)})
        return {'batch_id': batch_id, 'job_id': job_id, 'artifacts': len(artifacts), 'inference_performed': False}
    except BaseException as exc:
        with store.transaction() as db:
            current = store.get(db, 'job', job_id)
            current.update(state='failed', finished_at=now(), error={'message': str(exc), 'automatic_retry': False})
            store.put(db, 'job', current)
        raise
