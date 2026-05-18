"""Verify the Notion integration can read the configured root page.

Reads NOTION_TOKEN and NOTION_ROOT_PAGE_ID from .env and tries to fetch the
page via the official Notion API. Useful for confirming the integration is
connected to the destination page before running enex2notion.

Run from repo root:
    ./scripts/conda-run.sh python scripts/check_notion_auth.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import APIResponseError

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / '.env')


def title_of(page: dict) -> str:
    for prop in page.get('properties', {}).values():
        if prop.get('type') == 'title':
            parts = prop.get('title', [])
            return ''.join(p.get('plain_text', '') for p in parts) or '(untitled)'
    return '(no title prop)'


def main() -> int:
    token = os.environ.get('NOTION_TOKEN', '')
    page_id = os.environ.get('NOTION_ROOT_PAGE_ID', '')

    if not token:
        print('FAIL: NOTION_TOKEN not set in .env')
        return 1
    if not page_id:
        print('FAIL: NOTION_ROOT_PAGE_ID not set in .env')
        return 1

    client = Client(auth=token)
    try:
        page = client.pages.retrieve(page_id)
    except APIResponseError as exc:
        print(f'FAIL: Notion API returned {exc.status} — {exc.code}')
        print(f'  message: {exc.body}')
        if exc.code == 'object_not_found':
            print()
            print('Most common cause: the integration is not connected to this page.')
            print('Fix it from the page in Notion:')
            print('  1. Open the destination page in your browser')
            print('  2. Click the "..." (more) menu top-right of the page')
            print('  3. Find "Connections" (sometimes under "Add connections")')
            print('  4. Type your integration name and click it to connect')
            print('  5. Re-run this check')
        return 2

    print('OK')
    print(f'  page id:     {page["id"]}')
    print(f'  page title:  "{title_of(page)}"')
    print(f'  url:         {page.get("url", "?")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
