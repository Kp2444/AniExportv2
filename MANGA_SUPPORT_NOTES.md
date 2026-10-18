# Change record and verification status

Running record of what landed and what was actually verified. The user-facing description
lives in [README.md](README.md); what is queued next lives in [ROADMAP.md](ROADMAP.md).
The two planning records this work was built from, `PLAN.md` and `ANILIST_FIRST_PLAN.md`,
were removed once everything in them had shipped; they remain in git history
(`git show 8a35779:PLAN.md`).

## Manga support (first change)

- Kept the existing anime resolver and XML builder API; added AniList queries restricted to
  `type: MANGA` alongside them.
- Added a separate manga XML builder using the manga element and field names:
  `manga_mangadb_id`, `manga_title`, `manga_volumes`, `manga_chapters`, `my_read_volumes`,
  `my_read_chapters`, and `user_export_type` 2.
- Added the GUI media selector with media-specific statuses, table labels, links, progress
  metadata, builder selection and download names.
- Unknown chapter/volume counts stay `0`. Completed manga (and in-progress rereads) with known
  totals default read progress to those totals; every other status defaults to zero.
- Status strings are validated, mapped between the anime and manga vocabularies, and emitted with
  canonical MAL capitalization. Exactly one start-date and one finish-date element per entry.
- CDATA values handle embedded `]]>`.

### Two bugs this exposed

1. The GUI and the CLI wrote to the same default filenames, so a manga run could overwrite an
   anime export and serve it back under the other media type's name. Fixed with per-media paths
   (`_export_paths`) and `?media=` on the download endpoints.
2. `HTML_TEMPLATE` is a non-raw Python string, so a `\n` written for JavaScript became a real
   newline inside a JS string literal and made the **entire** `<script>` a syntax error — no
   client code ran at all. Fixed with `String.fromCharCode(10)` and guarded by
   `tests/test_gui_template.py`, which `node --check`s the extracted script and scans the
   template for backslash escapes.

## Later additions, per phase

| Phase | Landed | Verified by |
|---|---|---|
| 0 | Entry dates (`_mal_date`), per-row status, per-media export files, `/api/export-state`, `ValueError` → 400 / SSE `error`, adaptive pacing | 23 Python tests, 5 jsdom suites, live AniList calls |
| 1 | Batching (8 aliased `Page` blocks = 1 request), tiered ranking, variant retries, 7-day disk cache, `resolve_many_entries` | `tests/test_batch_resolution.py`; live: 339 titles in ~90 s vs ~12 min, warm cache 0 requests |
| 2 | Run lock (409 on a second run), cancel/pause/resume, `/api/search`, `/api/lookup`, per-row repair | `tests/test_run_control.py`, `domtest7.js` |
| 3 | localStorage session persistence, sortable columns, status chips, row cap, covers, drag-drop, Ctrl+Enter, row editor, preview modal, timestamped archives | `domtest5.js`, `domtest6.js`, archive/traversal tests |
| 4 | MAL XML import: `parse_mal_xml`, DOCTYPE refusal, 20 MB ceiling, merge keyed on MAL id, add-unmatched toggle, `.gz` via `DecompressionStream` | import cases in `tests/test_gui_manga_endpoint.py`, `domtest12.js` |
| 5 | Direct AniList write: token check, existing-entry read, add/update preview, `SaveMediaListEntry`, per-title validation messages | `tests/test_anilist_write.py` — **mocked API only** |
| 6 | README rewrite, `.gitignore`, `tests/test_cli.py`, three-way CLI reporting | 114 Python tests, 12 jsdom suites, `py_compile`, CLI smoke |
| polish | Dead code removed, accessibility, cache-header, layout and copy fixes (below) | 127 Python tests, 15 jsdom suites |

### AniList-first reframing (2026-10-18)

The validity test changed from `idMal > 0` to `anilist_id > 0`. A title AniList knows but
MyAnimeList does not is **AniList only**, not unresolved: it cannot go in the XML file but it can
be written straight to an account, so its row stays usable and its checkbox stays live when the
destination is AniList. Only a title AniList itself returns no hits for is **Not found**.

