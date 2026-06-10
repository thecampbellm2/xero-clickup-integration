# NEPM Job Automation — ClickUp ↔ Xero ↔ Gmail

Automates the full job intake and invoicing pipeline for National Estimation & Project Management.
George forwards a job email → job lands in ClickUp fully populated → invoices batched and sent to Xero at 3pm daily → daily summary email at 5:30pm.

---

## How it works

### Job intake (runs every 3 minutes)
1. George forwards a job email to **nepmclickup@gmail.com** with key details anywhere in the subject or body (job name, client, hours, rate)
2. Claude API parses the email — subject line takes priority over body for billing details
3. Client name is matched against the ClickUp dropdown — first by Claude, then by Python fuzzy matching as a fallback
4. **If all required fields are present** (Client, Hourly Rate, Time Estimate):
   - ClickUp task created fully populated
   - Status automatically set to `Deposit Invoice Pending`
   - Email attachments and body uploaded to the task
5. **If any required field is missing**:
   - Automated reply sent to George listing exactly what's missing
   - Job held in a pending queue (stored in GitHub Gist)
   - No ClickUp task created yet
   - George replies with the missing details → task created and invoiced
   - George replies "import as is" / "no idea" → task created with blanks (current behaviour)
   - No reply after **3 hours** → task imported as-is, notification sent to Mike and George

### Daily invoice batch (runs at 3pm Sydney time)
1. Finds all tasks in `Deposit Invoice Pending` or `Final Invoice Pending`
2. Groups by client — one invoice per client regardless of how many jobs
3. Each job = one line item: Quantity = hours, Unit price = hourly rate, Due date = today
4. If total hours < deposit hours → **credit note** created instead of final invoice
5. If total hours = deposit hours exactly → task flipped to Final Invoiced, no document created
6. Xero invoice/credit note number written back to all ClickUp tasks
7. All tasks flipped to `Deposit Invoiced` or `Final Invoiced`
8. Can be triggered manually at `/batch/invoices`

### Manual override (urgent invoices)
- Move task to `Send Deposit Invoice` → single invoice created immediately via webhook
- Move task to `Send Final Invoice` → single final invoice or credit note created immediately

### Payment tracking
When George reconciles and marks an invoice as paid in Xero, the webhook updates `Deposit Invoice Status` or `Final Invoice Status` in ClickUp to `Paid` automatically.

### New contact sync
When a new contact is created in Xero, a notification email is sent to Mike and George with instructions to manually add the contact to the ClickUp `Client` dropdown. ClickUp's API does not support adding dropdown options programmatically.

### Daily summary email (5:30pm Sydney time)
Sent every day to Mike and George from nepmclickup@gmail.com:
- **Active day**: stat cards (new jobs, completed jobs, credit notes, values), revenue breakdown bar, tables of deposits/finals/credits with job name, client, amount, invoice number
- **Quiet day**: friendly "Nothing to see here!" confirming the automation ran correctly
- Can be triggered manually at `/summary/send`

### Error notifications
Immediate email to mike@nationalestimation.com.au and georgina@nationalestimation.com.au for:
- Invoice creation failures
- Batch invoice failures per client
- Email parsing failures
- New Xero contact needs manual ClickUp action
- 3-hour timeout on pending jobs (imported as-is)

---

## ClickUp status flow

| Status | Set by | Meaning |
|---|---|---|
| `Not Started` | Script / manual | Job received — missing fields or manually overridden |
| `Deposit Invoice Pending` | Script (auto) | Queued for 3pm batch |
| `Send Deposit Invoice` | George (manual) | Urgent — fires immediately via webhook |
| `Deposit Invoiced` | Script (auto) | Draft deposit invoice in Xero |
| `Measuring` | George (manual) | Job in progress |
| `Submission` | George (manual) | Submission prepared, Total Hours Invoiced entered |
| `Final Invoice Pending` | George (manual) | Queued for 3pm batch |
| `Send Final Invoice` | George (manual) | Urgent — fires immediately via webhook |
| `Final Invoiced` | Script (auto) | Final invoice or credit note in Xero |
| `Completed` | George (manual) | Job complete |

---

## Invoice logic

| Scenario | Result |
|---|---|
| Total hours > deposit hours | Normal final invoice |
| Total hours = deposit hours | Task → Final Invoiced, no document created |
| Total hours < deposit hours | Credit note for the difference |

**Deposit line item:** `Deposit – Job Name – 4.0hrs (50% of assumed 8.0hrs)` \| Qty: 4.0 \| Unit: $80.00

**Final line item:** `Final – Job Name – 3.0hrs (7.0hrs total less 4.0hrs deposit)` \| Qty: 3.0 \| Unit: $80.00

