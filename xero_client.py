import requests
import json
import os
import hmac
import hashlib
import base64
import logging
import secrets
import time
from urllib.parse import urlencode

import config
from token_store import load_tokens, save_tokens
logger = logging.getLogger(__name__)

AUTH_URL        = 'https://login.xero.com/identity/connect/authorize'
TOKEN_URL       = 'https://identity.xero.com/connect/token'
CONNECTIONS_URL = 'https://api.xero.com/connections'
API_BASE        = 'https://api.xero.com/api.xro/2.0'
TOKEN_FILE      = 'xero_tokens.json'

SCOPES = ' '.join([
    'accounting.invoices',      # create/read invoices
    'accounting.contacts',      # read/write contacts
    'accounting.settings.read', # branding themes
    'offline_access',           # refresh tokens
])


class XeroClient:
    def __init__(self, client_id, client_secret, redirect_uri, webhook_key=''):
        self.client_id     = client_id
        self.client_secret = client_secret
        self.redirect_uri  = redirect_uri
        self.webhook_key   = webhook_key
        self._tokens       = self._load_tokens()

    # ------------------------------------------------------------------ #
    #  OAuth flow                                                          #
    # ------------------------------------------------------------------ #

    def get_auth_url(self) -> str:
        """Return the Xero OAuth authorisation URL. Visit this in a browser."""
        state  = secrets.token_urlsafe(16)
        params = {
            'response_type': 'code',
            'client_id':     self.client_id,
            'redirect_uri':  self.redirect_uri,
            'scope':         SCOPES,
            'state':         state,
        }
        return f'{AUTH_URL}?{urlencode(params)}'

    def exchange_code(self, code: str):
        """Exchange an auth code for tokens and store them."""
        resp = requests.post(TOKEN_URL, data={
            'grant_type':   'authorization_code',
            'code':         code,
            'redirect_uri': self.redirect_uri,
        }, auth=(self.client_id, self.client_secret))
        resp.raise_for_status()
        tokens = resp.json()
        tokens['expires_at'] = time.time() + tokens.get('expires_in', 1800) - 60

        # Grab the tenant (organisation) ID
        connections = requests.get(
            CONNECTIONS_URL,
            headers={'Authorization': f'Bearer {tokens["access_token"]}'}
        )
        connections.raise_for_status()
        orgs = connections.json()
        if not orgs:
            raise Exception('No Xero organisations found — make sure you connected an org during login.')
        tokens['tenant_id']   = orgs[0]['tenantId']
        tokens['tenant_name'] = orgs[0]['tenantName']
        self._save_tokens(tokens)
        logger.info(f'Xero connected to: {tokens["tenant_name"]}')

    # ------------------------------------------------------------------ #
    #  Token management                                                    #
    # ------------------------------------------------------------------ #

    def _load_tokens(self) -> dict:
        return load_tokens(TOKEN_FILE, 'xero', config.GITHUB_TOKEN, config.TOKEN_GIST_ID)

    def _save_tokens(self, tokens: dict):
        self._tokens = tokens
        save_tokens(TOKEN_FILE, 'xero', tokens, config.GITHUB_TOKEN, config.TOKEN_GIST_ID)

    def _get_access_token(self) -> str:
        if not self._tokens:
            raise Exception('Xero not authenticated. Visit /xero/auth to connect.')

        # Refresh if expired (or close to it)
        if time.time() >= self._tokens.get('expires_at', 0):
            logger.info('Access token expired — refreshing')
            resp = requests.post(TOKEN_URL, data={
                'grant_type':    'refresh_token',
                'refresh_token': self._tokens['refresh_token'],
            }, auth=(self.client_id, self.client_secret))
            resp.raise_for_status()
            new = resp.json()
            new['expires_at'] = time.time() + new.get('expires_in', 1800) - 60
            new['tenant_id']  = self._tokens['tenant_id']
            new['tenant_name'] = self._tokens['tenant_name']
            self._save_tokens(new)

        return self._tokens['access_token']

    def _headers(self) -> dict:
        return {
            'Authorization':  f'Bearer {self._get_access_token()}',
            'Xero-tenant-id': self._tokens['tenant_id'],
            'Accept':         'application/json',
            'Content-Type':   'application/json',
        }

    # ------------------------------------------------------------------ #
    #  Contacts                                                            #
    # ------------------------------------------------------------------ #

    def get_contact_id(self, name: str) -> str:
        """
        Return the ContactID for a Xero contact matching `name`.
        Raises if not found — contacts should be pre-created in Xero.
        """
        resp = requests.get(
            f'{API_BASE}/Contacts',
            headers=self._headers(),
            params={'where': f'Name=="{name}"', 'summaryOnly': 'true'}
        )
        resp.raise_for_status()
        contacts = resp.json().get('Contacts', [])
        if not contacts:
            raise Exception(
                f'Xero contact "{name}" not found. '
                f'Create it in Xero first (must match the ClickUp Client dropdown exactly).'
            )
        return contacts[0]['ContactID']

    # ------------------------------------------------------------------ #
    #  Invoices                                                            #
    # ------------------------------------------------------------------ #

    def create_invoice(
        self,
        contact_id: str,
        line_items: list,
        due_date: str,
        reference: str,
        branding_theme_id: str = '',
        credit_note: bool = False,
    ) -> dict:
        """
        Create a DRAFT invoice or credit note with one or more line items.
        Set credit_note=True to create an ACCRECCREDIT instead of ACCREC.

        Each item in line_items is a dict:
          { description, quantity, unit_amount, account_code }
        """
        doc = {
            'Status':    'DRAFT',
            'Contact':   {'ContactID': contact_id},
            'Reference': reference,
            'LineItems': [
                {
                    'Description': item['description'],
                    'Quantity':    round(float(item['quantity']), 2),
                    'UnitAmount':  round(float(item['unit_amount']), 2),
                    'AccountCode': item['account_code'],
                }
                for item in line_items
            ],
        }
        if branding_theme_id:
            doc['BrandingThemeID'] = branding_theme_id

        if credit_note:
            # Credit notes use a different endpoint and Date instead of DueDate
            doc['Date'] = due_date
            resp = requests.post(
                f'{API_BASE}/CreditNotes',
                headers=self._headers(),
                json={'CreditNotes': [doc]}
            )
            if not resp.ok:
                logger.error(f'Xero credit note error {resp.status_code}: {resp.text}')
            resp.raise_for_status()
            return resp.json()['CreditNotes'][0]
        else:
            doc['Type']    = 'ACCREC'
            doc['DueDate'] = due_date
            resp = requests.post(
                f'{API_BASE}/Invoices',
                headers=self._headers(),
                json={'Invoices': [doc]}
            )
            if not resp.ok:
                logger.error(f'Xero invoice error {resp.status_code}: {resp.text}')
            resp.raise_for_status()
            return resp.json()['Invoices'][0]


    def get_contact(self, contact_id: str) -> dict:
        resp = requests.get(f'{API_BASE}/Contacts/{contact_id}', headers=self._headers())
        resp.raise_for_status()
        contacts = resp.json().get('Contacts', [])
        return contacts[0] if contacts else {}

    def get_invoice(self, invoice_id: str) -> dict:
        resp = requests.get(f'{API_BASE}/Invoices/{invoice_id}', headers=self._headers())
        resp.raise_for_status()
        invoices = resp.json().get('Invoices', [])
        return invoices[0] if invoices else {}

    # ------------------------------------------------------------------ #
    #  Info helpers (used by /xero/info endpoint)                         #
    # ------------------------------------------------------------------ #

    def get_branding_themes(self) -> list:
        resp = requests.get(f'{API_BASE}/BrandingThemes', headers=self._headers())
        resp.raise_for_status()
        return resp.json().get('BrandingThemes', [])

    # ------------------------------------------------------------------ #
    #  Webhook verification                                                #
    # ------------------------------------------------------------------ #

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        """Verify the HMAC-SHA256 signature on incoming Xero webhooks."""
        if not self.webhook_key:
            logger.warning('XERO_WEBHOOK_KEY not set — skipping signature verification')
            return True
        computed = base64.b64encode(
            hmac.new(self.webhook_key.encode(), payload, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(computed, signature)

    # ------------------------------------------------------------------ #
    #  Daily summary queries                                               #
    # ------------------------------------------------------------------ #

    def get_todays_documents(self) -> dict:
        """
        Return all automation-created invoices and credit notes from today.
        Returns { 'invoices': [...], 'credit_notes': [...] }
        """
        from datetime import date
        import pytz
        today = date.today()
        date_filter = f'Date=DateTime({today.year},{today.month},{today.day})'

        def fetch(endpoint):
            resp = requests.get(
                f'{API_BASE}/{endpoint}',
                headers=self._headers(),
                params={'where': date_filter, 'order': 'UpdatedDateUTC DESC'}
            )
            resp.raise_for_status()
            key = 'Invoices' if endpoint == 'Invoices' else 'CreditNotes'
            return [
                doc for doc in resp.json().get(key, [])
                if str(doc.get('Reference', '')).startswith('CU-')
            ]

        return {
            'invoices':     fetch('Invoices'),
            'credit_notes': fetch('CreditNotes'),
        }
