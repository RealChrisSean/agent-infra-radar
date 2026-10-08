from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from infra_store import Store
from infra_notify import DASHBOARD_URL, dashboard_url, discover_connector, dispatch_pending, preview, status


class DashboardURLTests(unittest.TestCase):
    def test_dashboard_links_use_the_deployment_environment(self):
        with patch.dict(os.environ, {'RADAR_PUBLIC_URL': 'https://radar.example.com/'}):
            self.assertEqual(dashboard_url(), 'https://radar.example.com/#jobs')

    def test_local_development_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dashboard_url(), 'http://localhost:8080/#jobs')

    def test_rejects_credentials_and_non_web_links(self):
        for value in ('javascript:alert(1)', 'https://user:password@example.com',
                      'https://example.com?secret=value', 'https://example.com/#fragment',
                      'https://example.com/\r\ninvalid'):
            with self.subTest(value=value), patch.dict(os.environ, {'RADAR_PUBLIC_URL': value}):
                with self.assertRaises(ValueError):
                    dashboard_url()


class Response:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode()
    def read(self, size):
        return self.data[:size]
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'infra.sqlite')
        self.job = {'id': 'role-1', 'title': 'Agent <script>alert(1)</script> Engineer', 'company': 'Acme & Co',
                    'url': 'https://example.com/job?x=1&y=2', 'location': 'San Francisco',
                    'salary_min': 180000, 'salary_max': 220000, 'salary_currency': 'USD',
                    'match_status': 'match', 'status': 'open', 'reasons': ['Build agent runtime infrastructure'],
                    'caveats': ['Check C++ experience'], 'first_seen': 'first'}
        self.store.save_jobs([self.job])
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def configure(self):
        self.store.update_settings({'email_to': 'me@example.com', 'email_from': 'Radar <jobs@example.com>',
                                    'connector_base': 'https://api.sprites.dev/v1/gateway/resend/conn_123'})

    def success(self, request, timeout):
        self.calls.append(request)
        return Response({'id': 'email-1'})

    @staticmethod
    def identity(job, settings):
        return job

    def dispatch(self, opener=None, classifier=None):
        return dispatch_pending(self.store, opener=opener or self.success, classifier=classifier or self.identity, discover=False)

    def test_preview_escapes_untrusted_job_fields_and_does_not_send(self):
        result = preview(self.store)
        self.assertIn('&lt;script&gt;', result['html'])
        self.assertNotIn('<script>', result['html'])
        self.assertIn('Acme &amp; Co', result['html'])
        self.assertIn('x=1&amp;y=2', result['html'])
        self.assertIn('Personal fit has not been assessed', result['text'])
        self.assertEqual(self.calls, [])
        self.assertEqual(result['job_count'], 1)

    def test_status_current_recipient_queue_is_not_doubled_after_setup(self):
        self.assertEqual(status(self.store)['pending'], 1)
        self.store.update_settings({'email_to': 'me@example.com'})
        self.assertEqual(status(self.store)['pending'], 1)
        self.assertEqual(self.store.notification_counts()['pending'], 1)

    def test_status_stricter_settings_reduce_unconfigured_queue(self):
        self.assertEqual(status(self.store)['pending'], 1)
        self.store.update_settings({'min_salary': 500000})
        self.store.reclassify_jobs(lambda job, settings: {**job, 'match_status': 'excluded'})
        self.assertEqual(status(self.store)['pending'], 0)
        self.assertFalse(status(self.store)['configured'])

    def test_missing_configuration_stays_pending_without_provider_call(self):
        result = self.dispatch()
        self.assertFalse(result['configured'])
        self.assertEqual(self.store.notification_counts()['pending'], 1)
        self.assertEqual(self.calls, [])

    def test_delivery_and_permanent_deduplication_across_restart(self):
        self.configure()
        self.assertEqual(self.dispatch()['sent_now'], 1)
        self.assertEqual(self.dispatch()['sent_now'], 0)
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0]
        self.assertEqual(request.full_url, 'https://api.sprites.dev/v1/gateway/resend/conn_123/emails')
        self.assertNotIn('Authorization', request.headers)
        self.assertTrue(request.get_header('Idempotency-key').startswith('infra-radar-'))
        email = json.loads(request.data)
        self.assertEqual(email['to'], ['me@example.com'])
        self.assertNotIn('job_count', email)
        self.store = Store(self.store.path)
        self.assertEqual(self.dispatch()['sent_now'], 0)

    def test_many_jobs_send_one_digest_and_repeat_sends_nothing(self):
        self.configure()
        self.store.save_jobs([{**self.job, 'id': f'role-{n}', 'title': f'Agent Engineer {n:03d}',
                               'url': f'https://example.com/jobs/{n}'} for n in range(144)])
        result = self.dispatch()
        self.assertEqual(result['sent_now'], 144)
        self.assertEqual(result['emails_sent_now'], 1)
        self.assertEqual(result['highlighted_now'], 10)
        self.assertEqual(result['remaining_now'], 134)
        self.assertEqual(result['emails_sent'], 1)
        self.assertEqual(len(self.calls), 1)
        email = json.loads(self.calls[0].data)
        self.assertIn('top 10', email['subject'])
        self.assertEqual(email['html'].count('<article '), 10)
        self.assertIn('View all 144 matches', email['html'])
        self.assertIn('134 more matching roles', email['text'])
        self.assertIn(DASHBOARD_URL, email['text'])
        self.assertNotIn('Agent Engineer 143', email['html'])
        self.assertNotIn('Agent Engineer 143', email['text'])
        self.assertNotIn('https://example.com/jobs/143', email['text'])
        self.assertEqual(set(email), {'from', 'to', 'subject', 'html', 'text'})
        self.assertLess(len(email['html']), 100000)
        self.assertEqual(len(self.store.list_deliveries()), 1)
        self.assertEqual(self.dispatch()['emails_sent_now'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.notification_counts()['sent'], 144)

    def test_preview_ranks_profile_overlap_before_unrelated_earlier_matches(self):
        self.store.update_settings({'profile_text': 'Python and Kubernetes infrastructure',
                                    'include_keywords': ['agent', 'infrastructure']})
        earlier = [{**self.job, 'id': f'generic-{n}', 'title': f'Generic Engineer {n}',
                    'description': 'Build inference infrastructure in C++.'} for n in range(11)]
        best = {**self.job, 'id': 'personal-best', 'title': 'Agent Runtime Engineer',
                'description': 'Build agent runtimes in Python using Kubernetes.'}
        result = preview(self.store, earlier + [best])
        self.assertEqual(result['shown_count'], 10)
        self.assertEqual(result['more_count'], 2)
        self.assertLess(result['text'].index('Agent Runtime Engineer —'), result['text'].index('Generic Engineer'))
        self.assertIn('Shared skill terms with your profile: kubernetes, python.', result['text'])
        self.assertNotIn('Generic Engineer 9 —', result['text'])
        self.assertIn('View all 12 matches', result['text'])

    def test_without_profile_direct_agent_work_ranks_above_broader_inference(self):
        generic = {**self.job, 'title': 'Inference Engineer', 'description': 'Build inference systems.'}
        direct = {**self.job, 'id': 'direct', 'title': 'Agent Runtime Engineer', 'description': 'Build execution systems.'}
        result = preview(self.store, [generic, direct])
        self.assertLess(result['text'].index('Agent Runtime Engineer —'), result['text'].index('Inference Engineer —'))
        self.assertIn('Add your experience in Settings to personalize the ranking.', result['text'])
        self.assertNotIn(DASHBOARD_URL, result['text'])

    def test_ten_or_fewer_jobs_do_not_show_overflow_link(self):
        jobs = [{**self.job, 'id': str(n), 'title': f'Engineer {n}'} for n in range(10)]
        result = preview(self.store, jobs)
        self.assertEqual(result['shown_count'], 10)
        self.assertEqual(result['more_count'], 0)
        self.assertNotIn('more matching roles', result['text'])
        self.assertNotIn(DASHBOARD_URL, result['html'])

    def test_ranking_does_not_depend_on_insertion_order(self):
        jobs = [{**self.job, 'id': str(n), 'title': f'Engineer {n:02d}'} for n in range(12)]
        self.assertEqual(preview(self.store, jobs), preview(self.store, list(reversed(jobs))))

    def test_ambiguous_digest_retry_keeps_exact_bytes_after_job_and_setting_edits(self):
        self.configure()
        self.store.save_jobs([{**self.job, 'id': 'role-2'}])
        def timeout(request, timeout):
            self.calls.append(request)
            raise TimeoutError()
        self.dispatch(timeout)
        original = self.calls[0]
        self.store = Store(self.store.path)
        self.store.save_jobs([{**self.job, 'title': 'Changed role title', 'reasons': ['Changed explanation']}])
        self.store.update_settings({'focus': 'Updated infrastructure focus', 'profile_text': 'Rust and Kubernetes'})
        result = self.dispatch()
        self.assertEqual(result['sent_now'], 2)
        self.assertEqual(self.calls[1].data, original.data)
        self.assertEqual(self.calls[1].get_header('Idempotency-key'), original.get_header('Idempotency-key'))
        self.assertNotIn('Changed role title', self.calls[1].data.decode())

    def test_ambiguous_digest_past_idempotency_window_never_retries(self):
        self.configure()
        def timeout(request, timeout):
            self.calls.append(request)
            raise TimeoutError()
        self.dispatch(timeout)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.store.connection() as db:
            db.execute('UPDATE infra_deliveries SET attempted_at=?', (old,))
        result = self.dispatch()
        self.assertEqual(result['emails_sent_now'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.list_deliveries()[0]['status'], 'uncertain')

    def test_crashed_worker_recovers_same_persisted_digest(self):
        self.configure()
        def timeout(request, timeout):
            self.calls.append(request)
            raise TimeoutError()
        self.dispatch(timeout)
        original = self.calls[0]
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE infra_deliveries SET status='sending',updated_at=?", (old,))
        self.store = Store(self.store.path)
        self.assertEqual(self.dispatch()['emails_sent_now'], 1)
        self.assertEqual(self.calls[1].data, original.data)
        self.assertEqual(self.calls[1].get_header('Idempotency-key'), original.get_header('Idempotency-key'))

    def test_new_jobs_dont_change_existing_retry_digest(self):
        self.configure()
        def timeout(request, timeout):
            self.calls.append(request)
            raise TimeoutError()
        self.dispatch(timeout)
        original = self.calls[0].data
        self.store.save_jobs([{**self.job, 'id': 'role-new', 'title': 'Brand new role'}])
        self.assertEqual(self.dispatch()['sent_now'], 1)
        self.assertEqual(self.calls[1].data, original)
        self.assertEqual(self.dispatch()['sent_now'], 1)
        self.assertIn('Brand new role', self.calls[2].data.decode())

    def test_uncertain_digest_does_not_resend_if_member_is_now_hidden(self):
        self.configure()
        def timeout(request, timeout):
            self.calls.append(request)
            raise TimeoutError()
        self.dispatch(timeout)
        self.store.update_job('role-1', {'hidden': True})
        result = self.dispatch()
        self.assertEqual(result['emails_sent_now'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.list_deliveries()[0]['status'], 'uncertain')
        self.store.update_job('role-1', {'hidden': False})
        self.assertEqual(self.dispatch()['emails_sent_now'], 1)
        self.assertEqual(self.calls[1].data, self.calls[0].data)

    def test_parallel_dispatch_does_not_send_duplicate_digest(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.configure()
        self.store.save_jobs([{**self.job, 'id': 'role-2'}])
        started, finish = Event(), Event()
        def blocking(request, timeout):
            self.calls.append(request)
            started.set()
            self.assertTrue(finish.wait(5))
            return Response({'id': 'one-email'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.dispatch, blocking)
            self.assertTrue(started.wait(5))
            second = pool.submit(self.dispatch)
            try:
                self.assertEqual(second.result(5)['emails_sent_now'], 0)
            finally:
                finish.set()
            self.assertEqual(first.result(5)['sent_now'], 2)
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_timeout_reuses_same_key_within_window(self):
        self.configure()
        def timeout(request, timeout):
            self.calls.append(request)
            raise TimeoutError()
        self.dispatch(timeout)
        self.assertEqual(self.store.notification_counts()['uncertain'], 1)
        first_key = self.calls[0].get_header('Idempotency-key')
        self.assertEqual(self.dispatch()['sent_now'], 1)
        self.assertEqual(self.calls[1].get_header('Idempotency-key'), first_key)

    def test_old_uncertain_delivery_never_retries(self):
        self.configure()
        self.store.enqueue_alerts()
        alert = self.store.list_alerts()[0]
        self.store.update_alert(alert['id'], 'uncertain')
        before = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.store.connection() as db:
            db.execute('UPDATE infra_alerts SET attempted_at=? WHERE id=?', (before, alert['id']))
        result = self.dispatch()
        self.assertEqual(self.calls, [])
        self.assertEqual(result['uncertain'], 1)

    def test_changed_classification_is_checked_before_send(self):
        self.configure()
        self.store.enqueue_alerts()
        def excludes(job, settings):
            return {**job, 'match_status': 'excluded'}
        self.dispatch(classifier=excludes)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.notification_counts()['cancelled'], 1)

    def test_hidden_applied_or_paused_jobs_do_not_send(self):
        self.configure()
        self.store.enqueue_alerts()
        self.store.update_job('role-1', {'applied': True})
        self.dispatch()
        self.assertEqual(self.calls, [])
        self.store.update_job('role-1', {'applied': False})
        self.store.update_settings({'email_enabled': False})
        self.dispatch()
        self.assertEqual(self.calls, [])

    def test_known_failure_can_retry_and_does_not_store_sensitive_error_body(self):
        self.configure()
        def reject(request, timeout):
            raise HTTPError(request.full_url, 422, 'secret-provider-key', {}, None)
        self.dispatch(reject)
        alert = self.store.list_alerts()[0]
        self.assertEqual(alert['status'], 'pending')
        self.assertNotIn('secret-provider-key', alert['error'])
        self.assertEqual(self.dispatch()['sent_now'], 1)

    def test_recipient_change_cancels_old_queue_and_sends_new_address(self):
        self.store.enqueue_alerts()  # Blank recipient while setup is pending.
        self.configure()
        self.dispatch()
        self.assertEqual(self.store.notification_counts()['cancelled'], 1)
        self.assertEqual(self.store.notification_counts()['sent'], 1)

    def test_new_scan_during_dispatch_stops_remaining_batch(self):
        self.configure()
        current = datetime.now(timezone.utc).isoformat()
        self.store.save_run({'status': 'completed', 'jobs': [{**self.job, 'verified_at': current}, {**self.job, 'id': 'role-2', 'verified_at': current}]})
        def scan_changes(request, timeout):
            self.calls.append(request)
            self.store.save_run({'status': 'failed', 'jobs': []})
            return Response({'id': 'email-1'})
        result = self.dispatch(scan_changes)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('search results changed', result['message'])

    def test_recipient_change_during_dispatch_stops_remaining_batch(self):
        self.configure()
        self.store.save_jobs([{**self.job, 'id': 'role-2'}])
        def changed_settings(request, timeout):
            self.calls.append(request)
            self.store.update_settings({'email_to': 'new@example.com'})
            return Response({'id': 'email-1'})
        result = self.dispatch(changed_settings)
        self.assertEqual(result['sent_now'], 2)
        self.assertEqual(result['emails_sent_now'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.calls[0].data)['to'], ['me@example.com'])
        self.assertIn('Settings or search results changed', result['message'])

    def test_omitted_job_in_partial_scan_stays_pending(self):
        self.configure()
        self.store.save_jobs([{**self.job, 'verified_at': datetime.now(timezone.utc).isoformat()}])
        self.store.enqueue_alerts()
        self.store.save_run({'status': 'partial', 'jobs': [], 'coverage': [{'status': 'failed'}]})
        result = self.dispatch()
        self.assertEqual(self.calls, [])
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(self.store.list_alerts()[0]['status'], 'pending')
        self.assertEqual(self.store.get_job('role-1')['status'], 'open')

    def test_old_verified_job_is_deferred_until_fresh_scan(self):
        self.configure()
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.store.save_run({'status': 'completed', 'jobs': [{**self.job, 'verified_at': old}]})
        self.assertEqual(self.dispatch()['deferred'], 1)
        self.assertEqual(self.calls, [])
        self.store.save_run({'status': 'completed', 'jobs': [{**self.job, 'verified_at': datetime.now(timezone.utc).isoformat()}]})
        self.assertEqual(self.dispatch()['sent_now'], 1)

    def test_latest_failed_run_defers_even_recent_previous_match(self):
        self.configure()
        self.store.save_run({'status': 'completed', 'jobs': [{**self.job, 'verified_at': datetime.now(timezone.utc).isoformat()}]})
        self.store.save_run({'status': 'failed', 'jobs': []})
        result = self.dispatch()
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(self.calls, [])

    def test_verified_job_observed_in_partial_run_can_send(self):
        self.configure()
        self.store.save_run({'status': 'partial', 'jobs': [{**self.job, 'verified_at': datetime.now(timezone.utc).isoformat()}]})
        self.assertEqual(self.dispatch()['sent_now'], 1)

    def test_connector_discovery_accepts_only_resend_gateway_metadata(self):
        def opener(request, timeout):
            self.assertEqual(request.full_url, 'https://api.sprites.dev/v1/gateway/list')
            return Response({'available': [{'provider': 'resend'}], 'connections': [{'provider': 'openai', 'gateway_base_url': 'https://api.sprites.dev/v1/gateway/openai/conn_other'},
                                         {'provider': 'resend', 'gateway_base_url': 'https://api.sprites.dev/v1/gateway/resend/conn_123'}]})
        found = discover_connector(opener)
        self.assertTrue(found['ready'])
        self.assertEqual(found['base_url'], 'https://api.sprites.dev/v1/gateway/resend/conn_123')
        self.assertFalse(discover_connector(lambda *a, **k: Response({'provider': 'resend', 'url': 'https://api.resend.com'}))['ready'])

    def test_discovery_returns_safe_fixed_message_on_failure(self):
        def bad(request, timeout):
            raise HTTPError(request.full_url, 401, 'SECRET', {}, None)
        found = discover_connector(bad)
        self.assertFalse(found['ready'])
        self.assertNotIn('SECRET', str(found))

    def test_no_javascript_application_links(self):
        self.job['url'] = 'javascript:alert(1)'
        self.assertNotIn('href=', preview(self.store, [self.job])['html'])


if __name__ == '__main__':
    unittest.main()
