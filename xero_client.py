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

logger = logging.getLogger(__name__)

AUTH_URL        = 'https://login.xero.com/identity/connect/authorize'
TOKEN_URL       = 'https://identity.xero.com/connect/token'
CONNECTIONS_URL = 'https://api.xero.com/connections'
API_BASE        = 'https://api.xero.com/api.xro/2.0'
TOKEN_FILE      = 'xero_tokens.json'

SCOPES = ' '.join([
    'accounting.transactions.read',
    'accounting.transactions.create',
    'accounting.contacts.read',
    'accounting.contacts.create',
    'accounting.settings.read',
    'offline_access',
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
        description: str,
        amount: float,
        account_code: str,
        tax_type: str,
        line_amount_type: str,
        reference: str,
        branding_theme_id: str = '',
    ) -> dict:
        """Create a DRAFT sales invoice and return the Xero invoice object."""
        invoice = {
            'Type':    'ACCREC',
            'Status':  'DRAFT',
            'Contact': {'ContactID': contact_id},
            'Reference': reference,          # stores ClickUp task ID + type for payment tracking
            'LineAmountTypes': line_amount_type,
            'LineItems': [{
                'Description': description,
                'Quantity':    1.0,
                'UnitAmount':  round(amount, 2),
                'AccountCode': account_code,
                'TaxType':     tax_type,
            }],
        }
        if branding_theme_id:
            invoice['BrandingThemeID'] = branding_theme_id

        resp = requests.post(
            f'{API_BASE}/Invoices',
            headers=self._headers(),
            json={'Invoices': [invoice]}
        )
        resp.raise_for_status()
        return resp.json()['Invoices'][0]

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
