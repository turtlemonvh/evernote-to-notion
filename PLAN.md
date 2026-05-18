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

## Phase 1.5: Pre-Import Attachment Optimization

**Goal:** Process the ENEX files to fit Notion's free-plan 5 MB per-attachment limit
*before* import, by downscaling oversized images and offloading non-image (or
still-too-large) attachments to Dropbox with link replacement. Designed and run
only after Phase 1 inventory reveals the actual scope.

### Script

`phase1_5_attachments/optimize_attachments.py`

### Inputs / Outputs

- **Input:** `data/enex/` (canonical archive — never modified)
- **Output:** `data/enex_processed/` (used for Phase 2 import)
- **Staging:** `data/large_attachments/` (files pending Dropbox upload)
- **Log:** `data/reports/attachment_actions.csv`

### Image downscaling

For each `<resource>` with `mime` starting `image/` and base64-decoded size > 5 MB:

1. Decode base64 from `<resource><data>`
2. Use Pillow to resize (initial: max edge 2048 px, JPEG quality 85)
3. If still over 5 MB, iterate down (1600 px, 1280 px, quality 75 / 65)
4. Re-encode to base64, replace `<data>` content
5. Recompute MD5 hash of the new binary; update every inline `<en-media hash="...">`
   reference in the note body that pointed to the old hash

### Large non-image / unshrinkable-image offload

For each remaining oversized resource:

1. Write the binary to `data/large_attachments/<note_guid>/<original_filename>`
2. Upload to Dropbox (mechanism TBD — see "Open Questions" below)
3. Replace inline `<en-media hash="...">` with `<a href="<dropbox_url>"><filename></a>`
4. Remove the `<resource>` block from the note's ENEX entry

### Critical safety rules

- `data/enex/` is never modified — all changes go to `data/enex_processed/`
- Every action logged to CSV with: note title, note GUID, resource filename, MIME,
  original bytes, action (downscale/offload/kept), new bytes, new hash, Dropbox URL
- Resource hash linkage must stay consistent — if hash changes, every inline reference
  must update or the image breaks on import

### Manual audit step

After downscaling, every modified image is logged to `data/reports/audit_targets.csv`
with a `priority_review` flag set when:
- Aspect ratio > 2.0 (long screenshot — text legibility risk), OR
- Original was PNG (often text-heavy diagrams or UI screenshots)

User opens the priority targets in Notion (or the processed ENEX) and visually
confirms the downscaled images are still legible. If any fail audit, re-run the
optimizer for that specific note with a higher quality / lower max-edge cap.

### Decisions made (post-inventory)

- Threshold: 4.5 MB (leaves safety margin under Notion's 5 MB hard cap)
- Dropbox link minting: manual — 5 files only (3 audio + 2 PDFs); user uploads
  and pastes share URLs into `data/large_attachments/dropbox_links.json`
- HEIC dependency: `pillow-heif` installed in the conda env; HEIC images
  transcode to JPEG during downscale
- Image downscale ladder: max edge 2048 px, quality 85→75→65→55, then progressive
  edge reduction if still over target. Output as JPEG except PNGs with transparency.

---

## Phase 2: Import to Notion

**Goal:** Upload the processed ENEX files (`data/enex_processed/`) into Notion,
preserving structure and embedded resources. Accept that internal links will be
broken at this stage — that is expected and will be fixed in Phase 4.

### Why not Notion's native Evernote importer?

Notion's built-in Evernote import is **OAuth-only** and pulls live from your
Evernote account — it does not accept ENEX file uploads. That bypasses all of
Phase 1.5's attachment optimization and silently drops anything over 5 MB.

### Tool

[`subimage/enex2notion`](https://github.com/subimage/enex2notion) — a fork of
the original `vzhd1701/enex2notion` that uses official Notion **integration
tokens** (not the deprecated `token_v2` cookie) and has been updated for current
Notion API. Vendored as a git submodule at `vendor/enex2notion/` so we can patch
it as needed without losing track of upstream.

### Steps

1. Submodule already initialized: `vendor/enex2notion`
2. Install in editable mode:
   ```bash
   ./scripts/conda-run.sh pip install -e vendor/enex2notion
   ```
3. Create a Notion integration at https://www.notion.so/my-integrations.
   Capabilities: Read/Update/Insert content. Copy the `secret_...` token to `.env`
   as `NOTION_TOKEN`.
4. Create a destination page in Notion (e.g. "Evernote Archive"). On that page:
   `…` menu → Connections → add the integration. Copy the page ID from the URL
   to `.env` as `NOTION_ROOT_PAGE_ID`.
5. **Spike:** dry-run + small real upload on `data/enex_processed/Skitch.enex`
   (2 notes) to verify the toolchain end to end:
   ```bash
   ./scripts/conda-run.sh enex2notion --verbose data/enex_processed/Skitch.enex
   ./scripts/conda-run.sh enex2notion --token "$NOTION_TOKEN" \
       --pageid "$NOTION_ROOT_PAGE_ID" \
       --done-file data/reports/notion_import.done \
       data/enex_processed/Skitch.enex
   ```
6. Inspect the result in Notion. If broken, patch in `vendor/enex2notion/` (the
   editable install picks up changes immediately) and commit the patch to this
   repo's history.
7. Once the spike is clean, import in waves — small/medium notebooks first to
   surface bugs cheaply; biggest last (Ionic 320, Featurespace 227, Carley and
   Timothy 173).

### Idempotence and recovery

`enex2notion` is **not "overwrite on re-run"**. It uses `--done-file` to skip
notes whose content hash is already recorded as imported.

To re-import a single note (e.g. one that imported badly):

1. Delete the bad page in Notion.
2. Remove that note's hash line from `data/reports/notion_import.done`.
3. Re-run with the same ENEX file — it will skip everything else and re-upload
   just that note.

Failed notes get a `[UNFINISHED UPLOAD]` title marker; by default the tool
deletes them on failure. Pass `--keep-failed` to retain the partial page in
Notion for inspection.

### Auth caveats

The integration token doesn't expire (unlike `token_v2` cookies), but the
integration must be explicitly **connected** to each top-level page it needs to
write under. The script writes new notebooks as children of `NOTION_ROOT_PAGE_ID`,
which inherits the connection.

### What imports correctly

- Note text and basic formatting
- Tags (become Notion database properties in `DB` mode; metadata callout in `PAGE` mode)
- Created/modified dates as database properties
- Embedded images and attachments under 5 MB
- The `<a href="dropbox URL">filename</a>` markers Phase 1.5 inserted for
  oversized files — these become plain clickable links

### What does NOT import correctly

- **Internal note links** — `evernote:///` URIs are dead links (fixed in Phase 4)
- **Encrypted note sections** — not supported, will be blank
- **Note history** — not preserved
- **Some web clip styling** — Evernote web clips are converted to plain text by
  default (`--mode-webclips=TXT`)

### Notes on the free plan file limit

Notes with attachments over 5 MB are handled by **Phase 1.5** preprocessing:
oversized images are downscaled in-place; non-image / unshrinkable attachments
are offloaded to Dropbox and replaced with link references. The version of the
ENEX library that Phase 2 imports (`data/enex_processed/`) is already
within-limit on every resource.

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
