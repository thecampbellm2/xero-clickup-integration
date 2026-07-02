"""
Builds and sends the NEPM weekly summary email (Fridays, 5:35pm Sydney).

Reports new jobs and completed jobs for the last 7 days, compared with the
previous 7 days (week on week) and the same 7-day window 4 weeks earlier
(28-day offset). Reuses the shared NEPM theme in summary_theme.py and the
Xero/ClickUp data helpers.

"Completed" = a job whose ClickUp status is FINAL INVOICED or COMPLETED
(final-invoiced jobs are done work, just awaiting payment/reconciliation).
The check also treats any ClickUp status of type done/closed as completed, so
it survives if the done-status is named differently.

Note: the NEPM Xero account is only ~a month old, so the 4-weeks-ago column
will often be empty for now. MTD/YTD views are intentionally deferred until
there's more history — metrics_for_range() is generic so they drop in easily.
"""
import html
import json
import logging
import urllib.parse
from datetime import date, datetime, time as dt_time, timedelta

import pytz

import summary_theme as theme
from daily_summary import _parse_type

logger = logging.getLogger(__name__)
SYDNEY = pytz.timezone('Australia/Sydney')

# Job is "completed" if its status name is one of these (case-insensitive) …
COMPLETED_STATUS_NAMES = {'final invoiced', 'completed'}
# … or if ClickUp classifies the status type as one of these.
COMPLETED_STATUS_TYPES = {'done', 'closed'}


# ── small helpers ──────────────────────────────────────────────────
def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ''))


def _is_completed(task: dict) -> bool:
    st    = task.get('status') or {}
    name  = (st.get('status') or '').lower().strip()
    stype = (st.get('type') or '').lower().strip()
    return name in COMPLETED_STATUS_NAMES or stype in COMPLETED_STATUS_TYPES


def _completion_ms(task: dict) -> int:
    """Best-available timestamp for when a job was completed."""
    return _int(task.get('date_done') or task.get('date_updated'))


def _range_ms(start_dt: date, end_dt: date):
    start = SYDNEY.localize(datetime.combine(start_dt, dt_time.min))
    end   = SYDNEY.localize(datetime.combine(end_dt, dt_time.max))
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ── metrics ─────────────────────────────────────────────────────────
def metrics_for_range(xero, clickup, start_dt: date, end_dt: date, all_tasks=None) -> dict:
    """
    Compute job + revenue metrics for [start_dt, end_dt] inclusive.
    Pass all_tasks to reuse a single ClickUp fetch across several ranges.
    """
    start_ms, end_ms = _range_ms(start_dt, end_dt)
    tasks = all_tasks if all_tasks is not None else clickup.get_all_tasks()

    new_job_tasks   = [t for t in tasks if start_ms <= _int(t.get('date_created')) <= end_ms]
    completed_tasks = [t for t in tasks if _is_completed(t) and start_ms <= _completion_ms(t) <= end_ms]

    docs = xero.get_documents_between(start_dt, end_dt)
    deposit_value = sum(_f(i.get('SubTotal')) for i in docs['invoices']
                        if _parse_type(i.get('Reference', '')) == 'deposit')
    final_value   = sum(_f(i.get('SubTotal')) for i in docs['invoices']
                        if _parse_type(i.get('Reference', '')) != 'deposit')
    credit_value  = sum(_f(c.get('SubTotal')) for c in docs['credit_notes'])

    return {
        'new_jobs':        len(new_job_tasks),
        'completed_jobs':  len(completed_tasks),
        'new_job_tasks':   new_job_tasks,
        'completed_tasks': completed_tasks,
        'deposit_value':   deposit_value,
        'final_value':     final_value,
        'credit_value':    credit_value,
        'net_revenue':     deposit_value + final_value - credit_value,
    }


