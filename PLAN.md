# Evernote → Notion Migration Plan

A Claude Code-assisted migration project to move a large Evernote library into Notion,
with full inventory, import verification, and internal link restoration.

---

## Project Goals

- Export the complete Evernote library without manual notebook-by-notebook work
- Build a searchable inventory of all notes before touching Notion
- Import content into Notion and verify nothing was lost
- Automatically restore internal note-to-note links, which the Notion importer breaks
- Preserve Evernote as a read-only archive until the migration is fully verified

---

## Repository Structure

```
evernote-to-notion/
├── PLAN.md                  # This file
├── README.md                # Setup instructions and prerequisites
├── data/
│   ├── enex/                # Raw ENEX exports from evernote-backup (gitignored)
│   └── reports/             # Output CSVs and logs from each phase
├── phase1_inventory/
│   └── build_inventory.py   # Parse ENEX files, produce inventory spreadsheet
├── phase3_reconcile/
│   └── reconcile.py         # Compare ENEX inventory against Notion via API
├── phase4_links/
│   └── fix_links.py         # Resolve and rewrite internal Evernote links in Notion
├── utils/
│   ├── enex_parser.py       # Shared ENEX XML parsing helpers
│   └── notion_client.py     # Thin wrapper around Notion API calls
├── .env.example             # Template for API keys and config
└── .gitignore               # Exclude data/enex/, .env, reports
```

---

## Prerequisites

### Tools to install before starting

```bash
# Python 3.10+
pip install evernote-backup lxml pandas notion-client python-dotenv

# Verify
evernote-backup --version
```

### Credentials needed

- **Evernote**: Username/password (evernote-backup handles OAuth)
- **Notion API key**: Create an internal integration at https://www.notion.so/my-integrations
  - Grant it "Read content" and "Update content" permissions
  - Share your top-level workspace pages with the integration after creating it

Create a `.env` file (never commit this):

```
NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxx
NOTION_ROOT_PAGE_ID=         # optional: root page to scope API calls
ENEX_DIR=./data/enex
REPORTS_DIR=./data/reports
```

---

## Phase 0: Bulk Export from Evernote

**Goal:** Export the entire Evernote account to local ENEX files in one operation,
avoiding the painful one-notebook-at-a-time workflow in the Evernote desktop app.

### Tool

