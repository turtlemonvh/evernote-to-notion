"""For each priority audit target, produce a deep-link directly to its
Notion image block (instead of just the page).

Approach:
  * Each ENEX note's <content> has zero or more <en-media hash="..."> refs in
    order. The hash is the MD5 of the resource binary. After Phase 1.5,
    that hash is the *downscaled* image's MD5 — the same one in
    attachment_actions.csv as new_hash.
  * The Nth image-typed en-media in ENEX corresponds to the Nth image block
    in Notion (enex2notion uploads blocks in document order).
  * audit_targets.csv tells us which files are priority. We match each
    priority row to its position N in the note, then fetch the Nth image
    block from Notion and emit a https://www.notion.so/<page>#<block> anchor.

Run from repo root:
    ./scripts/conda-run.sh python scripts/audit_image_links.py
"""
from __future__ import annotations

import base64
import csv
import hashlib
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from lxml import etree
from notion_client import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from enex2notion.cli_notion import install_throttle  # noqa: E402

load_dotenv(REPO_ROOT / '.env')

AUDIT_CSV = REPO_ROOT / 'data' / 'reports' / 'audit_targets.csv'
ACTIONS_CSV = REPO_ROOT / 'data' / 'reports' / 'attachment_actions.csv'
PAGE_MAP_CSV = REPO_ROOT / 'data' / 'reports' / 'notion_page_map.csv'

EN_MEDIA_RE = re.compile(r'<en-media\b[^>]*\bhash="([a-f0-9]{32})"[^>]*/?>', re.IGNORECASE)
WS_RE = re.compile(r'\s+')


def resource_hash_to_mime(note: etree._Element) -> dict[str, str]:
    """Return {md5_hex: mime_type} for every <resource> in the note."""
    out: dict[str, str] = {}
    for r in note.findall('resource'):
        data_el = r.find('data')
        mime_el = r.find('mime')
        if data_el is None or mime_el is None:
            continue
        if not data_el.text:
            continue
        b64 = WS_RE.sub('', data_el.text)
        binary = base64.b64decode(b64)
        out[hashlib.md5(binary).hexdigest()] = mime_el.text or ''
    return out


def iter_image_block_ids(client, page_id: str):
    """Yield block ids of every image block on a page, in document order.
    Recurses into children of containers so images nested inside toggles or
    callouts are also visible."""
    stack = [page_id]
    while stack:
        bid = stack.pop(0)
        cursor = None
        ordered_children: list[dict] = []
        while True:
            kw = {'block_id': bid}
            if cursor:
                kw['start_cursor'] = cursor
            resp = client.blocks.children.list(**kw)
            ordered_children.extend(resp['results'])
            if not resp.get('has_more'):
                break
            cursor = resp.get('next_cursor')
        for child in ordered_children:
            if child.get('type') == 'image':
                yield child['id']
            if child.get('has_children'):
                stack.append(child['id'])


