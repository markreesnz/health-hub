"""Versioned study content, separate from device/drill state. Python stdlib only."""
import hashlib
import json
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

VERSION = 1
MAX_BYTES = 12_000_000
BLOCKS = {
    'cards.js': [('CARDS', 'cards')],
    'drills.js': [('DRILLS', 'drills')],
    'papers.js': [('PAPERS', 'papers')],
    'regions.js': [('REGIONS', 'regions')],
    'assessment.js': [('ASSESS', 'assessment'), ('CURRIC', 'legacy_curriculum'), ('TOLEARN', 'learning')],
    'backlog.js': [('BACKLOG', 'backlog')],
    'written_practice_data.js': [('WRITTEN_PRACTICE', 'written')],
    'curriculum_data.js': [('CURRICULUM_EVIDENCE', 'curriculum')],
}
KEYS = {key for rows in BLOCKS.values() for _, key in rows}
FORBIDDEN = {'scheme', 'answer', 'answers', 'raw_answer', 'corrected_answer', 'private_rubric_ref', 'marking_key', 'answer_key', 'private', 'source_files'}
ACTIVE_HTML = re.compile(r'<\s*(?:script|iframe|object|embed|svg|math|base|meta|link)\b|\bon[a-z]+\s*=|(?:javascript|vbscript)\s*:', re.I)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def require(ok, message):
    if not ok:
        raise APIError(400, message)


def safe_public(value, depth=0):
    require(depth < 40, 'Content nesting is too deep')
    if isinstance(value, dict):
        require(not (FORBIDDEN & set(value)), 'Private marking data is not permitted in public content')
        for k, v in value.items():
            require(k not in {'__proto__', 'constructor', 'prototype'}, 'Reserved object key')
            safe_public(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            safe_public(v, depth + 1)
    elif isinstance(value, str):
        require(not ACTIVE_HTML.search(value), 'Active HTML is not permitted in study content')


def unique(rows, field, label):
    require(isinstance(rows, list), label + ' must be an array')
    ids = [r.get(field) for r in rows if isinstance(r, dict)]
    require(len(ids) == len(rows) and all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in ids), label + ': missing identity')
    require(len(ids) == len(set(ids)), label + ': duplicate identity')
    return set(ids)


def validate(public, private):
    require(isinstance(public, dict) and set(public) == KEYS, 'Unexpected or missing public sections')
    require(isinstance(private, dict), 'Private archive must be an object')
    for key in KEYS - {'written', 'curriculum'}:
        require(isinstance(public[key], list), key + ' must be an array')
    safe_public(public)
    ids = unique(public['drills'], 'k', 'Drills')
    for d in public['drills']:
        require(isinstance(d.get('q'), str) and bool(d['q'].strip()), 'Empty drill question')
        require(isinstance(d.get('o'), list) and 2 <= len(d['o']) <= 6, 'Invalid drill options')
        require(all(isinstance(o, str) for o in d['o']) and len(set(d['o'])) == len(d['o']), 'Duplicate or invalid drill options')
        require(type(d.get('a')) is int and 0 <= d['a'] < len(d['o']), 'Invalid drill answer')
    unique(public['regions'], 'ch', 'Reading chapters')
    unique(public['papers'], 'id', 'Papers')
    bank = public['written']
    require(isinstance(bank, dict) and bank.get('version') == 1, 'Unsupported written bank')
    qids = unique(bank.get('questions'), 'id', 'Written questions')
    unique(bank.get('results'), 'attempt_id', 'Written results')
    qmap = {q['id']: q for q in bank['questions']}
    allowed = {'id', 'area', 'chapter', 'concept', 'family', 'lo', 'q', 'marks', 'reserve', 'scope'}
    for q in bank['questions']:
        require(set(q) <= allowed, 'Unexpected public question field')
        require(type(q.get('marks')) is int and 0 < q['marks'] <= 10, 'Invalid question marks')
    linked = set()
    for r in bank['results']:
        require(r.get('conditions') in {'unknown', 'guided', 'closed_book', 'assisted', 'retake', 'unseen_timed_written_mock'}, 'Unknown assessment conditions')
        unique(r.get('parts'), 'question_id', 'Result parts')
        require(isinstance(r.get('assessment_ids'), list) and r['assessment_ids'], 'Missing assessment link')
        for aid in r['assessment_ids']:
            require(isinstance(aid, str) and bool(re.fullmatch(r'[a-f0-9]{20}', aid)) and aid not in linked, 'Duplicate or invalid assessment link')
            linked.add(aid)
        for part in r['parts']:
            require(part['question_id'] in qids, 'Result references an unknown question')
            require(type(part.get('marks')) is int and 0 <= part['marks'] <= qmap[part['question_id']]['marks'], 'Invalid awarded marks')
    for row in public['assessment']:
        require(isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ('d', 'z', 'q', 'fmt')), 'Malformed assessment')
        if 'outOf' in row:
            require(type(row['outOf']) is int and row['outOf'] >= 0 and type(row.get('marks')) is int and 0 <= row['marks'] <= row['outOf'], 'Invalid assessment score')
    curriculum = public['curriculum']
    require(isinstance(curriculum, dict) and curriculum.get('version') == 1, 'Unsupported curriculum data')
    demands = unique(curriculum.get('demands'), 'id', 'Curriculum demands')
    topics = unique(curriculum.get('topics'), 'id', 'Curriculum topics')
    for d in curriculum['demands']:
        require(all(t in topics for t in d.get('topic_ids', [])), 'Unknown curriculum topic')
        for r in d.get('remediation', []):
            require(not r.get('in_pool') or r.get('drill_key') in ids, 'Missing remediation drill')
    # Full source-backed validation remains in the publishing tool. This endpoint
    # never invents marks or builds a marking key from public drill answers.


