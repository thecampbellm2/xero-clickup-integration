"""
Shared NEPM-branded HTML building blocks for the summary emails
(daily and weekly). Palette and type are taken from
nationalestimation.com.au — monochrome with a single signature red accent.

Email-client constraints honoured here:
  - all styling is inline (Gmail strips <style> in <head>)
  - layout uses tables + percentage widths, never flexbox (Outlook's Word
    engine ignores flex — the old daily-summary bar broke there)
  - brand web fonts are listed first but always fall back to system fonts
"""

# ── Palette (pulled from the live site's CSS) ──────────────────────
INK       = '#0a0a0a'   # primary near-black — headers, body text
INK_SOFT  = '#3a3735'   # softened ink for secondary text
RED       = '#c0392b'   # signature accent
RED_TINT  = '#fff0ee'   # pale red wash (callouts / highlights)
CREAM     = '#f4f3f0'   # warm page background
WARM_GREY = '#e0ddd8'   # borders / tertiary fills
MUTED     = '#8a8580'   # muted labels
WHITE     = '#ffffff'

# ── Type (web fonts degrade gracefully in email clients) ───────────
HEADING_FONT = "'Barlow Condensed','Arial Narrow',Arial,sans-serif"
BODY_FONT    = "'DM Sans',Arial,Helvetica,sans-serif"


def dollars(amount) -> str:
    return f'${float(amount or 0):,.2f}'


def page(subtitle: str, body_content: str, footer_note: str) -> str:
    """Wrap body_content in the branded shell (header band + card + footer)."""
    return f'''<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:{CREAM};font-family:{BODY_FONT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CREAM};padding:24px 0;">
  <tr><td align="center">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:{WHITE};border-radius:10px;overflow:hidden;border:1px solid {WARM_GREY};">

      <!-- Header -->
      <tr>
        <td style="background:{INK};padding:26px 24px;border-bottom:4px solid {RED};">
          <div style="color:{WHITE};font-family:{HEADING_FONT};font-size:24px;font-weight:700;letter-spacing:1px;text-transform:uppercase;">National Estimation &amp; Project Management</div>
          <div style="color:{MUTED};font-size:13px;margin-top:6px;font-family:{BODY_FONT};">{subtitle}</div>
        </td>
      </tr>

      {body_content}

      <!-- Footer -->
      <tr>
        <td style="background:{CREAM};padding:16px 24px;border-top:1px solid {WARM_GREY};">
          <div style="font-size:11px;color:{MUTED};text-align:center;font-family:{BODY_FONT};">
            {footer_note} &nbsp;&middot;&nbsp; nationalestimation.com.au
          </div>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>'''


def stat_card(value, label, accent=RED, sub='') -> str:
    """A single KPI cell — combine several with stat_row()."""
    sub_html = (f'<div style="font-size:11px;color:{MUTED};margin-top:6px;'
                f'font-family:{BODY_FONT};">{sub}</div>') if sub else ''
    return f'''<td width="33%" valign="top" style="padding:8px;">
      <div style="background:{WHITE};border:1px solid {WARM_GREY};border-top:4px solid {accent};border-radius:8px;padding:16px 12px;text-align:center;">
        <div style="font-family:{HEADING_FONT};font-size:28px;font-weight:700;color:{accent};line-height:1.1;">{value}</div>
        <div style="font-size:11px;color:{MUTED};margin-top:6px;text-transform:uppercase;letter-spacing:0.6px;font-family:{BODY_FONT};">{label}</div>
        {sub_html}
      </div>
    </td>'''


def stat_row(cards: list) -> str:
    return f'<tr>{"".join(cards)}</tr>'


def cards_block(rows: list) -> str:
    """rows is a list of card-lists; each inner list becomes one <tr>."""
    inner = ''.join(stat_row(r) for r in rows)
    return f'''<tr><td style="padding:16px 16px 8px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{inner}</table>
    </td></tr>'''


