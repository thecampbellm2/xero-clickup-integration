"""
job-automation — ClickUp ↔ Xero invoice automation
----------------------------------------------------
Endpoints:
  GET  /                   health check
  GET  /xero/auth          start Xero OAuth flow (open in browser)
  GET  /xero/callback      Xero OAuth callback (set as Redirect URI in Xero app)
  GET  /xero/info          list branding themes + connected org (run after auth)
  POST /webhooks/clickup   receives ClickUp status change events
  POST /webhooks/xero      receives Xero payment notifications
"""

import hashlib
import hmac
import logging
import os

from flask import Flask, jsonify, redirect, request
import requests
import pytz
from datetime import date
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler

import config
from clickup_client import ClickUpClient, STATUS_DEPOSIT_INVOICED, STATUS_FINAL_INVOICED
from xero_client import XeroClient, XeroAuthError
from gmail_client import GmailClient
from email_parser import parse_job_email, parse_reply_email
import notifier
from daily_summary import send_daily_summary
import pending_store

# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s'
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  App + clients                                                       #
# ------------------------------------------------------------------ #
app = Flask(__name__)

clickup = ClickUpClient(config.CLICKUP_API_TOKEN, config.CLICKUP_LIST_ID)
xero    = XeroClient(
    config.XERO_CLIENT_ID,
    config.XERO_CLIENT_SECRET,
    config.XERO_REDIRECT_URI,
    config.XERO_WEBHOOK_KEY,
)

gmail   = GmailClient(config.GMAIL_USERNAME, config.GMAIL_APP_PASSWORD)

# Status names that trigger automation (lowercase, must match ClickUp exactly)
TRIGGER_DEPOSIT  = 'send deposit invoice'   # manual override trigger
TRIGGER_FINAL    = 'send final invoice'     # manual override trigger
PENDING_DEPOSIT  = 'deposit invoice pending' # auto landing status
PENDING_FINAL    = 'final invoice pending'   # auto landing status


# ------------------------------------------------------------------ #
#  Health check                                                        #
# ------------------------------------------------------------------ #
@app.route('/')
def health():
    return jsonify({'status': 'running'}), 200


# ------------------------------------------------------------------ #
#  Xero OAuth                                                          #
# ------------------------------------------------------------------ #
@app.route('/xero/auth')
def xero_auth():
    """Open this URL in a browser to connect George's Xero organisation."""
    return redirect(xero.get_auth_url())

@app.route('/xero/debug')
def xero_debug():
    """Temporarily shows the auth URL for inspection — remove after debugging."""
    return jsonify({'auth_url': xero.get_auth_url()}), 200


@app.route('/xero/callback')
def xero_callback():
    """Xero redirects here after the user authorises the app."""
    code = request.args.get('code')
    if not code:
        return jsonify({'error': 'No authorisation code received from Xero.'}), 400
    try:
        xero.exchange_code(code)
        return jsonify({'message': 'Xero connected successfully! You can close this tab.'}), 200
    except Exception as e:
        logger.error(f'Xero auth error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/xero/info')
def xero_info():
    """
    After connecting Xero, visit this URL to see branding theme IDs.
    Copy the correct BrandingThemeID into XERO_BRANDING_THEME_ID in your .env / Render env vars.
    """
    try:
        themes = xero.get_branding_themes()
        return jsonify({
            'connected_org': xero._tokens.get('tenant_name', 'unknown'),
            'branding_themes': [
                {'name': t['Name'], 'id': t['BrandingThemeID']}
                for t in themes
            ]
        }), 200
    except Exception as e:
        logger.error(f'Error fetching Xero info: {e}')
        return jsonify({'error': str(e)}), 500




@app.route('/summary/send')
def summary_send_manual():
    """Manually trigger the daily summary email."""
    import threading
    threading.Thread(target=send_daily_summary, args=(xero, gmail, config.NOTIFICATION_EMAILS, config.SENDGRID_API_KEY), kwargs={'clickup_token': config.CLICKUP_API_TOKEN, 'clickup_channel': config.CLICKUP_ALERT_CHANNEL_ID}, daemon=True).start()
    return jsonify({'status': 'summary sending'}), 200


@app.route('/batch/invoices')
def batch_invoices_manual():
    """Manually trigger the batch invoice run — runs in background to avoid timeout."""
    import threading
    threading.Thread(target=batch_invoices, daemon=True).start()
    return jsonify({'status': 'batch started'}), 200


@app.route('/gmail/process')
def gmail_process_manual():
    """Manually trigger email processing — runs in background to avoid timeout."""
    import threading
    threading.Thread(target=process_job_emails, daemon=True).start()
    return jsonify({'status': 'processing started'}), 200

