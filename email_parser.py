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
- job_title: extract the location/project name only, remove client name and other details
- client: must exactly match one of the known clients listed, or be empty string ""
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