def stacked_bar(segments: list, title: str = '') -> str:
    """Table-based (Outlook-safe) stacked horizontal bar.

    segments: list of (pct:int, color:str, label:str). Zero-width segments are
    dropped from the bar but kept in the legend.
    """
    segs = [s for s in segments if s[0] > 0]
    cells = ''.join(
        f'<td width="{pct}%" style="height:16px;background:{color};font-size:0;line-height:16px;">&nbsp;</td>'
        for pct, color, _ in segs
    ) or f'<td style="height:16px;background:{WARM_GREY};font-size:0;line-height:16px;">&nbsp;</td>'
    legend = '&nbsp;&nbsp;&nbsp;'.join(
        f'<span style="color:{color};">&#9632;</span> <span style="color:{MUTED};">{label}</span>'
        for _, color, label in segments
    )
    title_html = (f'<div style="font-size:11px;color:{MUTED};margin-bottom:6px;'
                  f'text-transform:uppercase;letter-spacing:0.5px;font-family:{BODY_FONT};">{title}</div>') if title else ''
    return f'''<tr><td style="padding:4px 16px 16px;">
      {title_html}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-radius:6px;overflow:hidden;">
        <tr>{cells}</tr>
      </table>
      <div style="margin-top:6px;font-size:11px;font-family:{BODY_FONT};">{legend}</div>
    </td></tr>'''


def data_table(title: str, headers: list, rows: list, accent=RED) -> str:
    """Generic table section.

    headers: list of (label, align). rows: list of rows, each a list of (value, align).
    Returns '' when there are no rows.
    """
    if not rows:
        return ''
    head = ''.join(
        f'<th style="padding:8px 12px;font-size:11px;color:{WHITE};text-align:{align};font-weight:600;font-family:{BODY_FONT};">{label}</th>'
        for label, align in headers
    )
    body = ''
    for i, row in enumerate(rows):
        bg = CREAM if i % 2 == 0 else WHITE
        cells = ''.join(
            f'<td style="padding:10px 12px;font-size:13px;color:{INK};text-align:{align};font-family:{BODY_FONT};">{val}</td>'
            for val, align in row
        )
        body += f'<tr style="background:{bg};">{cells}</tr>'
    return f'''<tr><td style="padding:8px 16px 0;">
      <div style="font-family:{HEADING_FONT};font-size:15px;font-weight:700;color:{accent};text-transform:uppercase;letter-spacing:0.6px;padding:10px 0 6px;">{title}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border-radius:8px;overflow:hidden;border:1px solid {WARM_GREY};">
        <tr style="background:{accent};">{head}</tr>
        {body}
      </table>
    </td></tr>'''


def delta_badge(current, previous) -> str:
    """Week-on-week change indicator. Handles a young account (previous None/0)."""
    if previous is None:
        return f'<span style="color:{MUTED};font-size:11px;">&mdash; no prior data</span>'
    if previous == 0:
        if current == 0:
            return f'<span style="color:{MUTED};font-size:11px;">no change</span>'
        return f'<span style="color:{INK};font-size:11px;">&#9650; new</span>'
    pct = (current - previous) / previous * 100
    if abs(pct) < 0.5:
        return f'<span style="color:{MUTED};font-size:11px;">&#9644; 0%</span>'
    arrow = '&#9650;' if pct > 0 else '&#9660;'
    # Monochrome + red: upward/steady reads in ink; a drop draws the eye in red.
    color = INK if pct > 0 else RED
    return f'<span style="color:{color};font-size:11px;">{arrow} {abs(pct):.0f}%</span>'


def empty_state(message: str) -> str:
    return f'''<tr><td style="padding:40px 24px;text-align:center;">
      <div style="font-family:{HEADING_FONT};font-size:22px;font-weight:700;color:{INK};margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px;">Nothing to report</div>
      <div style="font-size:14px;color:{MUTED};line-height:1.6;font-family:{BODY_FONT};">{message}</div>
    </td></tr>'''
