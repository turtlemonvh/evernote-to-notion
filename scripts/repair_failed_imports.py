"""Repair the Phase 3 'missing' notes so they can be re-imported.

For each note in data/reports/missing_notes.csv whose match_type is 'missing':

  1. Open the source ENEX file (data/enex_processed/<...>.enex), locate the
     note by guid, and sanitize URLs in its <content> CDATA:
       - strip Evernote's "_blank" artifact tail ("%20/t%20_blank...")
       - collapse double-encoded ampersands ("&amp;amp;" -> "&amp;")
       - unwrap <a> tags whose href is file:// (keep the link text only)
     Writes the ENEX file back in place.

  2. Use enex2notion's own parser on the modified file to compute the
     post-sanitization note hash, then remove that hash from
     data/reports/notion_import.done (if present). The 3 URL-failure notes
     never made it to the done file, so their hash won't be there to remove;
     the silent-title-update failure (Research: Small follicies) is the case
     where this matters.

After this script, re-run enex2notion on data/enex_processed/ — only the
repaired notes will lack done-file entries, so only they get re-uploaded.

Run from repo root:
    ./scripts/conda-run.sh python scripts/repair_failed_imports.py
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

from lxml import etree

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from enex2notion.enex_parser import iter_notes as enex_iter_notes  # noqa: E402

PROCESSED_DIR = REPO_ROOT / 'data' / 'enex_processed'
MISSING_CSV = REPO_ROOT / 'data' / 'reports' / 'missing_notes.csv'
DONE_FILE = REPO_ROOT / 'data' / 'reports' / 'notion_import.done'

# Evernote sometimes exports "_blank" target attributes mashed into the URL,
# e.g. http://example.com%20/t%20_blank, %20/t _blank etc. Truncate at the
# first such suffix.
_BLANK_TAIL_RE = re.compile(r'(%20)?/t(%20|\s| )_blank.*$', re.IGNORECASE)


def sanitize_href(url: str) -> str:
    url = _BLANK_TAIL_RE.sub('', url)
    url = url.replace('&amp;amp;', '&amp;')
    return url.strip()


def repair_content(content: str) -> tuple[str, dict]:
    """Repair <content> CDATA text. Returns (new_content, change_counts)."""
    counts = {'blank_artifact': 0, 'double_entity': 0, 'file_url_stripped': 0}

    # 1) Unwrap <a> tags whose href is file:// — keep the link text only.
    def strip_file_anchor(m: re.Match) -> str:
        counts['file_url_stripped'] += 1
        return m.group('inner')

    content = re.sub(
        r'<a\b[^>]*href="file://[^"]*"[^>]*>(?P<inner>.*?)</a>',
        strip_file_anchor,
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 2) Clean remaining href attributes.
    def clean_href(m: re.Match) -> str:
        original = m.group(1)
        cleaned = sanitize_href(original)
        if cleaned != original:
            if '_blank' in original.lower():
                counts['blank_artifact'] += 1
            if '&amp;amp;' in original:
                counts['double_entity'] += 1
        return f'href="{cleaned}"'

    content = re.sub(r'href="([^"]*)"', clean_href, content)

    return content, counts


def repair_note_in_enex(enex_path: Path, target_guid: str) -> dict | None:
    """Find the note by guid in `enex_path`, sanitize its content, write back.
    Returns counts dict for the repair or None if not found / no change."""
    tree = etree.parse(
        str(enex_path),
        parser=etree.XMLParser(huge_tree=True, strip_cdata=False),
    )
    root = tree.getroot()

    for note in root.findall('note'):
        guid_el = note.find('guid')
        if guid_el is None or guid_el.text != target_guid:
            continue

        content_el = note.find('content')
        if content_el is None or not content_el.text:
            return None

        new_text, counts = repair_content(content_el.text)
        if new_text == content_el.text:
            return counts  # nothing changed

        content_el.text = etree.CDATA(new_text)
        # Preserve the doctype on serialization.
        with open(enex_path, 'wb') as fout:
            fout.write(b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n')
            fout.write(
                b'<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export3.dtd">\n'
            )
            fout.write(etree.tostring(root, pretty_print=True))
        return counts

    return None


def compute_post_repair_hash(enex_path: Path, note_title: str) -> str | None:
    """Use enex2notion's own parser to compute the hash of a specific note
    (matched by title within the modified ENEX)."""
    for parsed in enex_iter_notes(enex_path):
        if parsed.title == note_title:
            return parsed.note_hash
    return None


def remove_hash_from_done(target_hash: str) -> bool:
    if not DONE_FILE.exists():
        return False
    lines = DONE_FILE.read_text(encoding='utf-8').splitlines()
    if target_hash not in lines:
        return False
    lines.remove(target_hash)
    DONE_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return True


def resolve_enex_path(row: dict) -> Path:
    """missing_notes.csv lacks the source path — derive from inventory layout."""
    notebook = row['notebook']
    # data/enex_processed/<stack>/<notebook>.enex, where stack may or may not exist
    candidates = list(PROCESSED_DIR.rglob(f'{notebook}.enex'))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f'no ENEX file matches notebook "{notebook}"')
    raise RuntimeError(
        f'multiple ENEX files match notebook "{notebook}": {candidates}'
    )


def main() -> int:
    if not MISSING_CSV.exists():
        print(f'FAIL: {MISSING_CSV.relative_to(REPO_ROOT)} not found — run reconcile first')
        return 1

    with MISSING_CSV.open(newline='', encoding='utf-8') as f:
        rows = [r for r in csv.DictReader(f) if r['match_type'] == 'missing']

    if not rows:
        print('Nothing to repair: no rows with match_type=missing')
        return 0

    print(f'Repairing {len(rows)} note(s)...\n')
    summary_total = {'blank_artifact': 0, 'double_entity': 0, 'file_url_stripped': 0}
    removed_hashes = 0

    for row in rows:
        guid = row['guid']
        title = row['title']
        notebook = row['notebook']
        try:
            enex_path = resolve_enex_path(row)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f'  SKIP "{title}" ({notebook}): {exc}')
            continue

        counts = repair_note_in_enex(enex_path, guid)
        if counts is None:
            print(f'  NOT FOUND "{title}" ({notebook}) at {enex_path.relative_to(REPO_ROOT)}')
            continue
        changed = any(counts.values())
        flag = 'sanitised' if changed else 'no-url-change'
        print(f'  {flag:<14} "{title}"  -> {enex_path.relative_to(REPO_ROOT)}')
        if changed:
            for k, v in counts.items():
                if v:
                    summary_total[k] += v
                    print(f'      {k}: {v}')

        new_hash = compute_post_repair_hash(enex_path, title)
        if new_hash is None:
            print(f'      WARN: could not compute post-repair hash for "{title}"')
            continue
        if remove_hash_from_done(new_hash):
            removed_hashes += 1
            print(f'      removed stale hash from done file: {new_hash}')

    print()
    print('Repair summary')
    print('-' * 40)
    for k, v in summary_total.items():
        print(f'  {k:<20} {v:>4}')
    print(f'  hashes removed       {removed_hashes:>4}')
    print()
    print('Next step:')
    print('  ./scripts/conda-run.sh enex2notion --token "$NOTION_TOKEN" \\')
    print('      --pageid "$NOTION_ROOT_PAGE_ID" \\')
    print('      --done-file data/reports/notion_import.done \\')
    print('      --skip-failed data/enex_processed/')
    return 0


if __name__ == '__main__':
    sys.exit(main())
