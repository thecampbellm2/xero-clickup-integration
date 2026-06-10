"""
Builds and sends the NEPM daily summary email at 5:30pm Sydney time.
Queries Xero for today's automation-created invoices and credit notes.
"""
import logging
import re
from datetime import date
import pytz

logger = logging.getLogger(__name__)
SYDNEY = pytz.timezone('Australia/Sydney')


def _parse_job_name(description: str) -> str:
    """Extract job name from a Xero line item description."""
    m = re.match(r'^(?:Deposit|Final(?: Invoice)?|Credit)\s*[\u2013-]\s*(.+?)\s*[\u2013-]', description)
    return m.group(1).strip() if m else description[:50]


def _parse_type(reference: str) -> str:
    """Extract invoice type from reference e.g. CU-abc123-deposit → deposit"""
    parts = reference.split('-')
    return parts[-1] if parts else 'unknown'


def _dollars(amount) -> str:
    return f'${float(amount):,.2f}'


def build_summary(xero) -> dict:
    """Fetch today's documents from Xero and return structured summary data."""
    docs = xero.get_todays_documents()

    deposits = []
    finals   = []
    credits  = []

    for inv in docs['invoices']:
        ref      = inv.get('Reference', '')
        inv_type = _parse_type(ref)
        client   = inv.get('Contact', {}).get('Name', 'Unknown')
        subtotal = float(inv.get('SubTotal', 0))
        number   = inv.get('InvoiceNumber', '')
        desc     = inv.get('LineItems', [{}])[0].get('Description', '') if inv.get('LineItems') else ''
        job      = _parse_job_name(desc)

        row = {'client': client, 'job': job, 'amount': subtotal, 'number': number}
        if inv_type == 'deposit':
            deposits.append(row)
        else:
            finals.append(row)

    for cn in docs['credit_notes']:
        client   = cn.get('Contact', {}).get('Name', 'Unknown')
        subtotal = float(cn.get('SubTotal', 0))
        number   = cn.get('CreditNoteNumber', '')
        desc     = cn.get('LineItems', [{}])[0].get('Description', '') if cn.get('LineItems') else ''
        job      = _parse_job_name(desc)
        credits.append({'client': client, 'job': job, 'amount': subtotal, 'number': number})

    total_in    = sum(r['amount'] for r in deposits)
    total_out   = sum(r['amount'] for r in finals)
    total_cr    = sum(r['amount'] for r in credits)
    net_revenue = total_in + total_out - total_cr

    return {
        'deposits':    deposits,
        'finals':      finals,
        'credits':     credits,
        'total_in':    total_in,
        'total_out':   total_out,
        'total_cr':    total_cr,
        'net_revenue': net_revenue,
        'has_activity': bool(deposits or finals or credits),
    }


