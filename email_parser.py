"""
Uses the Claude API to parse plain-English job emails from George
into structured ClickUp task data.
"""
import json
import logging
import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API = 'https://api.anthropic.com/v1/messages'


def parse_job_email(subject: str, body: str, client_options: list, api_key: str) -> dict:
    """
    Pass the email subject/body to Claude and return structured job data.

    Returns a dict with keys:
      job_title       str   — cleaned job name
      client          str   — matched client name from client_options, or '' if unsure
      estimated_hours float — hours as a number, or None
      hourly_rate     float — rate in dollars, or None

    Returns None if parsing fails entirely.
    """
    if not api_key:
        logger.error('ANTHROPIC_API_KEY not set — cannot parse email')
        return None

    client_list = '\n'.join(f'- {c}' for c in client_options)

    prompt = f"""You are a data extraction assistant for a construction estimation business.
Extract job details from the following email and return ONLY a JSON object — no explanation, no markdown.

Email subject: {subject}
Email body: {body or '(no body)'}

Known clients (match against these exactly, pick the closest, or leave blank if unsure):
{client_list}

Return this exact JSON structure:
{{
  "job_title": "clean concise job name, e.g. Lane Cove High School",
  "client": "exact name from the known clients list above, or empty string if no confident match",
  "estimated_hours": 6.0,
  "hourly_rate": 130.0
}}

Rules:
- PRIORITY: The subject line is the primary source of truth for billing details (hourly_rate, estimated_hours, client). If the subject and body contain conflicting values for these fields, always use the subject line value.
- The body is supplementary — use it only to fill in a more complete job_title or project name if the subject is vague, or to find the client if not in the subject.
- job_title: extract the location/project name only, remove client name, rate, and hours
- client: match to the closest known client even if the email uses a shortened or informal version (e.g. "Finnbarr construction" should match "Finnbarr Construction Pty Ltd", "BR Masonry" should match "BR Masonry Pty Ltd"). Only leave blank if there is genuinely no recognisable match.
- estimated_hours: number only (e.g. 6.0), or null if not mentioned
- hourly_rate: number only (e.g. 130.0), or null if not mentioned
- Return ONLY the JSON object, nothing else"""

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                'x-api-key':         api_key,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-haiku-4-5-20251001',
                'max_tokens': 300,
                'messages':   [{'role': 'user', 'content': prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()['content'][0]['text'].strip()

        # Strip markdown fences if present
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        text = text.strip()

        data = json.loads(text)
        logger.info(f'Parsed email: {data}')
        return data

    except Exception as e:
        logger.error(f'Email parsing failed: {e}')
        return None


def parse_reply_email(reply_body: str, missing_fields: list, api_key: str) -> dict:
    """
    Parse George's reply to a missing-fields question.

    Returns one of:
      {'import_as_is': True}                         — import without the missing fields
      {'client': ..., 'hourly_rate': ..., ...}       — one or more fields now provided
    """
    if not api_key:
        return {'import_as_is': True}

    fields_str = ', '.join(missing_fields)

    prompt = f"""You are a data extraction assistant for a construction estimation business.

A job email was missing the following fields: {fields_str}

George has replied to ask for those details. Her reply is:
"{reply_body}"

Determine whether George has provided the missing details, or whether she wants the job imported as-is.

Return ONLY a JSON object — no explanation, no markdown.

If she wants to import as-is (e.g. "import as is", "no idea", "don't know", "later", "not sure yet", "just import it", "leave it"):
{{"import_as_is": true}}

If she has provided some or all of the missing fields:
{{
  "import_as_is": false,
  "client": "matched client name or empty string",
  "hourly_rate": 120.0,
  "estimated_hours": 6.0
}}

Rules:
- Only include fields that George actually provided in her reply
- Use null for fields she still hasn't provided
- import_as_is takes priority — if she seems unsure or dismissive, set it to true"""

    try:
        import requests as req
        resp = req.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         api_key,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-haiku-4-5-20251001',
                'max_tokens': 200,
                'messages':   [{'role': 'user', 'content': prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()['content'][0]['text'].strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'Reply parsing failed: {e}')
        return {'import_as_is': True}