[`evernote-backup`](https://github.com/vzhd1701/evernote-backup) — open source Python CLI
that authenticates via Evernote's sync API and exports all notebooks automatically.

### Steps

```bash
# 1. Initialize a local database (one-time; stores auth token)
evernote-backup init-db

# 2. Sync all content from Evernote to local DB (may take a while for large libraries)
evernote-backup sync

# 3. Export to ENEX files — one .enex per notebook
#    --add-guid is CRITICAL: embeds each note's unique ID in the export,
#    which is required for internal link resolution in Phase 4
evernote-backup export ./data/enex/ --add-guid
```

### Expected output

```
data/enex/
├── Work Projects.enex
├── Business Ideas.enex
├── Personal.enex
└── ... (one file per notebook)
```

### Notes

- The sync step downloads all content including attachments. Run it on a good connection.
- `--add-guid` adds a `<guid>` tag to each `<note>` element in the XML. Without it,
  the link-resolution step in Phase 4 cannot map Evernote GUIDs to note titles.
- Keep the local database (`en_backup.db`) — you can re-run `sync` + `export` anytime
  to get fresh data without re-authenticating.
- Do **not** cancel your Evernote subscription until Phase 4 is complete and verified.

---

## Phase 1: Build Inventory

**Goal:** Parse all ENEX files and produce a complete inventory of your Evernote library
*before* importing anything into Notion. This answers "what do I actually have?" and
identifies risk factors (heavily-linked notes, large attachments, etc.).

### Script

`phase1_inventory/build_inventory.py`

### What it does

Walks every `.enex` file in `data/enex/`, parses the XML, and for each note extracts:

| Field | Source | Notes |
|---|---|---|
| `title` | `<title>` | Primary key for matching across phases |
| `notebook` | Filename of the `.enex` | Evernote notebook name |
| `guid` | `<guid>` (requires `--add-guid`) | Evernote's internal note ID |
| `created` | `<created>` | Original creation timestamp |
| `modified` | `<updated>` | Last modified timestamp |
| `tags` | `<tag>` elements | All tags as semicolon-separated list |
| `word_count` | Content text | Proxy for note importance/size |
| `has_internal_links` | `evernote:///` in content | Boolean — needs link repair |
| `linked_guids` | Parsed from `evernote:///view/...` URLs | GUIDs this note links to |
| `attachment_count` | `<resource>` elements | Number of attached files |
| `max_attachment_mb` | `<data>` size (base64 decoded) | Flag notes >5MB for free-tier issues |
| `has_images` | `<resource>` with image MIME type | Notes with embedded images |

### Output

- `data/reports/inventory.csv` — full note inventory, one row per note
- `data/reports/inventory_summary.txt` — counts by notebook, total notes, link stats,
  attachment size warnings

### Key things to review in the inventory

- **Notes with `has_internal_links = True`**: These need Phase 4 treatment. How many are there?
- **Notes with `max_attachment_mb > 5`**: These will fail to upload on Notion's free plan.
  Plan to host those attachments in Google Drive and link instead.
- **Notes with `word_count > 5000`**: Large notes worth spot-checking after import.
- **Total note count**: Sets expectations for import time and verification effort.

---

## Phase 2: Import to Notion

**Goal:** Get content into Notion using the built-in importer. Accept that internal links
will be broken at this stage — that is expected and will be fixed in Phase 4.

### Steps

1. Open Notion → Settings → Import → Evernote
2. Authenticate with your Evernote account
3. Import **one notebook at a time**, in batches — do not try to import everything at once
4. After each notebook, wait for import to complete before starting the next
5. If an import appears stuck for more than 3 hours with no new notes appearing, cancel
   and re-import that notebook in smaller note-count chunks

### What imports correctly

- Note text and basic formatting
- Tags (become Notion page properties)
- Created/modified dates (may need a Date property added manually)
- Notebooks become pages; notes become database items within those pages

### What does NOT import correctly

- **Internal note links** — will be dead `evernote:///` links (fixed in Phase 4)
- **Images** may not render and may need cleanup
- **Encrypted note sections** — not supported, will be blank
- **Note history** — not preserved

### Notes on the free plan file limit

Notes with attachments over 5MB will fail silently or import without the attachment.
The Phase 1 inventory flags these. For affected notes, options are:
- Upload the Evernote attachment to Google Drive and paste the share link into the note
- Upgrade to Notion Plus ($10/month) during migration, then decide whether to stay paid

---

## Phase 3: Verify & Reconcile

**Goal:** Compare what's in your ENEX files (Phase 1 inventory) against what actually
landed in Notion, and surface any notes that are missing or imported with corrupted titles.

### Script

`phase3_reconcile/reconcile.py`

### What it does

1. Loads `data/reports/inventory.csv` (the ground truth from Phase 1)
2. Uses the Notion API to walk all pages in the workspace and collect:
   - Page title
   - Notion page ID
   - Notion page URL
3. Matches by exact title, then fuzzy title (handles minor import formatting changes)
4. Flags any notes present in ENEX but missing from Notion

### Output

- `data/reports/reconciliation.csv` — every ENEX note with its matched Notion page ID/URL,
  or a `MISSING` flag if not found
- `data/reports/missing_notes.csv` — just the missing ones, for targeted re-import
- `data/reports/notion_page_map.csv` — title → Notion page ID/URL map (reused in Phase 4)

### Handling missing notes

For notes flagged as `MISSING`:
- Extract just those notes from the ENEX files into individual single-note ENEX files
- Re-import them individually via the Notion importer
- Re-run reconciliation to verify they're now present

### Extracting individual notes from ENEX

Claude Code can write a small helper that reads a note's `<guid>` from
`missing_notes.csv`, finds it in the source `.enex` file, and writes it out as a
standalone `.enex` file for manual re-import.

---

## Phase 4: Fix Internal Links

**Goal:** Replace every dead `evernote:///view/...` link in Notion with the correct
live Notion page URL. This is the most technically involved phase but is fully automatable.

### Script

`phase4_links/fix_links.py`

### How Evernote internal links work

An internal link in an ENEX file looks like:

```
evernote:///view/12345678/s1/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/
```

The long UUID is the target note's GUID. With `--add-guid` export, each note in the
ENEX file has its own `<guid>` tag, so a full GUID → title map can be built.

### Resolution strategy

The script builds two lookup maps:

```
Map A (from ENEX):   evernote_guid  →  note_title
Map B (from Phase 3): note_title    →  notion_page_url
```

Combining them: `evernote_guid → note_title → notion_page_url`

For each Notion page that contains dead `evernote:///` links:
1. Extract all GUIDs from the dead links
2. Look up each GUID in Map A → get the target note title
3. Look up that title in Map B → get the Notion URL
4. Update the Notion page content via API, replacing the dead link with the Notion URL

### Fallback: link text matching

You noted that your link text is usually the target note's title (or a former title).
For any GUID that can't be resolved via Map A (e.g. a note renamed over the years),
the script falls back to:
1. Read the display text of the dead link
2. Fuzzy-search Notion page titles for the closest match
3. If confidence is high (>90%), replace automatically
4. If confidence is medium (70–90%), log for manual review
5. If confidence is low (<70%), leave the link as-is and log as unresolvable

### Output

- `data/reports/link_repair_log.csv` — every link processed:
  - Source note title
  - Dead link GUID
  - Resolution method (guid_map / fuzzy / unresolved)
  - Fuzzy match confidence (if applicable)
  - Target Notion URL (if resolved)
- `data/reports/needs_review.csv` — medium-confidence fuzzy matches for manual checking
- `data/reports/unresolved_links.csv` — links that could not be matched at all

### Notion API constraint

Notion's API updates page content at the block level. The script will need to:
1. Fetch the page's block children
2. Find blocks containing `evernote:///` links in their rich text
3. Reconstruct those blocks with the link URL replaced
4. PATCH the updated blocks back via the API

This works for standard text blocks. Links embedded in tables or toggles follow the
same pattern but may require recursive block traversal.

---

## Phase 5: Spot-Check & Sign-Off

Before cancelling Evernote, manually verify a sample of notes:

- [ ] 5–10 of your most important/most-referenced notes
- [ ] 5–10 notes that had the most internal links (from Phase 1 inventory)
- [ ] Any notes flagged in `needs_review.csv`
- [ ] A random sample from each notebook
- [ ] At least one note with an image attachment
- [ ] At least one note from your oldest content

Only after this checklist is complete should you cancel the Evernote subscription.
Keep the local ENEX export and `en_backup.db` indefinitely as a cold archive.

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Import fails for some notes | Medium | Phase 3 reconciliation catches these; re-import individually |
| Attachments >5MB silently dropped | Medium–High if image-heavy | Phase 1 flags these; use Google Drive for large files |
| GUID missing from ENEX (old export without `--add-guid`) | Low if flag used | Always use `--add-guid`; fallback to link-text matching |
| Notion API rate limits during Phase 4 | Low | Add retry logic with exponential backoff in `notion_client.py` |
| Note title changed since link was created | Low–Medium | Fuzzy matching handles this; medium-confidence matches flagged for review |
| Notion importer stalls | Low | Import in smaller batches; 3-hour timeout before retry |

---

## Key Reference Links

- [`evernote-backup` on GitHub](https://github.com/vzhd1701/evernote-backup)
- [Notion API docs](https://developers.notion.com)
- [Notion Import docs](https://www.notion.com/help/import-data-into-notion)
- [ENEX format reference](https://evernote.com/blog/how-evernotes-xml-export-format-works)
- [Notion MCP server](https://developers.notion.com/guides/mcp/overview) — for post-migration Claude integration
