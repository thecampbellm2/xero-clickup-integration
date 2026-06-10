"""
Manages the pending jobs queue in GitHub Gist.
Jobs sit here when they are missing required fields,
waiting for George to reply with the missing details.
"""
import json
import logging
import time
import requests

logger = logging.getLogger(__name__)
GIST_API  = 'https://api.github.com/gists'
GIST_KEY  = 'pending_jobs'
TIMEOUT_S = 3 * 3600   # 3 hours


def _gh_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }


def _fetch_gist(github_token, gist_id) -> dict:
    resp = requests.get(f'{GIST_API}/{gist_id}', headers=_gh_headers(github_token))
    resp.raise_for_status()
    content = resp.json().get('files', {}).get('tokens.json', {}).get('content', '{}')
    return json.loads(content)


def _save_gist(github_token, gist_id, all_data: dict):
    requests.patch(
        f'{GIST_API}/{gist_id}',
        headers=_gh_headers(github_token),
        json={'files': {'tokens.json': {'content': json.dumps(all_data, indent=2)}}}
    ).raise_for_status()


def add(github_token, gist_id, message_id: str, job: dict):
    """
    Add a pending job keyed by the original email's Message-ID.
    job dict should include: job_title, client, hourly_rate,
    estimated_hours, missing_fields, sender, subject, body, created_at
    """
    try:
        all_data = _fetch_gist(github_token, gist_id)
        pending  = all_data.get(GIST_KEY, {})
        pending[message_id] = {**job, 'created_at': time.time()}
        all_data[GIST_KEY]  = pending
        _save_gist(github_token, gist_id, all_data)
        logger.info(f'Pending job added: {job.get("job_title")} (msg {message_id})')
    except Exception as e:
        logger.error(f'Failed to add pending job: {e}')


def get_all(github_token, gist_id) -> dict:
    """Return all pending jobs {message_id: job_data}."""
    try:
        all_data = _fetch_gist(github_token, gist_id)
        return all_data.get(GIST_KEY, {})
    except Exception as e:
        logger.error(f'Failed to fetch pending jobs: {e}')
        return {}


def remove(github_token, gist_id, message_id: str):
    """Remove a pending job once resolved."""
    try:
        all_data = _fetch_gist(github_token, gist_id)
        pending  = all_data.get(GIST_KEY, {})
        if message_id in pending:
            del pending[message_id]
            all_data[GIST_KEY] = pending
            _save_gist(github_token, gist_id, all_data)
    except Exception as e:
        logger.error(f'Failed to remove pending job {message_id}: {e}')


def get_expired(github_token, gist_id) -> list:
    """Return jobs that have exceeded the 3-hour timeout as a list of (message_id, job) tuples."""
    all_pending = get_all(github_token, gist_id)
    now         = time.time()
    return [
        (mid, job) for mid, job in all_pending.items()
        if now - job.get('created_at', now) >= TIMEOUT_S
    ]
