"""Phase 1.5 step 2: optimize ENEX attachments for Notion import.

For every oversized resource (>= 4.5 MB):
  - Image MIMEs (jpeg/png/heic): downscaled in-place with Pillow; inline
    <en-media hash="..."> references in the note body are rewritten to the
    new MD5 hash.
  - Non-image MIMEs: <en-media> reference is rewritten to either an <a> link
    pointing at a Dropbox URL (looked up from dropbox_links.json) or, if the
    URL is blank, a plain-text "[Attachment removed: filename]" marker. The
    <resource> block is removed from the note.

Input:  data/enex/<stack>/<notebook>.enex
Output: data/enex_processed/<stack>/<notebook>.enex
Logs:   data/reports/attachment_actions.csv
        data/reports/audit_targets.csv

Run from repo root:
    ./scripts/conda-run.sh python phase1_5_attachments/optimize_attachments.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from lxml import etree

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.attachments import (  # noqa: E402
    PRIORITY_ASPECT_RATIO,
    TARGET_BYTES,
    downscale_image,
    is_priority_review,
    md5_hex,
    replace_enmedia_with_link,
    replace_enmedia_with_marker,
    resource_data_bytes,
    resource_filename,
    resource_mime,
    set_resource_data,
    update_enmedia_hash,
)
from utils.enex_parser import child_text  # noqa: E402

ENEX_DIR = REPO_ROOT / 'data' / 'enex'
PROCESSED_DIR = REPO_ROOT / 'data' / 'enex_processed'
LARGE_DIR = REPO_ROOT / 'data' / 'large_attachments'
DROPBOX_LINKS = LARGE_DIR / 'dropbox_links.json'
DROPBOX_LINKS_TEMPLATE = LARGE_DIR / 'dropbox_links.template.json'
REPORTS_DIR = REPO_ROOT / 'data' / 'reports'
ACTIONS_CSV = REPORTS_DIR / 'attachment_actions.csv'
AUDIT_CSV = REPORTS_DIR / 'audit_targets.csv'

ACTION_FIELDS = [
    'enex_path', 'note_title', 'note_guid', 'original_filename',
    'original_mime', 'original_mb', 'original_dims',
    'action', 'new_mime', 'new_mb', 'new_dims',
    'dropbox_url', 'old_hash', 'new_hash', 'error',
]

AUDIT_FIELDS = [
    'enex_path', 'note_title', 'note_guid', 'original_filename',
    'original_mime', 'original_mb', 'original_dims',
    'new_mime', 'new_mb', 'new_dims', 'priority_review', 'reason',
]


def load_dropbox_links() -> dict[tuple[str, str], str]:
    """Returns {(note_guid, filename): url} from dropbox_links.json."""
    if not DROPBOX_LINKS.exists():
        return {}
    payload = json.loads(DROPBOX_LINKS.read_text(encoding='utf-8'))
    return {
        (f['note_guid'], f['filename']): f.get('url', '').strip()
        for f in payload.get('files', [])
    }


def read_root_attrs(enex_path: Path) -> dict[str, str]:
    """Extract <en-export> root attributes from the file head."""
    with open(enex_path, 'rb') as fh:
        head = fh.read(16384).decode('utf-8', errors='ignore')
    m = re.search(r'<en-export\s+([^>]*)>', head)
    if not m:
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))


def process_enex(
    input_path: Path,
    output_path: Path,
    dropbox_map: dict,
    action_rows: list[dict],
    audit_rows: list[dict],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    root_attrs = read_root_attrs(input_path)
    relpath_in = str(input_path.relative_to(REPO_ROOT))

    with open(output_path, 'wb') as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n')
        out.write(b'<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export3.dtd">\n')
        attrs = ' '.join(f'{k}="{v}"' for k, v in root_attrs.items())
        out.write(f'<en-export {attrs}>\n'.encode('utf-8'))

        context = etree.iterparse(
            str(input_path), events=('end',), tag='note', huge_tree=True,
        )
        for _, note in context:
            process_note(note, relpath_in, dropbox_map, action_rows, audit_rows)
            # Re-wrap content in CDATA so HTML-like ENML markup survives serialization.
            cnt = note.find('content')
            if cnt is not None and cnt.text:
                cnt.text = etree.CDATA(cnt.text)
            out.write(etree.tostring(note, pretty_print=True))
            note.clear()
            while note.getprevious() is not None:
                del note.getparent()[0]
        del context

        out.write(b'</en-export>\n')


def process_note(
    note: etree._Element,
    enex_path: str,
    dropbox_map: dict,
    action_rows: list[dict],
    audit_rows: list[dict],
) -> None:
    note_title = child_text(note, 'title')
    note_guid = child_text(note, 'guid')
    content_el = note.find('content')
    content_text = content_el.text if (content_el is not None and content_el.text) else ''

    to_remove: list[etree._Element] = []

    for resource in list(note.findall('resource')):
        binary = resource_data_bytes(resource)
        if not binary or len(binary) < TARGET_BYTES:
            continue

        mime = resource_mime(resource)
        original_mb = round(len(binary) / 1024 / 1024, 3)
        old_hash = md5_hex(binary)
        fname = resource_filename(resource, fallback_hash=old_hash)

        row = {
            'enex_path': enex_path,
            'note_title': note_title,
            'note_guid': note_guid,
            'original_filename': fname,
            'original_mime': mime,
            'original_mb': original_mb,
            'original_dims': '',
            'action': '',
            'new_mime': '',
            'new_mb': '',
            'new_dims': '',
            'dropbox_url': '',
            'old_hash': old_hash,
            'new_hash': '',
            'error': '',
        }

        if mime.startswith('image/'):
            try:
                new_bin, new_mime, orig_dims, new_dims = downscale_image(binary, mime)
            except Exception as exc:
                row['action'] = 'error'
                row['error'] = repr(exc)
                action_rows.append(row)
                continue

            new_hash = md5_hex(new_bin)
            set_resource_data(resource, new_bin, new_mime)
            content_text = update_enmedia_hash(content_text, old_hash, new_hash, new_mime)

            row.update({
                'action': 'downscale',
                'new_mime': new_mime,
                'new_mb': round(len(new_bin) / 1024 / 1024, 3),
                'original_dims': f'{orig_dims[0]}x{orig_dims[1]}',
                'new_dims': f'{new_dims[0]}x{new_dims[1]}',
                'new_hash': new_hash,
            })
            action_rows.append(row)

            reasons = []
            if mime == 'image/png':
                reasons.append('PNG source (likely screenshot/diagram)')
            if min(orig_dims) > 0:
                ar = max(orig_dims) / min(orig_dims)
                if ar > PRIORITY_ASPECT_RATIO:
                    reasons.append(f'Aspect ratio {ar:.1f}:1 (long screenshot)')
            audit_rows.append({
                'enex_path': enex_path,
                'note_title': note_title,
                'note_guid': note_guid,
                'original_filename': fname,
                'original_mime': mime,
                'original_mb': original_mb,
                'original_dims': row['original_dims'],
                'new_mime': new_mime,
                'new_mb': row['new_mb'],
                'new_dims': row['new_dims'],
                'priority_review': is_priority_review(mime, orig_dims),
                'reason': '; '.join(reasons) or 'standard',
            })
        else:
            url = dropbox_map.get((note_guid, fname), '')
            if url:
                content_text = replace_enmedia_with_link(content_text, old_hash, url, fname)
                row['action'] = 'offload_dropbox'
                row['dropbox_url'] = url
            else:
                content_text = replace_enmedia_with_marker(content_text, old_hash, fname)
                row['action'] = 'offload_marker'
            to_remove.append(resource)
            action_rows.append(row)

    if content_el is not None:
        content_el.text = content_text
    for r in to_remove:
        note.remove(r)


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    if not ENEX_DIR.exists():
        sys.exit(f'input directory not found: {ENEX_DIR}')

    if DROPBOX_LINKS_TEMPLATE.exists() and not DROPBOX_LINKS.exists():
        sys.exit(
            f'Found {DROPBOX_LINKS_TEMPLATE.relative_to(REPO_ROOT)} but no '
            f'{DROPBOX_LINKS.relative_to(REPO_ROOT)}. Fill in URLs, rename, then re-run.'
        )

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    dropbox_map = load_dropbox_links()
    missing_urls = [k for k, v in dropbox_map.items() if not v]
    if missing_urls:
        print(f'Note: {len(missing_urls)} file(s) in dropbox_links.json have no URL — '
              'they will be replaced with "[Attachment removed]" markers.')

    enex_files = sorted(ENEX_DIR.rglob('*.enex'))
    print(f'Processing {len(enex_files)} ENEX files into {PROCESSED_DIR.relative_to(REPO_ROOT)}/...')

    action_rows: list[dict] = []
    audit_rows: list[dict] = []
    for ef in enex_files:
        rel = ef.relative_to(ENEX_DIR)
        out = PROCESSED_DIR / rel
        process_enex(ef, out, dropbox_map, action_rows, audit_rows)
        print(f'  {rel}')

    write_csv(ACTIONS_CSV, action_rows, ACTION_FIELDS)
    write_csv(AUDIT_CSV, audit_rows, AUDIT_FIELDS)

    n_downscale = sum(1 for r in action_rows if r['action'] == 'downscale')
    n_offload = sum(1 for r in action_rows if r['action'] == 'offload_dropbox')
    n_marker = sum(1 for r in action_rows if r['action'] == 'offload_marker')
    n_err = sum(1 for r in action_rows if r['action'] == 'error')
    n_priority = sum(1 for r in audit_rows if r['priority_review'])

    print()
    print('Phase 1.5 summary')
    print('-----------------')
    print(f'Downscaled images:        {n_downscale}')
    print(f'Offloaded to Dropbox:     {n_offload}')
    print(f'Removed with marker:      {n_marker}')
    print(f'Errors:                   {n_err}')
    print(f'Priority audit targets:   {n_priority} (of {len(audit_rows)} downscaled images)')
    print()
    print(f'Logs: {ACTIONS_CSV.relative_to(REPO_ROOT)}')
    print(f'      {AUDIT_CSV.relative_to(REPO_ROOT)}')


if __name__ == '__main__':
    main()
