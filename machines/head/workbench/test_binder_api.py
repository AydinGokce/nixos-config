"""Binder ownership, durable recovery and sealed candidate/library provenance.

All structures and native receipts here are test fixtures. The only native
preflight is replaced explicitly; systemd/cloud allocations never execute.
"""
import base64
from copy import deepcopy
import csv
import hashlib
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench import binder_api
from workbench.common import Error, canonical, file_sha, now, uid, write_json
from workbench.inputs import library
from workbench.runner import BinderProgress, JobProgress, native_phase, run_job, seal_results, validation, verify_prepared
from workbench import progress as telemetry
from workbench.binder_results import seal_inputs
from workbench.service import Daemon
from workbench.store import Store
from workbench.test_scheduler import OwnedUnits


def pdb(chains=('A',), residues=5):
    lines, serial = [], 1
    for chain in chains:
        for residue in range(1, residues + 1):
            for atom in ('N', 'CA', 'C', 'O'):
                lines.append(f'ATOM  {serial:5d} {atom:^4s} ALA {chain}{residue:4d}    '
                             f'{float(serial):8.3f}{1.:8.3f}{2.:8.3f}{1.:6.2f}{20.:6.2f}          {atom[0]:>2s}  ')
                serial += 1
        lines.append('TER')
    return ('\n'.join(lines) + '\nEND\n').encode()


class BinderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'state')
        self.tools = Path(__file__).resolve().parents[1]
        self.config = {'tools_dir': str(self.tools), 'bio_submit': sys.executable,
                       'library_root': str(self.root / 'library'), 'runtime_config': str(self.root / 'runtime.json'),
                       'max_jobs': 2, 'systemd_run': '/bin/false', 'systemctl': '/bin/false',
                       'inference_state': str(self.root / 'inference'), 'bindcraft_shared': str(self.root / 'shared')}
        self.api = API(self.store, 'alice', library_config=self.config, worker_config=self.config)
        self.bob = API(self.store, 'bob', library_config=self.config, worker_config=self.config)
        self.units = OwnedUnits()
        raw = pdb()
        upload = self.api.call('upload.begin', {'name': 'target.pdb', 'size': len(raw)})
        self.api.call('upload.chunk', {'upload_id': upload['upload_id'], 'offset': 0,
                                      'data_base64': base64.b64encode(raw).decode()})
        upload = self.api.call('upload.finish', {'upload_id': upload['upload_id'], 'sha256': hashlib.sha256(raw).hexdigest()})
        self.target = {'kind': 'upload', 'id': upload['upload_id'], 'sha256': upload['sha256']}

    def params(self, **changes):
        return {'request_key': uid(), 'name': 'Binder fixture', 'target': deepcopy(self.target), 'chains': ['A'],
                'hotspots': [{'chain': 'A', 'number': 3, 'insertion_code': ''}], 'lengths': [5, 6],
                'designs': 1, 'timeout_seconds': 600, 'max_cost_usd': 3.0, 'seed': 42, **changes}

    def daemon(self):
        return Daemon(Store(self.store.root), self.config, system=self.units.state, launch=self.units.launch)

    def receipt(self, batch, commit=True):
        ident = batch['batch_id']
        with self.store.connection() as db:
            row = dict(db.execute('SELECT * FROM operations WHERE object_id=?', (ident,)).fetchone())
        unit = self.units.states[row['unit']]
        binding = {'kind': 'validation', 'object_id': ident, 'unit': row['unit'],
                   'invocation_id': unit['InvocationID'], 'intent_sha256': row['intent_sha256'], 'started_at': now()}
        folder = self.store.directory('operations', ident)
        write_json(folder / 'started.json', binding)
        receipt = {**binding, 'state': 'complete', 'finished_at': now()}
        write_json(folder / 'terminal.json', receipt)
        unit['SubState'] = 'exited'
        with self.store.transaction() as db:
            db.execute('UPDATE operations SET state=?,invocation_id=?,data=? WHERE object_id=?',
                       ('complete' if commit else 'running', unit['InvocationID'], canonical(receipt).decode(), ident))

    def checked(self, params=None, commit=True):
        batch = self.api.call('binder.run', params or self.params())
        self.daemon().tick()
        with patch.object(binder_api, 'native_preflight', return_value={'schema': 1, 'status': 'ready', 'test_fixture': True}):
            validation(self.store, batch['batch_id'], self.config)
        self.receipt(batch, commit=commit)
        return batch

    def queued(self):
        batch = self.checked()
        self.assertTrue(self.api._automatic_run(batch['batch_id']))
        current = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        return self.store.read('job', current['jobs'][0]['job_id'])

    def seal_fixture(self, *, sequence='AAAAA', accepted=True):
        job = self.queued()
        root = self.root / ('outputs-' + job['job_id'])
        designs = root / 'native/designs'; designs.mkdir(parents=True)
        rows = [('trajectory_stats.csv', [{'Design': 'fixture', 'Sequence': 'AAAAA', 'pLDDT': '0.9'}]),
                ('mpnn_design_stats.csv', [dict(Design='fixture_mpnn1', Sequence=sequence,
                    Average_pLDDT='0.93', Average_i_pTM='0.8', Average_dG='-12.3', Seed='123')]),
                ('failure_csv.csv', [{'n_InterfaceUnsatHbonds': '0'}])]
        for filename, values in rows:
            with (designs / filename).open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(values[0])); writer.writeheader(); writer.writerows(values)
        trajectory = designs / 'Trajectory/Relaxed/fixture.pdb'
        trajectory.parent.mkdir(parents=True); trajectory.write_bytes(pdb(('A', 'B')))
        candidate = designs / ('Accepted' if accepted else 'MPNN/Relaxed') / 'fixture_mpnn1_model1.pdb'
        candidate.parent.mkdir(parents=True); candidate.write_bytes(pdb(('A', 'B')))
        write_json(root / 'native/bindcraft-result.json', {'schema': 1, 'kind': 'bindcraft-result',
                   'input_manifest': job['_prepared']['settings']['input_manifest'],
                   'status': 'completed', 'elapsed_seconds': 42})
        seal_results(self.store, job, root)
        with self.store.transaction() as db:
            job.update(state='complete', phase='complete', finished_at=now(), exit_code=0)
            self.store.put(db, 'job', job)
        return job

    def project(self):
        module = library(self.tools)
        registry = module.Registry(self.config['library_root']); registry.init()
        purpose = self.root / 'project.md'; purpose.write_text('# Test study\n\nBinder workflow fixture only.\n')
        record = registry.import_record(module.projects.project_document('test-study', name='Test study', members=[]),
                                        {'project.md': purpose})
        return module, registry, record

    def test_inspection_and_submission_enforce_actor_and_exact_source_hash(self):
        result = self.api.call('binder.inspect', {'target': self.target})
        self.assertEqual(result['chains'][0]['sequence'], 'AAAAA')
        self.assertEqual(result['target'], self.target)
        for api, target in ((self.bob, self.target), (self.api, {**self.target, 'sha256': '0' * 64})):
            with self.assertRaises(Error):
                api.call('binder.inspect', {'target': target})
            with self.assertRaises(Error):
                api.call('binder.run', self.params(target=target))
        self.assertEqual(self.store.listing('batch'), [])

    def test_replay_recovery_auto_queues_one_job_without_review_or_reexecution(self):
        params = self.params()
        batch = self.checked(params, commit=False)
        self.assertEqual(self.api.call('binder.run', params)['batch_id'], batch['batch_id'])
        self.assertEqual(self.store.listing('job'), [])
        self.assertEqual(self.daemon().tick()['errors'], [])
        current = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(current['workflow'], 'bindcraft')
        self.assertEqual(len(current['jobs']), 1)
        job = self.store.read('job', current['jobs'][0]['job_id'])
        self.assertFalse(job['provenance']['msa_applicable'])
        self.assertEqual(job['_prepared']['environment']['DC_MAX_JOB_COST_USD'], '3.0')
        self.assertEqual(job['_prepared']['environment']['DC_JOB_COST_SCOPE'], 'binder-' + batch['batch_id'])
        verify_prepared(job['_prepared'])
        self.daemon().tick()
        self.assertEqual(len(self.store.listing('job')), 1)
        with self.assertRaisesRegex(Error, 'different parameters'):
            self.api.call('binder.run', {**params, 'designs': 2})

    def test_missing_hotspot_is_rejected_asynchronously_without_paid_job(self):
        batch = self.checked(self.params(hotspots=[{'chain': 'A', 'number': 200}]))
        self.assertEqual(self.daemon().tick()['errors'], [])
        value = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(value['state'], 'validation_failed')
        self.assertEqual(value['pairs'][0]['state'], 'rejected')
        self.assertEqual(value['jobs'], [])

    def test_cancelled_validation_and_queued_job_never_allocate(self):
        params = self.params(); batch = self.api.call('binder.run', params)
        self.api.call('batch.cancel', {'batch_id': batch['batch_id']})
        self.daemon().tick(); self.assertEqual(self.units.launched, [])
        job = self.queued()
        launched = len(self.units.launched)
        self.api.call('job.cancel', {'job_id': job['job_id']})
        self.daemon().tick()
        self.assertEqual(len(self.units.launched), launched)

    def test_target_mutation_after_enqueue_stops_before_any_native_preflight(self):
        batch = self.api.call('binder.run', self.params())
        path = self.store.directory('uploads', self.target['id']) / 'content'
        path.chmod(0o600); path.write_bytes(pdb() + b'REMARK changed\n')
        with patch.object(binder_api, 'native_preflight', side_effect=AssertionError('must not run')):
            validation(self.store, batch['batch_id'], self.config)
        value = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(value['pairs'][0]['state'], 'rejected')
        self.assertIn('Retained target', value['pairs'][0]['reasons'][0])

    def test_candidates_are_structure_artifacts_with_finite_native_metrics_and_pagination(self):
        job = self.seal_fixture()
        response = self.api.call('binder.candidates', {'job_id': job['job_id'], 'limit': 1})
        self.assertEqual(response['summary']['accepted'], 1)
        self.assertEqual(response['summary']['trajectory'], 1)
        second = self.api.call('binder.candidates', {'job_id': job['job_id'], 'limit': 1, 'cursor': response['next_cursor']})
        row = second['candidates'][0]
        self.assertEqual(row['metrics']['rosetta_dg'], -12.3)
        self.assertEqual(row['structure_artifacts'][0]['role'], 'structure')
        self.assertEqual(row['status'], 'accepted')
        self.assertNotIn('_row', row)
        artifact = row['structure_artifacts'][0]
        chunk = self.api.call('artifact.read', {'artifact_id': artifact['artifact_id']})
        self.assertEqual(hashlib.sha256(base64.b64decode(chunk['data_base64'])).hexdigest(), artifact['sha256'])
        with self.assertRaises(Error):
            self.bob.call('binder.candidates', {'job_id': job['job_id']})

    def test_partial_evidence_is_not_silently_classified_as_rejected(self):
        job = self.seal_fixture(accepted=False)
        result = self.api.call('binder.candidates', {'job_id': job['job_id']})
        self.assertEqual(result['summary']['unclassified'], 1)
        self.assertEqual(result['summary']['rejected'], 0)

    def test_save_retains_target_mapping_and_candidate_files_and_supports_replay_undo_redo(self):
        job = self.seal_fixture()
        module, registry, project = self.project()
        candidate = next(c for c in self.api.call('binder.candidates', {'job_id': job['job_id']})['candidates'] if c['status'] == 'accepted')
        params = {'job_id': job['job_id'], 'candidate_id': candidate['candidate_id'],
                  'project_ref': module.reference(project), 'expected_sha256': project['sha256'],
                  'alt_name': 'Candidate One', 'request_key': uid()}
        saved = self.api.call('binder.save', params)
        self.assertEqual(self.api.call('binder.save', params), saved)
        record = registry.show(saved['ref'])
        self.assertEqual(record['identity']['sequence'], 'AAAAA')
        self.assertEqual(record['provenance']['bindcraft']['target']['sha256'], self.target['sha256'])
        self.assertEqual({a['path'] for a in record['attachments']}, {'attachments/description.md',
                         'attachments/bindcraft-provenance.json', 'attachments/predicted-complex.pdb', 'attachments/design-target.pdb'})
        status = self.api.call('library.history', {})
        self.api.call('library.undo', {'operation_id': status['undo']['operation_id'], 'request_key': uid()})
        self.assertTrue(self.api.call('library.get', {'ref': saved['ref'].split('@')[0]})['archived'])
        status = self.api.call('library.history', {})
        self.api.call('library.redo', {'operation_id': status['redo']['operation_id'], 'request_key': uid()})
        self.assertEqual(registry.verify()['records'], 7)
        with self.assertRaises(Error):
            self.bob.call('binder.save', params)

    def test_save_rejects_csv_sequence_that_does_not_match_predicted_binder_chain(self):
        job = self.seal_fixture(sequence='CCCCC')
        module, registry, project = self.project()
        candidate = next(c for c in self.api.call('binder.candidates', {'job_id': job['job_id']})['candidates'] if c['status'] == 'accepted')
        with self.assertRaisesRegex(Error, 'differs from its predicted binder chain'):
            self.api.call('binder.save', {'job_id': job['job_id'], 'candidate_id': candidate['candidate_id'],
                'project_ref': module.reference(project), 'expected_sha256': project['sha256'], 'request_key': uid()})
        self.assertEqual(registry.verify()['records'], 1)

    def test_native_progress_phases_do_not_claim_acceptance_from_design_confidence(self):
        self.assertEqual(native_phase('Starting trajectory: test\nStage 2: Softmax Optimisation')[0], 'binder design')
        self.assertEqual(native_phase('Fixing interface residues: B2,B3')[0], 'binder redesign and validation')
        self.assertEqual(native_phase('Unmet filter conditions for test_mpnn1'),
                         ('binder candidate filtering', 'Unmet filter conditions for test_mpnn1'))

    def test_native_counters_follow_complete_lines_without_double_counting(self):
        log = self.root / 'native.log'
        log.write_text('Starting trajectory: first\nUnmet filter conditions for first_mpnn1\nFound 1 MPNN designs passing filters\nFound 2 MPNN designs passing filters\nStarting traj')
        observed = BinderProgress()
        counts = observed.observe(log)
        self.assertEqual((counts['attempts_started'], counts['candidates_accepted'], counts['candidates_rejected']), (1, 2, 1))
        self.assertEqual(observed.observe(log), counts)
        with log.open('a') as stream:
            stream.write('ectory: second\nDesign and validation of trajectory first took: 1 minute\n')
        counts = observed.observe(log)
        self.assertEqual(counts['attempts_started'], 2)
        self.assertEqual(counts['trajectories_completed'], 1)
        log.write_bytes(b'')
        self.assertEqual(observed.observe(log)['state'], 'unavailable')

    def test_native_substage_survives_fresh_heartbeats_only_in_the_same_inference_stage(self):
        log = self.root / 'native-stages.log'
        epoch = time.time_ns() - 10**9
        def heartbeat(offset, **changes):
            event = {'schema': 1, 'scope': 'gpu', 'stage': 'inference', 'state': 'running',
                     'stage_id': 'one', 'timestamp_ns': epoch + offset,
                     'message': 'Loading BindCraft and designing binders', **changes}
            return telemetry.PREFIX + canonical(event).decode() + '\n'
        log.write_text(heartbeat(0) + 'Starting trajectory: first\nStage 1: Test Logits\n' + heartbeat(1))
        observer = JobProgress(log, None)
        phase, detail = observer.details()
        self.assertEqual((phase, detail['message']), ('binder design', 'Stage 1: Test Logits'))
        self.assertFalse(detail['stale'])
        self.assertEqual(detail['eta']['state'], 'unknown')
        with log.open('a') as stream:
            stream.write('Stage 2: Softmax Optimisation\n' + heartbeat(2))
        self.assertEqual(observer.details()[1]['message'], 'Stage 2: Softmax Optimisation')
        with log.open('a') as stream:
            stream.write(heartbeat(3, stage='result_transfer'))
        self.assertEqual(observer.details()[0], 'result transfer')
        with log.open('a') as stream:
            stream.write(heartbeat(4, stage_id='two'))
        self.assertEqual(observer.details()[0], 'inference')
        with log.open('a') as stream:
            stream.write('Starting trajectory: second\nStage 1: Test Logits\n' + heartbeat(5, stage_id='two'))
        self.assertEqual(observer.details()[1]['message'], 'Stage 1: Test Logits')
        with log.open('a') as stream:
            stream.write(heartbeat(6, stage_id='three'))
        self.assertEqual(observer.details()[0], 'inference')

    def test_native_substage_does_not_hide_stale_failed_or_unavailable_evidence(self):
        for kind in ('stale', 'failed', 'complete', 'truncated'):
            with self.subTest(kind=kind):
                log = self.root / ('native-' + kind + '.log')
                epoch = time.time_ns() - 10**9
                event = {'schema': 1, 'scope': 'gpu', 'stage': 'inference', 'state': 'running',
                         'stage_id': 'one', 'timestamp_ns': epoch, 'message': 'Generic inference heartbeat'}
                def line(value):
                    return telemetry.PREFIX + canonical(value).decode() + '\n'
                log.write_text(line(event) + 'Starting trajectory: first\nStage 1: Test Logits\n' +
                               line({**event, 'timestamp_ns': epoch + 1}))
                observer = JobProgress(log, None)
                self.assertEqual(observer.details()[0], 'binder design')
                if kind == 'stale':
                    with patch('workbench.progress.time.time', return_value=epoch / 10**9 + 60):
                        phase, detail = observer.details()
                    self.assertTrue(detail['stale'])
                    self.assertEqual(phase, 'inference')
                elif kind == 'truncated':
                    log.write_text(line({**event, 'timestamp_ns': epoch + 2}))
                    phase, detail = observer.details()
                    self.assertEqual(phase, 'inference')
                    self.assertEqual(detail['binder']['state'], 'unavailable')
                else:
                    with log.open('a') as stream:
                        stream.write(line({**event, 'timestamp_ns': epoch + 2, 'state': kind}))
                    phase, detail = observer.details()
                    self.assertEqual((phase, detail['stage_state']), ('inference', kind))

    def test_base_af2_rejections_are_exact_scoped_and_deduplicated(self):
        log = self.root / 'screen-rejections.log'
        message = 'Base AF2 filters not passed for first_mpnn1, skipping interface scoring'
        log.write_text('Starting trajectory: first\n' + message + '\n' + message + '\n' +
                       'Unmet filter conditions for first_mpnn1\n' +
                       'prefix ' + message + '\n' +
                       'Base AF2 filters not passed for first_mpnn2, skipping')
        observer = BinderProgress()
        result = observer.observe(log)
        self.assertEqual(result['candidates_rejected'], 1)
        self.assertEqual(result['rejection_screens'], {'base_af2': 1, 'final_filters': 1})
        with log.open('a') as stream:
            stream.write(' interface scoring\nStarting trajectory: second\n' +
                         'Base AF2 filters not passed for second_mpnn1, skipping interface scoring\n')
        result = observer.observe(log)
        self.assertEqual((result['attempts_started'], result['candidates_rejected']), (2, 3))
        self.assertEqual(result['rejection_screens'], {'base_af2': 3, 'final_filters': 1})
        self.assertEqual(native_phase(message)[0], 'binder candidate filtering')

    def test_terminal_binder_observations_retain_screen_counts_and_exact_log_provenance(self):
        job = self.queued()
        message = ('Starting trajectory: fixture\n'
                   'Base AF2 filters not passed for fixture_mpnn1, skipping interface scoring\n')
        job['_prepared']['argv'] = [sys.executable, '-c', 'print(' + repr(message) + ')']
        job['state'] = 'starting'
        with self.store.transaction() as db:
            self.store.put(db, 'job', job)
        run_job(self.store, job['job_id'], self.config)
        result = self.api.call('job.get', {'job_id': job['job_id']})
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['progress']['binder']['candidates_rejected'], 1)
        observed = result['provenance']['binder_observations']
        self.assertEqual(observed['rejection_screens'], {'base_af2': 1, 'final_filters': 0})
        artifact = self.store.read('artifact', observed['log_artifact_id'], 'alice')
        self.assertEqual(artifact['sha256'], observed['log_sha256'])
        self.assertIn('final sealed candidate tables', observed['counts_scope'])

    def test_context_archives_original_inputs_and_binds_actual_output_residue_ids(self):
        job = self.seal_fixture()
        root = self.store.directory('jobs', job['job_id']); root.mkdir(parents=True)
        first = seal_inputs(self.store, job, root)
        self.assertEqual(seal_inputs(self.store, job, root), first)
        candidate = next(c for c in self.api.call('binder.candidates', {'job_id': job['job_id']})['candidates'] if c['status'] == 'accepted')
        chosen = candidate['structure_artifacts'][0]
        context = self.api.call('binder.context', {'job_id': job['job_id'], 'artifact_id': chosen['artifact_id']})
        self.assertEqual(context['output_mapping']['status'], 'available')
        self.assertEqual(len(context['output_mapping']['pairs']), 5)
        self.assertEqual(context['output_mapping']['pairs'][2]['output'], {'chain': 'A', 'number': 3, 'insertion_code': ''})
        self.assertEqual(context['original_structure_artifact']['sha256'], self.target['sha256'])
        self.assertEqual(context['original_structure_artifact']['role'], 'target_structure')
        self.assertEqual(context['output_mapping']['sha256'], chosen['sha256'])
        with self.assertRaises(Error):
            self.bob.call('binder.context', {'job_id': job['job_id'], 'artifact_id': chosen['artifact_id']})

    def test_native_result_from_different_input_manifest_is_not_ingested(self):
        job = self.seal_fixture()
        artifact = next(a for a in self.api.call('job.artifacts', {'job_id': job['job_id']})['artifacts'] if a['name'].endswith('bindcraft-result.json'))
        path = self.store.directory('artifacts', artifact['artifact_id']) / 'content'
        path.chmod(0o600)
        value = {'schema': 1, 'kind': 'bindcraft-result', 'input_manifest': {'files': {}}}
        path.write_bytes(canonical(value))
        with self.store.transaction() as db:
            stored = self.store.get(db, 'artifact', artifact['artifact_id'])
            stored.update(sha256=file_sha(path), size=path.stat().st_size)
            self.store.put(db, 'artifact', stored)
        with self.assertRaisesRegex(Error, 'different input manifest'):
            self.api.call('binder.candidates', {'job_id': job['job_id']})


if __name__ == '__main__':
    unittest.main()
