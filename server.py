"""Dependency-free Agent Infra Radar dashboard.

The local server binds to loopback by default. When deployed on a Sprite, its
private proxy must supply access control before binding to all interfaces.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from infra_runner import get_store as infra_store, run_search, is_running, schedule_status
from infra_notify import status as notification_status, preview as email_preview, dispatch_pending

_search_start_lock = threading.Lock()

def run_background_search():
    try:
        run_search()
    except Exception:
        pass  # Run errors are persisted by the runner; do not expose internals.

ROOT = Path(__file__).resolve().parent

class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body, kind='application/json; charset=utf-8'):
        raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == '/api/infra/dashboard':
                store = infra_store()
                return self.respond(200, {'settings': store.get_settings(), 'jobs': store.list_jobs(),
                    'runs': store.list_runs(), 'notifications': notification_status(store),
                    'scan': {'running': is_running()}, 'schedule': schedule_status(store)})
            if path == '/api/infra/settings':
                return self.respond(200, infra_store().get_settings())
            if path == '/api/infra/health':
                return self.respond(200, {'ok': True, 'search_running': is_running()})
            routes = {'/': ('infra.html', 'text/html'),
                      '/infra.css': ('infra.css', 'text/css'), '/infra.js': ('infra.js', 'text/javascript')}
            if path in routes:
                name, kind = routes[path]
                return self.respond(200, (ROOT / 'public' / name).read_bytes(), kind + '; charset=utf-8')
            self.respond(404, {'error': 'Not found'})
        except Exception:
            self.respond(503, {'error': 'The role list is temporarily unavailable. Please try again.'})

    def do_PATCH(self):
        origin = self.headers.get('Origin')
        if (origin and urlsplit(origin).netloc != self.headers.get('Host')) or self.headers.get('X-Radar-Client') != '1':
            return self.respond(403, {'error': 'Request rejected'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            path = urlsplit(self.path).path
            limit = 65536 if path == '/api/infra/settings' else 16384
            if size <= 0 or size > limit:
                raise ValueError('Invalid request size')
            if not path.startswith('/api/infra/'):
                return self.respond(404, {'error': 'Not found'})
            changes = json.loads(self.rfile.read(size))
            if not isinstance(changes, dict):
                raise ValueError('JSON object required')
            if path == '/api/infra/settings':
                store = infra_store()
                settings = store.update_settings(changes)
                store.reclassify_jobs()
                store.enqueue_alerts()
                return self.respond(200, {'settings': settings})
            if path.startswith('/api/infra/jobs/'):
                job = infra_store().update_job(path.removeprefix('/api/infra/jobs/'), changes)
                return self.respond(200, {'ok': True, 'job': job})
            return self.respond(404, {'error': 'Not found'})
        except KeyError:
            self.respond(404, {'error': 'Unknown role'})
        except (ValueError, TypeError):
            self.respond(400, {'error': 'Invalid update'})
        except Exception:
            self.respond(503, {'error': 'Could not save. Please try again.'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if (origin and urlsplit(origin).netloc != self.headers.get('Host')) or self.headers.get('X-Radar-Client') != '1':
            return self.respond(403, {'error': 'Request rejected'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                raise ValueError('Invalid request size')
            if not isinstance(json.loads(self.rfile.read(size)), dict):
                raise ValueError('JSON object required')
            path = urlsplit(self.path).path
            if path == '/api/infra/scan':
                with _search_start_lock:
                    if is_running():
                        return self.respond(409, {'error': 'A search is already running.'})
                    threading.Thread(target=run_background_search, daemon=True).start()
                return self.respond(202, {'ok': True, 'message': 'Search started.'})
            if path == '/api/infra/preview':
                return self.respond(200, email_preview(infra_store()))
            if path == '/api/infra/notify':
                return self.respond(200, dispatch_pending(infra_store()))
            if path == '/api/infra/connector':
                return self.respond(200, notification_status(infra_store(), discover=True))
            return self.respond(404, {'error': 'Not found'})
        except (ValueError, TypeError):
            self.respond(400, {'error': 'Invalid request'})
        except Exception:
            self.respond(503, {'error': 'The operation could not complete. Please try again.'})

if __name__ == '__main__':
    ThreadingHTTPServer((os.environ.get('HOST', '127.0.0.1'), int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
