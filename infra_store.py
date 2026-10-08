"""Persistent, private state for the software-engineering job radar."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_settings():
    try:
        from infra_scan import DEFAULT_SOURCES
        sources = deepcopy(DEFAULT_SOURCES)
    except ImportError:
        sources = []
    return {
        'min_salary': 150000, 'currency': 'USD', 'salary_period': 'year',
        'salary_rule': 'minimum', 'remote_allowed': True,
        'remote_region': 'United States', 'locations': ['San Francisco'],
        'focus': 'Agent infrastructure software engineering',
        'include_keywords': ['agent', 'agents', 'agentic', 'infrastructure'],
        'exclude_keywords': ['developer relations', 'devrel', 'sales', 'engineering manager', 'director', 'vice president'],
        'profile_text': '', 'email_enabled': True, 'email_to': '', 'email_from': '',
        'connector_base': '', 'schedule_enabled': True, 'schedule_hour': 16,
        'schedule_minute': 0, 'timezone': 'America/Los_Angeles', 'sources': sources,
    }


def valid_gateway_url(value):
    """Only Sprite's credential-injecting Resend gateway, never arbitrary hosts."""
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        # Do not accept userinfo, alternate ports, query credentials or path traversal.
        if (parsed.scheme != 'https' or parsed.hostname != 'api.sprites.dev'
                or parsed.netloc != 'api.sprites.dev' or parsed.query or parsed.fragment):
            return False
        path = parsed.path.rstrip('/')
        return (path.startswith('/v1/gateway/resend/')
                and re.fullmatch(r'/v1/gateway/resend/[A-Za-z0-9_-]+', path) is not None
                and '..' not in path and '//' not in path)
    except ValueError:
        return False


