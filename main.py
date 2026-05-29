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
from apscheduler.schedulers.background import BackgroundScheduler

import config
from clickup_client import ClickUpClient, STATUS_DEPOSIT_INVOICED, STATUS_FINAL_INVOICED
from xero_client import XeroClient
from gmail_client import GmailClient
from email_parser import parse_job_email

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

gmail   = GmailClient(
    config.GMAIL_CLIENT_ID,
    config.GMAIL_CLIENT_SECRET,
    config.GMAIL_REDIRECT_URI,
)

# Status names that trigger automation (lowercase, must match ClickUp exactly)
TRIGGER_DEPOSIT = 'send deposit invoice'
TRIGGER_FINAL   = 'send final invoice'


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




# ------------------------------------------------------------------ #
#  Gmail OAuth                                                         #
# ------------------------------------------------------------------ #
@app.route('/gmail/auth')
def gmail_auth():
    """Open in browser to connect the nepmclickup@gmail.com account."""
    return redirect(gmail.get_auth_url())


@app.route('/gmail/callback')
def gmail_callback():
    code = request.args.get('code')
    if not code:
        return jsonify({'error': 'No authorisation code received from Google.'}), 400
    try:
        gmail.exchange_code(code)
        return jsonify({'message': 'Gmail connected successfully! You can close this tab.'}), 200
    except Exception as e:
        logger.error(f'Gmail auth error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/gmail/process')
def gmail_process_manual():
    """Manually trigger email processing (useful for testing)."""
    process_job_emails()
    return jsonify({'status': 'done'}), 200

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

    if new_status == TRIGGER_DEPOSIT:
        handle_deposit_invoice(task_id)
    elif new_status == TRIGGER_FINAL:
        handle_final_invoice(task_id)

    return jsonify({'status': 'ok'}), 200


