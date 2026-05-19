"""Archive zombie '[UNFINISHED UPLOAD]' rows left behind by enex2notion.

When enex2notion fails on a note upload mid-flight, it tries to delete the
partially-created page. If that delete call itself times out, the zombie page
remains with '[UNFINISHED UPLOAD]' suffix in its title while the retry creates
a new clean row — leaving a duplicate.

This script scans every database under NOTION_ROOT_PAGE_ID, finds any row
whose title still ends with '[UNFINISHED UPLOAD]', and archives it.

Run from repo root:
    ./scripts/conda-run.sh python scripts/cleanup_unfinished.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / '.env')

UNFINISHED_SUFFIX = '[UNFINISHED UPLOAD]'


def retry(fn, *args, **kwargs):
    last = None
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise last


def page_title(page: dict) -> str:
    for prop in page.get('properties', {}).values():
        if prop.get('type') == 'title':
            return ''.join(p.get('plain_text', '') for p in prop.get('title', []))
    return ''


def main() -> int:
    token = os.environ.get('NOTION_TOKEN', '')
    root_page = os.environ.get('NOTION_ROOT_PAGE_ID', '')
    if not (token and root_page):
        print('NOTION_TOKEN and NOTION_ROOT_PAGE_ID must be set in .env')
        return 1

    client = Client(auth=token, timeout_ms=60000)

    children = retry(client.blocks.children.list, block_id=root_page)
    databases = [b for b in children['results'] if b['type'] == 'child_database']
    print(f'Scanning {len(databases)} database(s) under root page...')

    archived_total = 0
    for block in databases:
        db = retry(client.databases.retrieve, block['id'])
        db_title = ''.join(p.get('plain_text', '') for p in db.get('title', [])) or '(untitled)'
        ds_id = db['data_sources'][0]['id']

        cursor = None
        archived_in_db = 0
        while True:
            kwargs = {'data_source_id': ds_id, 'page_size': 100}
            if cursor:
                kwargs['start_cursor'] = cursor
            page = retry(client.data_sources.query, **kwargs)
            for row in page['results']:
                title = page_title(row)
                if UNFINISHED_SUFFIX in title:
                    print(f'  archiving in "{db_title}": {title}')
                    retry(client.pages.update, page_id=row['id'], archived=True)
                    archived_in_db += 1
            if not page.get('has_more'):
                break
            cursor = page.get('next_cursor')
        archived_total += archived_in_db
        if archived_in_db:
            print(f'  -> {db_title}: archived {archived_in_db}')

    print()
    print(f'Total zombie rows archived: {archived_total}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
