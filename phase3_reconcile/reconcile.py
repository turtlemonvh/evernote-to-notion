"""Phase 3: Reconcile the ENEX inventory against what landed in Notion.

Walks every child_database under NOTION_ROOT_PAGE_ID, queries each one's data
source for its rows, and matches each ENEX note (from data/reports/inventory.csv)
to a Notion row.

Matching strategy (scoped to the corresponding notebook, since ENEX notebooks
map 1:1 to Notion databases):

  1. Exact title match           -> match_type='exact'
  2. Fuzzy match >= 0.90          -> match_type='fuzzy_high'   (auto-accept)
  3. Fuzzy match >= 0.70          -> match_type='fuzzy_medium' (needs review)
  4. No notebook -> Notion DB     -> match_type='no_db'
  5. Nothing found                -> match_type='missing'

Outputs:
  data/reports/reconciliation.csv  full per-note match results
  data/reports/missing_notes.csv   subset where status needs human attention
  data/reports/notion_page_map.csv (guid, notebook, title, notion_id, notion_url)
                                   only confident matches; Phase 4 input

Run from repo root:
    ./scripts/conda-run.sh python phase3_reconcile/reconcile.py
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client

from enex2notion.cli_notion import install_throttle

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / '.env')

INVENTORY_CSV = REPO_ROOT / 'data' / 'reports' / 'inventory.csv'
RECONCILE_CSV = REPO_ROOT / 'data' / 'reports' / 'reconciliation.csv'
MISSING_CSV = REPO_ROOT / 'data' / 'reports' / 'missing_notes.csv'
PAGE_MAP_CSV = REPO_ROOT / 'data' / 'reports' / 'notion_page_map.csv'

FUZZY_HIGH = 0.90
FUZZY_MEDIUM = 0.70

RECONCILE_FIELDS = [
    'guid', 'notebook', 'title',
    'match_type', 'confidence',
    'notion_id', 'notion_url', 'notion_title',
]
PAGE_MAP_FIELDS = ['guid', 'notebook', 'title', 'notion_id', 'notion_url']


def row_title(row: dict) -> str:
    for prop in row.get('properties', {}).values():
        if prop.get('type') == 'title':
            return ''.join(t.get('plain_text', '') for t in prop.get('title', []))
    return ''


def collect_notion(client, root_page_id: str) -> dict[str, list[dict]]:
    """Return {database_title: [{notion_id, notion_url, notion_title}, ...]}."""
    notion: dict[str, list[dict]] = defaultdict(list)
    db_block_ids: list[str] = []

    cursor = None
    while True:
        kwargs = {'block_id': root_page_id}
        if cursor:
            kwargs['start_cursor'] = cursor
        page = client.blocks.children.list(**kwargs)
        for block in page['results']:
            if block['type'] == 'child_database':
                db_block_ids.append(block['id'])
        if not page.get('has_more'):
            break
        cursor = page.get('next_cursor')

    print(f'Found {len(db_block_ids)} database(s) under root')

    for db_id in db_block_ids:
        db = client.databases.retrieve(db_id)
        title = ''.join(p.get('plain_text', '') for p in db.get('title', [])) or '(untitled)'
        data_sources = db.get('data_sources') or []
        if not data_sources:
            print(f'  WARN: {title} has no data source — skipping')
            continue
        ds_id = data_sources[0]['id']

        cursor = None
        n = 0
        while True:
            kw = {'data_source_id': ds_id, 'page_size': 100}
            if cursor:
                kw['start_cursor'] = cursor
            qpage = client.data_sources.query(**kw)
            for row in qpage['results']:
                notion[title].append({
                    'notion_id': row['id'],
                    'notion_url': row.get('url', ''),
                    'notion_title': row_title(row),
                })
                n += 1
            if not qpage.get('has_more'):
                break
            cursor = qpage.get('next_cursor')
        print(f'  {title:<35} {n:>4} rows')

    return notion


def best_fuzzy(target: str, candidates: list[dict]) -> tuple[dict | None, float]:
    best, best_score = None, 0.0
    for cand in candidates:
        score = SequenceMatcher(None, target, cand['notion_title']).ratio()
        if score > best_score:
            best, best_score = cand, score
    return best, best_score


def reconcile(inventory: list[dict], notion: dict[str, list[dict]]):
    # exact_buckets[db_title][note_title] -> list of all rows with that title.
    # Storing a list (not a single row) handles the case where multiple notes
    # in the same notebook share the same title — the first inventory match
    # claims the first row, the next one claims the second row, etc.
    exact_buckets: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for db_title, rows in notion.items():
        for row in rows:
            exact_buckets[db_title][row['notion_title']].append(row)

    claimed: set[str] = set()
    results: list[dict] = []

    for inv in inventory:
        notebook = inv['notebook']
        title = inv['title']
        result = {
            'guid': inv['guid'],
            'notebook': notebook,
            'title': title,
            'match_type': '',
            'confidence': '',
            'notion_id': '',
            'notion_url': '',
            'notion_title': '',
        }

        if notebook not in notion:
            result['match_type'] = 'no_db'
            results.append(result)
            continue

        # 1. Exact title match — find an unclaimed row in the bucket
        bucket = exact_buckets[notebook].get(title, [])
        exact = next((r for r in bucket if r['notion_id'] not in claimed), None)
        if exact:
            result.update({
                'match_type': 'exact',
                'confidence': '1.000',
                'notion_id': exact['notion_id'],
                'notion_url': exact['notion_url'],
                'notion_title': exact['notion_title'],
            })
            claimed.add(exact['notion_id'])
            results.append(result)
            continue

        # 2. Fuzzy match within the same notebook
        candidates = [r for r in notion[notebook] if r['notion_id'] not in claimed]
        if candidates:
            best, score = best_fuzzy(title, candidates)
            if best and score >= FUZZY_HIGH:
                result.update({
                    'match_type': 'fuzzy_high',
                    'confidence': f'{score:.3f}',
                    'notion_id': best['notion_id'],
                    'notion_url': best['notion_url'],
                    'notion_title': best['notion_title'],
                })
                claimed.add(best['notion_id'])
                results.append(result)
                continue
            if best and score >= FUZZY_MEDIUM:
                # Suggest but don't claim — operator picks
                result.update({
                    'match_type': 'fuzzy_medium',
                    'confidence': f'{score:.3f}',
                    'notion_id': best['notion_id'],
                    'notion_url': best['notion_url'],
                    'notion_title': best['notion_title'],
                })
                results.append(result)
                continue

        result['match_type'] = 'missing'
        results.append(result)

    return results, claimed


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    token = os.environ.get('NOTION_TOKEN', '')
    root = os.environ.get('NOTION_ROOT_PAGE_ID', '')
    if not (token and root):
        print('FAIL: NOTION_TOKEN and NOTION_ROOT_PAGE_ID must be set in .env')
        return 1

    if not INVENTORY_CSV.exists():
        print(f'FAIL: {INVENTORY_CSV.relative_to(REPO_ROOT)} not found — run Phase 1 first')
        return 2

    client = install_throttle(Client(auth=token, timeout_ms=60000))

    print(f'Loading inventory from {INVENTORY_CSV.relative_to(REPO_ROOT)}...')
    with INVENTORY_CSV.open(newline='', encoding='utf-8') as f:
        inventory = list(csv.DictReader(f))
    print(f'  {len(inventory)} ENEX notes\n')

    notion = collect_notion(client, root)
    total_notion_rows = sum(len(v) for v in notion.values())
    print(f'\nTotal Notion rows: {total_notion_rows}\n')

    results, claimed = reconcile(inventory, notion)

    write_csv(RECONCILE_CSV, results, RECONCILE_FIELDS)
    missing = [r for r in results if r['match_type'] in ('missing', 'no_db', 'fuzzy_medium')]
    write_csv(MISSING_CSV, missing, RECONCILE_FIELDS)
    confident_matches = [
        {
            'guid': r['guid'], 'notebook': r['notebook'], 'title': r['title'],
            'notion_id': r['notion_id'], 'notion_url': r['notion_url'],
        }
        for r in results if r['match_type'] in ('exact', 'fuzzy_high')
    ]
    write_csv(PAGE_MAP_CSV, confident_matches, PAGE_MAP_FIELDS)

    counts = Counter(r['match_type'] for r in results)
    print('Reconciliation summary')
    print('-' * 40)
    for mt in ('exact', 'fuzzy_high', 'fuzzy_medium', 'missing', 'no_db'):
        print(f'  {mt:<15} {counts.get(mt, 0):>5}')
    print(f'  {"TOTAL":<15} {len(results):>5}')

    all_notion_ids = {row['notion_id'] for rows in notion.values() for row in rows}
    orphans = all_notion_ids - claimed
    print(f'\nNotion rows with no ENEX match: {len(orphans)}')
    if orphans and len(orphans) <= 20:
        # Show the orphans inline so the operator can spot anomalies
        id_to_info = {
            row['notion_id']: (db, row['notion_title'])
            for db, rows in notion.items() for row in rows
        }
        print('  (probably the [UNFINISHED UPLOAD] residue or import-time test pages)')
        for nid in list(orphans)[:20]:
            db, t = id_to_info.get(nid, ('?', '?'))
            print(f'    {db}: "{t}"  ({nid})')

    print()
    print(f'Outputs:')
    print(f'  {RECONCILE_CSV.relative_to(REPO_ROOT)}')
    print(f'  {MISSING_CSV.relative_to(REPO_ROOT)}')
    print(f'  {PAGE_MAP_CSV.relative_to(REPO_ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
