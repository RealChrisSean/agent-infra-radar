"""Resend notifications through the Sprite connector; no provider keys are stored."""
from datetime import datetime, timezone
from html import escape
import json
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from infra_store import now_iso, valid_email, valid_gateway_url
from infra_scan import CORE_EXECUTION, DIRECT_AGENT, SKILLS, keyword_matches, role_scope

GATEWAY_LIST = 'https://api.sprites.dev/v1/gateway/list'
REQUEST_TIMEOUT = 20
# Resend idempotency keys expire after 24 hours. Never retry ambiguity past this window.
SAFE_RETRY_SECONDS = 23 * 60 * 60
MAX_VERIFICATION_AGE = 36 * 60 * 60
DIGEST_LIMIT = 10
def dashboard_url():
    """Resolve the deployment URL without embedding any user's Sprite address."""
    base = os.environ.get('RADAR_PUBLIC_URL', 'http://localhost:8080').strip()
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(char in base for char in '\r\n')):
        raise ValueError('RADAR_PUBLIC_URL must be an HTTP(S) app URL without credentials, query, or fragment')
    return base.rstrip('/') + '/#jobs'


DASHBOARD_URL = dashboard_url()


def shared_skills(job, settings):
    """Literal technical skill overlap, not an assessment of proficiency."""
    profile = settings.get('profile_text', '')
    evidence = str(job.get('title', '')) + '\n' + str(job.get('description', ''))
    return sorted(skill for skill in SKILLS
                  if keyword_matches([skill], profile) and keyword_matches([skill], evidence))


