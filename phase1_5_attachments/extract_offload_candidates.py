"""Phase 1.5 step 1: extract non-image attachments >= 4.5 MB from ENEX files.

For each oversized non-image resource, writes the binary to
    data/large_attachments/<note_guid>/<filename>

and produces
    data/large_attachments/dropbox_links.template.json

Workflow:
    1. Run this script.
    2. Manually upload each extracted file to Dropbox; copy its share link.
    3. Rename template -> dropbox_links.json and paste each URL into the "url" field.
       Leave "url" blank to mark a file for removal-with-marker instead.
    4. Run optimize_attachments.py.

Run from repo root:
    ./scripts/conda-run.sh python phase1_5_attachments/extract_offload_candidates.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.attachments import (  # noqa: E402
    TARGET_BYTES,
    md5_hex,
    resource_data_bytes,
    resource_filename,
    resource_mime,
)
from utils.enex_parser import child_text, iter_notes  # noqa: E402

ENEX_DIR = REPO_ROOT / 'data' / 'enex'
LARGE_DIR = REPO_ROOT / 'data' / 'large_attachments'
TEMPLATE_JSON = LARGE_DIR / 'dropbox_links.template.json'

# Filenames that don't fit on common filesystems get sanitized for the extraction dir.
_BAD_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    name = _BAD_FN.sub('_', name).strip()
    return name or 'attachment.bin'


def main() -> None:
    LARGE_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    enex_files = sorted(ENEX_DIR.rglob('*.enex'))
    print(f'Scanning {len(enex_files)} ENEX files for non-image attachments >= {TARGET_BYTES / 1024 / 1024:.1f} MB...')

    for ef in enex_files:
        for note in iter_notes(ef):
            note_title = child_text(note, 'title')
            note_guid = child_text(note, 'guid')

            for resource in note.findall('resource'):
                mime = resource_mime(resource)
                if mime.startswith('image/'):
                    continue
                binary = resource_data_bytes(resource)
                if len(binary) < TARGET_BYTES:
                    continue

                file_hash = md5_hex(binary)
                filename = safe_filename(resource_filename(resource, fallback_hash=file_hash))

                out_dir = LARGE_DIR / note_guid
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / filename
                if not out_path.exists():
                    out_path.write_bytes(binary)

                key = f'{note_guid}/{filename}'
                entries.append({
                    'key': key,
                    'note_title': note_title,
                    'note_guid': note_guid,
                    'filename': filename,
                    'mime': mime,
                    'size_mb': round(len(binary) / 1024 / 1024, 3),
                    'resource_hash': file_hash,
                    'extracted_to': str(out_path.relative_to(REPO_ROOT)),
                    'enex_path': str(ef.relative_to(REPO_ROOT)),
                    'url': '',
                })
                print(
                    f'  extracted: {key}  ({mime}, {len(binary) / 1024 / 1024:.2f} MB)'
                )

    payload = {
        '_instructions': (
            'Upload each file under "extracted_to" to Dropbox, copy its share '
            'link, and paste into the matching "url" field. Save this file as '
            'dropbox_links.json (drop the .template). Leave url blank to have '
            'the optimizer replace the attachment with a "[Attachment removed]" marker.'
        ),
        'files': entries,
    }
    TEMPLATE_JSON.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print(f'\nExtracted {len(entries)} file(s).')
    print(f'Template written to {TEMPLATE_JSON.relative_to(REPO_ROOT)}')
    print('Fill in the URLs, rename to dropbox_links.json, then run optimize_attachments.py.')


if __name__ == '__main__':
    main()
