import requests
import json
import os
import time
import base64
import logging
from urllib.parse import urlencode
import secrets

logger = logging.getLogger(__name__)

AUTH_URL     = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL    = 'https://oauth2.googleapis.com/token'
API_BASE     = 'https://gmail.googleapis.com/gmail/v1/users/me'
TOKEN_FILE   = 'gmail_tokens.json'
PROCESSED_LABEL = 'NEPM-Processed'

SCOPES = ' '.join([
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',   # needed to mark as read / add labels
])


class GmailClient:
    def __init__(self, client_id, client_secret, redirect_uri):
        self.client_id     = client_id
        self.client_secret = client_secret
        self.redirect_uri  = redirect_uri
        self._tokens       = self._load_tokens()
        self._label_id     = None   # cached label ID for 'NEPM-Processed'

    # ------------------------------------------------------------------ #
    #  OAuth flow                                                          #
    # ------------------------------------------------------------------ #

    def get_auth_url(self) -> str:
        params = {
            'response_type':  'code',
            'client_id':      self.client_id,
            'redirect_uri':   self.redirect_uri,
            'scope':          SCOPES,
            'access_type':    'offline',   # needed for refresh token
            'prompt':         'consent',   # forces refresh token on every auth
            'state':          secrets.token_urlsafe(16),
        }
        return f'{AUTH_URL}?{urlencode(params)}'

    def exchange_code(self, code: str):
        resp = requests.post(TOKEN_URL, data={
            'grant_type':   'authorization_code',
            'code':         code,
            'redirect_uri': self.redirect_uri,
            'client_id':    self.client_id,
            'client_secret': self.client_secret,
        })
        resp.raise_for_status()
        tokens = resp.json()
        tokens['expires_at'] = time.time() + tokens.get('expires_in', 3600) - 60
        self._save_tokens(tokens)
        logger.info('Gmail connected successfully')

    # ------------------------------------------------------------------ #
    #  Token management                                                    #
    # ------------------------------------------------------------------ #

    def _load_tokens(self) -> dict:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                return json.load(f)
        return {}

    def _save_tokens(self, tokens: dict):
        self._tokens = tokens
        with open(TOKEN_FILE, 'w') as f:
            json.dump(tokens, f, indent=2)

    def _get_access_token(self) -> str:
        if not self._tokens:
            raise Exception('Gmail not authenticated. Visit /gmail/auth to connect.')

        if time.time() >= self._tokens.get('expires_at', 0):
            logger.info('Gmail token expired — refreshing')
            resp = requests.post(TOKEN_URL, data={
                'grant_type':    'refresh_token',
                'refresh_token': self._tokens['refresh_token'],
                'client_id':     self.client_id,
                'client_secret': self.client_secret,
            })
            resp.raise_for_status()
            new = resp.json()
            new['expires_at']    = time.time() + new.get('expires_in', 3600) - 60
            new['refresh_token'] = self._tokens['refresh_token']   # preserve refresh token
            self._save_tokens(new)

        return self._tokens['access_token']

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._get_access_token()}'}

    # ------------------------------------------------------------------ #
    #  Label management                                                    #
    # ------------------------------------------------------------------ #

    def _get_or_create_label(self) -> str:
        """Get or create the NEPM-Processed label, return its ID."""
        if self._label_id:
            return self._label_id

        # List existing labels
        resp = requests.get(f'{API_BASE}/labels', headers=self._headers())
        resp.raise_for_status()
        for label in resp.json().get('labels', []):
            if label['name'] == PROCESSED_LABEL:
                self._label_id = label['id']
                return self._label_id

        # Create it
        resp = requests.post(
            f'{API_BASE}/labels',
            headers=self._headers(),
            json={'name': PROCESSED_LABEL, 'labelListVisibility': 'labelHide', 'messageListVisibility': 'hide'}
        )
        resp.raise_for_status()
        self._label_id = resp.json()['id']
        logger.info(f'Created Gmail label: {PROCESSED_LABEL}')
        return self._label_id

    # ------------------------------------------------------------------ #
    #  Reading emails                                                      #
    # ------------------------------------------------------------------ #

    def get_unprocessed_emails(self) -> list:
        """
        Return unread emails in the inbox that haven't been processed yet.
        Excludes anything already labelled NEPM-Processed.
        """
        label_id = self._get_or_create_label()
        query    = f'is:unread in:inbox -label:{PROCESSED_LABEL}'

        resp = requests.get(
            f'{API_BASE}/messages',
            headers=self._headers(),
            params={'q': query, 'maxResults': 10}
        )
        resp.raise_for_status()
        messages = resp.json().get('messages', [])

        emails = []
        for msg in messages:
            detail = self._get_message(msg['id'])
            if detail:
                emails.append(detail)
        return emails

    def _get_message(self, message_id: str) -> dict:
        resp = requests.get(
            f'{API_BASE}/messages/{message_id}',
            headers=self._headers(),
            params={'format': 'full'}
        )
        resp.raise_for_status()
        msg = resp.json()

        headers     = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
        subject     = headers.get('Subject', '')
        sender      = headers.get('From', '')
        body        = self._extract_body(msg.get('payload', {}))
        attachments = self._extract_attachments(msg.get('payload', {}))

        return {
            'id':          message_id,
            'subject':     subject,
            'sender':      sender,
            'body':        body,
            'attachments': attachments,   # list of {attachment_id, filename, mime_type}
        }

    def _extract_attachments(self, payload: dict) -> list:
        """Return a list of attachment metadata from the message payload."""
        attachments = []
        for part in payload.get('parts', []):
            filename      = part.get('filename', '')
            attachment_id = part.get('body', {}).get('attachmentId')
            mime_type     = part.get('mimeType', 'application/octet-stream')
            if filename and attachment_id:
                attachments.append({
                    'attachment_id': attachment_id,
                    'filename':      filename,
                    'mime_type':     mime_type,
                })
            # Recurse into nested multipart
            if part.get('parts'):
                attachments.extend(self._extract_attachments(part))
        return attachments

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download and return the raw bytes of a Gmail attachment."""
        resp = requests.get(
            f'{API_BASE}/messages/{message_id}/attachments/{attachment_id}',
            headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json().get('data', '')
        return base64.urlsafe_b64decode(data)

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from a Gmail message payload."""
        # Direct body
        if payload.get('body', {}).get('data'):
            return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

        # Multipart — look for text/plain first
        for part in payload.get('parts', []):
            if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')

        # Fallback: any part with data
        for part in payload.get('parts', []):
            if part.get('body', {}).get('data'):
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')

        return ''

    # ------------------------------------------------------------------ #
    #  Marking emails as processed                                         #
    # ------------------------------------------------------------------ #

    def mark_processed(self, message_id: str):
        """Mark an email as read and add the NEPM-Processed label."""
        label_id = self._get_or_create_label()
        resp = requests.post(
            f'{API_BASE}/messages/{message_id}/modify',
            headers=self._headers(),
            json={
                'addLabelIds':    [label_id],
                'removeLabelIds': ['UNREAD'],
            }
        )
        resp.raise_for_status()
        logger.info(f'Message {message_id} marked as processed')

    def is_authenticated(self) -> bool:
        return bool(self._tokens)