def build_html(summary: dict, today_str: str) -> str:
    """Build the HTML email body."""

    def stat_card(value, label, color):
        return f'''
        <td style="width:33%;padding:8px;">
          <div style="background:white;border-radius:8px;padding:16px 12px;text-align:center;
                      border-top:4px solid {color};box-shadow:0 1px 4px rgba(0,0,0,0.08);">
            <div style="font-size:26px;font-weight:bold;color:{color};line-height:1.2;">{value}</div>
            <div style="font-size:11px;color:#888;margin-top:4px;text-transform:uppercase;
                        letter-spacing:0.5px;">{label}</div>
          </div>
        </td>'''

    def section_table(rows, color, type_label):
        if not rows:
            return ''
        header_bg = color
        rows_html = ''
        for i, r in enumerate(rows):
            bg = '#f9f9f9' if i % 2 == 0 else 'white'
            rows_html += f'''
            <tr style="background:{bg};">
              <td style="padding:10px 12px;font-size:13px;color:#333;">{r["job"]}</td>
              <td style="padding:10px 12px;font-size:13px;color:#555;">{r["client"]}</td>
              <td style="padding:10px 12px;font-size:13px;color:#333;font-weight:bold;
                         text-align:right;">{_dollars(r["amount"])}</td>
              <td style="padding:10px 12px;font-size:11px;color:#999;text-align:right;">{r["number"]}</td>
            </tr>'''

        return f'''
        <tr><td style="padding:8px 16px 0;">
          <div style="font-size:13px;font-weight:bold;color:{color};text-transform:uppercase;
                      letter-spacing:0.8px;padding:8px 0 4px;">{type_label}</div>
          <table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden;">
            <tr style="background:{header_bg};">
              <th style="padding:8px 12px;font-size:11px;color:white;text-align:left;font-weight:600;">JOB</th>
              <th style="padding:8px 12px;font-size:11px;color:white;text-align:left;font-weight:600;">CLIENT</th>
              <th style="padding:8px 12px;font-size:11px;color:white;text-align:right;font-weight:600;">AMOUNT (EX GST)</th>
              <th style="padding:8px 12px;font-size:11px;color:white;text-align:right;font-weight:600;">REF</th>
            </tr>
            {rows_html}
          </table>
        </td></tr>
        <tr><td style="padding:4px 16px 8px;">
          <div style="text-align:right;font-size:12px;color:#555;padding:6px 0;">
            Subtotal: <strong>{_dollars(sum(r["amount"] for r in rows))}</strong> ex GST
            &nbsp;|&nbsp; <strong>{_dollars(sum(r["amount"] for r in rows) * 1.1)}</strong> inc GST
          </div>
        </td></tr>'''

    if summary['has_activity']:
        cards_html = f'''
        <tr><td style="padding:16px 16px 8px;">
          <table style="width:100%;border-collapse:collapse;">
            <tr>
              {stat_card(len(summary["deposits"]), "New Jobs In", "#2E6DA4")}
              {stat_card(len(summary["finals"]), "Jobs Completed", "#27AE60")}
              {stat_card(len(summary["credits"]), "Credit Notes", "#E67E22")}
            </tr>
            <tr>
              {stat_card(_dollars(summary["total_in"]), "Deposit Value", "#2E6DA4")}
              {stat_card(_dollars(summary["total_out"]), "Final Invoice Value", "#27AE60")}
              {stat_card(_dollars(summary["net_revenue"]), "Net Revenue Today", "#0D0D0D")}
            </tr>
          </table>
        </td></tr>'''

        # Simple CSS bar showing deposit vs final split
        total = summary['total_in'] + summary['total_out'] + summary['total_cr']
        dep_pct  = int((summary['total_in']  / total * 100)) if total else 0
        fin_pct  = int((summary['total_out'] / total * 100)) if total else 0
        cr_pct   = 100 - dep_pct - fin_pct

        bar_html = f'''
        <tr><td style="padding:4px 16px 16px;">
          <div style="font-size:11px;color:#888;margin-bottom:6px;text-transform:uppercase;
                      letter-spacing:0.5px;">Revenue breakdown</div>
          <div style="display:flex;border-radius:6px;overflow:hidden;height:16px;">
            <div style="width:{dep_pct}%;background:#2E6DA4;" title="Deposits"></div>
            <div style="width:{fin_pct}%;background:#27AE60;" title="Finals"></div>
            <div style="width:{cr_pct}%;background:#E67E22;" title="Credits"></div>
          </div>
          <div style="display:flex;gap:16px;margin-top:6px;font-size:11px;color:#888;">
            <span>&#9632; <span style="color:#2E6DA4">Deposits</span></span>
            <span>&#9632; <span style="color:#27AE60">Finals</span></span>
            <span>&#9632; <span style="color:#E67E22">Credits</span></span>
          </div>
        </td></tr>'''

        sections_html = (
            section_table(summary['deposits'], '#2E6DA4', '📥 Deposit Invoices — Jobs In') +
            section_table(summary['finals'],   '#27AE60', '📤 Final Invoices — Jobs Out') +
            section_table(summary['credits'],  '#E67E22', '↩️ Credit Notes')
        )
        body_content = cards_html + bar_html + sections_html

    else:
        body_content = '''
        <tr><td style="padding:40px 24px;text-align:center;">
          <div style="font-size:48px;margin-bottom:12px;">😴</div>
          <div style="font-size:20px;font-weight:bold;color:#0D0D0D;margin-bottom:8px;">
            Nothing to see here!
          </div>
          <div style="font-size:14px;color:#888;line-height:1.6;">
            No invoices or credit notes were created today.<br>
            The automation ran as expected — just a quiet one.
          </div>
        </td></tr>'''

    return f'''
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#ECECEC;font-family:Arial,sans-serif;">
<table style="width:100%;background:#ECECEC;padding:24px 0;" cellpadding="0" cellspacing="0">
  <tr><td align="center">
    <table style="max-width:600px;width:100%;background:white;border-radius:10px;
                  overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.1);"
           cellpadding="0" cellspacing="0">

      <!-- Header -->
      <tr>
        <td style="background:#0D0D0D;padding:28px 24px;">
          <div style="color:white;font-size:20px;font-weight:bold;
                      letter-spacing:0.5px;">NATIONAL ESTIMATION &amp; PROJECT MANAGEMENT</div>
          <div style="color:#888;font-size:13px;margin-top:4px;">Daily Summary — {today_str}</div>
        </td>
      </tr>

      {body_content}

      <!-- Footer -->
      <tr>
        <td style="background:#f5f5f5;padding:16px 24px;border-top:1px solid #eee;">
          <div style="font-size:11px;color:#aaa;text-align:center;">
            Generated by NEPM Automation at 5:30pm Sydney time
            &nbsp;·&nbsp; nationalestimation.com.au
          </div>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>'''


def send_daily_summary(xero, gmail, recipients: list):
    """Build and send the daily summary email."""
    try:
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        import smtplib

        today_str = date.today().strftime('%A, %d %B %Y')
        summary   = build_summary(xero)
        html      = build_html(summary, today_str)

        activity = (
            f"{len(summary['deposits'])} deposit{'s' if len(summary['deposits'])!=1 else ''}, "
            f"{len(summary['finals'])} final{'s' if len(summary['finals'])!=1 else ''}"
            if summary['has_activity'] else 'No activity'
        )
        subject = f"NEPM Daily Summary — {today_str} — {activity}"

        msg            = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'NEPM Automation <{gmail.username}>'
        msg['To']      = ', '.join(recipients)
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail.username, gmail.app_password)
            server.sendmail(gmail.username, recipients, msg.as_string())

        logger.info(f'Daily summary sent → {recipients} ({activity})')

    except Exception as e:
        logger.error(f'Failed to send daily summary: {e}')
