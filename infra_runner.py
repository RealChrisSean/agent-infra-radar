"""Run the private job search from the UI or an external scheduled wakeup."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from infra_store import Store
from infra_scan import scan
from infra_notify import dispatch_pending

ROOT = Path(__file__).resolve().parent


def data_path():
    return Path(os.environ.get('RADAR_DATA', ROOT / 'data')) / 'agent-infra'


def get_store():
    return Store(data_path() / 'state.sqlite')


@contextmanager
def scan_lock():
    folder = data_path()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'scan.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A search is already running.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def is_running():
    try:
        with scan_lock():
            return False
    except RuntimeError:
        return True


def schedule_status(store=None):
    store = store or get_store()
    settings = store.get_settings()
    active = (data_path() / 'schedule-active.json').exists()
    enabled = settings['schedule_enabled']
    return {
        'status': 'active' if active and enabled else 'paused' if active else 'not_configured',
        'message': ('Daily at 4 PM Pacific' if enabled else 'Scheduled searches paused') if active
                   else 'Daily schedule has not been connected yet.',
        'timezone': 'America/Los_Angeles',
    }


def run_search(scheduled=False, notify=True):
    store = get_store()
    with scan_lock():
        settings = store.get_settings()
        if scheduled:
            if not settings['schedule_enabled']:
                return {'status': 'skipped', 'summary': 'Scheduled searches are paused.'}
            local_day = datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
            marker = data_path() / 'last-scheduled.json'
            if marker.exists():
                try:
                    if json.loads(marker.read_text()).get('day') == local_day:
                        return {'status': 'skipped', 'summary': 'Today’s scheduled search already ran.'}
                except (OSError, ValueError):
                    pass
        started = datetime.now(timezone.utc).isoformat()
        begin = time.monotonic()
        try:
            result = scan(settings)
        except Exception:
            # Keep internal/network details out of the private API and scheduled output.
            result = {'status': 'failed', 'summary': 'The search could not complete. Previous jobs were kept.',
                      'jobs': [], 'coverage': []}
        result.update(started_at=started, completed_at=datetime.now(timezone.utc).isoformat(),
                      duration_seconds=round(time.monotonic() - begin, 1), trigger='scheduled' if scheduled else 'manual')
        saved = store.save_run(result)
        # Settings may have changed during network fetches. Recheck retained and
        # newly fetched roles against the current criteria before queuing mail.
        store.reclassify_jobs()
        store.enqueue_alerts()
        delivery = dispatch_pending(store) if notify else {'message': 'Email delivery skipped for this run.'}
        if scheduled and result['status'] != 'failed':
            marker.write_text(json.dumps({'day': local_day, 'run_id': saved.get('id')}))
        return {'status': result['status'], 'summary': result['summary'], 'run_id': saved.get('id'),
                'jobs_found': len(result.get('jobs', [])), 'notifications': delivery}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run_search(args.scheduled, not args.no_notify)))
    except RuntimeError as exc:
        print(json.dumps({'status': 'skipped', 'summary': str(exc)}))
