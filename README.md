# Job Automation — ClickUp ↔ Xero

Automatically creates draft invoices in Xero when George moves a job through ClickUp,
and marks invoices as paid in ClickUp when George reconciles in Xero.

---

## How it works

| ClickUp trigger | Script action | ClickUp result |
|---|---|---|
| Job → `Send Deposit Invoice` | Creates draft deposit invoice in Xero (50% of estimated hours × rate) | Status → `Deposit Invoiced`, Deposit Invoice # filled, Deposit Invoice Status → Invoiced |
| Job → `Send Final Invoice` | Creates draft final invoice in Xero (total hours − deposit hours × rate) | Status → `Final Invoiced`, Final Invoice # filled, Final Invoice Status → Invoiced |
| Invoice marked PAID in Xero | Finds the matching ClickUp job via the invoice Reference field | Deposit/Final Invoice Status → Paid |

---

## ClickUp fields required on every job

| Field | Type | Filled by |
|---|---|---|
| Client | Dropdown | George (must match Xero contact name exactly) |
| Hourly Rate | Currency | George |
| Time Estimate | Native ClickUp field | George |
| Total Hours Invoiced | Number | George (before moving to Send Final Invoice) |
| Deposit Invoice # | Text | Script (auto) |
| Deposit Invoice Status | Dropdown (Invoiced / Paid) | Script (auto) |
| Final Invoice # | Text | Script (auto) |
| Final Invoice Status | Dropdown (Invoiced / Paid) | Script (auto) |

---

## Deployment on Render

### 1 — Push to GitHub
Create a GitHub repo and push this folder to it.

### 2 — Create a Web Service on Render
- New → Web Service → connect your GitHub repo
- **Runtime:** Python 3
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn main:app`

### 3 — Add environment variables on Render
Copy everything from `.env.example` into Render → Environment → Environment Variables.
Fill in the values you have so far (ClickUp token, Xero Client ID/Secret).
Leave the blank ones for now — you'll fill them in the steps below.

### 4 — Update the Xero app Redirect URI
In the Xero Developer Portal → your app → Redirect URIs:
Add `https://your-app.onrender.com/xero/callback`

### 5 — Authorise Xero
Visit `https://your-app.onrender.com/xero/auth` in your browser.
Log in with your Xero credentials and authorise the app.
You'll see "Xero connected successfully!" when done.

### 6 — Get the branding theme ID
Visit `https://your-app.onrender.com/xero/info`
Copy the ID of the branding theme George uses and paste it into
`XERO_BRANDING_THEME_ID` in Render's environment variables.

### 7 — Set up the ClickUp webhook
In ClickUp: Settings → Integrations → Webhooks → New Webhook
- **Endpoint:** `https://your-app.onrender.com/webhooks/clickup`
- **Events:** Task status updated
- **List:** Jobs (901614096713)
Copy the webhook secret into `CLICKUP_WEBHOOK_SECRET` in Render.

### 8 — Set up the Xero webhook (for payment tracking)
In the Xero Developer Portal → your app → Webhooks
- **Delivery URL:** `https://your-app.onrender.com/webhooks/xero`
- Subscribe to: Invoices
Copy the webhook key into `XERO_WEBHOOK_KEY` in Render.

---

## Re-authentication after a new Render deploy
Render's free tier does not persist files across new deployments.
After each new deploy, re-authorise Xero by visiting `/xero/auth` again.
(Normal spin-up/spin-down between deploys does NOT require re-auth.)

---

## Invoice description format

**Deposit:** `Deposit Invoice – Job Name – 4.0hrs (50% of assumed 8.0hrs)`
**Final:**   `Final Invoice – Job Name – 3.0hrs (7.0hrs total less 4.0hrs deposit)`

---

## Troubleshooting
- Check Render logs for detailed error messages
- Common issues:
  - `"Client" field is empty` — George hasn't filled in the Client dropdown
  - `Xero contact not found` — the Client name in ClickUp doesn't exactly match the Xero contact
  - `Time estimate is 0` — no time estimate set on the job
  - `Total Hours Invoiced is empty` — George hasn't entered this before moving to Send Final Invoice
