import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infra_store import Store, valid_gateway_url


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'infra.sqlite'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **patch):
        return {'id': 'role-1', 'title': 'Agent Infrastructure Engineer', 'company': 'Acme',
                'url': 'https://example.com/jobs/1', 'match_status': 'match',
                'status': 'open', 'reasons': ['Base salary clears the minimum.'], **patch}

    def test_defaults_and_persistence(self):
        settings = self.store.get_settings()
        self.assertEqual(settings['min_salary'], 150000)
        self.assertEqual(settings['salary_rule'], 'minimum')
        self.assertEqual(settings['profile_text'], '')
        self.assertEqual(settings['timezone'], 'America/Los_Angeles')
        self.store.update_settings({'min_salary': 180000, 'email_to': 'me@example.com'})
        self.assertEqual(Store(self.path).get_settings()['min_salary'], 180000)

    def test_settings_reject_unknown_and_invalid_fields(self):
        for patch in [{'api_key': 'secret'}, {'min_salary': True}, {'min_salary': -1},
                      {'schedule_hour': 24}, {'salary_rule': 'wishful'},
                      {'timezone': 'Mars/Olympus'}, {'email_to': 'a@example.com\nBcc:b@example.com'},
                      {'sources': [{'provider': 'custom', 'slug': 'x', 'company': 'X'}]},
                      {'sources': [{'provider': 'ashby', 'slug': '../oops', 'company': 'X'}]},
                      {'include_keywords': ['']}, {'remote_allowed': 'yes'},
                      {'email_to': 'first@example.com;second.com'}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.store.update_settings(patch)

    def test_gateway_rejects_provider_credentials_and_ssrf(self):
        self.assertTrue(valid_gateway_url('https://api.sprites.dev/v1/gateway/resend/conn_123'))
        for url in ['https://api.resend.com', 'http://api.sprites.dev/v1/gateway/resend',
                    'https://evil.example/v1/gateway/resend',
                    'https://api.sprites.dev@evil.example/v1/gateway/resend',
                    'https://api.sprites.dev:444/v1/gateway/resend',
                    'https://api.sprites.dev/v1/gateway/resend/conn_123?key=secret',
                    'https://api.sprites.dev/v1/gateway/../admin',
                    'https://api.sprites.dev/v1/gateway/list']:
            with self.subTest(url=url):
                self.assertFalse(valid_gateway_url(url))
                with self.assertRaises(ValueError):
                    self.store.update_settings({'connector_base': url})

    def test_source_settings_are_validated(self):
        source = {'provider': 'ashby', 'slug': 'acme', 'company': 'Acme', 'enabled': True}
        self.store.update_settings({'sources': [source]})
        with self.assertRaises(ValueError):
            self.store.update_settings({'sources': [source, source]})

    def test_refresh_preserves_flags_first_seen_and_omitted_roles(self):
        self.store.save_jobs([self.job(first_seen='2026-01-01', last_seen='2026-01-02')])
        self.store.update_job('role-1', {'saved': True, 'applied': True})
        self.store.save_jobs([self.job(first_seen='2026-02-01', last_seen='2026-02-02', applied=False)])
        self.store.save_run({'status': 'partial', 'coverage': [{'status': 'failed'}], 'jobs': []})
        job = self.store.get_job('role-1')
        self.assertTrue(job['saved'])
        self.assertTrue(job['applied'])
        self.assertEqual(job['first_seen'], '2026-01-01')
        self.assertEqual(job['last_seen'], '2026-02-02')
        self.assertEqual(job['status'], 'open')
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(self.store.list_runs()[0]['status'], 'partial')
        self.assertEqual(self.store.list_runs()[0]['job_ids'], [])

    def test_run_records_only_observed_job_ids(self):
        self.store.save_run({'status': 'completed', 'jobs': [self.job(), self.job(id='role-2')], 'job_ids': ['invented']})
        self.assertEqual(self.store.list_runs()[0]['job_ids'], ['role-1', 'role-2'])

    def test_flags_only_are_mutable_by_user(self):
        self.store.save_jobs([self.job()])
        with self.assertRaises(ValueError):
            self.store.update_job('role-1', {'match_status': 'match'})
        with self.assertRaises(KeyError):
            self.store.update_job('unknown', {'saved': True})

    def test_reclassification_preserves_observed_timestamps_and_flags(self):
        self.store.save_jobs([self.job(first_seen='first', last_seen='last')])
        self.store.update_job('role-1', {'saved': True})
        self.store.update_settings({'min_salary': 250000})
        def classify(job, settings):
            return {**job, 'match_status': 'excluded', 'last_seen': 'now', 'saved': False}
        self.store.reclassify_jobs(classify)
        job = self.store.get_job('role-1')
        self.assertEqual(job['match_status'], 'excluded')
        self.assertEqual(job['last_seen'], 'last')
        self.assertTrue(job['saved'])

    def test_outbox_persists_and_deduplicates_each_recipient(self):
        self.store.save_jobs([self.job()])
        self.assertEqual(self.store.enqueue_alerts(), 1)
        self.assertEqual(Store(self.path).enqueue_alerts(), 0)
        self.store.update_settings({'email_to': 'me@example.com'})
        self.assertEqual(self.store.enqueue_alerts(), 1)
        alerts = self.store.list_alerts()
        self.assertEqual(len(alerts), 2)
        actual = next(a for a in alerts if a['recipient'])
        self.store.update_alert(actual['id'], 'sent', provider_id='sent-1')
        self.assertEqual(self.store.enqueue_alerts(), 0)
        self.assertEqual(self.store.notification_counts()['sent'], 1)

    def test_recipient_setup_reconciles_blank_queue_without_double_count(self):
        self.store.save_jobs([self.job()])
        self.store.enqueue_alerts()
        self.assertEqual(self.store.notification_counts()['pending'], 1)
        self.store.update_settings({'email_to': 'me@example.com'})
        self.store.enqueue_alerts()
        self.assertEqual(self.store.notification_counts()['pending'], 1)
        self.assertEqual(self.store.notification_counts('me@example.com')['pending'], 1)
        self.assertEqual(self.store.notification_counts()['cancelled'], 1)

    def test_reclassified_unsent_queue_reconciles_without_connector(self):
        self.store.save_jobs([self.job(id='keep'), self.job(id='remove')])
        self.store.enqueue_alerts()
        self.store.update_settings({'min_salary': 250000})
        self.store.reclassify_jobs(lambda job, settings: {**job, 'match_status': 'match' if job['id'] == 'keep' else 'excluded'})
        self.store.enqueue_alerts()
        self.assertEqual(self.store.notification_counts()['pending'], 1)
        self.assertEqual(self.store.notification_counts()['cancelled'], 1)

    def test_reconciliation_does_not_cancel_sending_or_uncertain(self):
        self.store.save_jobs([self.job(id='sending'), self.job(id='uncertain')])
        self.store.enqueue_alerts()
        for alert in self.store.list_alerts():
            self.store.update_alert(alert['id'], alert['job_id'])
        self.store.update_settings({'email_to': 'new@example.com'})
        self.store.reclassify_jobs(lambda job, settings: {**job, 'match_status': 'excluded'})
        self.store.enqueue_alerts()
        counts = self.store.notification_counts()
        self.assertEqual(counts['sending'], 1)
        self.assertEqual(counts['uncertain'], 1)
        self.assertNotIn('cancelled', counts)

    def test_ineligible_roles_never_queue(self):
        self.store.save_jobs([self.job(id='closed', status='closed'), self.job(id='review', match_status='review'), self.job(id='hidden'), self.job(id='applied')])
        self.store.update_job('hidden', {'hidden': True})
        self.store.update_job('applied', {'applied': True})
        self.assertEqual(self.store.enqueue_alerts(), 0)

    def test_delivery_reservation_is_atomic_and_payload_is_persisted(self):
        self.store.update_settings({'email_to': 'me@example.com'})
        self.store.save_jobs([self.job()])
        self.store.enqueue_alerts()
        ids = [a['id'] for a in self.store.list_alerts()]
        email = {'to': ['me@example.com'], 'from': 'jobs@example.com', 'subject': 'One', 'text': 'Original'}
        delivery = self.store.reserve_delivery(ids, email, 'https://api.sprites.dev/v1/gateway/resend/conn_123/emails', self.store.settings_revision(), None)
        self.assertIsNotNone(delivery)
        email['text'] = 'Edited after reservation'
        persisted = Store(self.path).list_deliveries()[0]
        self.assertIn('Original', persisted['payload'])
        self.assertNotIn('Edited after reservation', persisted['payload'])
        self.assertIsNone(self.store.reserve_delivery(ids, email, delivery['endpoint'], self.store.settings_revision(), None))
        self.assertTrue(self.store.claim_delivery(delivery['id']))
        self.assertFalse(self.store.claim_delivery(delivery['id']))

    def test_claim_rejects_changed_settings_or_hidden_member(self):
        self.store.update_settings({'email_to': 'me@example.com'})
        self.store.save_jobs([self.job()])
        self.store.enqueue_alerts()
        ids = [a['id'] for a in self.store.list_alerts()]
        revision = self.store.settings_revision()
        email = {'to': ['me@example.com'], 'from': 'jobs@example.com', 'subject': 'One', 'text': 'Original'}
        delivery = self.store.reserve_delivery(ids, email, 'https://api.sprites.dev/v1/gateway/resend/conn_123/emails', revision, None)
        self.store.update_settings({'min_salary': 200000})
        self.assertFalse(self.store.claim_delivery(delivery['id'], revision, None))
        self.store.update_job('role-1', {'hidden': True})
        self.assertFalse(self.store.claim_delivery(delivery['id']))

    def test_legacy_tables_are_not_modified(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE preferences (id TEXT PRIMARY KEY, saved INTEGER)')
            db.execute("INSERT INTO preferences VALUES ('legacy',1)")
        self.store.save_jobs([self.job()])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT * FROM preferences').fetchall(), [('legacy', 1)])


if __name__ == '__main__':
    unittest.main()