# ── chart ───────────────────────────────────────────────────────────
def _quickchart_url(this_wk: dict, last_wk: dict, last_month: dict) -> str:
    """Grouped bar chart (New vs Completed × 3 periods) as a QuickChart image URL."""
    chart = {
        'type': 'bar',
        'data': {
            'labels': ['New Jobs', 'Completed Jobs'],
            'datasets': [
                {'label': 'This week',   'backgroundColor': theme.RED,
                 'data': [this_wk['new_jobs'], this_wk['completed_jobs']]},
                {'label': 'Last week',   'backgroundColor': theme.INK,
                 'data': [last_wk['new_jobs'], last_wk['completed_jobs']]},
                {'label': '4 weeks ago', 'backgroundColor': theme.MUTED,
                 'data': [last_month['new_jobs'], last_month['completed_jobs']]},
            ],
        },
        'options': {
            'legend': {'position': 'bottom'},
            'scales': {'yAxes': [{'ticks': {'beginAtZero': True, 'precision': 0}}]},
        },
    }
    encoded = urllib.parse.quote(json.dumps(chart, separators=(',', ':')))
    return f'https://quickchart.io/chart?w=520&h=260&bkg=white&c={encoded}'


# ── HTML ────────────────────────────────────────────────────────────
def build_weekly_html(clickup, this_wk: dict, last_wk: dict, last_month: dict, ranges: dict) -> str:
    footer   = 'Generated by NEPM Automation &middot; weekly, Fridays 5:35pm Sydney time'
    subtitle = f"Weekly Summary &mdash; {ranges['this']}"

    if this_wk['new_jobs'] == 0 and this_wk['completed_jobs'] == 0 and this_wk['net_revenue'] == 0:
        body = theme.empty_state('No new or completed jobs, and no invoicing activity this week.')
        return theme.page(subtitle, body, footer)

    def client_of(task):
        try:
            return clickup.parse_custom_fields(task).get('client', '') or '—'
        except Exception:
            return '—'

    # Headline KPI cards with week-on-week deltas
    cards = theme.cards_block([
        [
            theme.stat_card(this_wk['new_jobs'], 'New Jobs', theme.RED,
                            sub=theme.delta_badge(this_wk['new_jobs'], last_wk['new_jobs']) + ' vs last wk'),
            theme.stat_card(this_wk['completed_jobs'], 'Jobs Completed', theme.INK,
                            sub=theme.delta_badge(this_wk['completed_jobs'], last_wk['completed_jobs']) + ' vs last wk'),
            theme.stat_card(theme.dollars(this_wk['net_revenue']), 'Net Revenue', theme.INK,
                            sub=theme.delta_badge(this_wk['net_revenue'], last_wk['net_revenue']) + ' vs last wk'),
        ],
    ])

    # Comparison chart (image) with a text table fallback for image-blocked clients
    chart_url = _quickchart_url(this_wk, last_wk, last_month)
    chart = (
        f'<tr><td style="padding:8px 16px 4px;text-align:center;">'
        f'<div style="font-size:11px;color:{theme.MUTED};margin-bottom:8px;text-transform:uppercase;'
        f'letter-spacing:0.5px;font-family:{theme.BODY_FONT};">This week vs last week vs 4 weeks ago</div>'
        f'<img src="{chart_url}" width="520" alt="Jobs: this week vs last week vs 4 weeks ago" '
        f'style="max-width:100%;height:auto;border:1px solid {theme.WARM_GREY};border-radius:8px;"/>'
        f'</td></tr>'
    )

    r = 'right'
    wow_headers = [('METRIC', 'left'), ('THIS WEEK', r), ('LAST WEEK', r), ('4 WKS AGO', r)]
    wow_rows = [
        [('New Jobs', 'left'), (str(this_wk['new_jobs']), r), (str(last_wk['new_jobs']), r), (str(last_month['new_jobs']), r)],
        [('Jobs Completed', 'left'), (str(this_wk['completed_jobs']), r), (str(last_wk['completed_jobs']), r), (str(last_month['completed_jobs']), r)],
        [('Deposit Value', 'left'), (theme.dollars(this_wk['deposit_value']), r), (theme.dollars(last_wk['deposit_value']), r), (theme.dollars(last_month['deposit_value']), r)],
        [('Final Value', 'left'), (theme.dollars(this_wk['final_value']), r), (theme.dollars(last_wk['final_value']), r), (theme.dollars(last_month['final_value']), r)],
        [('Net Revenue', 'left'), (theme.dollars(this_wk['net_revenue']), r), (theme.dollars(last_wk['net_revenue']), r), (theme.dollars(last_month['net_revenue']), r)],
    ]
    wow_table = theme.data_table('Week on week', wow_headers, wow_rows)

    # Revenue breakdown bar (this week)
    total = this_wk['deposit_value'] + this_wk['final_value'] + this_wk['credit_value']
    dep_pct = int(this_wk['deposit_value'] / total * 100) if total else 0
    fin_pct = int(this_wk['final_value'] / total * 100) if total else 0
    cr_pct  = max(0, 100 - dep_pct - fin_pct)
    bar = theme.stacked_bar(
        [(dep_pct, theme.RED, 'Deposits'), (fin_pct, theme.INK, 'Finals'), (cr_pct, theme.MUTED, 'Credits')],
        title='Revenue breakdown (this week)',
    )

    # Job detail tables for the week
    new_rows = [
        [(_esc(t.get('name', 'Unknown Job')), 'left'),
         (_esc(client_of(t)), 'left'),
         (_esc((t.get('status') or {}).get('status', '').title()), 'left')]
        for t in this_wk['new_job_tasks']
    ]
    new_tbl = theme.data_table('New Jobs This Week',
                               [('JOB', 'left'), ('CLIENT', 'left'), ('STATUS', 'left')],
                               new_rows, theme.RED)

    comp_rows = [
        [(_esc(t.get('name', 'Unknown Job')), 'left'), (_esc(client_of(t)), 'left')]
        for t in this_wk['completed_tasks']
    ]
    comp_tbl = theme.data_table('Completed This Week',
                                [('JOB', 'left'), ('CLIENT', 'left')],
                                comp_rows, theme.INK)

    body = cards + chart + wow_table + bar + new_tbl + comp_tbl
    return theme.page(subtitle, body, footer)