**Credit note line item:** `Credit – Job Name – 1.0hr overpaid (3.0hrs total, 4.0hrs deposit already invoiced)` \| Qty: 1.0 \| Unit: $80.00

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
| `GMAIL_USERNAME` | `nepmclickup@gmail.com` |
| `GMAIL_APP_PASSWORD` | 16-character App Password (Google Account → Security → App Passwords) |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `GITHUB_TOKEN` | GitHub personal access token (gist scope only) |
| `TOKEN_GIST_ID` | ID of the private GitHub Gist used for token + pending job storage |
| `NOTIFICATION_EMAILS` | Comma-separated — defaults to mike@ and georgina@nationalestimation.com.au |

---

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check (pinged by UptimeRobot every 5 mins) |
| `/xero/auth` | GET | Start Xero OAuth — open in browser |
| `/xero/callback` | GET | Xero OAuth callback |
| `/xero/info` | GET | List branding themes + connected org |
| `/gmail/process` | GET | Manually trigger email poll |
| `/batch/invoices` | GET | Manually trigger the invoice batch |
| `/summary/send` | GET | Manually trigger the daily summary email |
| `/webhooks/clickup` | POST | Receives ClickUp status change events |
| `/webhooks/xero` | POST | Receives Xero payment + new contact notifications |

---

## Gmail — App Password (no OAuth, no expiry)

Gmail uses IMAP/SMTP with an App Password instead of OAuth. This means:
- No 7-day token expiry
- No re-auth ever required
- App Password persists until explicitly revoked

To regenerate: Google Account (nepmclickup@gmail.com) → Security → App Passwords → create new → update `GMAIL_APP_PASSWORD` in Render.

---

## Token persistence (Xero)

Xero OAuth tokens are stored in a private GitHub Gist (`tokens.json`, key: `xero`). The pending jobs queue is also stored in the same Gist (key: `pending_jobs`). On startup the script loads from the Gist if no local file exists — tokens survive Render spin-downs and re-deploys automatically.

The only time Xero re-auth is required:
- Access is explicitly revoked
- Refresh token unused for 60+ days (shouldn't happen with daily batch running)

To re-auth: visit `/xero/auth`

---

## Keeping the service awake

UptimeRobot pings `https://xero-clickup-integration.onrender.com/` every 5 minutes via HTTP(s) monitor (not PING — must be HTTP(s) to keep Render awake). Without this, Render's free tier spins down after 15 minutes of inactivity and the 3pm batch would be missed.

---

## Pending jobs queue

When a job email is missing required fields (Client, Hourly Rate, or Time Estimate):
1. Automated reply sent to George asking for the missing details
2. Partial job stored in Gist under `pending_jobs` key
3. Email marked as processed
4. No ClickUp task created yet

When George replies:
- Provides details → task created fully populated
- "Import as is" / "no idea" / "not sure yet" → task created with available data
- No reply within 3 hours → imported as-is, notification sent to Mike and George

---

## Client name matching

Three-stage matching when parsing emails:
1. **Claude API** — matches informal/abbreviated names (e.g. "Finnbarr construction" → "Finnbarr Construction Pty Ltd"). Subject line takes priority over body for billing details.
2. **Python fuzzy match** — if Claude returns empty, `difflib` searches the full email text
3. **Pending queue** — if still no match, job is held pending George's reply

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Job not appearing in ClickUp | Email held in pending queue | Check nepmclickup@gmail.com for automated question; reply with missing details |
| Job stuck in pending queue | George hasn't replied | Imports automatically after 3 hours, or reply to the question email |
| Client field blank on task | Reply said "import as is" | Fill in manually and move to `Send Deposit Invoice` |
| No invoice at 3pm | Batch failed or task missing fields | Check Render logs; fix fields; trigger `/batch/invoices` |
| `Xero contact not found` | Client dropdown name doesn't exactly match Xero contact | Check spelling in both places |
| Credit note instead of invoice | Total hours < deposit hours | Expected behaviour — correct accounting |
| Duplicate tasks | George forwarded same email twice | Delete the duplicate task in ClickUp |
| Webhook suspended in ClickUp | Too many failed deliveries | Reactivate: `PUT /api/v2/webhook/{id}` with `{"status":"active"}` |
| No daily summary email | Xero auth expired or Gmail issue | Check Render logs; re-auth Xero at `/xero/auth` if needed |
| Service sleeping at 3pm | UptimeRobot set as PING not HTTP(s) | Edit monitor → change type to HTTP(s) |
| Xero auth expired | Refresh token unused 60+ days | Visit `/xero/auth` to re-authenticate |