# ------------------------------------------------------------------ #
#  ClickUp webhook                                                     #
# ------------------------------------------------------------------ #
@app.route('/webhooks/clickup', methods=['POST'])
def clickup_webhook():
    """Receives task status change events from ClickUp."""

    # Verify signature if secret is configured
    if config.CLICKUP_WEBHOOK_SECRET:
        sig = request.headers.get('X-Signature', '')
        expected = hmac.new(
            config.CLICKUP_WEBHOOK_SECRET.encode(),
            request.data,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning('ClickUp webhook: invalid signature — ignoring')
            return jsonify({'error': 'Invalid signature'}), 401

    data  = request.json or {}
    event = data.get('event')

    if event != 'taskStatusUpdated':
        return jsonify({'status': 'ignored', 'event': event}), 200

    task_id    = data.get('task_id')
    items      = data.get('history_items', [{}])
    new_status = items[0].get('after', {}).get('status', '').lower().strip()

    logger.info(f'ClickUp event: task {task_id} → "{new_status}"')

    # Process in a background thread so we respond to ClickUp immediately
    # (prevents ClickUp timing out and retrying, which caused duplicate invoices)
    import threading
    if new_status == TRIGGER_DEPOSIT:
        threading.Thread(target=handle_deposit_invoice, args=(task_id,), daemon=True).start()
    elif new_status == TRIGGER_FINAL:
        threading.Thread(target=handle_final_invoice, args=(task_id,), daemon=True).start()

    return jsonify({'status': 'ok'}), 200


# ------------------------------------------------------------------ #
#  Xero payment webhook                                                #
# ------------------------------------------------------------------ #
@app.route('/webhooks/xero', methods=['POST'])
def xero_webhook():
    """
    Receives contact/invoice notifications from Xero.

    IMPORTANT: Xero requires a response within 5 seconds or it considers the
    delivery failed and retries the SAME batch (immediately, then with
    decreasing frequency for 24h). Bank-reconciliation-driven Contact CREATE
    events can arrive in batches of 15-20+, and processing each one
    synchronously (Xero lookup + ClickUp + SendGrid email) easily blows past
    5 seconds — which both triggers Xero retries AND can exceed gunicorn's
    worker timeout. So: verify + ack first, then do the real work in a
    background thread.

    Note: Xero sends an "intent to receive" validation request when you
    first register this endpoint — this handler responds correctly to it.
    """
    payload   = request.data
    signature = request.headers.get('x-xero-signature', '')

    if not xero.verify_webhook(payload, signature):
        logger.warning('Xero webhook: invalid signature')
        return '', 401  # Xero expects a 401 on bad signature (not 200)

    # Empty payload = intent-to-receive validation ping from Xero
    if not payload or payload == b'{}':
        return '', 200

    events = (request.json or {}).get('events', [])

    def _process_events(events):
        for event in events:
            category  = event.get('eventCategory')
            eventtype = event.get('eventType')
            resource  = event.get('resourceId')

            if eventtype == 'UPDATE' and category == 'INVOICE':
                handle_payment(resource)
            elif eventtype == 'CREATE' and category == 'CONTACT':
                handle_new_contact(resource)

    import threading
    threading.Thread(target=_process_events, args=(events,), daemon=True).start()

    return '', 200


# ------------------------------------------------------------------ #
#  Business logic                                                      #
# ------------------------------------------------------------------ #

def handle_deposit_invoice(task_id: str):
    """
    Triggered when a job moves to 'Send Deposit Invoice'.
    Creates a draft deposit invoice in Xero, then updates ClickUp.
    """
    try:
        task   = clickup.get_task(task_id)
        fields = clickup.parse_custom_fields(task)

        # --- Idempotency check: skip if deposit invoice already exists ---
        existing_invoice = fields.get('deposit_invoice_number')
        if existing_invoice:
            logger.info(f'Task {task_id} already has deposit invoice {existing_invoice} — skipping')
            return

        # --- Validate required fields ---
        client_name = fields.get('client')
        hourly_rate = fields.get('hourly_rate')
        est_ms      = task.get('time_estimate') or 0   # ClickUp stores estimate in milliseconds

        if not client_name:
            logger.error(f'Task {task_id}: "Client" field is empty — cannot invoice')
            return
        if not hourly_rate:
            logger.error(f'Task {task_id}: "Hourly Rate" field is empty — cannot invoice')
            return
        if not est_ms:
            logger.error(f'Task {task_id}: Time estimate is 0 — cannot invoice')
            return

        est_hours     = float(est_ms) / 3_600_000          # ms → hours
        deposit_hours = est_hours / 2
        rate          = float(hourly_rate)
        job_name      = task.get('name', 'Unknown Job')

        description = (
            f"Deposit Invoice \u2013 {job_name} \u2013 "
            f"{deposit_hours:.1f}hrs (50% of assumed {est_hours:.1f}hrs)"
        )

        # --- Create invoice in Xero ---
        contact_id = xero.get_contact_id(client_name)
        invoice    = xero.create_invoice(
            contact_id        = contact_id,
            line_items        = [{
                'description':  description,
                'quantity':     deposit_hours,
                'unit_amount':  rate,
                'account_code': config.XERO_ACCOUNT_CODE,
            }],
            due_date          = date.today().strftime('%Y-%m-%d'),
            branding_theme_id = config.XERO_BRANDING_THEME_ID,
            reference         = f'CU-{task_id}-deposit',
        )

        inv_number = invoice.get('InvoiceNumber', '')
        logger.info(f'Deposit invoice {inv_number} created (task {task_id}, {deposit_hours:.1f}hrs @ ${rate:.2f}/hr)')

        # --- Update ClickUp ---
        clickup.set_field(task_id, 'Deposit Invoice #',      inv_number)
        clickup.set_field(task_id, 'Deposit Invoice Status', 'Invoiced')
        clickup.update_status(task_id, STATUS_DEPOSIT_INVOICED)

    except Exception as e:
        logger.error(f'handle_deposit_invoice failed for task {task_id}: {e}')
        notifier.invoice_failed(gmail, config.NOTIFICATION_EMAILS, task_id, task.get('name','Unknown') if 'task' in dir() else 'Unknown', '', str(e), 'deposit', sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)


def handle_final_invoice(task_id: str):
    """
    Triggered when a job moves to 'Send Final Invoice'.
    Creates a draft final invoice in Xero for the balance, then updates ClickUp.
    Formula: (Total Hours Invoiced - Estimated Hours / 2) × Hourly Rate
    """
    try:
        task   = clickup.get_task(task_id)
        fields = clickup.parse_custom_fields(task)

        # --- Idempotency check: skip if final invoice already exists ---
        existing_invoice = fields.get('final_invoice_number')
        if existing_invoice:
            logger.info(f'Task {task_id} already has final invoice {existing_invoice} — skipping')
            return

        # --- Validate required fields ---
        client_name          = fields.get('client')
        hourly_rate          = fields.get('hourly_rate')
        total_hours_invoiced = fields.get('total_hours_invoiced')
        est_ms               = task.get('time_estimate') or 0

        if not client_name:
            logger.error(f'Task {task_id}: "Client" field is empty — cannot invoice')
            return
        if not hourly_rate:
            logger.error(f'Task {task_id}: "Hourly Rate" field is empty — cannot invoice')
            return
        if not total_hours_invoiced:
            logger.error(f'Task {task_id}: "Total Hours Invoiced" is empty — cannot invoice')
            return
        # If no estimate, check if a deposit was ever sent
        if not est_ms:
            deposit_invoice_num = fields.get('deposit_invoice_number') or ''
            if deposit_invoice_num:
                logger.error(f'Task {task_id}: deposit was sent but estimate is 0 — cannot calculate balance')
                return
            # No deposit sent — invoice for full amount
            deposit_hrs = 0.0
        else:
            est_hours   = float(est_ms) / 3_600_000
            deposit_hrs = est_hours / 2
        total_hrs    = float(total_hours_invoiced)
        final_hrs    = total_hrs - deposit_hrs
        rate         = float(hourly_rate)

        is_credit = final_hrs < 0
        if final_hrs == 0:
            logger.info(f'Task {task_id}: deposit exactly covers total — moving to Final Invoiced')
            clickup.update_status(task_id, STATUS_FINAL_INVOICED)
            return

        job_name = task.get('name', 'Unknown Job')

        if is_credit:
            credit_hrs  = abs(final_hrs)
            description = (
                f"Credit \u2013 {job_name} \u2013 "
                f"{credit_hrs:.1f}hrs overpaid "
                f"({total_hrs:.1f}hrs total, {deposit_hrs:.1f}hrs deposit already invoiced)"
            )
            qty = credit_hrs
            ref = f'CU-{task_id}-credit'
            doc_type = 'Credit note'
        else:
            description = (
                f"Final Invoice \u2013 {job_name} \u2013 "
                f"{final_hrs:.1f}hrs "
                f"({total_hrs:.1f}hrs total less {deposit_hrs:.1f}hrs deposit)"
            )
            qty = final_hrs
            ref = f'CU-{task_id}-final'
            doc_type = 'Final invoice'

        # --- Create invoice or credit note in Xero ---
        contact_id = xero.get_contact_id(client_name)
        invoice    = xero.create_invoice(
            contact_id        = contact_id,
            line_items        = [{
                'description':  description,
                'quantity':     qty,
                'unit_amount':  rate,
                'account_code': config.XERO_ACCOUNT_CODE,
            }],
            due_date          = date.today().strftime('%Y-%m-%d'),
            branding_theme_id = config.XERO_BRANDING_THEME_ID,
            reference         = ref,
            credit_note       = is_credit,
        )

        inv_number = invoice.get('InvoiceNumber', '') or invoice.get('CreditNoteNumber', '')
        logger.info(f'{doc_type} {inv_number} created (task {task_id}, {qty:.1f}hrs @ ${rate:.2f}/hr)')

        # --- Update ClickUp ---
        clickup.set_field(task_id, 'Final Invoice #',      inv_number)
        clickup.set_field(task_id, 'Final Invoice Status', 'Invoiced')
        clickup.update_status(task_id, STATUS_FINAL_INVOICED)

    except Exception as e:
        logger.error(f'handle_final_invoice failed for task {task_id}: {e}')
        notifier.invoice_failed(gmail, config.NOTIFICATION_EMAILS, task_id, task.get('name','Unknown') if 'task' in dir() else 'Unknown', '', str(e), 'final', sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)


def handle_payment(invoice_id: str):
    """
    Called when Xero reports an invoice update.
    If it's been paid, finds the ClickUp task via the invoice Reference field
    and marks the relevant Invoice Status field as 'Paid'.
    """
    try:
        invoice = xero.get_invoice(invoice_id)
        if not invoice or invoice.get('Status') != 'PAID':
            return

        reference = invoice.get('Reference', '')
        if not reference:
            return

        # Reference may contain multiple tasks: CU-taskid1-deposit,CU-taskid2-final
        refs = [r.strip() for r in reference.split(',')]
        updated = 0
        for ref in refs:
            if not ref.startswith('CU-'):
                continue
            parts = ref.split('-')   # ['CU', task_id, 'deposit'|'final']
            if len(parts) != 3:
                continue
            task_id      = parts[1]
            invoice_type = parts[2]
            clickup.mark_invoice_paid(task_id, invoice_type)
            updated += 1

        if updated:
            logger.info(f'Invoice {invoice_id} paid → {updated} task(s) updated')
        else:
            logger.info(f'Invoice {invoice_id} paid but no CU- references found — skipping')

    except XeroAuthError as e:
        logger.error(f'handle_payment failed for invoice {invoice_id}: {e}')
        notifier.xero_auth_failed(gmail, config.NOTIFICATION_EMAILS, sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)

    except Exception as e:
        logger.error(f'handle_payment failed for invoice {invoice_id}: {e}')




import re

# Xero's bank-reconciliation engine auto-creates a real Contact record every time
# it matches an unrecognized bank-statement payee description — these are NOT
# real construction clients and shouldn't trigger a "add to ClickUp" alert.
# This is a heuristic, not a perfect classifier — refine the pattern list as
# new false positives/negatives turn up.
_BANK_FEED_CONTACT_PATTERN = re.compile(
    r'transfer\s+(to|from)\b'      # "Transfer to xx72", "Transfer From D R DUREN"
    r'|\bbpay\b'                   # "906919044 partial CommBank app BPAY"
    r'|\bcommbank\b'
    r'|\bpayid\b'
    r'|\bcard\s*xx?\d+'            # "Card xx1696"
    r'|\bxx\d+\b'                  # masked account refs: "xx72", "xx0282"
    r'|^\d{5,}'                    # starts with a long bank reference number
    r'|^unknown$',
    re.IGNORECASE,
)


def _is_bank_feed_contact(name: str) -> bool:
    return bool(_BANK_FEED_CONTACT_PATTERN.search(name))


def handle_new_contact(contact_id: str):
    """
    Triggered when a new contact is created in Xero.
    Adds the contact name to the Client dropdown in ClickUp.
    """
    try:
        contact = xero.get_contact(contact_id)
        if not contact:
            logger.warning(f'Contact {contact_id} not found in Xero')
            return

        name = contact.get('Name', '').strip()
        if not name:
            logger.warning(f'Contact {contact_id} has no name — skipping')
            return

        if _is_bank_feed_contact(name):
            logger.info(f'Contact {contact_id} ("{name}") looks like a bank-feed artifact — skipping')
            return

        added = clickup.add_client_option(name)
        if not added:
            notifier.new_contact_action_required(gmail, config.NOTIFICATION_EMAILS, name, sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)

    except XeroAuthError as e:
        logger.error(f'handle_new_contact failed for contact {contact_id}: {e}')
        notifier.xero_auth_failed(gmail, config.NOTIFICATION_EMAILS, sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)


    except Exception as e:
        logger.error(f'handle_new_contact failed for contact {contact_id}: {e}')



REQUIRED_FIELDS = {
    'client':          'Client name',
    'hourly_rate':     'Hourly rate (e.g. $120/hr)',
    'estimated_hours': 'Estimated hours (e.g. 6 hours)',
}


def build_question_email(job_title: str, missing: list) -> str:
    lines = '\n'.join(f'  \u2022 {REQUIRED_FIELDS[f]}' for f in missing)
    return f"""Hi George,

A job email came through but is missing some required details needed to create a deposit invoice:

\U0001f4cb Job: {job_title}

Missing details:
{lines}

Please reply with the missing information in plain English, for example:
"Client is BR Masonry, rate is $120/hr, estimated 6 hours"

If you don\u2019t have these details yet, just reply with something like:
"Import as is" or "Don\u2019t know yet" \u2014 the job will be added to ClickUp with the information available.

The job will be automatically imported after 3 hours if no reply is received.

\u2014 NEPM Automation"""


def _missing_fields(client_name, hourly_rate, estimated_hours) -> list:
    missing = []
    if not client_name:
        missing.append('client')
    if not hourly_rate:
        missing.append('hourly_rate')
    if not estimated_hours:
        missing.append('estimated_hours')
    return missing


def process_job_emails():
    """
    Poll Gmail for unprocessed emails from George and create ClickUp tasks.
    Also processes replies to pending-field questions and checks for timeouts.
    Runs automatically every 3 minutes via the scheduler.
    """
    if not gmail.is_authenticated():
        logger.warning('Gmail not authenticated — skipping email poll')
        return

    try:
        # ── Check for expired pending jobs (3-hour timeout) ───────────
        expired = pending_store.get_expired(config.GITHUB_TOKEN, config.TOKEN_GIST_ID)
        for mid, job in expired:
            logger.warning(f'Pending job timed out — importing as-is: {job.get("job_title")}')
            create_job_task(
                name             = job.get('job_title', 'Unknown Job'),
                client_name      = job.get('client', ''),
                hourly_rate      = job.get('hourly_rate'),
                time_estimate_ms = int(job['estimated_hours'] * 3_600_000) if job.get('estimated_hours') else None,
                due_date         = job.get('due_date'),
                description      = job.get('body', ''),
                attachments      = [],
                message_id       = '',
            )
            notifier.email_parse_failed(
                gmail, config.NOTIFICATION_EMAILS, job.get('subject', ''),
                f'Job imported after 3-hour timeout — missing fields: {", ".join(job.get("missing_fields", []))}',
                sendgrid_api_key=config.SENDGRID_API_KEY,
                clickup_token=config.CLICKUP_API_TOKEN,
                clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID,
            )
            pending_store.remove(config.GITHUB_TOKEN, config.TOKEN_GIST_ID, mid)

        # ── Fetch unread emails ───────────────────────────────────────
        emails = gmail.get_unprocessed_emails()
        if not emails:
            return

        logger.info(f'Processing {len(emails)} new job email(s)')

        # Load pending jobs to detect replies
        all_pending    = pending_store.get_all(config.GITHUB_TOKEN, config.TOKEN_GIST_ID)
        pending_by_mid = {job['original_message_id']: (mid, job)
                          for mid, job in all_pending.items()
                          if 'original_message_id' in job}

        # Get current Client dropdown options for fuzzy matching
        client_field   = clickup._fields.get('client', {})
        client_options = [o['name'] for o in client_field.get('type_config', {}).get('options', [])]

        for email in emails:
            try:
                in_reply_to = email.get('in_reply_to', '').strip()

                # ── Is this a reply to a pending job question? ────────
                if in_reply_to and in_reply_to in pending_by_mid:
                    mid, pending_job = pending_by_mid[in_reply_to]
                    logger.info(f'Reply received for pending job: {pending_job.get("job_title")}')

                    reply_data = parse_reply_email(
                        reply_body     = email['body'] or '',
                        missing_fields = pending_job.get('missing_fields', []),
                        api_key        = config.ANTHROPIC_API_KEY,
                    )

                    if reply_data.get('import_as_is'):
                        client_name     = pending_job.get('client', '')
                        hourly_rate     = pending_job.get('hourly_rate')
                        estimated_hours = pending_job.get('estimated_hours')
                    else:
                        client_name     = (reply_data.get('client') or pending_job.get('client') or '').strip()
                        hourly_rate     = reply_data.get('hourly_rate') or pending_job.get('hourly_rate')
                        estimated_hours = reply_data.get('estimated_hours') or pending_job.get('estimated_hours')
                        if not client_name and client_options:
                            import difflib
                            matches = difflib.get_close_matches(
                                email['body'] or '', client_options, n=1, cutoff=0.3
                            )
                            if matches:
                                client_name = matches[0]

                    create_job_task(
                        name             = pending_job.get('job_title', 'Unknown Job'),
                        client_name      = client_name,
                        hourly_rate      = hourly_rate,
                        time_estimate_ms = int(estimated_hours * 3_600_000) if estimated_hours else None,
                        due_date         = pending_job.get('due_date'),
                        description      = pending_job.get('body', ''),
                        attachments      = [],
                        message_id       = '',
                    )
                    pending_store.remove(config.GITHUB_TOKEN, config.TOKEN_GIST_ID, mid)
                    gmail.mark_processed(email['id'])
                    continue

                # ── New job email ─────────────────────────────────────
                data = parse_job_email(
                    subject        = email['subject'],
                    body           = email['body'],
                    client_options = client_options,
                    api_key        = config.ANTHROPIC_API_KEY,
                )

                if not data:
                    logger.error(f'Could not parse email {email["id"]} — skipping')
                    notifier.email_parse_failed(gmail, config.NOTIFICATION_EMAILS, email['subject'], 'Claude API could not extract job details', sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)
                    gmail.mark_processed(email['id'])
                    continue

                if data.get('is_job_email') is False:
                    # Inbox gets non-job mail too (account notifications, newsletters, etc.)
                    # — quietly mark as processed rather than spinning up a phantom job.
                    logger.info(f'Not a job email — skipping: {email["subject"]}')
                    gmail.mark_processed(email['id'])
                    continue

                job_title       = (data.get('job_title') or '').strip() or email['subject']
                client_name     = (data.get('client') or '').strip()
                estimated_hours = data.get('estimated_hours')
                hourly_rate     = data.get('hourly_rate')
                due_date        = data.get('due_date') or None

                # Fuzzy fallback for client
                if not client_name and client_options:
                    import difflib
                    matches = difflib.get_close_matches(
                        email['subject'] + ' ' + (email['body'] or ''),
                        client_options, n=1, cutoff=0.3
                    )
                    if matches:
                        client_name = matches[0]
                        logger.info(f'Fuzzy matched client "{client_name}"')

                missing = _missing_fields(client_name, hourly_rate, estimated_hours)

                if missing:
                    # Send automated question to George's primary NEPM address.
                    # We deliberately do NOT reply to email['sender'] because George often
                    # forwards jobs from a client address (e.g. georgina@brmasonry.com.au)
                    # — replying there means she never sees the question.
                    sender   = email['sender']
                    question = build_question_email(job_title, missing)
                    gmail.send_reply(
                        to               = config.GEORGE_EMAIL,
                        subject          = email['subject'],
                        body             = question,
                        in_reply_to      = email.get('message_id', ''),
                        sendgrid_api_key = config.SENDGRID_API_KEY,
                    )
                    pending_store.add(
                        config.GITHUB_TOKEN, config.TOKEN_GIST_ID,
                        email['id'],
                        {
                            'job_title':           job_title,
                            'client':              client_name,
                            'hourly_rate':         hourly_rate,
                            'estimated_hours':     estimated_hours,
                            'due_date':            due_date,
                            'missing_fields':      missing,
                            'subject':             email['subject'],
                            'body':                email['body'],
                            'sender':              sender,
                            'original_message_id': email.get('message_id', ''),
                        }
                    )
                    logger.info(f'Question sent for "{job_title}" — missing: {missing}')
                    gmail.mark_processed(email['id'])
                    continue

                # All fields present — create task
                est_ms = int(estimated_hours * 3_600_000) if estimated_hours else None
                task   = create_job_task(
                    name             = job_title,
                    client_name      = client_name,
                    hourly_rate      = hourly_rate,
                    time_estimate_ms = est_ms,
                    due_date         = due_date,
                    description      = email['body'],
                    attachments      = email.get('attachments', []),
                    message_id       = email['id'],
                )
                if task:
                    logger.info(f'Created task "{job_title}" (client: {client_name})')
                gmail.mark_processed(email['id'])

            except Exception as e:
                logger.error(f'Failed to process email {email["id"]}: {e}')
                gmail.mark_processed(email['id'])

    except Exception as e:
        logger.error(f'process_job_emails error: {e}')


def create_job_task(name: str, client_name: str, hourly_rate, time_estimate_ms,
                    due_date: str = None, description: str = '', attachments: list = None, message_id: str = '') -> dict:
    """Create a ClickUp task in the Jobs list with pre-filled fields, description and attachments."""
    try:
        task_data = {
            'name':   name,
            'status': 'not started',
        }
        if time_estimate_ms:
            task_data['time_estimate'] = time_estimate_ms
        if description:
            task_data['markdown_description'] = description.strip()
        if due_date:
            try:
                from datetime import datetime, timezone
                dt = datetime.strptime(due_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                task_data['due_date'] = int(dt.timestamp() * 1000)
            except ValueError:
                logger.warning(f'Could not parse due_date "{due_date}" — skipping')

        resp = requests.post(
            f'https://api.clickup.com/api/v2/list/{config.CLICKUP_LIST_ID}/task',
            headers={
                'Authorization': config.CLICKUP_API_TOKEN,
                'Content-Type':  'application/json',
            },
            json=task_data,
        )
        resp.raise_for_status()
        task    = resp.json()
        task_id = task['id']

        # Ensure description is set (belt and braces — update after create)
        if description:
            requests.put(
                f'https://api.clickup.com/api/v2/task/{task_id}',
                headers={
                    'Authorization': config.CLICKUP_API_TOKEN,
                    'Content-Type':  'application/json',
                },
                json={'markdown_description': description.strip()},
            )

        # Set custom fields
        if client_name:
            clickup.set_field(task_id, 'Client', client_name)
        if hourly_rate:
            clickup.set_field(task_id, 'Hourly Rate', hourly_rate)

        # Auto-trigger deposit invoice if all required fields are present
        if client_name and hourly_rate and time_estimate_ms:
            logger.info(f'All fields present — auto-triggering deposit invoice for task {task_id}')
            clickup.update_status(task_id, PENDING_DEPOSIT)
        else:
            missing = [f for f, v in [('Client', client_name), ('Hourly Rate', hourly_rate), ('Time Estimate', time_estimate_ms)] if not v]
            logger.warning(f'Task {task_id} missing fields {missing} — left at Not Started for manual review')

        # Upload attachments (data already included from IMAP fetch)
        if attachments:
            for att in attachments:
                try:
                    file_bytes = att.get('data')
                    if not file_bytes:
                        continue
                    upload_resp = requests.post(
                        f'https://api.clickup.com/api/v2/task/{task_id}/attachment',
                        headers={'Authorization': config.CLICKUP_API_TOKEN},
                        files={'attachment': (att['filename'], file_bytes, att['mime_type'])},
                    )
                    if upload_resp.ok:
                        logger.info(f'Uploaded attachment "{att["filename"]}" to task {task_id}')
                    else:
                        logger.error(f'Failed to upload "{att["filename"]}": {upload_resp.status_code} {upload_resp.text}')
                except Exception as e:
                    logger.error(f'Attachment upload error for "{att["filename"]}": {e}')

        return task

    except Exception as e:
        logger.error(f'Failed to create ClickUp task "{name}": {e}')
        return None



def batch_invoices():
    """
    3pm daily batch: find all tasks in 'deposit invoice pending' and
    'final invoice pending', group by client, and create one consolidated
    Xero invoice per client covering all pending items.
    Runs at 3pm Sydney time via the scheduler.
    """
    try:
        deposit_tasks = clickup.get_tasks_by_status(PENDING_DEPOSIT)
        final_tasks   = clickup.get_tasks_by_status(PENDING_FINAL)
        all_tasks     = deposit_tasks + final_tasks

        if not all_tasks:
            logger.info('Batch invoices: no pending tasks — nothing to do')
            return

        logger.info(f'Batch invoices: {len(deposit_tasks)} deposit + {len(final_tasks)} final tasks')

        # Tracks every task skipped due to missing data so we can alert on it
        # once at the end of the run, rather than relying on someone reading Render logs.
        incomplete_tasks = []   # list of dicts: {task_id, task_name, client_name, reason}

        # Group tasks by client
        client_groups = defaultdict(list)
        for task in all_tasks:
            fields      = clickup.parse_custom_fields(task)
            client_name = fields.get('client', '').strip()
            if not client_name:
                logger.warning(f'Task {task["id"]} has no Client field — skipping')
                incomplete_tasks.append({
                    'task_id':     task['id'],
                    'task_name':   task.get('name', 'Unknown Job'),
                    'client_name': '(none)',
                    'reason':      'No Client field set',
                })
                continue
            client_groups[client_name].append((task, fields))

        today = date.today().strftime('%Y-%m-%d')

        for client_name, task_list in client_groups.items():
            try:
                line_items         = []
                task_refs          = []
                task_updates       = []   # (task_id, inv_type, new_status)

                for task, fields in task_list:
                    task_id        = task['id']
                    task_name      = task.get('name', 'Unknown Job')
                    hourly_rate    = float(fields.get('hourly_rate') or 0)
                    est_ms         = task.get('time_estimate') or 0
                    est_hours      = float(est_ms) / 3_600_000
                    deposit_hrs    = est_hours / 2
                    current_status = task.get('status', {}).get('status', '').lower().strip()

                    # Idempotency: skip if already invoiced
                    if current_status == PENDING_DEPOSIT:
                        if fields.get('deposit_invoice_number'):
                            logger.info(f'Task {task_id} already has deposit invoice — skipping')
                            continue
                        if not hourly_rate or not est_ms:
                            logger.warning(f'Task {task_id}: missing rate or estimate — skipping')
                            incomplete_tasks.append({
                                'task_id':     task_id,
                                'task_name':   task_name,
                                'client_name': client_name,
                                'reason':      'Missing Hourly Rate or Time Estimate (deposit pending)',
                            })
                            continue
                        desc = (
                            f"Deposit \u2013 {task_name} \u2013 "
                            f"{deposit_hrs:.1f}hrs (50% of assumed {est_hours:.1f}hrs)"
                        )
                        line_items.append({
                            'description':  desc,
                            'quantity':     deposit_hrs,
                            'unit_amount':  hourly_rate,
                            'account_code': config.XERO_ACCOUNT_CODE,
                        })
                        task_refs.append(f'CU-{task_id}-deposit')
                        task_updates.append((task_id, 'deposit', STATUS_DEPOSIT_INVOICED))

                    elif current_status == PENDING_FINAL:
                        if fields.get('final_invoice_number'):
                            logger.info(f'Task {task_id} already has final invoice — skipping')
                            continue
                        total_hours = float(fields.get('total_hours_invoiced') or 0)
                        if not total_hours or not hourly_rate:
                            logger.warning(f'Task {task_id}: missing total hours or rate — skipping')
                            incomplete_tasks.append({
                                'task_id':     task_id,
                                'task_name':   task_name,
                                'client_name': client_name,
                                'reason':      'Missing Total Hours Invoiced or Hourly Rate (final pending)',
                            })
                            continue
                        # If no estimate set, check whether a deposit was ever sent
                        if not est_ms:
                            deposit_invoice_num = fields.get('deposit_invoice_number') or ''
                            if deposit_invoice_num:
                                logger.warning(f'Task {task_id}: deposit was sent but estimate is 0 — skipping')
                                incomplete_tasks.append({
                                    'task_id':     task_id,
                                    'task_name':   task_name,
                                    'client_name': client_name,
                                    'reason':      'Deposit invoiced but Time Estimate is 0 — needs manual review',
                                })
                                continue
                            # No deposit sent — invoice for full amount
                            deposit_hrs = 0.0
                        final_hrs = total_hours - deposit_hrs
                        if final_hrs < 0:
                            # Client was overcharged on deposit — create a credit note
                            credit_hrs = abs(final_hrs)
                            desc = (
                                f"Credit \u2013 {task_name} \u2013 "
                                f"{credit_hrs:.1f}hrs overpaid "
                                f"({total_hours:.1f}hrs total, {deposit_hrs:.1f}hrs deposit already invoiced)"
                            )
                            line_items.append({
                                'description':  desc,
                                'quantity':     credit_hrs,
                                'unit_amount':  hourly_rate,
                                'account_code': config.XERO_ACCOUNT_CODE,
                                'credit_note':  True,
                            })
                            task_refs.append(f'CU-{task_id}-credit')
                            task_updates.append((task_id, 'credit', STATUS_FINAL_INVOICED))
                        elif final_hrs == 0:
                            logger.info(f'Task {task_id}: deposit exactly covers total — no final invoice needed')
                            clickup.update_status(task_id, STATUS_FINAL_INVOICED)
                        else:
                            desc = (
                                f"Final \u2013 {task_name} \u2013 "
                                f"{final_hrs:.1f}hrs "
                                f"({total_hours:.1f}hrs total less {deposit_hrs:.1f}hrs deposit)"
                            )
                            line_items.append({
                                'description':  desc,
                                'quantity':     final_hrs,
                                'unit_amount':  hourly_rate,
                                'account_code': config.XERO_ACCOUNT_CODE,
                                'credit_note':  False,
                            })
                            task_refs.append(f'CU-{task_id}-final')
                            task_updates.append((task_id, 'final', STATUS_FINAL_INVOICED))

                if not line_items:
                    logger.info(f'No valid line items for {client_name} — skipping')
                    continue

                # Split into regular line items and credit note line items
                regular_items = [(i, r, u) for i, (r, u) in zip(
                    [item for item in line_items if not item.get('credit_note')],
                    [(ref, upd) for ref, upd in zip(task_refs, task_updates) if upd[1] != 'credit']
                )] if False else None  # placeholder

                regular_lines  = [i for i in line_items if not i.get('credit_note')]
                credit_lines   = [i for i in line_items if i.get('credit_note')]
                regular_refs   = [r for r, u in zip(task_refs, task_updates) if u[1] != 'credit']
                credit_refs    = [r for r, u in zip(task_refs, task_updates) if u[1] == 'credit']
                regular_updates = [u for u in task_updates if u[1] != 'credit']
                credit_updates  = [u for u in task_updates if u[1] == 'credit']

                contact_id = xero.get_contact_id(client_name)

                def write_back(doc, updates, doc_type):
                    doc_number = doc.get('InvoiceNumber', '') or doc.get('CreditNoteNumber', '')
                    logger.info(f'Batch {doc_type} {doc_number} created for {client_name} ({len(updates)} item(s))')
                    for task_id, inv_type, new_status in updates:
                        inv_field    = 'Deposit Invoice #' if inv_type == 'deposit' else 'Final Invoice #'
                        status_field = 'Deposit Invoice Status' if inv_type == 'deposit' else 'Final Invoice Status'
                        clickup.set_field(task_id, inv_field,    doc_number)
                        clickup.set_field(task_id, status_field, 'Invoiced')
                        clickup.update_status(task_id, new_status)

                # Create regular invoice if there are regular line items
                if regular_lines:
                    invoice = xero.create_invoice(
                        contact_id        = contact_id,
                        line_items        = regular_lines,
                        due_date          = today,
                        reference         = ','.join(regular_refs),
                        branding_theme_id = config.XERO_BRANDING_THEME_ID,
                        credit_note       = False,
                    )
                    write_back(invoice, regular_updates, 'invoice')

                # Create credit note if there are credit line items
                if credit_lines:
                    credit_note = xero.create_invoice(
                        contact_id        = contact_id,
                        line_items        = credit_lines,
                        due_date          = today,
                        reference         = ','.join(credit_refs),
                        branding_theme_id = config.XERO_BRANDING_THEME_ID,
                        credit_note       = True,
                    )
                    write_back(credit_note, credit_updates, 'credit note')

            except Exception as e:
                logger.error(f'Batch invoice error for client {client_name}: {e}')
                notifier.batch_failed(gmail, config.NOTIFICATION_EMAILS, client_name, str(e), sendgrid_api_key=config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID)

        if incomplete_tasks:
            logger.warning(f'Batch invoices: {len(incomplete_tasks)} task(s) skipped due to missing data')
            notifier.batch_incomplete_tasks(
                gmail, config.NOTIFICATION_EMAILS, incomplete_tasks,
                sendgrid_api_key=config.SENDGRID_API_KEY,
                clickup_token=config.CLICKUP_API_TOKEN,
                clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID,
            )

    except Exception as e:
        logger.error(f'batch_invoices failed: {e}')

# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #
# Start the background scheduler for Gmail polling
sydney = pytz.timezone('Australia/Sydney')
scheduler = BackgroundScheduler(timezone=sydney)
scheduler.add_job(process_job_emails, 'interval', minutes=3, id='email_poll')
scheduler.add_job(batch_invoices, 'cron', hour=15, minute=0, id='batch_invoices')
scheduler.add_job(lambda: send_daily_summary(xero, gmail, config.NOTIFICATION_EMAILS, config.SENDGRID_API_KEY, clickup_token=config.CLICKUP_API_TOKEN, clickup_channel=config.CLICKUP_ALERT_CHANNEL_ID), 'cron', hour=17, minute=30, id='daily_summary')
scheduler.start()
logger.info('Scheduler started — email poll every 3 mins, batch invoices at 3pm, daily summary at 5:30pm Sydney')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
