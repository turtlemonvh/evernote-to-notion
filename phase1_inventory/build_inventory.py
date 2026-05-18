"""Phase 1: Walk every ENEX file in data/enex/ and produce an inventory CSV.

Outputs:
    data/reports/inventory.csv          one row per note
    data/reports/inventory_summary.txt  aggregate stats + per-notebook counts

Run from repo root:
    ./scripts/conda-run.sh python phase1_inventory/build_inventory.py
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.enex_parser import (  # noqa: E402
    child_text,
    extract_content,
    extract_tags,
    find_internal_links,
    iter_notes,
    resource_info,
    word_count,
)

ENEX_DIR = REPO_ROOT / 'data' / 'enex'
REPORTS_DIR = REPO_ROOT / 'data' / 'reports'
INVENTORY_CSV = REPORTS_DIR / 'inventory.csv'
SUMMARY_TXT = REPORTS_DIR / 'inventory_summary.txt'
LARGE_ATTACHMENTS_CSV = REPORTS_DIR / 'large_attachments.csv'

# Resources at or above this size get written to large_attachments.csv as
# Phase 1.5 candidates. Notion's hard cap is 5 MB; 4.5 leaves a safety margin.
LARGE_ATTACHMENT_THRESHOLD_MB = 4.5

FIELDS = [
    'title',
    'stack',
    'notebook',
    'guid',
    'created',
    'modified',
    'tags',
    'word_count',
    'has_internal_links',
    'linked_guid_count',
    'linked_guids',
    'attachment_count',
    'max_attachment_mb',
    'max_attachment_mime',
    'total_attachment_mb',
    'has_images',
    'enex_path',
]

LARGE_ATTACHMENT_FIELDS = [
    'size_mb',
    'mime',
    'filename',
    'note_title',
    'note_guid',
    'stack',
    'notebook',
    'enex_path',
]


def stack_and_notebook(enex_path: Path) -> tuple[str, str]:
    """data/enex/Hobbies/Cooking.enex -> ('Hobbies', 'Cooking')
    data/enex/Skitch.enex            -> ('', 'Skitch')"""
    rel = enex_path.relative_to(ENEX_DIR)
    if len(rel.parts) >= 2:
        return rel.parts[0], rel.stem
    return '', rel.stem


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    enex_files = sorted(ENEX_DIR.rglob('*.enex'))
    print(f'Scanning {len(enex_files)} ENEX files from {ENEX_DIR}...')

    rows: list[dict] = []
    large_rows: list[dict] = []
    threshold_bytes = int(LARGE_ATTACHMENT_THRESHOLD_MB * 1024 * 1024)

    for ef in enex_files:
        stack, notebook = stack_and_notebook(ef)
        per_file = 0
        for note in iter_notes(ef):
            content = extract_content(note)
            tags = extract_tags(note)
            linked = find_internal_links(content)
            resources = resource_info(note)
            title = child_text(note, 'title')
            guid = child_text(note, 'guid')

            sizes = [r[1] for r in resources]
            max_bytes = max(sizes, default=0)
            total_bytes = sum(sizes)
            has_images = any(r[0].startswith('image/') for r in resources)
            max_mime = ''
            if resources:
                max_mime = max(resources, key=lambda r: r[1])[0]

            rows.append({
                'title': title,
                'stack': stack,
                'notebook': notebook,
                'guid': guid,
                'created': child_text(note, 'created'),
                'modified': child_text(note, 'updated'),
                'tags': ';'.join(tags),
                'word_count': word_count(content),
                'has_internal_links': bool(linked),
                'linked_guid_count': len(linked),
                'linked_guids': ';'.join(linked),
                'attachment_count': len(resources),
                'max_attachment_mb': round(max_bytes / 1024 / 1024, 3),
                'max_attachment_mime': max_mime,
                'total_attachment_mb': round(total_bytes / 1024 / 1024, 3),
                'has_images': has_images,
                'enex_path': str(ef.relative_to(REPO_ROOT)),
            })
            per_file += 1

            for mime, size_bytes, filename in resources:
                if size_bytes >= threshold_bytes:
                    large_rows.append({
                        'size_mb': round(size_bytes / 1024 / 1024, 3),
                        'mime': mime,
                        'filename': filename,
                        'note_title': title,
                        'note_guid': guid,
                        'stack': stack,
                        'notebook': notebook,
                        'enex_path': str(ef.relative_to(REPO_ROOT)),
                    })
        print(f'  {ef.relative_to(ENEX_DIR)}: {per_file} notes')

    print(f'\nParsed {len(rows)} notes total. Writing {INVENTORY_CSV.relative_to(REPO_ROOT)}...')
    with INVENTORY_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    large_rows.sort(key=lambda r: -r['size_mb'])
    with LARGE_ATTACHMENTS_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=LARGE_ATTACHMENT_FIELDS)
        w.writeheader()
        w.writerows(large_rows)
    print(f'Wrote {LARGE_ATTACHMENTS_CSV.relative_to(REPO_ROOT)} ({len(large_rows)} attachments >= {LARGE_ATTACHMENT_THRESHOLD_MB} MB)')

    write_summary(rows, large_rows)
    print(f'Wrote {SUMMARY_TXT.relative_to(REPO_ROOT)}')


def write_summary(rows: list[dict], large_rows: list[dict]) -> None:
    notebook_counts = Counter((r['stack'], r['notebook']) for r in rows)
    with_links = sum(1 for r in rows if r['has_internal_links'])
    total_link_refs = sum(r['linked_guid_count'] for r in rows)
    big_5mb = [r for r in rows if r['max_attachment_mb'] > 5]
    big_notes = [r for r in rows if r['word_count'] > 5000]
    with_images = sum(1 for r in rows if r['has_images'])
    with_attachments = sum(1 for r in rows if r['attachment_count'] > 0)
    pct = lambda n: f'{n * 100 / max(len(rows), 1):.1f}%'

    mime_counts = Counter(r['mime'] for r in large_rows)

    lines = [
        'Phase 1 Inventory Summary',
        '=========================',
        f'Total notes:                     {len(rows)}',
        f'Notebooks (incl. stacks):        {len(notebook_counts)}',
        '',
        f'Notes with internal links:       {with_links} ({pct(with_links)})',
        f'Total internal link references:  {total_link_refs}',
        '',
        f'Notes with any attachment:       {with_attachments} ({pct(with_attachments)})',
        f'Notes with images:               {with_images} ({pct(with_images)})',
        f'Notes with attachment > 5 MB:    {len(big_5mb)}  <-- Phase 1.5 candidates',
        f'Attachments >= 4.5 MB (any):     {len(large_rows)}  (full list in large_attachments.csv)',
        '',
        f'Notes with > 5000 words:         {len(big_notes)}',
        '',
        'Notes per notebook',
        '-' * 70,
    ]
    for (stack, nb), n in sorted(notebook_counts.items()):
        label = f'{stack}/{nb}' if stack else nb
        lines.append(f'  {label:<58} {n:>6}')

    if large_rows:
        lines += [
            '',
            'Large attachments by MIME type',
            '-' * 70,
        ]
        for mime, n in mime_counts.most_common():
            lines.append(f'  {mime or "(unknown)":<40} {n:>5}')

        lines += [
            '',
            'Largest attachments (top 25)',
            '-' * 90,
            f'  {"Size MB":>8}  {"MIME":<25} {"Note title":<50}',
        ]
        for r in large_rows[:25]:
            lines.append(
                f'  {r["size_mb"]:>8.2f}  {r["mime"][:25]:<25} {r["note_title"][:50]}'
            )

    SUMMARY_TXT.write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