# ── send ────────────────────────────────────────────────────────────
def send_weekly_summary(xero, clickup, gmail, recipients: list, sendgrid_api_key: str = '',
                        clickup_token: str = '', clickup_channel: str = ''):
    """Build and send the weekly summary email via SendGrid."""
    from xero_client import XeroAuthError
    import notifier

    try:
        today = date.today()
        this_start,  this_end  = today - timedelta(days=6),  today
        last_start,  last_end  = today - timedelta(days=13), today - timedelta(days=7)
        lm_start,    lm_end    = today - timedelta(days=34), today - timedelta(days=28)

        all_tasks  = clickup.get_all_tasks()
        this_wk    = metrics_for_range(xero, clickup, this_start, this_end, all_tasks)
        last_wk    = metrics_for_range(xero, clickup, last_start, last_end, all_tasks)
        last_month = metrics_for_range(xero, clickup, lm_start,   lm_end,   all_tasks)

        ranges = {'this': f'{this_start:%a %d %b} – {this_end:%a %d %b %Y}'}
        html_body = build_weekly_html(clickup, this_wk, last_wk, last_month, ranges)

        activity = f"{this_wk['new_jobs']} new, {this_wk['completed_jobs']} completed"
        subject  = f"NEPM Weekly Summary — {this_start:%d %b} to {this_end:%d %b} — {activity}"

        gmail.send_email(
            to_list          = recipients,
            subject          = subject,
            body             = f"NEPM Weekly Summary\n{ranges['this']}\n{activity}",
            html_body        = html_body,
            sendgrid_api_key = sendgrid_api_key,
        )
        logger.info(f'Weekly summary sent → {recipients} ({activity})')

    except XeroAuthError as e:
        logger.error(f'Weekly summary failed — Xero auth error: {e}')
        notifier.xero_auth_failed(
            gmail, recipients,
            sendgrid_api_key=sendgrid_api_key,
            clickup_token=clickup_token,
            clickup_channel=clickup_channel,
        )

    except Exception as e:
        logger.error(f'Failed to send weekly summary: {e}')