This recovered half of one 339-title list's unmatched rows — four titles existed on AniList with
`idMal: null` (AL 148167, 146267, 124629, 192823). The remaining four were misspellings that
AniList returns zero hits for, which is what **Fix unmatched** is for: it steps through each one
and accepts a search, an anilist.co / myanimelist.net link, or a bare id.

The same mislabel existed on the CLI, which reported AniList-only titles as
`omitted (missing MAL ID)`. It now reports three populations and prints the AniList ids.

## Polish pass (2026-10-18)

Removals — all verified unreachable before deleting, not assumed:

- `ANILIST_SEARCH_QUERY` and `ANILIST_MANGA_SEARCH_QUERY`: superseded by `_search_batch_query`.
  A top-level `Media` query returns one node, so it cannot carry a candidate list.
- Nine CSS rules no selector could match (`.btn-success`, `.btn-icon`, `.cell-id`, `.tag-eps`,
  `.unresolved-bar .spacer`). The only class name this template builds dynamically is
  `'toast-' + type`, so `.toast-success` / `.toast-error` were kept.
- `textarea { font-family: var(--font-mono) }`: the control system's later `textarea` rule sets
  the family at equal specificity, so the box has always rendered sans. Confirmed by reading the
  computed style in Chrome rather than from the cascade on paper.

Fixes:

- **Sortable headers were mouse-only and silent.** They now take focus, respond to Enter and
  Space, carry `aria-sort`, and only one column claims a sort at a time.
- **Dialogs were not dialogs.** `role`, `aria-modal` and `aria-labelledby` added; opening one
  moves focus inside and closing it returns focus to whatever opened it. All 14 call sites now go
  through `showModal` / `hideModal`, and a test fails the build if one toggles the class directly.
- The filter box and the select-all checkbox had no accessible name (a placeholder is not one).
- Every `<th>` carries `scope="col"`.
- `prefers-reduced-motion: reduce` disables the 14 transitions and animations.
- `color-scheme` is declared per theme, so the browser's own scrollbars, spinners and date picker
  follow the page instead of staying light on a dark page.
- The page and the export downloads send `Cache-Control: no-store`. Both change between runs; a
  cached download would hand back the previous selection's XML.

### Design pass (2026-10-18)

Done by rendering the page in Chrome at seven widths and reading the screenshots, not by reasoning
about the CSS. Three of the five findings were invisible from the source.

- **The page broke below ~760px.** Measured: at 600px the title column was squeezed to 10px; at
  430px it vanished entirely and the export buttons ran 159px off the page. One breakpoint now
  stacks the action rows and gives the table a 780px minimum so it scrolls sideways instead.
  Clean at 1440, 1280, 1080, 900, 768, 600 and 430.
- **The notice said the direct AniList write "is not built yet"** — while the Write to AniList
  button sat two inches above it. It now names that button as the answer.
- **The primary button said "Normalize & Generate XML" with the AniList destination selected.**
  It reads "Resolve titles" there, and the empty state follows the same label from one source.
- **The first run asked the same question twice**, once in the picker card and once in a
  "Choose a destination..." select below it. The select now appears only after a choice exists,
  which is what the card already promised ("Pick once; change it any time").
- **One chip read "Reading / Watching"** on a list that is only ever one media type.

## Verified

```bash
python -m unittest discover -s tests -t .    # 127 tests
python -m py_compile generate_mal_xml.py gui.py
python generate_mal_xml.py --help
```

Plus 15 jsdom suites, 295 assertions, driving the real handlers (theme, sample, clipboard, clear,
select-all under a filter, copy-unmatched, Ctrl+Enter, Escape, drag-drop, import merge, run
lifecycle, persistence, fixer, sorting, dialog focus, destination copy), four gap audits at zero,
14 state×theme contrast checks, and seven viewport widths measured in Chrome.

## Not verified

- **The direct AniList write has never been run against a live account.** All its coverage is
  mocked. The duplicate risk is designed out — existing entries are read first and updated by
  their list-entry `id` — but the live path is untested. Try a throwaway account or a handful of
  rows first, and record the result here.
- The XML is structurally tested against a public description of the MAL format, not against a
  real MyAnimeList import. Import a small sample before a list you care about.
- Ranking prefers exact titles, full series over one-shots, and released entries, but it cannot
  know intent. A confident match can still be wrong; the match picker exists for that.
