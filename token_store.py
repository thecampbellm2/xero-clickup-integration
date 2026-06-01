"""
Persistent token storage using a private GitHub Gist.
Tokens survive Render spin-downs and re-deploys without triggering
a new deployment (unlike Render env var updates).

On save  → write to local file + update GitHub Gist
On load  → try local file first, fall back to GitHub Gist
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

GIST_API = 'https://api.github.com/gists'


def _github_headers(token: str) -> dict:
    return {
        'Authorization': f'Bearer {token}',
        'Accept':        'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }


def load_tokens(token_file: str, gist_key: str, github_token: str, gist_id: str) -> dict:
    """
    Load tokens for a service.
    Priority: local file → GitHub Gist → empty dict.

    token_file  — local filename, e.g. 'xero_tokens.json'
    gist_key    — key inside the Gist JSON object, e.g. 'xero' or 'gmail'
    """
    # 1. Try local file (fastest, works when file survives)
    if os.path.exists(token_file):
        try:
            with open(token_file) as f:
                data = json.load(f)
            if data:
                logger.debug(f'Loaded {gist_key} tokens from local file')
                return data
        except Exception as e:
            logger.warning(f'Could not read {token_file}: {e}')

    # 2. Fall back to GitHub Gist
    if github_token and gist_id:
        try:
            resp = requests.get(f'{GIST_API}/{gist_id}', headers=_github_headers(github_token))
            resp.raise_for_status()
            gist_data = resp.json()
            content   = gist_data.get('files', {}).get('tokens.json', {}).get('content', '{}')
            all_tokens = json.loads(content)
            tokens     = all_tokens.get(gist_key, {})
            if tokens:
                logger.info(f'Loaded {gist_key} tokens from GitHub Gist — writing to local file')
                _write_file(token_file, tokens)
            return tokens
        except Exception as e:
            logger.error(f'Could not load {gist_key} tokens from Gist: {e}')

    return {}


def save_tokens(token_file: str, gist_key: str, tokens: dict, github_token: str, gist_id: str):
    """
    Save tokens to local file and update the GitHub Gist.

    token_file  — local filename, e.g. 'xero_tokens.json'
    gist_key    — key inside the Gist JSON object, e.g. 'xero' or 'gmail'
    tokens      — the token dict to save
    """
    # Always write locally first
    _write_file(token_file, tokens)

    # Update GitHub Gist
    if not github_token or not gist_id:
        logger.warning('GITHUB_TOKEN or TOKEN_GIST_ID not set — tokens not persisted to Gist')
        return

    try:
        # Fetch current Gist content so we don't overwrite other keys
        resp = requests.get(f'{GIST_API}/{gist_id}', headers=_github_headers(github_token))
        resp.raise_for_status()
        gist_data  = resp.json()
        content    = gist_data.get('files', {}).get('tokens.json', {}).get('content', '{}')
        all_tokens = json.loads(content)

        # Update just our key
        all_tokens[gist_key] = tokens

        # Patch the Gist
        patch = requests.patch(
            f'{GIST_API}/{gist_id}',
            headers=_github_headers(github_token),
            json={'files': {'tokens.json': {'content': json.dumps(all_tokens, indent=2)}}}
        )
        patch.raise_for_status()
        logger.info(f'Saved {gist_key} tokens to GitHub Gist')

    except Exception as e:
        logger.error(f'Could not save {gist_key} tokens to Gist: {e}')


def _write_file(path: str, data: dict):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