def valid_email(value):
    if not isinstance(value, str) or len(value) > 320 or '\n' in value or '\r' in value:
        return False
    # Resend supports either a plain address or a display name plus <address>.
    match = re.fullmatch(r'(?:[^<>\r\n]{1,100}\s*<)?([^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)>?', value)
    if not match or ('<' in value) != value.endswith('>'):
        return False
    address = match.group(1)
    return bool(re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", address))


def validate_settings(patch):
    defaults = default_settings()
    if not isinstance(patch, dict) or set(patch) - set(defaults):
        raise ValueError('Unknown settings field')
    for key, value in patch.items():
        if key in {'remote_allowed', 'email_enabled', 'schedule_enabled'}:
            if type(value) is not bool:
                raise ValueError(f'{key} must be boolean')
        elif key in {'min_salary', 'schedule_hour', 'schedule_minute'}:
            high = {'min_salary': 2000000, 'schedule_hour': 23, 'schedule_minute': 59}[key]
            if type(value) is not int or not 0 <= value <= high:
                raise ValueError(f'Invalid {key}')
        elif key == 'salary_rule':
            if value not in {'minimum', 'range_reaches'}:
                raise ValueError('salary_rule must be minimum or range_reaches')
        elif key == 'currency':
            if value != 'USD':
                raise ValueError('Only USD salaries are supported')
        elif key == 'salary_period':
            if value != 'year':
                raise ValueError('Only annual salaries are supported')
        elif key in {'email_to', 'email_from'}:
            if value != '' and not valid_email(value):
                raise ValueError(f'Invalid {key}')
        elif key == 'connector_base':
            if value != '' and not valid_gateway_url(value):
                raise ValueError('Use the HTTPS Sprite gateway URL; do not enter a provider API key')
        elif key == 'timezone':
            if not isinstance(value, str) or len(value) > 80:
                raise ValueError('Invalid timezone')
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('Unknown timezone') from None
        elif key in {'locations', 'include_keywords', 'exclude_keywords'}:
            if (not isinstance(value, list) or len(value) > 50
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 120 for item in value)):
                raise ValueError(f'Invalid {key}')
        elif key == 'sources':
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError('Invalid sources')
            unique = set()
            for source in value:
                if not isinstance(source, dict) or set(source) - {'provider', 'slug', 'company', 'enabled'}:
                    raise ValueError('Invalid source fields')
                if source.get('provider') not in {'greenhouse', 'lever', 'ashby'}:
                    raise ValueError('Unsupported source provider')
                if not isinstance(source.get('slug'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source['slug']):
                    raise ValueError('Invalid source slug')
                if not isinstance(source.get('company'), str) or not 1 <= len(source['company']) <= 120:
                    raise ValueError('Invalid source company')
                if type(source.get('enabled', True)) is not bool:
                    raise ValueError('Invalid source enabled flag')
                identity = (source['provider'], source['slug'])
                if identity in unique:
                    raise ValueError('Duplicate source')
                unique.add(identity)
        else:
            high = 30000 if key == 'profile_text' else 400
            if not isinstance(value, str) or len(value) > high or '\x00' in value:
                raise ValueError(f'Invalid {key}')
    return deepcopy(patch)


class Store:
    """Uses its own database, without touching the legacy DevRel tables/files."""
    def __init__(self, db_path):
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS infra_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS infra_jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS infra_runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS infra_alerts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, recipient TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    attempted_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    provider_id TEXT, error TEXT, ambiguous INTEGER NOT NULL DEFAULT 0,
                    delivery_id TEXT, UNIQUE(job_id, recipient));
                CREATE TABLE IF NOT EXISTS infra_deliveries (
                    id TEXT PRIMARY KEY, recipient TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, endpoint TEXT NOT NULL, alert_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, attempted_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, provider_id TEXT, error TEXT,
                    ambiguous INTEGER NOT NULL DEFAULT 0);
            ''')
            if 'ambiguous' not in {row[1] for row in db.execute('PRAGMA table_info(infra_alerts)')} :
                db.execute('ALTER TABLE infra_alerts ADD COLUMN ambiguous INTEGER NOT NULL DEFAULT 0')
            if 'delivery_id' not in {row[1] for row in db.execute('PRAGMA table_info(infra_alerts)')}:
                db.execute('ALTER TABLE infra_alerts ADD COLUMN delivery_id TEXT')
            db.execute('INSERT OR IGNORE INTO infra_settings(id,data) VALUES(1,?)', (json.dumps(default_settings()),))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    def get_settings(self):
        with self.connection() as db:
            row = db.execute('SELECT data FROM infra_settings WHERE id=1').fetchone()
        return {**default_settings(), **json.loads(row['data'])}

    def settings_revision(self):
        with self.connection() as db:
            return db.execute('SELECT revision FROM infra_settings WHERE id=1').fetchone()[0]

    def update_settings(self, patch):
        patch = validate_settings(patch)
        with self.connection() as db:
            current = json.loads(db.execute('SELECT data FROM infra_settings WHERE id=1').fetchone()[0])
            if any(current.get(key) != value for key, value in patch.items()):
                current.update(patch)
                db.execute('UPDATE infra_settings SET data=?,revision=revision+1 WHERE id=1', (json.dumps(current),))
        return self.get_settings()

    def list_jobs(self, filters=None, **kwargs):
        filters = {**(filters or {}), **kwargs}
        with self.connection() as db:
            jobs = [json.loads(row['data']) for row in db.execute('SELECT data FROM infra_jobs')]
        for key in ('match_status', 'status', 'saved', 'applied', 'hidden'):
            if filters.get(key) is not None:
                jobs = [job for job in jobs if job.get(key) == filters[key]]
        if filters.get('q'):
            query = str(filters['q']).lower()
            jobs = [job for job in jobs if query in ' '.join(str(job.get(k, '')) for k in ('title', 'company', 'location')).lower()]
        return sorted(jobs, key=lambda job: (job.get('first_seen', ''), job.get('company', '')), reverse=True)

    def get_job(self, job_id):
        with self.connection() as db:
            row = db.execute('SELECT data FROM infra_jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def save_jobs(self, jobs):
        revision = self.settings_revision()
        timestamp = now_iso()
        with self.connection() as db:
            for incoming in jobs:
                if not isinstance(incoming, dict) or not isinstance(incoming.get('id'), str) or not incoming['id']:
                    raise ValueError('Job id required')
                row = db.execute('SELECT data FROM infra_jobs WHERE id=?', (incoming['id'],)).fetchone()
                old = json.loads(row['data']) if row else {}
                job = {**old, **deepcopy(incoming)}
                job['first_seen'] = old.get('first_seen') or incoming.get('first_seen') or timestamp
                job['last_seen'] = incoming.get('last_seen') or timestamp
                job['settings_revision'] = revision
                job.setdefault('status', 'open')
                job.setdefault('match_status', 'review')
                job.setdefault('reasons', [])
                job.setdefault('caveats', [])
                for flag in ('saved', 'applied', 'hidden', 'seen'):
                    job[flag] = bool(old.get(flag, False))
                db.execute('INSERT INTO infra_jobs(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (job['id'], json.dumps(job)))
        return len(jobs)

    def reclassify_jobs(self, classifier=None):
        if classifier is None:
            from infra_scan import categorize_job
            classifier = categorize_job
        settings = self.get_settings()
        revision = self.settings_revision()
        with self.connection() as db:
            for row in db.execute('SELECT id,data FROM infra_jobs').fetchall():
                original = json.loads(row['data'])
                job = {**original, **classifier(deepcopy(original), settings), 'settings_revision': revision}
                # A settings edit is not evidence of a job being refreshed or closed.
                for key in ('first_seen', 'last_seen', 'saved', 'applied', 'hidden', 'seen', 'status'):
                    if key in original:
                        job[key] = original[key]
                db.execute('UPDATE infra_jobs SET data=? WHERE id=?', (json.dumps(job), row['id']))
        return self.list_jobs()

    def update_job(self, job_id, patch):
        flags = {'saved', 'applied', 'hidden', 'seen'}
        if not isinstance(patch, dict) or not patch or set(patch) - flags or any(type(value) is not bool for value in patch.values()):
            raise ValueError('Use boolean saved, applied, hidden or seen fields')
        with self.connection() as db:
            row = db.execute('SELECT data FROM infra_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise KeyError('Unknown job')
            job = json.loads(row['data'])
            job.update(patch)
            db.execute('UPDATE infra_jobs SET data=? WHERE id=?', (json.dumps(job), job_id))
        return job

    def save_run(self, run, jobs=None):
        record = deepcopy(run)
        incoming = record.pop('jobs', []) if jobs is None else jobs
        self.save_jobs(incoming)
        record['job_ids'] = list(dict.fromkeys(job['id'] for job in incoming))
        record.setdefault('id', str(uuid4()))
        record.setdefault('created_at', now_iso())
        record.setdefault('finished_at', record['created_at'])
        record.setdefault('jobs_found', len(incoming))
        with self.connection() as db:
            db.execute('INSERT INTO infra_runs(id,created_at,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (record['id'], record['created_at'], json.dumps(record)))
        return record

    def list_runs(self, limit=20):
        limit = max(1, min(int(limit), 100))
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM infra_runs ORDER BY created_at DESC LIMIT ?', (limit,))]

    @staticmethod
    def alert_eligible(job):
        return (job.get('status') == 'open' and job.get('match_status') == 'match'
                and not job.get('hidden') and not job.get('applied'))

    def enqueue_alerts(self):
        settings = self.get_settings()
        # Keep candidate alerts pending even before an email address is configured.
        recipient = settings['email_to'].strip().lower()
        timestamp = now_iso()
        count = 0
        with self.connection() as db:
            jobs = {job['id']: job for job in (json.loads(row[0]) for row in db.execute('SELECT data FROM infra_jobs').fetchall())}
            # Superseded setup candidates must not inflate the queue. Do not touch
            # reserved/in-flight or ambiguous deliveries: they retain their history.
            for alert in db.execute("SELECT * FROM infra_alerts WHERE status='pending' AND delivery_id IS NULL AND ambiguous=0").fetchall():
                job = jobs.get(alert['job_id'])
                if alert['recipient'] != recipient or not job or not self.alert_eligible(job):
                    db.execute("UPDATE infra_alerts SET status='cancelled',error=?,updated_at=? WHERE id=? AND status='pending' AND delivery_id IS NULL AND ambiguous=0", ('No longer an eligible match for the current recipient.', timestamp, alert['id']))
            if not settings['email_enabled']:
                return 0
            for job in jobs.values():
                if not self.alert_eligible(job):
                    continue
                identity = hashlib.sha256((job['id'] + '\n' + recipient).encode()).hexdigest()
                result = db.execute('INSERT OR IGNORE INTO infra_alerts(id,job_id,recipient,status,created_at,updated_at) VALUES(?,?,?,?,?,?)', (identity, job['id'], recipient, 'pending', timestamp, timestamp))
                count += result.rowcount
                # Restore cancelled unsent matches, but never a previously sent alert.
                db.execute("UPDATE infra_alerts SET status='pending',error=NULL,updated_at=? WHERE id=? AND status='cancelled' AND delivery_id IS NULL", (timestamp, identity))
        return count

    def list_alerts(self, status=None):
        with self.connection() as db:
            if status:
                rows = db.execute('SELECT * FROM infra_alerts WHERE status=? ORDER BY created_at,id', (status,)).fetchall()
            else:
                rows = db.execute('SELECT * FROM infra_alerts ORDER BY created_at,id').fetchall()
        return [dict(row) for row in rows]

    def notification_counts(self, recipient=None):
        with self.connection() as db:
            if recipient is None:
                rows = db.execute('SELECT status,COUNT(*) AS n FROM infra_alerts GROUP BY status')
            else:
                rows = db.execute('SELECT status,COUNT(*) AS n FROM infra_alerts WHERE recipient=? GROUP BY status', (recipient.strip().lower(),))
            return {row['status']: row['n'] for row in rows}

    def sent_delivery_count(self):
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM infra_deliveries WHERE status='sent'").fetchone()[0]

    def update_alert(self, alert_id, status, error=None, provider_id=None, attempted=False):
        if status not in {'pending', 'sending', 'sent', 'uncertain', 'cancelled'}:
            raise ValueError('Invalid alert status')
        timestamp = now_iso()
        with self.connection() as db:
            db.execute('UPDATE infra_alerts SET status=?,error=?,provider_id=COALESCE(?,provider_id),updated_at=?,attempts=attempts+?,attempted_at=CASE WHEN ? THEN COALESCE(attempted_at,?) ELSE attempted_at END,ambiguous=CASE WHEN ? THEN 1 ELSE ambiguous END WHERE id=?', (status, error, provider_id, timestamp, int(attempted), int(attempted), timestamp, int(status == 'uncertain'), alert_id))

    def claim_alert(self, alert_id):
        """Avoid two threads/processes dispatching the same pending alert."""
        with self.connection() as db:
            result = db.execute("UPDATE infra_alerts SET status='sending',attempts=attempts+1,attempted_at=COALESCE(attempted_at,?),updated_at=? WHERE id=? AND status IN ('pending','uncertain')", (now_iso(), now_iso(), alert_id))
            return result.rowcount == 1

    def list_deliveries(self, active_only=False):
        with self.connection() as db:
            query = "SELECT * FROM infra_deliveries"
            if active_only:
                query += " WHERE status IN ('pending','sending','uncertain')"
            rows = db.execute(query + ' ORDER BY created_at,id').fetchall()
        result = []
        for row in rows:
            delivery = dict(row)
            delivery['alert_ids'] = json.loads(delivery['alert_ids'])
            result.append(delivery)
        return result

    def reserve_delivery(self, alert_ids, email, endpoint, settings_revision, run_id):
        """Atomically bind exact email bytes to pending alerts before any network call."""
        if not alert_ids or len(alert_ids) != len(set(alert_ids)):
            return None
        timestamp = now_iso()
        delivery_id = uuid4().hex
        recipient = email['to'][0].strip().lower()
        payload = json.dumps(email, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            revision = db.execute('SELECT revision FROM infra_settings WHERE id=1').fetchone()[0]
            current_run = db.execute('SELECT id FROM infra_runs ORDER BY created_at DESC LIMIT 1').fetchone()
            if revision != settings_revision or (current_run[0] if current_run else None) != run_id:
                return None
            # Another worker's fresh digest owns delivery for this recipient already.
            if db.execute("SELECT 1 FROM infra_deliveries WHERE recipient=? AND status IN ('pending','sending') LIMIT 1", (recipient,)).fetchone():
                return None
            for alert_id in alert_ids:
                row = db.execute('SELECT * FROM infra_alerts WHERE id=?', (alert_id,)).fetchone()
                if not row or row['recipient'] != recipient or row['status'] != 'pending' or row['delivery_id'] or row['ambiguous']:
                    return None
                job_row = db.execute('SELECT data FROM infra_jobs WHERE id=?', (row['job_id'],)).fetchone()
                if not job_row or not self.alert_eligible(json.loads(job_row[0])):
                    return None
            db.execute('INSERT INTO infra_deliveries(id,recipient,status,payload,endpoint,alert_ids,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (delivery_id, recipient, 'pending', payload, endpoint, json.dumps(alert_ids), timestamp, timestamp))
            for alert_id in alert_ids:
                db.execute('UPDATE infra_alerts SET delivery_id=?,updated_at=? WHERE id=?', (delivery_id, timestamp, alert_id))
        return next(item for item in self.list_deliveries() if item['id'] == delivery_id)

    def claim_delivery(self, delivery_id, settings_revision=None, run_id=None):
        timestamp = now_iso()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if settings_revision is not None:
                revision = db.execute('SELECT revision FROM infra_settings WHERE id=1').fetchone()[0]
                latest = db.execute('SELECT id FROM infra_runs ORDER BY created_at DESC LIMIT 1').fetchone()
                if revision != settings_revision or (latest[0] if latest else None) != run_id:
                    return False
            rows = db.execute('SELECT j.data FROM infra_alerts a LEFT JOIN infra_jobs j ON a.job_id=j.id WHERE a.delivery_id=?', (delivery_id,)).fetchall()
            if not rows or any(not row[0] or not self.alert_eligible(json.loads(row[0])) for row in rows):
                return False
            changed = db.execute("UPDATE infra_deliveries SET status='sending',attempts=attempts+1,attempted_at=COALESCE(attempted_at,?),updated_at=? WHERE id=? AND status IN ('pending','uncertain')", (timestamp, timestamp, delivery_id))
            if changed.rowcount != 1:
                return False
            db.execute("UPDATE infra_alerts SET status='sending',attempts=attempts+1,attempted_at=COALESCE(attempted_at,?),updated_at=? WHERE delivery_id=? AND status!='sent'", (timestamp, timestamp, delivery_id))
            return True

    def update_delivery(self, delivery_id, status, error=None, provider_id=None):
        if status not in {'pending', 'sent', 'uncertain', 'cancelled'}:
            raise ValueError('Invalid delivery status')
        timestamp = now_iso()
        with self.connection() as db:
            db.execute('UPDATE infra_deliveries SET status=?,error=?,provider_id=COALESCE(?,provider_id),updated_at=?,ambiguous=CASE WHEN ? THEN 1 ELSE ambiguous END WHERE id=?', (status, error, provider_id, timestamp, int(status == 'uncertain'), delivery_id))
            db.execute('UPDATE infra_alerts SET status=?,error=?,provider_id=COALESCE(?,provider_id),updated_at=?,ambiguous=CASE WHEN ? THEN 1 ELSE ambiguous END WHERE delivery_id=? AND status!=?', (status, error, provider_id, timestamp, int(status == 'uncertain'), delivery_id, 'sent'))
            if status == 'cancelled':
                # Cancellation is used only when delivery is known not to have happened.
                db.execute('UPDATE infra_alerts SET delivery_id=NULL WHERE delivery_id=?', (delivery_id,))
