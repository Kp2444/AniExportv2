<div align="center">

# Change Record & Verification Status

**What shipped, what was actually verified, and what is known to be broken.**

[![Tests](https://img.shields.io/badge/tests-127%20passing-success)](#-verified)
[![Browser](https://img.shields.io/badge/jsdom-15%20suites%20%C2%B7%20295%20assertions-success)](#-verified)
[![Live write](https://img.shields.io/badge/AniList%20write-overwrites%20%28measured%20live%29-critical)](#-not-verified)
[![Defects](https://img.shields.io/badge/known%20defects-8-orange)](#-known-defects)

</div>

---

This is the running record. [README.md](README.md) is the user-facing description;
[ROADMAP.md](ROADMAP.md) is what is queued next and why.

The two planning documents this work was built from, `PLAN.md` and `ANILIST_FIRST_PLAN.md`, were
deleted once everything in them had shipped. They remain in git history:

```bash
git show 8a35779:PLAN.md
git show 8a35779:ANILIST_FIRST_PLAN.md
```

### Contents

[Manga support](#-manga-support-the-first-change) ·
[Per-phase record](#-later-additions-per-phase) ·
[AniList-first](#anilist-first-reframing-2026-10-18) ·
[Polish](#-polish-pass-2026-10-18) ·
[Verified](#-verified) ·
[Known defects](#-known-defects) ·
[Not verified](#-not-verified)

---

## 📚 Manga support, the first change

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

> [!WARNING]
> **1. Shared export filenames.** The GUI and the CLI wrote to the same default paths, so a manga
> run could overwrite an anime export and then serve it back under the *other* media type's
> filename — silent, and the file looked plausible. Fixed with per-media paths (`_export_paths`)
> and `?media=` on the download endpoints.

> [!WARNING]
> **2. The entire `<script>` was a syntax error.** `HTML_TEMPLATE` is a non-raw Python string, so a
> `\n` written for JavaScript became a real newline *inside* a JS string literal. No client code
> ran at all — the page rendered and was completely inert. Fixed with `String.fromCharCode(10)`
> and guarded by `tests/test_gui_template.py`, which `node --check`s the extracted script and
> scans the template for backslash escapes.

---

## 🗂 Later additions, per phase

| Phase | Landed | Verified by |
|---|---|---|
| **0** | Entry dates (`_mal_date`), per-row status, per-media export files, `/api/export-state`, `ValueError` → 400 / SSE `error`, adaptive pacing | 23 Python tests, 5 jsdom suites, live AniList calls |
| **1** | Batching (8 aliased `Page` blocks = 1 request), tiered ranking, variant retries, 7-day disk cache, `resolve_many_entries` | `tests/test_batch_resolution.py`; live: 339 titles in ~90 s vs ~12 min, warm cache 0 requests |
| **2** | Run lock (409 on a second run), cancel/pause/resume, `/api/search`, `/api/lookup`, per-row repair | `tests/test_run_control.py`, `domtest7.js` |
| **3** | localStorage session persistence, sortable columns, status chips, row cap, covers, drag-drop, Ctrl+Enter, row editor, preview modal, timestamped archives | `domtest5.js`, `domtest6.js`, archive/traversal tests |
| **4** | MAL XML import: `parse_mal_xml`, DOCTYPE refusal, 20 MB ceiling, merge keyed on MAL id, add-unmatched toggle, `.gz` via `DecompressionStream` | import cases in `tests/test_gui_manga_endpoint.py`, `domtest12.js` |
| **5** | Direct AniList write: token check, existing-entry read, add/update preview, `SaveMediaListEntry`, per-title validation messages | `tests/test_anilist_write.py` — ⚠️ **mocked API only** |
| **6** | README rewrite, `.gitignore`, `tests/test_cli.py`, three-way CLI reporting | 114 Python tests, 12 jsdom suites, `py_compile`, CLI smoke |
| **polish** | Dead code removed; accessibility, cache-header, layout and copy fixes | 127 Python tests, 15 jsdom suites |

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

---

## ✨ Polish pass (2026-10-18)

### Removals — each verified unreachable before deleting, not assumed

| Removed | Why it was dead |
|---|---|
| `ANILIST_SEARCH_QUERY`, `ANILIST_MANGA_SEARCH_QUERY` | Superseded by `_search_batch_query`. A top-level `Media` query returns one node, so it cannot carry a candidate list |
| Nine CSS rules (`.btn-success`, `.btn-icon`, `.cell-id`, `.tag-eps`, `.unresolved-bar .spacer`) | No selector could match them. The only class name this template builds dynamically is `'toast-' + type`, so `.toast-success` / `.toast-error` were **kept** |
| `textarea { font-family: var(--font-mono) }` | The control system's later `textarea` rule sets the family at equal specificity, so the box has always rendered sans. Confirmed by reading the computed style in Chrome rather than from the cascade on paper |

### Fixes

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
about the CSS. **Three of the five findings were invisible from the source.**

| Finding | Detail |
|---|---|
| **The page broke below ~760px** | Measured: at 600px the title column was squeezed to 10px; at 430px it vanished entirely and the export buttons ran 159px off the page. One breakpoint now stacks the action rows and gives the table a 780px minimum so it scrolls sideways instead. Clean at 1440, 1280, 1080, 900, 768, 600 and 430 |
| **A shipped feature was disclaimed** | The notice said the direct AniList write "is not built yet" — while the Write to AniList button sat two inches above it. It now names that button as the answer |
| **The primary button named the wrong destination** | It said "Normalize & Generate XML" with the AniList destination selected. It reads "Resolve titles" there, and the empty state follows the same label from one source |
| **The first run asked the same question twice** | Once in the picker card and once in a "Choose a destination…" select below it. The select now appears only after a choice exists, which is what the card already promised ("Pick once; change it any time") |
| **One chip named both media types** | It read "Reading / Watching" on a list that is only ever one media type |

---

## ✅ Verified

```bash
python -m unittest discover -s tests -t .    # 127 tests
python -m py_compile generate_mal_xml.py gui.py
python generate_mal_xml.py --help
```

Plus **15 jsdom suites, 295 assertions**, driving the real handlers — theme, sample, clipboard,
clear, select-all under a filter, copy-unmatched, Ctrl+Enter, Escape, drag-drop, import merge, run
lifecycle, persistence, fixer, sorting, dialog focus, destination copy — four gap audits at zero,
14 state×theme contrast checks, and seven viewport widths measured in Chrome.

---

## 🐛 Known defects

Found by reading the write path and the GUI while researching [ROADMAP.md](ROADMAP.md). Each was
verified in the code. **None is a regression from the passes above — they all predate them.**
Worst first, with the phase that closes it.

Defects 1 and 4 have since been **reproduced against a live throwaway account** (2026-10-19, both
the anime and the manga path; evidence in `ROADMAP.md` §2.3). They are measurements now, not
readings.

> [!CAUTION]
> **1 — The direct write overwrites what is already on the account.** `iter_write_payloads`
> (`generate_mal_xml.py:1379-1450`) sends `status`, `scoreRaw` and `progress` on *every* row
> unconditionally, and takes only `id` from the existing entry. If the account is at chapter 224
> and the pasted row says 142, **142 is written**. If the row is blank, `_clamp_progress` makes it
> `0` and **`0` is written**. The existing `status`, `progress`, `progressVolumes` and `score`
> *are* fetched (`ANILIST_EXISTING_ENTRIES_QUERY`, `:1219-1232`) and then never read. The confirm
> step shows counts only (`gui.py:4553-4561`), so none of this is visible before it happens.
>
> **Measured live, and worse than the paragraph above.** A manga entry sitting at
> `COMPLETED, 147 chapters, 13 volumes, score 90` was handed one row with the chapters left blank
> and a status of Reading. It came back `CURRENT, 0, 0, score 0` — four fields destroyed in one
> write, reported as `"ok": true`. On the anime path an account at episode 10 went to 3 the same
> way. → **ROADMAP Phase 1**

**2 — The list-wide score box re-stamps every row on every keystroke.** `inputScore`
(`gui.py:3653-3657`) is bound to `input` and does `currentAnimes.forEach(...)`. With defect 1, one
stray keypress before a write can zero the score of every entry it touches on the account. The
status select behaves the same way. Both also overwrite rows the user hand-edited, rows left
unchecked, and rows hidden by the filter. → **ROADMAP Phase 4**

**3 — A failed existing-entries chunk is swallowed.** `anilist_existing_entries` does
`if result.get("error"): continue` (`:1358-1359`), so 50 rows silently become creates with no
`id` — exactly the `mediaId`-only behaviour the design comment at `:1209-1213` says it refuses to
rely on. → **ROADMAP Phase 1**

**4 — A reread lowers the account's repeat count.** `repeat` is written as the literal `1`
(`:1430-1431`) whenever the row is marked Rereading/Rewatching. An account showing `repeat: 5` is
set back to `1`. **Measured live:** an entry set to `repeat: 5` was written as Rereading and came
back `repeat: 1`. → **ROADMAP Phase 1**

**5 — The inline Find button guesses.** `repairRow` (`gui.py:3500-3535`) applies `entries[0]`
without rendering the candidate list, unlike the fixer modal. → **ROADMAP Phase 4**

**6 — Match confidence is computed and thrown away.** `_media_rank` (`:328-364`) runs
`difflib.SequenceMatcher` against both titles, but the ratio only ever serves as a sort key, so a
confidently-wrong match renders identically to a perfect one. → **ROADMAP Phase 4**

**7 — A stale completion date is left behind.** `completedAt` is sent only when the row carries
one (`:1435-1440`), which is what stops a blank row from blanking it — but it also means a write
that moves an entry from `COMPLETED` to `CURRENT` leaves the old completion date in place. Found
live: an entry ended up in progress *and* finished on a date. Nothing in the tool can clear it,
since clearing requires sending an empty value. → **ROADMAP Phase 1**

**8 — A created entry's id is discarded.** `anilist_write_entry` returns `{"ok", "id", "status"}`
(`:1453-1462`) and the caller keeps none of it (`gui.py:4620-4640`). That id is the only handle on
a row the write created, so there is currently no way to undo a create. → **ROADMAP Phase 1**

### Latent, not yet biting

Two facts that are harmless today only because the fetched data is unused. They become defects the
moment anything compares against it, and ROADMAP Phase 1 does exactly that:

- **`score` is fetched in the account's own scale.** `MediaList.score` takes a
  `format: ScoreFormat` argument; without it AniList answers in the user's setting — `POINT_3`,
  `POINT_5`, `POINT_10`, `POINT_10_DECIMAL` or `POINT_100` — while the write sends `scoreRaw` on a
  fixed 0–100. Comparing the two compares different units.
- **`progressVolumes` is `null` on anime entries.** Verified against a live account. `None < 5`
  raises `TypeError` in Python 3, so any naive "never lower" comparison crashes on every anime row.
  On manga it is written and read back correctly (`ROADMAP.md` §2.3, M1), so the guard has to be
  media-type aware rather than simply skipped.
- **AniList stamps its own `startedAt`.** On a live create with no `startedAt` sent, the entry came
  back with today's date; a create that did send one kept it (§2.3, M7). The date comes from an
  account setting, not from this tool — but it means an undo cannot reliably restore "no start
  date", which Phase 1's snapshot design has to state rather than assume.

### Performance limitation

The write is one `SaveMediaListEntry` per row through `_throttle()`, so **339 rows take roughly
twelve minutes**. Resolution solved the identical problem by aliasing 8 queries per document.
→ **ROADMAP Phase 2**

---

## ⚠️ Not verified

> [!IMPORTANT]
> **The direct AniList write has been run against a live throwaway account** (2026-10-19, both the
> anime and the manga path; evidence in `ROADMAP.md` §2.3). Established: creates work,
> `progressVolumes`, `notes` and dates all land correctly, `0000-00-00` is correctly skipped, a
> repeated write of the same row is classified as an update and keeps the same list-entry `id` — so
> duplicate creation really is designed out — and defects 1, 4 and 7 are real rather than inferred.
>
> Still untested live: `DeleteMediaListEntry`, a multi-row write, cancellation mid-write, and
> **every failure path** — each row in both runs returned `"ok": true`, so nothing exercised the
> per-row error reporting. All automated coverage remains mocked. Add what you find here.

- The XML is structurally tested against a public description of the MAL format, not against a
  real MyAnimeList import. Import a small sample before a list you care about.
- Ranking prefers exact titles, full series over one-shots, and released entries, but it cannot
  know intent. A confident match can still be wrong; the match picker exists for that.

<div align="center">

Records are kept honest on purpose · see [ROADMAP.md](ROADMAP.md) for what closes each defect

</div>