def rank_jobs(jobs, settings):
    """Prioritize profile overlap, direct agent work, then saved search terms."""
    def priority(job):
        title = str(job.get('title', ''))
        duties = role_scope(str(job.get('description', '')))
        directness = (3 if DIRECT_AGENT.search(title) else
                      2 if CORE_EXECUTION.search(duties) else
                      1 if DIRECT_AGENT.search(duties) else 0)
        keywords = keyword_matches(settings.get('include_keywords', []), title + '\n' + duties)
        return (-len(shared_skills(job, settings)), -directness, -len(keywords),
                str(job.get('company', '')).casefold(), title.casefold(),
                str(job.get('id') or job.get('url', '')))
    return sorted(jobs, key=priority)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout=REQUEST_TIMEOUT):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def discover_connector(opener=None):
    """Return safe, public routing metadata for a connected Resend gateway."""
    opener = opener or _open
    try:
        request = urllib.request.Request(GATEWAY_LIST, headers={'Accept': 'application/json'})
        with opener(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read(262145)
        if len(body) > 262144:
            return {'ready': False, 'base_url': '', 'message': 'Connector discovery returned too much data.'}
        payload = json.loads(body)
    except urllib.error.HTTPError as exc:
        exc.close()
        return {'ready': False, 'base_url': '', 'message': 'Connect Resend to this Sprite to enable email delivery.'}
    except (OSError, ValueError, urllib.error.URLError):
        return {'ready': False, 'base_url': '', 'message': 'Connect Resend to this Sprite to enable email delivery.'}

    connections = payload.get('connections', []) if isinstance(payload, dict) else []
    candidates = list(dict.fromkeys(
        connection['gateway_base_url'].rstrip('/')
        for connection in connections
        if isinstance(connection, dict) and connection.get('provider') == 'resend'
        and valid_gateway_url(connection.get('gateway_base_url'))
    ))
    if len(candidates) == 1:
        return {'ready': True, 'base_url': candidates[0], 'message': 'Resend connector is ready.'}
    if len(candidates) > 1:
        return {'ready': False, 'base_url': '', 'message': 'Multiple Resend gateways found. Choose the connector URL in Settings.'}
    return {'ready': False, 'base_url': '', 'message': 'Resend is not connected to this Sprite yet.'}


def _salary(job):
    low, high = job.get('salary_min'), job.get('salary_max')
    currency = job.get('salary_currency') or 'USD'
    if low is None and high is None:
        return 'Salary not disclosed'
    values = [f'{currency} {value:,.0f}' for value in (low, high) if isinstance(value, (int, float))]
    return ' – '.join(dict.fromkeys(values)) + ' / year'


def preview(store, jobs=None):
    """Build an inspectable digest without contacting Resend."""
    settings = store.get_settings()
    if jobs is None:
        jobs = [job for job in store.list_jobs() if store.alert_eligible(job)]
    jobs = rank_jobs(jobs, settings)
    count, shown = len(jobs), min(len(jobs), DIGEST_LIMIT)
    remaining = count - shown
    subject = (f"New match: {jobs[0]['title']} at {jobs[0]['company']}" if count == 1
               else f'Your top {shown} agent infrastructure job matches' if count
               else 'No new agent infrastructure job matches')
    intro = f'Your {shown} best matches' + (f' from {count} matching roles.' if remaining else '.')
    ranking = ('Ranked by shared technical skill terms from your profile, then agent infrastructure relevance and your search keywords.'
               if settings.get('profile_text', '').strip() else
               'Ranked by agent infrastructure relevance and your search keywords. Add your experience in Settings to personalize the ranking.')
    text = [subject, '', intro, ranking]
    cards = []
    for job in jobs[:DIGEST_LIMIT]:
        title = str(job.get('title', 'Role'))[:240]
        company = str(job.get('company', 'Company'))[:120]
        location = str(job.get('location', 'Location not disclosed'))[:300]
        salary = _salary(job)
        reasons = [str(reason)[:500] for reason in job.get('reasons', [])[:4]]
        skills = shared_skills(job, settings)
        if skills:
            reasons.insert(0, 'Shared skill terms with your profile: ' + ', '.join(skills) + '.')
        caveats = [str(caveat)[:500] for caveat in job.get('caveats', [])[:3]]
        url = str(job.get('url', ''))
        # Only official HTTPS application links are rendered as links; no executable URLs.
        safe_link = url.startswith('https://') and not any(char in url for char in '\r\n')
        text.extend(['', f'{title} — {company}', f'{location} | {salary}', 'Why it matches:'])
        text.extend(f'- {reason}' for reason in reasons)
        if caveats:
            text.append('Check before applying:')
            text.extend(f'- {caveat}' for caveat in caveats)
        if safe_link:
            text.append('View role / apply: ' + url)
        reason_html = ''.join('<li>' + escape(reason) + '</li>' for reason in reasons)
        caveat_html = ('<h3>Check before applying</h3><ul>' + ''.join('<li>' + escape(caveat) + '</li>' for caveat in caveats) + '</ul>') if caveats else ''
        link_html = ('<p><a href="' + escape(url, quote=True) + '">View role / apply</a></p>') if safe_link else ''
        cards.append('<article style="border:1px solid #dce2e8;border-radius:12px;padding:24px;margin:20px 0">'
                     + '<h2>' + escape(title) + '</h2><p><strong>' + escape(company) + '</strong></p><p>'
                     + escape(location) + '<br>' + escape(salary) + '</p><h3>Why it matches</h3><ul>'
                     + reason_html + '</ul>' + caveat_html + link_html + '</article>')
    if remaining:
        text.extend(['', f'{remaining} more matching roles are waiting in Agent Infra Radar.',
                     f'View all {count} matches: {DASHBOARD_URL}'])
        cards.append('<p>' + str(remaining) + ' more matching roles are waiting in Agent Infra Radar.</p>'
                     + '<p><a href="' + escape(DASHBOARD_URL, quote=True) + '" style="display:inline-block;background:#4f46e5;color:white;padding:14px 20px;border-radius:8px;text-decoration:none">View all '
                     + str(count) + ' matches</a></p>')
    if not settings.get('profile_text', '').strip():
        text.extend(['', 'Personal fit has not been assessed: add your experience in Settings for context.'])
        profile_note = '<p>Personal fit has not been assessed. Add your experience in Settings for context.</p>'
    else:
        text.extend(['', 'This is a search match, not a guarantee of personal fit or interview eligibility.'])
        profile_note = '<p>This is a search match. Compare each role with your experience before applying.</p>'
    html = '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#152233;max-width:640px;margin:32px auto;padding:0 20px"><p style="color:#536878">AGENT INFRA RADAR</p><h1>' + escape(subject) + '</h1><p>' + escape(intro) + '</p><p>' + escape(ranking) + '</p>' + ''.join(cards) + profile_note + '<p style="color:#536878">Sent by your private Sprite through Resend.</p></body></html>'
    return {'from': settings['email_from'], 'to': [settings['email_to']], 'subject': subject, 'html': html, 'text': '\n'.join(text), 'job_count': count, 'shown_count': shown, 'more_count': remaining}


def status(store, discover=False, opener=None):
    store.enqueue_alerts()
    settings = store.get_settings()
    base = settings.get('connector_base', '').rstrip('/')
    discovery = None
    if discover and not base:
        discovery = discover_connector(opener)
        base = discovery['base_url']
        if discovery['ready']:
            store.update_settings({'connector_base': base})
    connector_ready = valid_gateway_url(base)
    configured = bool(settings['email_enabled'] and valid_email(settings['email_to'])
                      and valid_email(settings['email_from']) and connector_ready)
    if not settings['email_enabled']:
        message = 'Email notifications are paused.'
    elif not connector_ready:
        message = (discovery or {}).get('message', 'Connect Resend to this Sprite, then check the connector in Settings.')
    elif not valid_email(settings['email_to']) or not valid_email(settings['email_from']):
        message = 'Add your recipient address and a verified Resend sender in Settings.'
    else:
        message = 'Ready to email new matching roles.'
    counts = store.notification_counts()
    current_counts = store.notification_counts(settings['email_to'])
    return {'configured': configured, 'connector_ready': connector_ready, 'message': message,
            'pending': current_counts.get('pending', 0) + current_counts.get('sending', 0),
            'sent': counts.get('sent', 0), 'emails_sent': store.sent_delivery_count(),
            'uncertain': counts.get('uncertain', 0),
            'cancelled': counts.get('cancelled', 0)}


def _age(timestamp):
    try:
        moment = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - moment).total_seconds()
    except (AttributeError, ValueError, TypeError):
        return float('inf')


