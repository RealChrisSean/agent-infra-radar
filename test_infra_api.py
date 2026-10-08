import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import server
import infra_runner


class InfraAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'RADAR_DATA': self.temp.name})
        self.env.start()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.httpd.server_port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.temp.cleanup()

    def request(self, path, data=None, method='GET', headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                                         method=method, headers=headers or {})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.code, response.read()

    def test_settings_and_job_state_persist_in_a_fresh_directory(self):
        status, body = self.request('/api/infra/dashboard')
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state['settings']['min_salary'], 150000)
        self.assertEqual(state['settings']['locations'], ['San Francisco'])
        self.assertEqual(state['schedule']['status'], 'not_configured')
        headers = {'X-Radar-Client': '1', 'Content-Type': 'application/json'}
        status, _ = self.request('/api/infra/settings', {'min_salary': 180000}, 'PATCH', headers)
        self.assertEqual(status, 200)
        self.assertEqual(infra_runner.get_store().get_settings()['min_salary'], 180000)
        self.assertFalse((Path(self.temp.name) / 'catalog.json').exists())
        self.assertFalse((Path(self.temp.name) / 'preferences.sqlite').exists())
        self.assertTrue((Path(self.temp.name) / 'agent-infra' / 'state.sqlite').exists())

    def test_standalone_page_and_assets_load_without_runtime_state(self):
        for path, content_type in (('/', 'text/html'), ('/infra.css', 'text/css'),
                                   ('/infra.js', 'text/javascript')):
            with self.subTest(path=path), urllib.request.urlopen(self.base + path) as response:
                self.assertEqual(response.code, 200)
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                self.assertTrue(response.read())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_legacy_routes_and_private_files_are_not_served(self):
        for path in ('/devrel', '/index.html', '/app.css', '/app.js', '/logic.js',
                     '/api/dashboard', '/api/health', '/data/catalog.json',
                     '/data/agent-infra/state.sqlite', '/../infra_store.py'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)
        headers = {'X-Radar-Client': '1', 'Content-Type': 'application/json'}
        for path in ('/api/jobs/example', '/api/applications/example'):
            with self.subTest(path=path):
                status, _ = self.request(path, {'saved': True}, 'PATCH', headers)
                self.assertEqual(status, 404)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_mutation_requires_same_origin_and_client_header(self):
        status, _ = self.request('/api/infra/settings', {'min_salary': 10}, 'PATCH', {'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        status, _ = self.request('/api/infra/settings', {'min_salary': 10}, 'PATCH', {
            'Content-Type': 'application/json', 'X-Radar-Client': '1', 'Origin': 'https://unrelated.example'})
        self.assertEqual(status, 403)

    def test_preview_does_not_send(self):
        with patch.object(server, 'dispatch_pending') as send:
            status, body = self.request('/api/infra/preview', {}, 'POST', {'Content-Type': 'application/json', 'X-Radar-Client': '1'})
            self.assertEqual(status, 200)
            self.assertIn('subject', json.loads(body))
            send.assert_not_called()


class RunnerTests(unittest.TestCase):
    def test_scheduled_run_saves_results_and_runs_once_per_day(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RADAR_DATA': directory}):
            result = {'jobs': [], 'status': 'completed', 'summary': 'No matches.', 'coverage': []}
            with patch.object(infra_runner, 'scan', return_value=result) as scan, patch.object(infra_runner, 'dispatch_pending', return_value={'sent_now': 0}):
                self.assertEqual(infra_runner.run_search(scheduled=True)['status'], 'completed')
                self.assertEqual(infra_runner.run_search(scheduled=True)['status'], 'skipped')
                self.assertEqual(scan.call_count, 1)

    def test_paused_schedule_does_not_search(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RADAR_DATA': directory}):
            infra_runner.get_store().update_settings({'schedule_enabled': False})
            with patch.object(infra_runner, 'scan') as scan:
                self.assertEqual(infra_runner.run_search(scheduled=True)['status'], 'skipped')
                scan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
