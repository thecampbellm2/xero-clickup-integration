# NEPM Job Automation — ClickUp ↔ Xero ↔ Gmail

Automates the full job intake and invoicing pipeline for National Estimation & Project Management.
George forwards a job email → job lands in ClickUp fully populated → invoices batched and sent to Xero at 3pm daily.

---

## How it works

### Job intake (runs every 3 minutes)
1. George forwards a job email to **nepmclickup@gmail.com** with key details anywhere in the subject or body (job name, client, hours, rate)
2. Claude API parses the email and extracts structured data
3. Client name is matched against the ClickUp dropdown — first by Claude, then by Python fuzzy matching as a fallback
4. A ClickUp task is created with all fields pre-filled
5. If all fields are present, status is automatically set to `Deposit Invoice Pending`
6. If the client can't be matched or fields are missing, task lands at `Not Started` and a notification email is sent to Mike and George
7. Email attachments and body are uploaded to the ClickUp task
8. Email is marked as processed so it won't be picked up again

### Daily invoice batch (runs at 3pm Sydney time)
1. Script finds all tasks in `Deposit Invoice Pending` or `Final Invoice Pending`
2. Groups them by client — one invoice per client regardless of how many jobs
3. Each job becomes a line item: Quantity = hours, Unit price = hourly rate, Due date = today
4. Xero invoice number written back to all ClickUp tasks
5. All tasks flipped to `Deposit Invoiced` or `Final Invoiced`
6. Can be triggered manually any time at `/batch/invoices`

### Manual override (urgent invoices)
- Move a task to `Send Deposit Invoice` → single invoice created immediately in Xero
- Move a task to `Send Final Invoice` → single final invoice created immediately in Xero

### Payment tracking
When George reconciles and marks an invoice as paid in Xero, the webhook updates `Deposit Invoice Status` or `Final Invoice Status` in ClickUp to `Paid` automatically.

### New contact sync
When a new contact is created in Xero, a notification email is sent to Mike and George with instructions to manually add the contact to the ClickUp `Client` dropdown. Note: ClickUp's API does not support adding dropdown options programmatically.

### Error notifications
All errors and action-required events send an immediate email to mike@nationalestimation.com.au and georgina@nationalestimation.com.au from nepmclickup@gmail.com, including:
- Invoice creation failures
- Batch invoice failures
- Email parsing failures (job email could not be processed)
- New Xero contact needs manual ClickUp action

---

## ClickUp status flow

| Status | Set by | Meaning |
|---|---|---|
| `Not Started` | Script / manual | Job received — missing fields, needs review |
| `Deposit Invoice Pending` | Script (auto) | Queued for 3pm batch |
| `Send Deposit Invoice` | George (manual) | Urgent — fires immediately via webhook |
| `Deposit Invoiced` | Script (auto) | Draft deposit invoice in Xero |
| `Measuring` | George (manual) | Job in progress |
| `Submission` | George (manual) | Submission prepared, Total Hours Invoiced entered |
| `Final Invoice Pending` | George (manual) | Queued for 3pm batch |
| `Send Final Invoice` | George (manual) | Urgent — fires immediately via webhook |
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
| `NOTIFICATION_EMAILS` | Comma-separated list — defaults to mike@ and georgina@nationalestimation.com.au |

---

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check (also pinged by cron-job.org every 10 mins to keep service awake) |
| `/xero/auth` | GET | Start Xero OAuth — open in browser |
| `/xero/callback` | GET | Xero OAuth callback |
| `/xero/info` | GET | List branding themes + connected org |
| `/gmail/auth` | GET | Start Gmail OAuth — sign in as nepmclickup@gmail.com |
| `/gmail/callback` | GET | Gmail OAuth callback |
| `/gmail/process` | GET | Manually trigger email poll |
| `/batch/invoices` | GET | Manually trigger the invoice batch |
| `/webhooks/clickup` | POST | Receives ClickUp status change events |
| `/webhooks/xero` | POST | Receives Xero payment + new contact notifications |

---

## Token persistence

Tokens for Xero and Gmail are stored in a private GitHub Gist (`tokens.json`). On startup the script loads from the Gist if no local file exists — tokens survive Render spin-downs and re-deploys automatically. **No manual re-auth needed after deploys.**

The only time manual re-auth is required:
- Access is explicitly revoked
- A new OAuth scope is added (e.g. adding `gmail.send` required a one-time re-auth)

To re-auth:
- **Xero:** visit `/xero/auth`
- **Gmail:** visit `/gmail/auth` — sign in as nepmclickup@gmail.com

---

## Keeping the service awake

Render's free tier spins down after 15 minutes without an HTTP request. A cron job at **cron-job.org** pings `https://xero-clickup-integration.onrender.com/` every 10 minutes to keep the service permanently awake. Without this, the 3pm batch would fail on quiet days.

---

## Invoice format

**Batch deposit line item:**
`Deposit – Job Name – 4.0hrs (50% of assumed 8.0hrs)` | Qty: 4.0 | Unit: $80.00

**Batch final line item:**
`Final – Job Name – 3.0hrs (7.0hrs total less 4.0hrs deposit)` | Qty: 3.0 | Unit: $80.00

**Manual single invoice:**
Same format but one line item per invoice.

---

## Client name matching

When parsing George's emails, client matching works in three stages:
1. **Claude API** — matches even informal/abbreviated names (e.g. "Finnbarr construction" → "Finnbarr Construction Pty Ltd")
2. **Python fuzzy match** — if Claude returns empty, `difflib` searches the full email text against all dropdown options
3. **Manual fallback** — if both fail, task lands at `Not Started` and a notification email is sent

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Job not appearing in ClickUp | Email not unread / already processed | Check nepmclickup inbox; remove NEPM-Processed label and mark unread to reprocess (will create a new task) |
| Client field blank | Neither Claude nor fuzzy match found a match | Check notification email; set client manually and move to `Send Deposit Invoice` |
| No invoice at 3pm | Task missing required fields, or service was asleep | Check Render logs; fix missing fields and trigger `/batch/invoices` manually |
| `Xero contact not found` | Client dropdown name doesn't exactly match Xero contact | Check spelling in both places |
| `Time estimate is 0` | No time estimate on task | George adds estimate; move status away and back to re-trigger |
| `Total Hours Invoiced is empty` | George hasn't entered before moving to Final Invoice Pending | George fills in field first |
| Duplicate tasks in ClickUp | George forwarded the same email twice | Delete the duplicate task |
| Webhook suspended in ClickUp | Too many failed deliveries during downtime | Reactivate: `PUT /api/v2/webhook/{id}` with `{"status":"active"}` |
| Invoice shows `amount is not defined` | Old code still deployed | Upload latest main.py and redeploy |
| No notification emails received | Gmail re-auth needed after gmail.send scope was added | Visit `/gmail/auth` and re-authenticate |
| Service sleeping at 3pm | cron-job.org ping not set up | Set up 10-minute ping to `/` at cron-job.org |
