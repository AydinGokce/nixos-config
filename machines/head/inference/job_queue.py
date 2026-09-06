"""Head-local SQLite queue. Shared GPU storage contains only atomic spools.

Expired executions become interrupted, never implicitly retried. A lease token
fences late outputs; every explicit retry has a separate durable attempt.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from .common import canonical, digest, identifier, now


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                  payload TEXT NOT NULL, payload_sha TEXT NOT NULL, config_id TEXT NOT NULL,
                  state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                  attempt INTEGER NOT NULL DEFAULT 0, worker TEXT, generation TEXT,
                  token TEXT, lease_until REAL, result TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(state,config_id,created);
                CREATE TABLE IF NOT EXISTS attempts(
                  job_id TEXT NOT NULL, number INTEGER NOT NULL, worker TEXT NOT NULL,
                  generation TEXT NOT NULL, token TEXT UNIQUE NOT NULL,
                  state TEXT NOT NULL, started REAL NOT NULL, finished REAL,
                  result TEXT, error TEXT, PRIMARY KEY(job_id,number));
                CREATE TABLE IF NOT EXISTS events(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, utc REAL NOT NULL,
                  job_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    @staticmethod
    def event(db, job_id, kind, data):
        db.execute('INSERT INTO events(utc,job_id,kind,data) VALUES(?,?,?,?)',
                   (now(), job_id, kind, canonical(data).decode()))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ('payload', 'result', 'error'):
            if result.get(key) is not None:
                result[key] = json.loads(result[key])
        return result

    def enqueue(self, payload, *, request_key=None):
        payload = json.loads(canonical(payload))
        config_id = payload['config_id']
        if not isinstance(config_id, str) or len(config_id) != 64:
            raise ValueError('A pinned resident configuration is required')
        job_id = identifier(payload.get('id') or uuid.uuid4().hex)
        if payload.get('required_worker_id') is not None:
            identifier(payload['required_worker_id'])
        payload['id'] = job_id
        request_key = request_key or job_id
        fingerprint = digest({k: v for k, v in payload.items() if k != 'id'})
        with self.transaction() as db:
            existing = db.execute('SELECT * FROM jobs WHERE request_key=?', (request_key,)).fetchone()
            if existing:
                if existing['payload_sha'] != fingerprint:
                    raise ValueError('Request key already belongs to different input/settings')
                return self.decode(existing)
            timestamp = now()
            db.execute('INSERT INTO jobs(id,request_key,payload,payload_sha,config_id,state,created,updated) '
                       'VALUES(?,?,?,?,?,?,?,?)', (job_id, request_key, canonical(payload).decode(), fingerprint,
                       config_id, 'queued', timestamp, timestamp))
            self.event(db, job_id, 'enqueued', {'config_id': config_id})
            return self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def claim(self, config_id, worker, generation, *, deadline, lease_seconds=90):
        identifier(worker); identifier(generation)
        timestamp = now()
        if deadline <= timestamp + 30:
            return None
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE worker=? AND state='running'", (worker,)).fetchone():
                return None
            candidates = db.execute("SELECT * FROM jobs WHERE config_id=? AND state='queued' ORDER BY created,id",
                                    (config_id,))
            row = next((candidate for candidate in candidates
                        if json.loads(candidate['payload']).get('required_worker_id') in (None, worker)), None)
            if row is None:
                return None
            number = row['attempt'] + 1
            token = uuid.uuid4().hex
            lease_until = min(deadline, timestamp + lease_seconds)
            db.execute("UPDATE jobs SET state='running',attempt=?,worker=?,generation=?,token=?,lease_until=?,updated=?,error=NULL WHERE id=?",
                       (number, worker, generation, token, lease_until, timestamp, row['id']))
            db.execute('INSERT INTO attempts(job_id,number,worker,generation,token,state,started) VALUES(?,?,?,?,?,?,?)',
                       (row['id'], number, worker, generation, token, 'running', timestamp))
            self.event(db, row['id'], 'claimed', {'attempt': number, 'worker': worker, 'generation': generation})
            return self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())

    def renew(self, job_id, token, generation, *, deadline, lease_seconds=90):
        timestamp = now()
        with self.transaction() as db:
            count = db.execute("UPDATE jobs SET lease_until=?,updated=? WHERE id=? AND state='running' AND token=? AND generation=? AND lease_until>?",
                               (min(deadline, timestamp + lease_seconds), timestamp, job_id, token, generation, timestamp)).rowcount
            return count == 1

    def finish(self, job_id, token, generation, result, *, error=None):
        timestamp = now()
        state = 'failed' if error is not None else 'predicted'
        encoded = canonical(result).decode() if result is not None else None
        encoded_error = canonical(error).decode() if error is not None else None
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['state'] != 'running' or row['token'] != token or row['generation'] != generation or row['lease_until'] <= timestamp:
                raise ValueError('Stale or expired execution receipt')
            db.execute('UPDATE jobs SET state=?,result=?,error=?,updated=? WHERE id=?',
                       (state, encoded, encoded_error, timestamp, job_id))
            db.execute('UPDATE attempts SET state=?,result=?,error=?,finished=? WHERE token=?',
                       (state, encoded, encoded_error, timestamp, token))
            self.event(db, job_id, state, {'attempt': row['attempt']})

    def postprocessed(self, job_id, result, *, error=None, token=None):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['state'] != 'predicted':
                raise ValueError('Prediction must finish before postprocessing')
            if token is not None and row['token'] != token:
                raise ValueError('CPU outcome belongs to an earlier attempt')
            state = 'failed' if error is not None else 'complete'
            encoded = canonical(result).decode()
            encoded_error = canonical(error).decode() if error is not None else None
            timestamp = now()
            db.execute('UPDATE jobs SET state=?,result=?,error=?,updated=? WHERE id=?',
                       (state, encoded, encoded_error, timestamp, job_id))
            db.execute('UPDATE attempts SET state=?,result=?,error=?,finished=? WHERE token=?',
                       (state, encoded, encoded_error, timestamp, row['token']))
            self.event(db, job_id, state, {'stage': 'postprocess', 'attempt': row['attempt']})

    def expire(self):
        timestamp = now()
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM jobs WHERE state='running' AND lease_until<=?", (timestamp,)).fetchall()
            for row in rows:
                error = canonical({'reason': 'worker_lease_expired', 'automatic_retry': False}).decode()
                db.execute("UPDATE jobs SET state='interrupted',error=?,updated=? WHERE id=?", (error, timestamp, row['id']))
                db.execute("UPDATE attempts SET state='interrupted',error=?,finished=? WHERE token=?", (error, timestamp, row['token']))
                self.event(db, row['id'], 'interrupted', {'attempt': row['attempt']})
            return [row['id'] for row in rows]

    def retry(self, job_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['state'] not in ('failed', 'interrupted'):
                raise ValueError('Only a failed/interrupted job can be explicitly retried')
            db.execute("UPDATE jobs SET state='queued',worker=NULL,generation=NULL,token=NULL,lease_until=NULL,result=NULL,error=NULL,updated=? WHERE id=?", (now(), job_id))
            self.event(db, job_id, 'explicit_retry', {'previous_attempt': row['attempt']})

    def get(self, job_id):
        with self.connection() as db:
            result = self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            if result:
                result['attempts'] = [self.decode(x) for x in db.execute('SELECT * FROM attempts WHERE job_id=? ORDER BY number', (job_id,))]
            return result

    def list(self, state=None):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM jobs' + (' WHERE state=?' if state else '') + ' ORDER BY created,id', (state,) if state else ())
            return [self.decode(row) for row in rows]