# ------------------------------------------------------------------ #
#  Xero payment webhook                                                #
# ------------------------------------------------------------------ #
@app.route('/webhooks/xero', methods=['POST'])
def xero_webhook():
    """
    Receives payment notifications from Xero.
    When an invoice is marked PAID, updates the matching ClickUp task.

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
    for event in events:
        category  = event.get('eventCategory')
        eventtype = event.get('eventType')
        resource  = event.get('resourceId')

        if eventtype == 'UPDATE' and category == 'INVOICE':
            handle_payment(resource)
        elif eventtype == 'CREATE' and category == 'CONTACT':
            handle_new_contact(resource)

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
        amount        = round(deposit_hours * rate, 2)
        job_name      = task.get('name', 'Unknown Job')

        description = (
            f"Deposit Invoice \u2013 {job_name} \u2013 "
            f"{deposit_hours:.1f}hrs (50% of assumed {est_hours:.1f}hrs)"
        )

        # --- Create invoice in Xero ---
        contact_id = xero.get_contact_id(client_name)
        invoice    = xero.create_invoice(
            contact_id       = contact_id,
            description      = description,
            amount           = amount,
            account_code     = config.XERO_ACCOUNT_CODE,
            tax_type         = config.XERO_TAX_TYPE,
            line_amount_type = config.XERO_LINE_AMOUNT_TYPE,
            branding_theme_id = config.XERO_BRANDING_THEME_ID,
            # Reference encodes task ID + type so we can find it from a Xero payment webhook
            reference        = f'CU-{task_id}-deposit',
        )

        inv_number = invoice.get('InvoiceNumber', '')
        logger.info(f'Deposit invoice {inv_number} created (task {task_id}, ${amount:.2f})')

        # --- Update ClickUp ---
        clickup.set_field(task_id, 'Deposit Invoice #',      inv_number)
        clickup.set_field(task_id, 'Deposit Invoice Status', 'Invoiced')
        clickup.update_status(task_id, STATUS_DEPOSIT_INVOICED)

    except Exception as e:
        logger.error(f'handle_deposit_invoice failed for task {task_id}: {e}')


def handle_final_invoice(task_id: str):
    """
    Triggered when a job moves to 'Send Final Invoice'.
    Creates a draft final invoice in Xero for the balance, then updates ClickUp.
    Formula: (Total Hours Invoiced - Estimated Hours / 2) × Hourly Rate
    """
    try:
        task   = clickup.get_task(task_id)
        fields = clickup.parse_custom_fields(task)

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
        if not est_ms:
            logger.error(f'Task {task_id}: Time estimate is 0 — cannot calculate balance')
            return

        est_hours    = float(est_ms) / 3_600_000
        deposit_hrs  = est_hours / 2
        total_hrs    = float(total_hours_invoiced)
        final_hrs    = total_hrs - deposit_hrs
        rate         = float(hourly_rate)

        if final_hrs <= 0:
            logger.error(
                f'Task {task_id}: Final hours = {final_hrs:.2f} '
                f'(total {total_hrs}hrs - deposit {deposit_hrs}hrs). Nothing to invoice.'
            )
            return

        amount   = round(final_hrs * rate, 2)
        job_name = task.get('name', 'Unknown Job')

        description = (
            f"Final Invoice \u2013 {job_name} \u2013 "
            f"{final_hrs:.1f}hrs "
            f"({total_hrs:.1f}hrs total less {deposit_hrs:.1f}hrs deposit)"
        )

        # --- Create invoice in Xero ---
        contact_id = xero.get_contact_id(client_name)
        invoice    = xero.create_invoice(
            contact_id        = contact_id,
            description       = description,
            amount            = amount,
            account_code      = config.XERO_ACCOUNT_CODE,
            tax_type          = config.XERO_TAX_TYPE,
            line_amount_type  = config.XERO_LINE_AMOUNT_TYPE,
            branding_theme_id = config.XERO_BRANDING_THEME_ID,
            reference         = f'CU-{task_id}-final',
        )

        inv_number = invoice.get('InvoiceNumber', '')
        logger.info(f'Final invoice {inv_number} created (task {task_id}, ${amount:.2f})')

        # --- Update ClickUp ---
        clickup.set_field(task_id, 'Final Invoice #',      inv_number)
        clickup.set_field(task_id, 'Final Invoice Status', 'Invoiced')
        clickup.update_status(task_id, STATUS_FINAL_INVOICED)

    except Exception as e:
        logger.error(f'handle_final_invoice failed for task {task_id}: {e}')


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

        reference = invoice.get('Reference', '')   # format: CU-{task_id}-deposit/final
        if not reference.startswith('CU-'):
            logger.info(f'Invoice {invoice_id} paid but not from this automation — skipping')
            return

        parts = reference.split('-')  # ['CU', '{task_id}', 'deposit'|'final']
        if len(parts) != 3:
            logger.warning(f'Unexpected reference format: {reference}')
            return

        task_id      = parts[1]
        invoice_type = parts[2]   # 'deposit' or 'final'

        clickup.mark_invoice_paid(task_id, invoice_type)
        logger.info(f'Invoice {invoice_id} paid → task {task_id} ({invoice_type}) updated')

    except Exception as e:
        logger.error(f'handle_payment failed for invoice {invoice_id}: {e}')




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

        added = clickup.add_client_option(name)
        if added:
            logger.info(f'New Xero contact "{name}" added to ClickUp Client dropdown')

    except Exception as e:
        logger.error(f'handle_new_contact failed for contact {contact_id}: {e}')



def process_job_emails():
    """
    Poll Gmail for unprocessed emails from George and create ClickUp tasks.
    Runs automatically every 3 minutes via the scheduler.
    """
    if not gmail.is_authenticated():
        logger.warning('Gmail not authenticated — skipping email poll')
        return

    try:
        emails = gmail.get_unprocessed_emails()
        if not emails:
            return

        logger.info(f'Processing {len(emails)} new job email(s)')

        # Get current Client dropdown options for fuzzy matching
        client_field  = clickup._fields.get('client', {})
        client_options = [o['name'] for o in client_field.get('type_config', {}).get('options', [])]

        for email in emails:
            try:
                # Parse with Claude
                data = parse_job_email(
                    subject        = email['subject'],
                    body           = email['body'],
                    client_options = client_options,
                    api_key        = config.ANTHROPIC_API_KEY,
                )

                if not data:
                    logger.error(f'Could not parse email {email["id"]} — skipping')
                    gmail.mark_processed(email['id'])
                    continue

                job_title       = data.get('job_title', '').strip() or email['subject']
                client_name     = data.get('client', '').strip()
                estimated_hours = data.get('estimated_hours')
                hourly_rate     = data.get('hourly_rate')

                if not client_name:
                    logger.warning(f'No client match for email "{email["subject"]}" — task will have blank Client field')

                # Convert hours to milliseconds for ClickUp time estimate
                est_ms = int(estimated_hours * 3_600_000) if estimated_hours else None

                # Create the ClickUp task
                task = create_job_task(
                    name             = job_title,
                    client_name      = client_name,
                    hourly_rate      = hourly_rate,
                    time_estimate_ms = est_ms,
                    description      = email['body'],
                    attachments      = email.get('attachments', []),
                    message_id       = email['id'],
                )

                if task:
                    logger.info(f'Created task "{job_title}" (client: {client_name or "blank"})')

                # Mark email as processed regardless of task creation outcome
                gmail.mark_processed(email['id'])

            except Exception as e:
                logger.error(f'Failed to process email {email["id"]}: {e}')
                gmail.mark_processed(email['id'])   # mark processed to avoid infinite retry

    except Exception as e:
        logger.error(f'process_job_emails error: {e}')


def create_job_task(name: str, client_name: str, hourly_rate, time_estimate_ms,
                    description: str = '', attachments: list = None, message_id: str = '') -> dict:
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
            clickup.update_status(task_id, 'send deposit invoice')
        else:
            missing = [f for f, v in [('Client', client_name), ('Hourly Rate', hourly_rate), ('Time Estimate', time_estimate_ms)] if not v]
            logger.warning(f'Task {task_id} missing fields {missing} — left at Not Started for manual review')

        # Upload attachments
        if attachments and message_id:
            for att in attachments:
                try:
                    file_bytes = gmail.download_attachment(message_id, att['attachment_id'])
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

# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #
# Start the background scheduler for Gmail polling
scheduler = BackgroundScheduler()
scheduler.add_job(process_job_emails, 'interval', minutes=3, id='email_poll')
scheduler.start()
logger.info('Email polling scheduler started (every 3 minutes)')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