def preserve(old, new):
    for label, a, b, field in [
        ('drills', old['drills'], new['drills'], 'k'),
        ('questions', old['written']['questions'], new['written']['questions'], 'id'),
        ('results', old['written']['results'], new['written']['results'], 'attempt_id'),
    ]:
        require({x[field] for x in a} <= {x[field] for x in b}, 'Refusing to remove existing ' + label)
    key = lambda a: (a['d'], a['z'], a['fmt'], a['q'])
    require({key(a) for a in old['assessment']} <= {key(a) for a in new['assessment']}, 'Refusing to remove existing assessments')


def requirements(public):
    return {'drills': [d['k'] for d in public['drills']],
            'questions': [q['id'] for q in public['written']['questions']],
            'results': [r['attempt_id'] for r in public['written']['results']],
            'assessment': [digest([a['d'], a['z'], a['fmt'], a['q']]) for a in public['assessment']],
            'topics': [t['id'] for t in public['curriculum']['topics']],
            'demands': [d['id'] for d in public['curriculum']['demands']]}


def validate_seed(base, public):
    text = base.decode('utf8')
    match = re.search(r'/\*WSET_SEED_BEGIN\n(.*?)\nWSET_SEED_END\*/', text, re.S)
    if not match:
        raise APIError(503, 'App is missing study baseline protection')
    expected, actual = json.loads(match[1]), requirements(public)
    for key, values in expected.items():
        require(set(values) <= set(actual[key]), 'Publication omits embedded ' + key)


class StudyStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        os.chmod(self.path, 0o600)
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('CREATE TABLE IF NOT EXISTS revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL, request_digest TEXT NOT NULL, public_digest TEXT NOT NULL, created TEXT NOT NULL, actor TEXT NOT NULL, public BLOB NOT NULL, private BLOB NOT NULL)')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def unpack(row, include_private=False):
        if not row:
            return {'schema_version': VERSION, 'revision': 0, 'public_digest': None, 'public': None}
        result = {'schema_version': VERSION, 'revision': row[0], 'request_id': row[1], 'public_digest': row[3], 'created': row[4], 'public': json.loads(zlib.decompress(row[6]))}
        if include_private:
            result['private'] = json.loads(zlib.decompress(row[7]))
        return result

    def read(self, include_private=False, revision=None):
        if not os.path.exists(self.path):
            if revision is not None:
                raise APIError(404, 'Unknown revision')
            return self.unpack(None)
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM revisions WHERE revision=?', (revision,)).fetchone() if revision is not None else conn.execute('SELECT * FROM revisions ORDER BY revision DESC LIMIT 1').fetchone()
            if revision is not None and not row:
                raise APIError(404, 'Unknown revision')
            return self.unpack(row, include_private)

    def history(self):
        if not os.path.exists(self.path):
            return []
        with self.connect() as conn:
            return [dict(zip(('revision', 'request_id', 'public_digest', 'created'), r)) for r in conn.execute('SELECT revision,request_id,public_digest,created FROM revisions ORDER BY revision DESC LIMIT 100')]

    def commit(self, request, actor):
        require(isinstance(request, dict) and set(request) == {'schema_version', 'request_id', 'expected_revision', 'public', 'private'}, 'Invalid commit envelope')
        require(request['schema_version'] == VERSION and type(request['expected_revision']) is int and request['expected_revision'] >= 0, 'Invalid API version or revision')
        require(isinstance(request['request_id'], str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request['request_id'])), 'Invalid request ID')
        validate(request['public'], request['private'])
        req_hash = digest(request)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT revision,request_digest,public_digest FROM revisions WHERE request_id=?', (request['request_id'],)).fetchone()
            if previous:
                if previous[1] != req_hash:
                    raise APIError(409, 'Request ID was already used with different data')
                return {'ok': True, 'revision': previous[0], 'public_digest': previous[2], 'duplicate': True}
            current = conn.execute('SELECT * FROM revisions ORDER BY revision DESC LIMIT 1').fetchone()
            revision = current[0] if current else 0
            if request['expected_revision'] != revision:
                raise APIError(409, 'Revision conflict; read the latest export before retrying')
            if current:
                preserve(self.unpack(current)['public'], request['public'])
            pub_hash = digest(request['public'])
            cursor = conn.execute('INSERT INTO revisions(request_id,request_digest,public_digest,created,actor,public,private) VALUES(?,?,?,?,?,?,?)', (request['request_id'], req_hash, pub_hash, datetime.now(timezone.utc).isoformat(), actor, zlib.compress(encoded(request['public'])), zlib.compress(encoded(request['private']))))
            return {'ok': True, 'revision': cursor.lastrowid, 'public_digest': pub_hash, 'duplicate': False}


def render(base, snapshot):
    if not snapshot['revision']:
        return base
    html = base.decode('utf8')
    for name, fields in BLOCKS.items():
        start, end = '/*WSET_DATA_BEGIN:' + name + '*/', '/*WSET_DATA_END:' + name + '*/'
        if html.count(start) != 1 or html.count(end) != 1:
            raise APIError(503, 'App code is incompatible with saved study data')
        body = '\n'.join('var ' + variable + '=' + json.dumps(snapshot['public'][key], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029') + ';' for variable, key in fields)
        a, b = html.index(start) + len(start), html.index(end)
        html = html[:a] + '\n' + body + '\n' + html[b:]
    html = html.replace('window.__wsetStudyRevision=0;', 'window.__wsetStudyRevision=' + str(snapshot['revision']) + ';', 1)
    return html.encode('utf8')