def dispatch_pending(store, opener=None, classifier=None, discover=True):
    """Deliver at most one persisted digest; retries reuse its exact bytes and key."""
    opener = opener or _open
    info = status(store, discover=discover, opener=opener)
    store.enqueue_alerts()
    if not info['configured']:
        return {**status(store), 'sent_now': 0, 'emails_sent_now': 0}
    try:
        store.reclassify_jobs(classifier)
    except (ImportError, ValueError, TypeError, KeyError):
        return {**status(store), 'sent_now': 0, 'emails_sent_now': 0,
                'message': 'Refresh job matches before sending notifications.'}
    store.enqueue_alerts()
    settings = store.get_settings()
    revision = store.settings_revision()
    recipient = settings['email_to'].strip().lower()
    endpoint = settings['connector_base'].rstrip('/') + '/emails'
    runs = store.list_runs(limit=1)
    latest_run = runs[0] if runs else None
    run_id = latest_run['id'] if latest_run else None
    observed_ids = set(latest_run.get('job_ids', [])) if latest_run else set()
    jobs = {job['id']: job for job in store.list_jobs()}
    alerts = {alert['id']: alert for alert in store.list_alerts()}
    deferred = 0

    def fresh(job):
        return bool(job) and (latest_run is None or (
            latest_run.get('status') in {'completed', 'partial'}
            and job['id'] in observed_ids
            and -300 <= _age(job.get('verified_at')) <= MAX_VERIFICATION_AGE))

    def unchanged():
        current = store.list_runs(limit=1)
        return (store.settings_revision() == revision
                and (current[0]['id'] if current else None) == run_id)

    selected = None
    for delivery in store.list_deliveries(active_only=True):
        members = [alerts.get(alert_id) for alert_id in delivery['alert_ids']]
        if delivery['status'] == 'sending':
            if _age(delivery['updated_at']) < 120:
                # An active worker owns this batch. Do not create overlapping work.
                return {**status(store), 'sent_now': 0, 'emails_sent_now': 0,
                        'message': 'A notification digest is already being delivered.'}
            store.update_delivery(delivery['id'], 'uncertain', 'Previous digest delivery did not finish locally.')
            delivery['status'], delivery['ambiguous'] = 'uncertain', 1
        ambiguous = bool(delivery['ambiguous'] or delivery['status'] == 'uncertain')
        payload = json.loads(delivery['payload'])
        same_destination = (delivery['recipient'] == recipient and delivery['endpoint'] == endpoint
                            and payload.get('from') == settings['email_from'])
        eligible = all(member and jobs.get(member['job_id'])
                       and store.alert_eligible(jobs[member['job_id']]) for member in members)
        verified = all(member and fresh(jobs.get(member['job_id'])) for member in members)
        if not same_destination or not eligible:
            if ambiguous:
                # An earlier attempt may already have succeeded. Never replace its body/key.
                deferred += len(members)
                continue
            store.update_delivery(delivery['id'], 'cancelled', 'Unsent digest no longer matches current settings.')
            continue
        if not verified:
            if ambiguous:
                deferred += len(members)
            else:
                store.update_delivery(delivery['id'], 'cancelled', 'Unsent digest needs fresh source verification.')
            continue
        if ambiguous and _age(delivery['attempted_at']) >= SAFE_RETRY_SECONDS:
            store.update_delivery(delivery['id'], 'uncertain', 'Delivery could not be confirmed. Automatic retry stopped to avoid a duplicate email.')
            deferred += len(members)
            continue
        selected = delivery
        break

    if selected is None:
        # Cancelled, definitely-unsent batches may now be rebuilt for current settings.
        store.enqueue_alerts()
        eligible_alerts, selected_jobs = [], []
        for alert in store.list_alerts():
            if alert['status'] in {'sent', 'cancelled'} or alert.get('delivery_id'):
                continue
            job = jobs.get(alert['job_id'])
            if alert['recipient'] != recipient or not job or not store.alert_eligible(job):
                if not alert.get('ambiguous'):
                    store.update_alert(alert['id'], 'cancelled', 'No longer an eligible match for the current recipient.')
                continue
            if not fresh(job):
                deferred += 1
                continue
            if alert.get('ambiguous') or alert['status'] in {'uncertain', 'sending'}:
                # Pre-digest uncertain sends have no immutable batch payload. Require review.
                deferred += 1
                continue
            eligible_alerts.append(alert['id'])
            selected_jobs.append(job)
        if eligible_alerts and unchanged():
            email = preview(store, selected_jobs)
            for field in ('job_count', 'shown_count', 'more_count'):
                email.pop(field, None)
            email['from'], email['to'] = settings['email_from'], [settings['email_to']]
            selected = store.reserve_delivery(eligible_alerts, email, endpoint, revision, run_id)

    if selected is None or not unchanged():
        result = {**status(store), 'sent_now': 0, 'emails_sent_now': 0, 'deferred': deferred}
        if not unchanged():
            result['message'] = 'Settings or search results changed. The digest will be checked again before delivery.'
        elif deferred:
            result['message'] = f'{deferred} alert(s) need a fresh source check or delivery review before sending.'
        return result
    if not store.claim_delivery(selected['id'], revision, run_id):
        return {**status(store), 'sent_now': 0, 'emails_sent_now': 0, 'deferred': deferred}

    # This exact persisted string is used on the first request and every retry.
    request = urllib.request.Request(selected['endpoint'], data=selected['payload'].encode('utf-8'), method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'Idempotency-Key': 'infra-radar-digest-' + selected['id'],
    })
    sent_now, emails_sent_now = 0, 0
    try:
        with opener(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('Unexpected response')
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get('id'), str):
                raise ValueError('No delivery receipt')
        store.update_delivery(selected['id'], 'sent', provider_id=result['id'][:256])
        sent_now, emails_sent_now = len(selected['alert_ids']), 1
    except urllib.error.HTTPError as exc:
        if exc.code in {408, 409, 429} or exc.code >= 500:
            store.update_delivery(selected['id'], 'uncertain', 'Digest delivery temporarily unavailable. Retry uses the same body and idempotency key.')
        else:
            store.update_delivery(selected['id'], 'pending', f'Resend gateway rejected delivery (HTTP {exc.code}). Check sender and connector settings.')
        exc.close()
    except (TimeoutError, socket.timeout, OSError, ValueError, urllib.error.URLError):
        store.update_delivery(selected['id'], 'uncertain', 'Digest delivery could not be confirmed. Retry uses the same body and idempotency key.')
    result = {**status(store), 'sent_now': sent_now, 'emails_sent_now': emails_sent_now,
              'highlighted_now': min(DIGEST_LIMIT, sent_now),
              'remaining_now': max(0, sent_now - DIGEST_LIMIT), 'deferred': deferred}
    if not unchanged():
        result['message'] = 'Settings or search results changed during delivery. Remaining alerts will be checked again.'
    elif deferred:
        result['message'] = f'{deferred} alert(s) need a fresh source check or delivery review before sending.'
    return result