def main() -> int:
    if not AUDIT_CSV.exists():
        print(f'FAIL: {AUDIT_CSV.relative_to(REPO_ROOT)} not found — run Phase 1.5 first')
        return 1

    with AUDIT_CSV.open(newline='', encoding='utf-8') as f:
        audits = [r for r in csv.DictReader(f) if r['priority_review'] == 'True']
    if not audits:
        print('No priority audit targets.')
        return 0

    # Group priority targets by note GUID
    by_note: dict[str, list[dict]] = defaultdict(list)
    for r in audits:
        by_note[r['note_guid']].append(r)

    # Look up Notion URL per note
    notion_url_for: dict[tuple[str, str], str] = {}
    with PAGE_MAP_CSV.open(newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            notion_url_for[(r['notebook'], r['title'])] = r['notion_url']

    # Build (note_guid, original_filename, original_mb_str) -> new_hash from
    # attachment_actions.csv. This uniquely identifies a downscaled resource
    # even when multiple resources in a note share a generic filename like
    # "image.png" (Evernote's default).
    new_hash_for: dict[tuple[str, str, str], str] = {}
    with ACTIONS_CSV.open(newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r['action'] != 'downscale':
                continue
            key = (r['note_guid'], r['original_filename'], r['original_mb'])
            new_hash_for[key] = r['new_hash']

    token = os.environ['NOTION_TOKEN']
    client = install_throttle(Client(auth=token, timeout_ms=60000))

    output: list[dict] = []
    for guid, rows in by_note.items():
        enex_path = REPO_ROOT / rows[0]['enex_path'].replace('data/enex/', 'data/enex_processed/')
        note_title = rows[0]['note_title']
        notebook = enex_path.stem
        page_url = notion_url_for.get((notebook, note_title), '(URL not found)')

        # Parse ENEX, find this note, walk its content for ordered en-media hashes.
        tree = etree.parse(
            str(enex_path),
            parser=etree.XMLParser(huge_tree=True, strip_cdata=False),
        )
        target_note = None
        for note in tree.getroot().findall('note'):
            guid_el = note.find('guid')
            if guid_el is not None and guid_el.text == guid:
                target_note = note
                break
        if target_note is None:
            print(f'! note guid {guid} not found in {enex_path}')
            continue

        content_el = target_note.find('content')
        if content_el is None or not content_el.text:
            print(f'! note "{note_title}" has no content')
            continue

        hash_to_mime = resource_hash_to_mime(target_note)
        en_media_hashes_in_content = EN_MEDIA_RE.findall(content_el.text)

        # Compute "image index" for each en-media reference in document order.
        # Only image-mime references count toward the index.
        image_index_of_hash: dict[str, list[int]] = defaultdict(list)
        image_counter = 0
        for h in en_media_hashes_in_content:
            mime = hash_to_mime.get(h, '')
            if mime.startswith('image/'):
                image_index_of_hash[h].append(image_counter)
                image_counter += 1

        # Pre-fetch all Notion image block ids for this page (ordered)
        page_id = page_url.rsplit('-', 1)[-1]
        image_block_ids = list(iter_image_block_ids(client, page_id))

        # Each priority audit row -> the new_hash of its downscaled resource
        # (looked up by (guid, filename, original_mb) from attachment_actions.csv).
        # That hash is what the en-media references in the current ENEX.
        priority_hashes_in_note: list[tuple[str, dict]] = []
        for r in rows:
            key = (r['note_guid'], r['original_filename'], r['original_mb'])
            hsh = new_hash_for.get(key)
            if not hsh:
                print(f'! no new_hash found for "{r["note_title"]}" '
                      f'/ {r["original_filename"]} / {r["original_mb"]} MB')
                continue
            priority_hashes_in_note.append((hsh, r))

        for hsh, r in priority_hashes_in_note:
            indices = image_index_of_hash.get(hsh, [])
            if not indices:
                output.append({
                    'note_title': note_title, 'notebook': notebook,
                    'filename': r['original_filename'],
                    'image_index': '?', 'block_url': '(no matching en-media)',
                    'original_mb': r['original_mb'], 'new_mb': r['new_mb'],
                    'original_dims': r['original_dims'], 'new_dims': r['new_dims'],
                    'reason': r['reason'],
                })
                continue
            for idx in indices:
                if idx < len(image_block_ids):
                    block_id = image_block_ids[idx].replace('-', '')
                    block_url = f'{page_url}#{block_id}'
                else:
                    block_url = f'(only {len(image_block_ids)} image blocks on page)'
                output.append({
                    'note_title': note_title, 'notebook': notebook,
                    'filename': r['original_filename'],
                    'image_index': idx + 1,
                    'total_image_blocks': len(image_block_ids),
                    'block_url': block_url,
                    'original_mb': r['original_mb'], 'new_mb': r['new_mb'],
                    'original_dims': r['original_dims'], 'new_dims': r['new_dims'],
                    'reason': r['reason'],
                })

    print(f'{len(output)} priority image deep-links:\n')
    for r in output:
        print(f'  "{r["note_title"]}"  ({r["notebook"]})')
        print(f'    image #{r["image_index"]} of {r.get("total_image_blocks", "?")}  '
              f'[{r["filename"]}]')
        print(f'    {r["original_mb"]} MB ({r["original_dims"]}) -> '
              f'{r["new_mb"]} MB ({r["new_dims"]})')
        print(f'    {r["block_url"]}')
        print(f'    reason: {r["reason"]}')
        print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
