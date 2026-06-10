import os
from dotenv import load_dotenv

load_dotenv()

# ClickUp
CLICKUP_API_TOKEN     = os.environ.get('CLICKUP_API_TOKEN')
CLICKUP_LIST_ID       = os.environ.get('CLICKUP_LIST_ID', '901614096713')
CLICKUP_WEBHOOK_SECRET = os.environ.get('CLICKUP_WEBHOOK_SECRET', '')

# Xero OAuth
XERO_CLIENT_ID        = os.environ.get('XERO_CLIENT_ID')
XERO_CLIENT_SECRET    = os.environ.get('XERO_CLIENT_SECRET')
XERO_REDIRECT_URI     = os.environ.get('XERO_REDIRECT_URI')         # e.g. https://your-app.onrender.com/xero/callback
XERO_WEBHOOK_KEY      = os.environ.get('XERO_WEBHOOK_KEY', '')       # from Xero developer portal after webhook setup

# Xero invoice settings
XERO_ACCOUNT_CODE     = os.environ.get('XERO_ACCOUNT_CODE', '200')
XERO_BRANDING_THEME_ID = os.environ.get('XERO_BRANDING_THEME_ID', '')  # fill after running /xero/info
XERO_TAX_TYPE         = os.environ.get('XERO_TAX_TYPE', 'OUTPUT2')  # OUTPUT2 = GST on income (Australia)
XERO_LINE_AMOUNT_TYPE = os.environ.get('XERO_LINE_AMOUNT_TYPE', 'EXCLUSIVE')  # prices are ex-GST

# Gmail (IMAP/SMTP — App Password, no OAuth)
GMAIL_USERNAME    = os.environ.get('GMAIL_USERNAME', 'nepmclickup@gmail.com')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')

# Anthropic (for email parsing)
ANTHROPIC_API_KEY   = os.environ.get('ANTHROPIC_API_KEY')

# GitHub Gist (persistent token storage)
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
TOKEN_GIST_ID = os.environ.get('TOKEN_GIST_ID', '')

# Error notifications
NOTIFICATION_EMAILS = [e.strip() for e in os.environ.get('NOTIFICATION_EMAILS', 'mike@nationalestimation.com.au,georgina@nationalestimation.com.au').split(',')]

# SendGrid (transactional email via HTTP — avoids Render SMTP port blocking)
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
