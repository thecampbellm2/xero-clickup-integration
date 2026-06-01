# NEPM Job Automation — ClickUp ↔ Xero ↔ Gmail

Automates the full job intake and invoicing pipeline for National Estimation & Project Management.
George forwards a job email → job lands in ClickUp fully populated → invoices batched and sent to Xero at 3pm daily.

---

## How it works

### Job intake (runs every 3 minutes)
1. George forwards a job email to **nepmclickup@gmail.com** with key details in plain English (job name, client, hours, rate — anywhere in subject or body)
2. Claude API parses the email and extracts structured data
3. A ClickUp task is created with all fields pre-filled
4. Status is set to `Deposit Invoice Pending` automatically
5. Email is marked as processed so it won't be picked up again

### Daily invoice batch (runs at 3pm Sydney time)
1. Script finds all tasks in `Deposit Invoice Pending` or `Final Invoice Pending`
2. Groups them by client — one invoice per client regardless of how many jobs
3. Each job becomes a line item: Quantity = hours, Unit price = hourly rate
4. Due date = same day
5. Xero invoice number written back to all ClickUp tasks
6. All tasks flipped to `Deposit Invoiced` or `Final Invoiced`

### Manual override (urgent invoices)
- Move a task to `Send Deposit Invoice` → single invoice created immediately in Xero
- Move a task to `Send Final Invoice` → single final invoice created immediately in Xero
- These bypass the 3pm batch and fire straight away via webhook

### Payment tracking
When George reconciles and marks an invoice as paid in Xero, the Xero webhook fires and automatically updates the `Deposit Invoice Status` or `Final Invoice Status` field in ClickUp to `Paid`.

### New contact sync
When a new contact is created in Xero, it's automatically added to the `Client` dropdown in ClickUp — no manual entry needed.

---

## ClickUp status flow

| Status | Set by | Meaning |
|---|---|---|
| `Not Started` | Email-to-task / manual | Job received, awaiting details |
| `Deposit Invoice Pending` | Script (auto) | Queued for 3pm batch |
| `Send Deposit Invoice` | George (manual) | Urgent — fires immediately |
| `Deposit Invoiced` | Script (auto) | Draft invoice in Xero |
| `Measuring` | George (manual) | Job in progress |
| `Submission` | George (manual) | Submission prepared, hours entered |
| `Final Invoice Pending` | George (manual) | Queued for 3pm batch |
| `Send Final Invoice` | George (manual) | Urgent — fires immediately |
| `Final Invoiced` | Script (auto) | Final invoice in Xero |
| `Completed` | George (manual) | Job complete |

---

## ClickUp custom fields

| Field | Type | Filled by |
|---|---|---|
| Client | Dropdown | Script (from email) / George |
| Hourly Rate | Currency | Script (from email) / George |
| Time Estimate | Native ClickUp field | Script (from email) / George |
| Total Hours Invoiced | Number | George (before Final Invoice Pending) |
| Deposit Invoice # | Text | Script (auto) |
| Deposit Invoice Status | Dropdown (Invoiced / Paid) | Script (auto) |
| Final Invoice # | Text | Script (auto) |
| Final Invoice Status | Dropdown (Invoiced / Paid) | Script (auto) |

---

## Environment variables (Render)

| Variable | Description |
|---|---|
| `CLICKUP_API_TOKEN` | ClickUp API token (Settings → Apps → API Token) |
| `CLICKUP_LIST_ID` | Jobs list ID — `901614096713` |
| `CLICKUP_WEBHOOK_SECRET` | From ClickUp webhook setup |
| `XERO_CLIENT_ID` | From Xero Developer Portal → Configuration |
| `XERO_CLIENT_SECRET` | From Xero Developer Portal → Configuration |
| `XERO_REDIRECT_URI` | `https://xero-clickup-integration.onrender.com/xero/callback` |
| `XERO_ACCOUNT_CODE` | `200` (Sales) |
| `XERO_BRANDING_THEME_ID` | From `/xero/info` endpoint after auth |
| `XERO_WEBHOOK_KEY` | From Xero Developer Portal → Webhooks |
| `GMAIL_CLIENT_ID` | From Google Cloud Console → Credentials |
| `GMAIL_CLIENT_SECRET` | From Google Cloud Console → Credentials |
| `GMAIL_REDIRECT_URI` | `https://xero-clickup-integration.onrender.com/gmail/callback` |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `GITHUB_TOKEN` | GitHub personal access token (gist scope only) |
| `TOKEN_GIST_ID` | ID of the private GitHub Gist used for token persistence |

---

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check |
| `/xero/auth` | GET | Start Xero OAuth — open in browser |
| `/xero/callback` | GET | Xero OAuth callback (set in Xero app) |
| `/xero/info` | GET | List branding themes + connected org |
| `/gmail/auth` | GET | Start Gmail OAuth — open in browser, sign in as nepmclickup@gmail.com |
| `/gmail/callback` | GET | Gmail OAuth callback (set in Google Cloud) |
| `/gmail/process` | GET | Manually trigger email poll |
| `/batch/invoices` | GET | Manually trigger the invoice batch (normally runs at 3pm) |
| `/webhooks/clickup` | POST | Receives ClickUp status change events |
| `/webhooks/xero` | POST | Receives Xero payment notifications |

---

## Token persistence

Tokens for Xero and Gmail are stored in a private GitHub Gist (`tokens.json`).
On startup the script loads from the Gist if no local file exists — this means tokens survive Render spin-downs and re-deploys automatically. **No manual re-auth needed after deploys.**

The only time manual re-auth is required is if access is explicitly revoked.

To re-auth:
- **Xero:** visit `/xero/auth`
- **Gmail:** visit `/gmail/auth` — sign in as nepmclickup@gmail.com

---

## Invoice format

**Batch deposit line item:**
`Deposit – Job Name – 4.0hrs (50% of assumed 8.0hrs)` | Qty: 4.0 | Unit: $80.00

**Batch final line item:**
`Final – Job Name – 3.0hrs (7.0hrs total less 4.0hrs deposit)` | Qty: 3.0 | Unit: $80.00

**Manual single invoice:**
Same format but one line item per invoice.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Job not appearing in ClickUp | Email not unread / already processed | Check nepmclickup inbox; remove NEPM-Processed label and mark unread to reprocess |
| Client field blank | Claude couldn't match client name | George fills in manually; consider adding the client to Xero so it syncs |
| No invoice at 3pm | Task missing required fields | Check Render logs for warning messages; fix missing fields and trigger `/batch/invoices` manually |
| `Xero contact not found` | Client dropdown name doesn't exactly match Xero contact | Check spelling in both places |
| `Time estimate is 0` | No time estimate on task | George adds estimate; re-trigger by moving status away and back |
| `Total Hours Invoiced is empty` | George hasn't entered before moving to Final Invoice Pending | George fills in field first |
| Duplicate tasks in ClickUp | George forwarded the same email twice | Delete the duplicate task |
| Webhook suspended in ClickUp | Too many failed deliveries during downtime | Reactivate via API: `PUT /api/v2/webhook/{id}` with `{"status":"active"}` |
| Auth errors after deploy | Gist token not loading | Check `GITHUB_TOKEN` and `TOKEN_GIST_ID` env vars; visit `/xero/auth` and `/gmail/auth` to re-auth |
