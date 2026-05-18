# Evernote → Notion Migration

A Claude Code-assisted migration of a personal Evernote library into Notion,
with full inventory, attachment optimization, automated import, and
post-migration internal-link repair.

See [PLAN.md](./PLAN.md) for the full multi-phase plan and rationale. This
README is the operator's quick reference.

## Status

| Phase | Description | Status |
|---|---|---|
| 0 | Bulk export from Evernote (`evernote-backup`) | ✅ Done |
| 1 | Build CSV inventory of every note | ✅ Done |
| 1.5 | Pre-import attachment optimization (downscale + Dropbox offload) | ✅ Done |
| 2 | Import ENEX into Notion via vendored `enex2notion` | In progress |
| 3 | Reconcile ENEX vs. Notion (find missing notes) | Pending |
| 4 | Rewrite dead `evernote:///` links to live Notion URLs | Pending |
| 5 | Manual spot-check and sign-off | Pending |

## Setup

```bash
# One-time: create the conda env and install deps
conda create -n evernote-to-notion python=3.11 -y
conda activate evernote-to-notion
pip install evernote-backup lxml pandas notion-client python-dotenv pillow pillow-heif

# Pull the vendored Notion importer (submodule)
git submodule update --init --recursive
pip install -e vendor/enex2notion

# Copy env template and fill in
cp .env.example .env
```

All Python entry points are run inside the conda env via the wrapper:

```bash
./scripts/conda-run.sh <command...>
```

Once permissions are granted, this lets the Claude Code agent run scripts without
re-activating conda for every invocation.

## Repository layout

```
.
├── PLAN.md                       # Full multi-phase migration plan
├── README.md                     # You are here
├── scripts/
│   └── conda-run.sh              # Conda-env wrapper for all Python entry points
├── utils/
│   ├── enex_parser.py            # Shared ENEX iter/parse helpers
│   └── attachments.py            # Image downscale, hash, en-media rewrite
├── phase1_inventory/
│   └── build_inventory.py        # ENEX -> inventory.csv + summary
├── phase1_5_attachments/
│   ├── extract_offload_candidates.py  # Pull non-image >4.5 MB files for Dropbox
│   └── optimize_attachments.py        # Downscale images, rewrite oversized refs
├── phase3_reconcile/
│   └── reconcile.py              # (Pending) match ENEX -> Notion via API
├── phase4_links/
│   └── fix_links.py              # (Pending) rewrite dead evernote:/// links
├── vendor/
│   └── enex2notion/              # Submodule: subimage/enex2notion (Phase 2 tool)
├── data/                         # Gitignored — all bulk content lives here
│   ├── enex/                     # Raw ENEX export (canonical archive)
│   ├── enex_processed/           # Phase 1.5 output, ready for Notion import
│   ├── large_attachments/        # Extracted >4.5 MB non-image files + Dropbox URL map
│   └── reports/                  # Inventory, action logs, audit lists, import logs
└── .env                          # Gitignored — Notion token, dropbox dir, paths
```

## Phase-by-phase quick reference

### Phase 0 — Export from Evernote

```bash
./scripts/conda-run.sh evernote-backup init-db --oauth
./scripts/conda-run.sh evernote-backup sync 2>&1 | tee data/reports/sync.log
./scripts/conda-run.sh evernote-backup export ./data/enex/ --add-guid
```

The `--add-guid` flag is mandatory — Phase 4 link repair depends on the embedded
note GUIDs.

After sync **and** after export, copy the artifacts to durable cold storage
(`en_backup.db` and `data/enex/` respectively).

### Phase 1 — Inventory

```bash
./scripts/conda-run.sh python phase1_inventory/build_inventory.py
```

Produces:
- `data/reports/inventory.csv` — one row per note
- `data/reports/inventory_summary.txt` — aggregate stats + per-notebook counts
- `data/reports/large_attachments.csv` — every resource ≥ 4.5 MB

### Phase 1.5 — Attachment optimization

```bash
# Step 1: extract non-image >=4.5MB attachments and generate a Dropbox URL template
./scripts/conda-run.sh python phase1_5_attachments/extract_offload_candidates.py
# -> data/large_attachments/dropbox_links.template.json

# Step 2 (manual): upload extracted files to Dropbox, paste share URLs into the
#                  template, rename template -> dropbox_links.json.

# Step 3: run the optimizer
./scripts/conda-run.sh python phase1_5_attachments/optimize_attachments.py
# -> data/enex_processed/...        Notion-ready ENEX files
# -> data/reports/attachment_actions.csv
# -> data/reports/audit_targets.csv  (priority_review=True items worth a visual check)
```

The optimizer never modifies `data/enex/`. Output is a parallel tree at
`data/enex_processed/`.

### Phase 2 — Import to Notion

Notion's native Evernote importer is OAuth-only and does not accept ENEX
uploads, which would bypass Phase 1.5's work. Instead we use the
[`subimage/enex2notion`](https://github.com/subimage/enex2notion) fork via the
official Notion API. See [PLAN.md → Phase 2](./PLAN.md#phase-2-import-to-notion)
for the full procedure.

```bash
# Dry-run (no token needed) — sanity-check parsing
./scripts/conda-run.sh enex2notion --verbose data/enex_processed/Skitch.enex

# Real upload — small notebook first to validate the toolchain
./scripts/conda-run.sh enex2notion \
    --token "$NOTION_TOKEN" \
    --pageid "$NOTION_ROOT_PAGE_ID" \
    --done-file data/reports/notion_import.done \
    data/enex_processed/Skitch.enex
```

Use `--done-file` from the start. To re-import a single bad note: delete it in
Notion, remove its hash from the done file, re-run on the same ENEX.

## Conventions

- **Never modify `data/enex/`** — it is the canonical local archive of the
  Evernote export. All transformations write to sibling directories.
- **Back up after each long-running step.** Convention used in this project:
  one directory per artifact under `<DROPBOX_BACKUP_DIR>/`, suffixed with the
  date (e.g. `en_backup-YYYY-MM-DD.db`, `enex-YYYY-MM-DD/`).
- **Run inside the conda env** via `scripts/conda-run.sh` — direct `python` and
  `enex2notion` invocations bypass the env and will pick up the wrong deps.

## Patching the vendored importer

The Phase 2 tool is a submodule with an editable install. To patch:

```bash
# Edit files under vendor/enex2notion/...
# Changes are immediately live; no re-install needed.
cd vendor/enex2notion && git diff   # review your changes
```

Patches are intentionally kept locally — we are not pushing back to
`subimage/enex2notion`. Patch history lives in this repo's submodule
pointer + a `vendor/patches/` directory if/when accumulated.
