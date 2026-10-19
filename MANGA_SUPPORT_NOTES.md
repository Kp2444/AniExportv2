<div align="center">

# Change Record & Verification Status

**What shipped, what was actually verified, and what is known to be broken.**

[![Tests](https://img.shields.io/badge/tests-216%20passing-success)](#-verified)
[![Browser](https://img.shields.io/badge/jsdom-16%20suites%20%C2%B7%20360%20assertions-success)](#-verified)
[![Live write](https://img.shields.io/badge/AniList%20write-guarded%20%2B%20undoable%20%28verified%20live%29-success)](#-not-verified)
[![Defects](https://img.shields.io/badge/known%20defects-4-orange)](#-known-defects)

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
[Write safety](#-write-safety-2026-10-19) ·
[Skip list](#-persistent-skip-list-2026-10-19) ·
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
python -m unittest discover -s tests -t .    # 216 tests
python -m py_compile generate_mal_xml.py gui.py
python generate_mal_xml.py --help
```

Plus **16 jsdom suites, 352 assertions**, driving the real handlers — theme, sample, clipboard,
clear, select-all under a filter, copy-unmatched, Ctrl+Enter, Escape, drag-drop, import merge, run
lifecycle, persistence, fixer, sorting, dialog focus, destination copy — four gap audits at zero,
14 state×theme contrast checks, and seven viewport widths measured in Chrome.

---

## 🐛 Known defects

Found by reading the write path and the GUI while researching [ROADMAP.md](ROADMAP.md). Each was
verified in the code. **None is a regression from the passes above — they all predate them.**
Worst first, with the phase that closes it.

Defects 1 and 4 were **reproduced against a live throwaway account** (2026-10-19, both the anime
and the manga path; evidence in `ROADMAP.md` §2.3) — measurements, not readings. **Defects 1, 3,
4, 7 and 8 are now fixed** by the write-safety pass below; they are kept here with their fixes
noted, because the measurement is the reason the fix is shaped the way it is.

> [!NOTE]
> **1 — FIXED. The direct write overwrote what was already on the account.** `iter_write_payloads`
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
> way.
>
> **Fixed** by `_reconcile_with_existing` (ROADMAP Phase 1, Step 2): progress, volumes, score and
> repeat can no longer go backwards, and `COMPLETED` is only replaced by a reread. Every change is
> shown field by field before it is sent, and each blocked field carries a per-row override. →
> **shipped 2026-10-19**

**2 — The list-wide score box re-stamps every row on every keystroke.** `inputScore`
(`gui.py:3653-3657`) is bound to `input` and does `currentAnimes.forEach(...)`. With defect 1, one
stray keypress before a write can zero the score of every entry it touches on the account. The
status select behaves the same way. Both also overwrite rows the user hand-edited, rows left
unchecked, and rows hidden by the filter. → **ROADMAP Phase 4**

**3 — FIXED. A failed existing-entries chunk was swallowed.** `anilist_existing_entries` did
`if result.get("error"): continue`, so 50 rows silently became creates with no `id` — exactly the
`mediaId`-only behaviour the design comment it sits under says it refuses to rely on. It now
raises `AniListReadError`, and both the preview and the write refuse rather than guess. →
**shipped 2026-10-19**

**4 — FIXED. A reread lowered the account's repeat count.** `repeat` is written as the literal
`1` whenever the row is marked Rereading/Rewatching. An account showing `repeat: 5` was set back
to `1`. **Measured live:** an entry set to `repeat: 5` was written as Rereading and came back
`repeat: 1`. `repeat` now gets the same never-lower rule as progress. → **shipped 2026-10-19**

**5 — The inline Find button guesses.** `repairRow` (`gui.py:3500-3535`) applies `entries[0]`
without rendering the candidate list, unlike the fixer modal. → **ROADMAP Phase 4**

**6 — Match confidence is computed and thrown away.** `_media_rank` (`:328-364`) runs
`difflib.SequenceMatcher` against both titles, but the ratio only ever serves as a sort key, so a
confidently-wrong match renders identically to a perfect one. → **ROADMAP Phase 4**

**7 — FIXED. A stale completion date was left behind.** `completedAt` is sent only when the row
carries one, which is what stops a blank row from blanking it — but it also meant a write that
moved an entry from `COMPLETED` to `CURRENT` left the old completion date in place. Found live: an
entry ended up in progress *and* finished on a date. A payload in that state now carries
`stale_completed_at`, the confirm step flags the row, and **clear the finish date** sends an empty
`FuzzyDateInput` — the only place in the tool that deliberately sends an empty value, and only
when ticked. → **shipped 2026-10-19**

**8 — FIXED. A created entry's id was discarded.** `anilist_write_entry` returns
`{"ok", "id", "status"}` and the caller kept none of it. That id is the only handle on a row the
write created, so there was no way to undo a create. The writer now records it into the undo
snapshot as each create succeeds and re-saves, so a crash mid-run still leaves the snapshot
accurate up to the last completed row. → **shipped 2026-10-19**

### Latent, not yet biting

Three facts that were harmless only because the fetched data was unused. Phase 1 compares against
it, so all three are now handled rather than latent:

- **`score` is fetched in the account's own scale.** `MediaList.score` takes a
  `format: ScoreFormat` argument; without it AniList answers in the user's setting — `POINT_3`,
  `POINT_5`, `POINT_10`, `POINT_10_DECIMAL` or `POINT_100` — while the write sends `scoreRaw` on a
  fixed 0–100. Comparing the two compares different units. **Handled:** the query now asks for
  `score (format: POINT_100)`, and the diff labels the value `/100` so it cannot be read as a 1–10
  score.
- **`progressVolumes` is `null` on anime entries.** Verified against a live account. `None < 5`
  raises `TypeError` in Python 3, so any naive "never lower" comparison crashes on every anime row.
  On manga it is written and read back correctly (`ROADMAP.md` §2.3, M1), so the guard has to be
  media-type aware rather than simply skipped. **Handled:** every comparison coerces with
  `int(x or 0)`, and the volumes rule only applies to manga.
- **AniList stamps its own `startedAt`.** On a live create with no `startedAt` sent, the entry came
  back with today's date; a create that did send one kept it (§2.3, M7). The date comes from an
  account setting, not from this tool — but it means an undo cannot reliably restore "no start
  date". **Stated, not solved:** `iter_undo_payloads` says so in its docstring, the undo confirm
  says so to the user, and the README repeats it. No amount of code on this side can override an
  account setting on the other.

### Performance limitation

The write is one `SaveMediaListEntry` per row through `_throttle()`, so **339 rows take roughly
twelve minutes**. Resolution solved the identical problem by aliasing 8 queries per document.
→ **ROADMAP Phase 2**

---

## 🛡 Write safety (2026-10-19)

The direct write was the only irreversible thing in the tool, it had no guard, and defect 1 above
is what it did when pointed at a real account. ROADMAP Phase 1, all six steps, shipped together
because they are one feature: a write you can see before it happens and reverse after it does.

**What the account is read for.** `ANILIST_EXISTING_ENTRIES_QUERY` already fetched `status`,
`progress`, `progressVolumes` and `score` and read none of them. It now also fetches `repeat`,
`notes`, `startedAt`, `completedAt` and — load-bearing, not cosmetic — `score (format: POINT_100)`.
Same request count, same 50-id chunking. A chunk that fails now raises instead of returning a
partial answer, because the ids it is missing would be written as creates and duplicate entries
the user already has.

**The policy.** `_reconcile_with_existing` applies one rule per field in place and returns the
diff rows, so what the confirm step shows is literally what will be sent. Rules and their
evidence:

| Field | Rule | Why |
|---|---|---|
| `progress` | never lower | defect 1, measured |
| `progressVolumes` | never lower, manga only | defect 1 + `null` on anime |
| `scoreRaw` | never `0` over a non-zero score | a `0` is indistinguishable from "unset" |
| `repeat` | never lower | defect 4, measured |
| `status` | `COMPLETED` only yields to `REPEATING` | a reread is the one real transition off it |
| `notes`, `startedAt`, `completedAt` | no guard; reported in the diff | already only sent when the row carries one |

A deliberate consequence, stated rather than hidden: finishing a series, starting it again, and
marking the row *Reading* rather than *Rereading* is blocked. The diff says so and the per-row
**keep mine** tick is the way through. Guessing that `COMPLETED` → `CURRENT` means a reread would
discard a completion silently, which is the bug this replaces.

**Undo.** Every write first saves what it is about to replace to
`.aniexport_undo_<unix_ts>.json` beside the response cache (gitignored, five kept, never holding a
token). A created row has no prior state, so its new entry id — which exists only in the mutation
response — is recorded into the snapshot as each create succeeds, and the file is re-saved. A
restore sends the prior values *including the empty ones*, which is the one place that has to
differ from the write path. Deleting the entries a run created is destructive, so it is listed
separately in the undo confirm and happens only when ticked; a create whose id was never recorded
is reported as unrecoverable rather than guessed at. A snapshot name arriving over HTTP is matched
against `^\.aniexport_undo_\d+\.json$` before it is opened.

**Retry.** The failed *payloads* of the most recent run are kept in memory under its run id, so
"Retry N failed" re-sends exactly those without rebuilding them or re-reading the account. The
retry keeps writing into the original run's snapshot, so an entry created on the second attempt is
still undoable.

**AniList edits some fields itself**, which the diff has to predict or it lies. Measured by probes
that bypass this tool entirely (`ROADMAP.md` §2.3, L7-L9):

- While `status` is `COMPLETED`, `progress` and `progressVolumes` are **forced to the media
  total** - when a lower value is sent explicitly, and when none is sent at all. So a forced
  downgrade on a row whose status stays `COMPLETED` is a no-op on the account.
- `REPEATING` -> `COMPLETED` **increments `repeat` by one**, whatever is sent.
- A transition *to* `REPEATING` zeroes `progress` only when no progress is sent. Every payload
  built here sends one, so this is recorded and deliberately not acted on.

`_anilist_side_effects` folds the first two into the diff, so `applied` means **what the account
will hold** rather than what the payload says, and `by_anilist` marks the fields that are not this
tool's doing. None of this was in the plan - it was found by the override test failing against a
real account, which is the whole argument for running step 3 live.

**Verified, mocked:** 60 new test methods in `tests/test_anilist_write.py` - one per policy rule,
including the blank-progress and score-zero cases that motivate the whole thing, the `COMPLETED`
-> `REPEATING` carve-out, an anime row whose `progressVolumes` is `None`, the two AniList-side
edits above, snapshot round-tripping, path traversal, pruning, both undo paths and the retry -
plus 8 markup-contract checks in `tests/test_gui_template.py` and one node suite of 57 assertions
driving the shipped diff renderer. 214 tests pass in total, 140 before this pass.
`DeleteMediaListEntry(id: Int) -> Deleted { deleted }` and `FuzzyDateInput`'s nullable fields were
confirmed by introspection against the live API before either was used.

**Verified, live** on the throwaway account, through the running server's own endpoints
(`ROADMAP.md` §2.3, P1-P12): 8 fields blocked across a 3-row conflict write, every one predicted
beforehand; `CURRENT` -> `REPEATING` still allowed, so the carve-out does not over-block; the
override honoured; the stale finish date stranded on purpose and then cleared, which **answers the
last open question - an all-`null` `FuzzyDateInput` does clear a date**; that cleared date restored
by undo; a 3-row undo matching the snapshot field for field, notes and both dates included;
retention pruning caught dropping the sixth-oldest snapshot; `created_entry_id` recorded mid-write
and the created entry deleted only once the box was ticked, the **first live exercise of
`DeleteMediaListEntry`**; and a genuine per-row rejection reported by name, then retried as 1 row
rather than 2.

**Two defects in Phase 1 as first shipped, found by that live run and fixed** (`ROADMAP.md` §2.3,
P13-P14). Both are recorded because neither was reachable by reading the code:

- **Undo could not restore `repeat` after a reread write.** The carve-out lets a row take an entry
  `COMPLETED` -> `REPEATING`; undoing that sends `REPEATING` -> `COMPLETED`, and L8's increment
  beats the `repeat` sent alongside the status. Measured: prior 4, restored as 5. A second write
  carrying only `repeat` does stick, so `iter_undo_payloads` now emits one - for a row with a
  reread count to lose only, so an ordinary undo costs no extra request.
- **Two rows resolving to one AniList entry were both written**, the later over the earlier, and
  the snapshot held two rows for one entry with only one recorded id. The XML path already refused
  this (`/api/update-export`) and the write path did not. `iter_write_payloads` now keeps the
  first and skips the rest with a reason. `deduplicate_entries` could not have done it: it keys on
  MAL id or title, and these rows differ in both.

**Two more found in Chrome once the extension connected, both fixed:**

- **The diff line read as a no-op.** A blocked field rendered `from -> applied`, so it showed
  `Completed -> Completed`, `147 -> 147`, `80/100 -> 80/100`, and what the row actually asked for
  was buried in prose. The arrow now always shows what was asked, struck through when it will not
  land, with the outcome stated beside it: `147 -> 40  stays 147`. Ticking "keep mine" beats the
  policy but not AniList, which a node test now pins.
- **Every phone override in the stylesheet was dead.** `@media (max-width: 760px)` sat at line 911,
  *above* the components it targets. Every rule in it is a plain class, the same specificity as the
  component rule, so source order decided and the component won - **13 declarations across 9
  selectors never applied**, including `.fix-card` padding and `.fix-result .cover-thumb` sizing
  from the match-picker pass, which is exactly the phone readability that pass was for. Measured in
  Chrome: `.diff-field` computed `min-width: 88px` where the narrow rule says `0`. The block now
  sits last. Two guards were added, and both were confirmed to **fail** with the block back in its
  old position before being kept - a test that cannot fail is not a test, and the earlier
  "the rules are present in the narrow block" check passed happily the whole time they were dead.

**Still not verified:** cancellation mid-write, and a write large enough to need more than one
50-id existing-entries chunk.

---

## 🧹 Persistent skip list (2026-10-19)

Unchecking a row only ever excluded it from the current list; paste the same titles again and they
came back. **Always skip unchecked** writes those titles to `localStorage` under
`aniexport.ignored`, so they stay unchecked in future lists, and **Forget N skipped** clears them.

| Decision | Why |
|---|---|
| A separate button rather than making the checkbox itself persistent | The checkbox has always meant "include in this export". Making an unchecked box permanent would change an existing control silently, which is the kind of surprise this project avoids |
| Keyed by `entryKey` — MAL id when present, else lower-cased title | Same key the session store and the server's `deduplicate_entries` use, so a remembered row matches however it was resolved |
| Stored per media type | A MAL id identifies a different work in each type, so one list per type |
| A manually picked match re-enables the row | Choosing a match by hand is explicit; it overrides the skip list rather than being silently overruled by it |
| `.toolbar-end` gained `flex-wrap: wrap` | Three buttons now live there; without it the toolbar overflowed at the 430px width the narrow pass fixed |

Covered by five static guards in `tests/test_gui_template.py::SkipListTests` (the wiring) and eight
behavioural checks run in node against the functions extracted from the template itself (the
logic): a fresh browser, persistence across a reload, per-media-type isolation, the title fallback,
an unresolved row staying disabled, the Forget button hiding itself, and a blocked `localStorage`
write surfacing as an error rather than throwing.

### Not a defect: no export path field

The GUI has no "save to this path" control, and should not grow one. `Download XML` sends
`Content-Disposition: attachment` (`gui.py:4262`), so the browser's own save dialog already chooses
the location. A server-side path field would mean accepting a filesystem path from a web page, and
any page in the browser can post to a local port.

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
