"""
Sends error and action-required notifications to Mike and George via email.
All notifications come from nepmclickup@gmail.com.
"""
import logging
import traceback
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

SYDNEY = pytz.timezone('Australia/Sydney')


def _now() -> str:
    return datetime.now(SYDNEY).strftime('%d %b %Y %I:%M %p')


def _send(gmail, recipients, subject, body, sendgrid_api_key=''):
    """Send notification — fails silently so it never breaks the main flow."""
    try:
        gmail.send_email(
            to_list          = recipients,
            subject          = f'[NEPM Automation] {subject}',
            body             = body,
            sendgrid_api_key = sendgrid_api_key,
        )
    except Exception as e:
        logger.error(f'Could not send notification email: {e}')


# ── Notification functions ────────────────────────────────────────────────────

def invoice_failed(gmail, recipients, task_id, job_name, client, error, invoice_type='deposit', sendgrid_api_key=''):
    subject = f'⚠️ {invoice_type.title()} invoice failed — {job_name}'
    body = f"""An error occurred while creating a {invoice_type} invoice.

Time:       {_now()}
Job:        {job_name}
Client:     {client or 'Unknown'}
Task ID:    {task_id}
Error:      {error}

Action required:
The task is still sitting at its current status in ClickUp. Once the issue is resolved, move it to "Send {invoice_type.title()} Invoice" to retry.

— NEPM Automation"""
    _send(gmail, recipients, subject, body, sendgrid_api_key)


def batch_failed(gmail, recipients, client, error, sendgrid_api_key=''):
    subject = f'⚠️ Batch invoice failed — {client}'
    body = f"""The 3pm batch invoice run encountered an error for a client.

Time:       {_now()}
Client:     {client}
Error:      {error}

Action required:
Check ClickUp for jobs belonging to {client} that are still sitting at "Invoice Pending" or "Final Invoice Pending". Once the issue is resolved, hit /batch/invoices to retry.

— NEPM Automation"""
    _send(gmail, recipients, subject, body, sendgrid_api_key)


def email_parse_failed(gmail, recipients, email_subject, error, sendgrid_api_key=''):
    subject = f'⚠️ Job email could not be processed — {email_subject[:50]}'
    body = f"""An email arrived but could not be processed into a ClickUp task.

Time:           {_now()}
Email subject:  {email_subject}
Error:          {error}

Action required:
Log into nepmclickup@gmail.com, find this email, and create the ClickUp task manually. The email has been marked as processed so it won't be retried automatically.

— NEPM Automation"""
    _send(gmail, recipients, subject, body, sendgrid_api_key)


def new_contact_action_required(gmail, recipients, contact_name, sendgrid_api_key=''):
    subject = f'ℹ️ New Xero contact — add to ClickUp dropdown: {contact_name}'
    body = f"""A new contact was created in Xero and needs to be manually added to the Client dropdown in ClickUp.

Time:           {_now()}
Contact name:   {contact_name}

Action required:
In ClickUp, go to the Jobs list → Custom Fields → Client → Add option: "{contact_name}"
Make sure the name matches Xero exactly.

— NEPM Automation"""
    _send(gmail, recipients, subject, body, sendgrid_api_key)


def xero_auth_failed(gmail, recipients):
    subject = '⚠️ Xero authentication error — re-auth required'
    body = f"""The automation could not connect to Xero. The access token may have expired or been revoked.

Time:   {_now()}

Action required:
Visit https://xero-clickup-integration.onrender.com/xero/auth in your browser to re-authenticate.

— NEPM Automation"""
    _send(gmail, recipients, subject, body, sendgrid_api_key)


def gmail_auth_failed(recipients):
    """Special case — Gmail itself is broken so we can't email. Just log."""
    logger.error(
        f'CRITICAL: Gmail authentication failed at {_now()}. '
        f'Visit /gmail/auth to re-authenticate. '
        f'Notification could not be sent as Gmail is the notification channel.'
    )
