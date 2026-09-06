from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .common import canonical, digest, identifier, no_links, now, parse, require, string, uid


class Store:
    def __init__(self, root):
        self.root = no_links(Path(root))
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = no_links(self.root / 'state.sqlite')
        with self.connection() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS objects(
                kind TEXT NOT NULL,id TEXT PRIMARY KEY,actor TEXT NOT NULL,state TEXT NOT NULL,
                data TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS object_list ON objects(kind,actor,created,id);
              CREATE INDEX IF NOT EXISTS artifact_parent ON objects(kind,actor,json_extract(data,'$.job_id'));
              CREATE TABLE IF NOT EXISTS requests(
                actor TEXT NOT NULL,method TEXT NOT NULL,key TEXT NOT NULL,
                sha256 TEXT NOT NULL,object_id TEXT NOT NULL,
                PRIMARY KEY(actor,method,key));
              CREATE TABLE IF NOT EXISTS events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,object_id TEXT NOT NULL,
                created TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS operations(
                object_id TEXT PRIMARY KEY,kind TEXT NOT NULL,unit TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,intent_sha256 TEXT NOT NULL,invocation_id TEXT,
                data TEXT NOT NULL,updated TEXT NOT NULL);
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

    def directory(self, kind, ident):
        require(kind in {'uploads', 'batches', 'jobs', 'operations', 'artifacts'}, 'Unknown storage kind')
        return no_links(self.root / kind / identifier(ident))

    @staticmethod
    def get(db, kind, ident, actor=None):
        identifier(ident)
        row = db.execute('SELECT * FROM objects WHERE kind=? AND id=?', (kind, ident)).fetchone()
        require(row is not None and (actor is None or row['actor'] == actor), 'Resource not found', 'not_found')
        return parse(row['data'])

    def read(self, kind, ident, actor=None):
        with self.connection() as db:
            return self.get(db, kind, ident, actor)

    @staticmethod
    def put(db, kind, data, actor=None):
        ident = data[kind + '_id']
        row = db.execute('SELECT actor,created FROM objects WHERE id=? AND kind=?', (ident, kind)).fetchone()
        actor = row['actor'] if row else string(actor, 'actor', 128)
        data['created_at'] = row['created'] if row else data.get('created_at', now())
        data['updated_at'] = now()
        db.execute('INSERT INTO objects(kind,id,actor,state,data,created,updated) VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET state=excluded.state,data=excluded.data,updated=excluded.updated',
                   (kind, ident, actor, data.get('state', ''), canonical(data).decode(), data['created_at'], data['updated_at']))
        return data

    @staticmethod
    def event(db, ident, kind, data):
        db.execute('INSERT INTO events(object_id,created,kind,data) VALUES(?,?,?,?)',
                   (ident, now(), kind, canonical(data).decode()))

    @staticmethod
    def idem(db, actor, method, key, payload, ident=None):
        string(key, 'request_key', 200)
        fingerprint = digest(payload)
        row = db.execute('SELECT * FROM requests WHERE actor=? AND method=? AND key=?', (actor, method, key)).fetchone()
        if row:
            require(row['sha256'] == fingerprint, 'Request key belongs to different parameters', 'conflict')
            return row['object_id']
        if ident is not None:
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?)', (actor, method, key, fingerprint, ident))
        return None

    def listing(self, kind, actor=None, states=None):
        query, args = 'SELECT data FROM objects WHERE kind=?', [kind]
        if actor is not None:
            query += ' AND actor=?'; args.append(actor)
        if states is not None:
            query += ' AND state IN (' + ','.join('?' for _ in states) + ')'; args.extend(states)
        query += ' ORDER BY created,id'
        with self.connection() as db:
            return [parse(row['data']) for row in db.execute(query, args)]

    def actor(self, ident):
        with self.connection() as db:
            row = db.execute('SELECT actor FROM objects WHERE id=?', (identifier(ident),)).fetchone()
            require(row is not None, 'Resource not found', 'not_found')
            return row['actor']
