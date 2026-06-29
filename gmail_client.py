"""
Gmail client using IMAP/SMTP with an App Password.
No OAuth required — App Passwords don't expire unless explicitly revoked.
"""
import imaplib
import requests
import smtplib
import email
import logging
import re
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

IMAP_HOST = 'imap.gmail.com'
IMAP_PORT = 993
SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 587


class GmailClient:
    def __init__(self, username: str, app_password: str):
        self.username     = username
        self.app_password = app_password

    def is_authenticated(self) -> bool:
        return bool(self.username and self.app_password)

    # ------------------------------------------------------------------ #
    #  IMAP connection                                                     #
    # ------------------------------------------------------------------ #

    def _connect(self):
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(self.username, self.app_password)
        return mail

    # ------------------------------------------------------------------ #
    #  Reading emails                                                      #
    # ------------------------------------------------------------------ #

    def get_unprocessed_emails(self) -> list:
        """Return all unread emails from the inbox."""
        emails = []
        mail   = None
        try:
            mail = self._connect()
            mail.select('INBOX')
            status, data = mail.search(None, 'UNSEEN')
            if status != 'OK':
                return []
            msg_ids = data[0].split()
            logger.info(f'Found {len(msg_ids)} unread email(s)')
            for msg_id in msg_ids:
                try:
                    detail = self._fetch_message(mail, msg_id)
                    if detail:
                        emails.append(detail)
                except Exception as e:
                    logger.error(f'Error fetching message {msg_id}: {e}')
        except Exception as e:
            raise   # Re-raise so caller can notify
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except Exception:
                    pass
        return emails

    def _fetch_message(self, mail, msg_id) -> dict:
        """Fetch and parse a single IMAP message."""
        status, data = mail.fetch(msg_id, '(BODY.PEEK[])')
        if status != 'OK':
            return None

        raw = data[0][1] if isinstance(data[0][1], bytes) else data[0][1]
        msg      = email.message_from_bytes(raw)
        subject    = self._decode_str(msg.get('Subject', ''))
        sender     = msg.get('From', '')
        message_id = msg.get('Message-ID', msg.get('Message-Id', ''))
        in_reply_to = msg.get('In-Reply-To', '')
        references  = msg.get('References', '')
        body     = ''
        attachments = []

        for part in msg.walk():
            content_type        = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition', ''))

            if 'attachment' in content_disposition:
                filename = part.get_filename()
                if filename:
                    attachments.append({
                        'filename':  self._decode_str(filename),
                        'mime_type': content_type,
                        'data':      part.get_payload(decode=True),
                    })
            elif content_type == 'text/plain' and not body:
                try:
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception:
                    pass
            elif content_type == 'text/html' and not body:
                try:
                    html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    body = re.sub(r'<[^>]+>', ' ', html)
                    body = re.sub(r'[ \t]+', ' ', body).strip()
                except Exception:
                    pass

        return {
            'id':          msg_id.decode(),
            'subject':     subject,
            'sender':      sender,
            'body':        body,
            'attachments': attachments,
            'message_id':  message_id,
            'in_reply_to': in_reply_to,
            'references':  references,
        }

    def _decode_str(self, value: str) -> str:
        """Decode an encoded email header string."""
        if not value:
            return ''
        parts  = decode_header(value)
        result = ''
        for part, charset in parts:
            if isinstance(part, bytes):
                result += part.decode(charset or 'utf-8', errors='ignore')
            else:
                result += part
        return result

    # ------------------------------------------------------------------ #
    #  Marking as processed                                                #
    # ------------------------------------------------------------------ #

    def mark_processed(self, message_id: str):
        """Mark an email as read so it won't be picked up again."""
        mail = None
        try:
            mail = self._connect()
            mail.select('INBOX')
            mail.store(message_id.encode(), '+FLAGS', '\\Seen')
            logger.info(f'Message {message_id} marked as processed')
        except Exception as e:
            logger.error(f'Could not mark message {message_id} as read: {e}')
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    #  Sending notification emails                                         #
    # ------------------------------------------------------------------ #

    def send_email(self, to_list: list, subject: str, body: str,
                   html_body: str = '', sendgrid_api_key: str = '',
                   headers: dict = None):
        """Send email via SendGrid HTTP API (avoids Render SMTP port blocking).

        `headers` is an optional dict of custom MIME headers (e.g. In-Reply-To,
        References) added to the outgoing message so replies thread correctly.
        """
        if not sendgrid_api_key:
            logger.error('SENDGRID_API_KEY not set — cannot send email')
            return
        try:
            content = []
            if body:
                content.append({'type': 'text/plain', 'value': body})
            if html_body:
                content.append({'type': 'text/html', 'value': html_body})
            if not content:
                logger.error('No email body provided')
                return

            payload = {
                'personalizations': [{'to': [{'email': e} for e in to_list]}],
                'from':    {'email': self.username, 'name': 'NEPM Automation'},
                'subject': subject,
                'content': content,
            }
            if headers:
                payload['headers'] = headers

            resp = requests.post(
                'https://api.sendgrid.com/v3/mail/send',
                headers={
                    'Authorization': f'Bearer {sendgrid_api_key}',
                    'Content-Type':  'application/json',
                },
                json=payload,
                timeout=15,
            )
            if resp.ok:
                logger.info(f'Email sent via SendGrid → {to_list}: {subject}')
            else:
                logger.error(f'SendGrid error {resp.status_code}: {resp.text}')
        except Exception as e:
            logger.error(f'Failed to send email: {e}')

    def send_reply(self, to: str, subject: str, body: str,
                   in_reply_to: str = '', sendgrid_api_key: str = ''):
        """Send a reply email via SendGrid HTTP API.

        Sets In-Reply-To/References to `in_reply_to` (the Message-ID of the
        original job email) so that when George replies to this question, her
        reply carries the original Message-ID in its References chain — which is
        how process_job_emails matches the reply back to the pending job.
        """
        reply_subject = subject if subject.lower().startswith('re:') else f'Re: {subject}'
        headers = None
        if in_reply_to:
            headers = {'In-Reply-To': in_reply_to, 'References': in_reply_to}
        self.send_email(
            to_list           = [to],
            subject           = reply_subject,
            body              = body,
            sendgrid_api_key  = sendgrid_api_key,
            headers           = headers,
        )
