<div align="center">

# Change Record & Verification Status

**What shipped, what was actually verified, and what is known to be broken.**

[![Tests](https://img.shields.io/badge/tests-333%20passing-success)](#-verified)
[![Browser](https://img.shields.io/badge/jsdom-16%20suites%20%C2%B7%20360%20assertions-success)](#-verified)
[![Live write](https://img.shields.io/badge/AniList%20write-guarded%20%2B%20undoable%20%28verified%20live%29-success)](#-not-verified)
[![Defects](https://img.shields.io/badge/known%20defects-0-success)](#-known-defects)

</div>

---

This is the running record. [README.md](README.md) is the user-facing description;
Every claim here is backed by [a measurement against the live API](#-measured-against-the-live-api).

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
[Batched writes](#-batched-writes-2026-10-19) ·
[Reading a list](#-reading-a-list-back-out-of-anilist-2026-10-19) ·
[Match confidence](#-match-confidence-2026-10-19) ·
[Still open](#-still-open) ·
[Measured live](#-measured-against-the-live-api) ·
[Ground-up audit](#-ground-up-audit-2026-10-20) ·
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
python -m unittest discover -s tests -t .    # 333 tests
python -m py_compile generate_mal_xml.py gui.py
python generate_mal_xml.py --help
```

Plus **16 jsdom suites, 352 assertions**, driving the real handlers — theme, sample, clipboard,
clear, select-all under a filter, copy-unmatched, Ctrl+Enter, Escape, drag-drop, import merge, run
lifecycle, persistence, fixer, sorting, dialog focus, destination copy — four gap audits at zero,
14 state×theme contrast checks, and seven viewport widths measured in Chrome.

---

## 🐛 Known defects

Found by reading the write path and the GUI while planning the write-safety work. Each was
verified in the code. **None is a regression from the passes above — they all predate them.**
Worst first, with the phase that closes it.

Defects 1 and 4 were **reproduced against a live throwaway account** (2026-10-19, both the anime
and the manga path; evidence in [measured live](#-measured-against-the-live-api)) — measurements, not readings. **Defects 1, 3,
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
> **Fixed** by `_reconcile_with_existing`: progress, volumes, score and
> repeat can no longer go backwards, and `COMPLETED` is only replaced by a reread. Every change is
> shown field by field before it is sent, and each blocked field carries a per-row override. →
> **shipped 2026-10-19**

**2 — FIXED. The list-wide score box re-stamped every row on every keystroke.** `inputScore` was
bound to `input` and did `currentAnimes.forEach(...)`, so typing "10" wrote a 1 across the whole
list first. The status select behaved the same way. Both also overwrote rows the user had
hand-edited, rows left unchecked, and rows hidden by the filter. Both now go through
`bulkTargets()` — selected-and-visible rows, or every visible row when nothing is selected — the
score moved to `change`, and each control carries a label saying which of the two is in force. →
**shipped 2026-10-19**

**3 — FIXED. A failed existing-entries chunk was swallowed.** `anilist_existing_entries` did
`if result.get("error"): continue`, so 50 rows silently became creates with no `id` — exactly the
`mediaId`-only behaviour the design comment it sits under says it refuses to rely on. It now
raises `AniListReadError`, and both the preview and the write refuse rather than guess. →
**shipped 2026-10-19**

**4 — FIXED. A reread lowered the account's repeat count.** `repeat` is written as the literal
`1` whenever the row is marked Rereading/Rewatching. An account showing `repeat: 5` was set back
to `1`. **Measured live:** an entry set to `repeat: 5` was written as Rereading and came back
`repeat: 1`. `repeat` now gets the same never-lower rule as progress. → **shipped 2026-10-19**

**5 — FIXED. The inline Find button guessed.** `repairRow` applied `entries[0]` without rendering
the candidate list, which is the same confidently-wrong failure the fixer exists to prevent. More
than one hit, or a top hit below the review floor, now opens the fixer on that row with the
results already in hand. A bare id still applies outright: there is nothing to choose between. →
**shipped 2026-10-19**

**6 — FIXED, and the fix is smaller than it looks.** `_media_rank` ran `difflib.SequenceMatcher`
against both titles and used the ratio only as a sort key, so a confidently-wrong match rendered
identically to a perfect one. The ratio is now extracted into `_title_ratio`, carried on every
parsed entry as `match_ratio`, and paired with `runner_up_gap`. **What the calibration showed is
that the ratio is a poor correctness signal on real data** — see "Match confidence" below. →
**shipped 2026-10-19**

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
  On manga it is written and read back correctly ([measured live](#-measured-against-the-live-api), M1), so the guard has to be
  media-type aware rather than simply skipped. **Handled:** every comparison coerces with
  `int(x or 0)`, and the volumes rule only applies to manga.
- **AniList stamps its own `startedAt`.** On a live create with no `startedAt` sent, the entry came
  back with today's date; a create that did send one kept it (§2.3, M7). The date comes from an
  account setting, not from this tool — but it means an undo cannot reliably restore "no start
  date". **Stated, not solved:** `iter_undo_payloads` says so in its docstring, the undo confirm
  says so to the user, and the README repeats it. No amount of code on this side can override an
  account setting on the other.

### Performance limitation — fixed

The write was one `SaveMediaListEntry` per row through `_throttle()`, so **339 rows took roughly
twelve minutes**. It now batches eight per request, the same trick resolution uses, measured at
one rate-limit unit per document. → **shipped 2026-10-19**, see "Batched writes" above

---

## 🛡 Write safety (2026-10-19)

The direct write was the only irreversible thing in the tool, it had no guard, and defect 1 above
is what it did when pointed at a real account. All six steps of the write-safety work shipped together
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
that bypass this tool entirely ([L7-L9](#-measured-against-the-live-api)):

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
([P1-P12](#-measured-against-the-live-api)): 8 fields blocked across a 3-row conflict write, every one predicted
beforehand; `CURRENT` -> `REPEATING` still allowed, so the carve-out does not over-block; the
override honoured; the stale finish date stranded on purpose and then cleared, which **answers the
last open question - an all-`null` `FuzzyDateInput` does clear a date**; that cleared date restored
by undo; a 3-row undo matching the snapshot field for field, notes and both dates included;
retention pruning caught dropping the sixth-oldest snapshot; `created_entry_id` recorded mid-write
and the created entry deleted only once the box was ticked, the **first live exercise of
`DeleteMediaListEntry`**; and a genuine per-row rejection reported by name, then retried as 1 row
rather than 2.

**Two defects in Phase 1 as first shipped, found by that live run and fixed** ([P13-P14](#-measured-against-the-live-api)). Both are recorded because neither was reachable by reading the code:

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

## 📦 Batched writes (2026-10-19)

The write sent one `SaveMediaListEntry` per row through a ~2.1s throttle, so 339 rows took roughly
twelve minutes. `_write_batch_query` now builds one document of up to eight aliased saves, with
only the arguments each row actually carries declared - sending an explicit null for a field the
row left alone would blank it on the account.

**Partial failure.** Aliases execute serially and a failing one does not stop the rest, so the
authoritative per-row signal is `data["mk"] is None`. Error messages carry `locations` but no
`path`, so they are matched back to an alias by the character offset recorded when the document
was built; an error that cannot be placed is reported against every failed alias in that batch
rather than guessed at.

> [!CAUTION]
> **One bad row used to take its whole batch down, and the audit found it.** AniList validates the
> arguments of the entire document *before* executing any of it and answers HTTP 400, so a single
> stale media id came back as `{m0: null, m1: null, m2: null}` - **the good rows never ran**. The
> roadmap's premise that "a failing alias does not stop later ones" is true for runtime errors and
> false for validation. Worse, "Retry failed" re-batched the same rows, so the batch could never
> succeed. Measured live: 3 rows with one bad id wrote **0 of 3**.
>
> `anilist_write_batch` now re-sends the stalled rows one at a time when a document is refused on
> validation, and only then - auth and transport failures are not retried, because every row would
> fail the same way. Same case after the fix: **2 of 3 written**, the bad row named on its own.

**Measured, at the size that ships.** The original spike used two aliases and argued eight should
behave the same. A real eight-alias document took `X-RateLimit-Remaining` from 18 to 17 - **one
request for eight mutations**, against a control request that also cost 1 - and all eight aliases
applied. A three-row write through the endpoint took 4.6s wall clock with one request, three
progress events and an ETA.

**The undo batches too.** Restores are `SaveMediaListEntry` like any write, so they go eight to
a document; deletes are a different mutation and cannot share one, so they stay serial. Order
survives batching, which matters because a repeat repair is a second write for a row and has to
land after the restore it repairs - verified live: 4 restores went out as **one request**, with
four progress events and `restored: 3`, the repair not counted twice.

**The snapshot is saved once per create, not once per batch.** Saving per batch left a window
where a crash between a batch returning and the file being written stranded up to eight created
entries with no recorded id - and undo cannot delete what it cannot name. The file is small and
the write is atomic, so the cost is noise.

**Two things the UI had to say differently.** The write phase gained the ETA only the resolve
phase had. And cancellation is now batch-level: a cancel lands between batches, so the rows
already in flight still get written, and the toast says so rather than implying otherwise.

---

## 📥 Reading a list back out of AniList (2026-10-19)

Nothing could read an AniList list, which made AniList → MAL XML impossible and left no way to ask
"how much of this list am I already tracking". `anilist_list_collection` reads one, chunking while
`hasNextChunk`.

Details that are not arbitrary:

- `forceSingleCompletedList: true`, or a split completed list comes back as several groups.
- Groups with `isCustomList` are skipped, and everything is deduped by media id anyway: a custom
  list repeats entries that already appear under their status group.
- `score (format: POINT_100)`, for the same reason the write needed it - a bare score arrives in
  whatever format the account displays.
- A FuzzyDate becomes a date **only when all three parts are present**. A partial one becomes
  blank rather than an invented first-of-the-month.
- `ANILIST_STATUS_REVERSE` is media-type aware, because the forward map is not invertible:
  `CURRENT` is reached from both "reading" and "watching". A test asserts every enum member maps
  back for both types, so a future AniList addition fails loudly.
- `perChunk` starts at 500 and halves **at most twice**. A wrong username fails identically to an
  over-large chunk, and the original five retries only delayed a wrong answer; the error now names
  all three possible causes instead of claiming AniList was unreachable.

A token travels even when a username is given. Dropping it meant you could not read **your own
private** list by typing your own name - only by leaving the field blank. Visibility is decided by
who the request authenticates as, so sending both is simply correct.

Three surfaces, one merge: `POST /api/anilist-import`, a **From AniList…** button, and
`--from-anilist USERNAME` on the CLI. The button reuses the MAL-XML import merge rather than
growing a second one - the merge gained an AniList-id fallback, since an AniList row may have no
MAL id at all and a MAL export never carries an AniList id.

**Measured:** 14 entries read from a real account with a token *and* by public username with none;
written to MAL XML by the CLI; read back by `parse_mal_xml` with status, score, progress, notes
and both dates intact. The one entry with no MAL id was reported, not dropped.

---

## 🔍 Match confidence (2026-10-19)

`_media_rank` computed a title-similarity ratio and used it only as a sort key, so a confidently
wrong match looked exactly like a perfect one. The ratio is now extracted into `_title_ratio`,
used by both the ranker and the badge, and carried on every parsed entry.

**The calibration is the interesting part, because it overturned the plan.** Run over the user's
own 338-line `manga.txt` (339 titles, 268 resolved):

| Threshold | Rows flagged |
|---|---|
| `ratio < 0.6` (proposed) | 121 of 268 — **45.1%** |
| `ratio < 0.6 or gap < 0.05` (proposed rule) | 138 of 268 — **51.5%** |
| `gap < 0 or ratio < 0.15` (shipped) | 9 of 268 — **3.4%** |

A rule that flags half the list is not a review queue; it trains the user to ignore the chip. The
reason the proposed floor fails is visible in the data: this list is full of translated titles
whose official romaji name shares almost no characters with the English one that was pasted.
`First God Of War` resolves **correctly** to `Di Yi Zhan Shen` at a ratio of 0.129. Median ratio is
0.677 and a third of all matches are exact, so the low tail is mostly correct, not wrong.

**No floor works.** The known-bad case recorded in the original plan —
`I Am Being Chased To Fall In Love Everyday` → `Forced to Date with BOSS Everyday` — sits at
**0.56**, mid-distribution. A floor of 0.55 misses it; 0.6 catches it along with 121 others. It
resolved to a single candidate, so the runner-up gap cannot catch it either. **That case is not
detectable by either signal**, and saying so is more honest than shipping a threshold that flags
half the list to catch one row.

**What does carry signal is an inversion:** the entry the ranker chose matched the searched title
*worse* than a candidate it passed over, because a tier above the title score - usually "has a MAL
id" - overrode it. Five of 268 rows, and all five look wrong on inspection, the worst being
`Dragon Master` → `I Got Reincarnated as a Maid…` at a gap of −0.612. A plain **tie** is not a
signal: `One Punch Man` ties with `One-Punch Man` at ratio 1.0 and is right, which is why
`REVIEW_TIE_GAP` is 0.0 rather than the proposed 0.05.

The chip is a filter and the row carries the reason, because "needs review" with no explanation is
just nagging. A hand-picked match clears the flag: the ratio describes the original search and
means nothing once a person has chosen.

**A bug the calibration found first.** The bulk resolver parses the chosen node directly and
attaches `_candidates` separately, so `runner_up_gap` - which only exists on the ranked list -
never reached the resolved row. Every row in the first run came back with `gap: None`.

**Cost:** `CACHE_VERSION` goes to 2, so every cache is invalidated once and the next run is a cold
resolve. The alternative is serving entries with no confidence data at all.

---

## 🧭 Still open

Not unfinished work - everything planned has shipped and been verified. These two need data that
only a real account can supply:

- **The review floor has only been calibrated on manga.** `REVIEW_RATIO_FLOOR = 0.15` came from a
  338-line manga list where translated titles are the norm and a correct match often shares almost
  no characters with what was pasted. An anime list should sit much higher, so the constant may
  deserve to be per media type rather than one number. Re-run the calibration against an anime list
  before changing it.
- **Any change to `_media_rank` invalidates both thresholds.** They describe the ranker as it is
  today, not a property of the data.

---

## 🔬 Measured against the live API

Everything below was observed, not reasoned about: a response from
`graphql.anilist.co`, or an account read back after a write. The labels are stable because the
code cites them — a comment saying "measured, not assumed (L5)" points here.

These moved from the planning document when it was deleted; all four phases it described had
shipped and been verified. It lives in git history (`git show 7e90450:ROADMAP.md`) along with the
reasoning that produced them.

### Schema facts (E) — introspection and unauthenticated queries

| # | Fact |
|---|---|
| E2 | `MediaListCollection` accepts a **username with no token**, and pages through `hasNextChunk` |
| E4 | **`MediaList.score` takes `format: ScoreFormat`.** Without it the account's own display scale comes back, so a `POINT_5` user's 4 would be compared against a 0–100 value |
| E6 | **`progressVolumes` is `null` on anime entries** |
| E8 | **Split completed lists appear as separate groups with the same status.** One account returned `Completed TV` (7) and `Completed TV Short` (1); `forceSingleCompletedList: true` merged them into one group of 8 |
| E9 | `MediaListStatus` is exactly `CURRENT, PLANNING, COMPLETED, DROPPED, PAUSED, REPEATING` |
| E10 | `FuzzyDate` and `FuzzyDateInput` are both `{year, month, day}` as nullable `Int` |
| E11 | Aliased mutations parse and resolve **per alias** — each gets its own entry in `data` and its own error with `locations`. True for runtime errors; **false for validation**, see P22 |
| E12 | `errors[]` carries `message`, `status`, `locations` and `validation`. **No `path` field**, so an error cannot be mapped to a row by path |
| E13 | `DeleteMediaListEntry(id)` exists and returns `Deleted { deleted }` |
| E15 | `SaveMediaListEntry` accepts `id, mediaId, status, score, scoreRaw, progress, progressVolumes, repeat, priority, private, notes, hiddenFromStatusLists, customLists, advancedScores, startedAt, completedAt` |

### The write, on a throwaway account (L, M)

The account was empty before the first run. Media ids were resolved through this repo's own
lookups, so nothing depended on search ranking.

| # | Step | Result |
|---|---|---|
| L1 | Write Lain at episode 3 onto an empty account | Entry created, `CURRENT progress=3`. **The create path had never been run before** |
| L2 | Write the identical row again | Classified as an update, entry id **unchanged**. The one guarantee the README makes — a repeat cannot duplicate — holds live |
| L3 | Write episode 10, then episode 3 | Account went `3 → 10 → 3`. **The overwrite reproduced, reported as `"ok": true`** |
| L5 | Rate-limit cost of a 2-alias mutation document | Control: three single requests took `X-RateLimit-Remaining` 15 → 14 → 13. The aliased document then took it 13 → 12: **1 request for 2 mutations** |
| L6 | Did both aliases apply? | Yes — two distinct new entries, both present on a fresh read |
| L7 | **While `status` is `COMPLETED`, AniList forces `progress` and `progressVolumes` to the media total** | Sent `COMPLETED` + `progress: 40` against a 147-chapter work → stored **147**. An entry at `CURRENT, 40` sent only `status: COMPLETED` → also **147**. `CURRENT` + 40 stores 40, so the status is what does it |
| L8 | **`REPEATING` → `COMPLETED` increments `repeat` by one**, whatever is sent | Sent `repeat: 5` → stored **6**. Sent none → also **6** |
| L9 | A transition **to** `REPEATING` zeroes `progress` only when none is sent | With `progress: 100` → kept 100; without → 0. Every payload here sends one, so this cannot bite |
| M1 | Create Punpun at chapter 40 / volume 4 | `ch=40 vol=4`. **`progressVolumes` works live.** AniList also stamped `startedAt` with today, which was never sent |
| M3 | Same row with **chapters blank**, `Reading` | `COMPLETED 147/13 score 90` → **`CURRENT 0/0 score 0`.** One blank row destroyed four fields, reported `"ok": true` |
| M4 | `completedAt` after M3 | **Still set.** The entry was in progress *and* finished on a date |
| M5 | `repeat: 5`, then write the row as `Rereading` | `5` → **`1`**: the literal `1` in the payload reset the account's count |
| M6 | Create with notes and dates, `finish_date: 0000-00-00` | Notes and `startedAt` both stored; the placeholder date correctly skipped |
| M7 | Does AniList invent a `startedAt`? | **Yes**, when an entry becomes in-progress without one. An account setting, not this tool — so restoring "no start date" may not be possible |

### Write safety, after it shipped (P)

| # | Step | Result |
|---|---|---|
| P1 | 3-row write with deliberate conflicts | **8 fields blocked across 3 rows**, every one named in the preview first. The account did not move. Two blocks the plan never listed: a `Plan to read` row would have zeroed 249 chapters *and* 22 volumes |
| P2 | `CURRENT` → `REPEATING` on the same write | **Allowed.** The `COMPLETED` carve-out does not over-block |
| P5 | **Does an all-`null` `FuzzyDateInput` clear a date?** | **Yes.** This is how the stale finish date is cleared |
| P6 | Undo the write that cleared it | Date **restored** |
| P7 | Undo a 3-row write | All three restored field for field, notes and both dates included |
| P8 | Snapshot retention | The oldest snapshot was **gone** after six writes; five are kept, and undo 404s cleanly rather than guessing |
| P9 | Create + undo, delete box unticked | `created_entry_id` recorded as the write ran; undo restored the update and **left the created entry alone** |
| P10 | Same snapshot, box ticked | The created entry was deleted. **First live exercise of the delete path** |
| P11 | A row AniList rejects, mid-write | Reported against that row by name; the other row still wrote |
| P12 | Retry | Re-sent **1 row, not 2**, with no fresh account read. A spent run id is refused |
| P13 | Undo after a reread write | **Found a defect.** Undo restored `repeat` as 5, not 4 — L8's increment beats the value sent alongside the status. A follow-up write carrying only `repeat` does stick, so undo now emits one |
| P14 | Two rows resolving to one entry | **Found a second defect.** Both were written, the later over the earlier. The first now wins and the rest are skipped with a reason |
| P15 | **An 8-alias document**, the real batch size | `X-RateLimit-Remaining` 18 → 17: **one request for eight mutations**. All eight applied |
| P20 | Reading a list back | 14 entries read with a token **and** by public username with none; written to MAL XML and read back with every field intact |
| P22 | **A batch with one bad row** | `{m0: null, m1: null, m2: null}` and HTTP 400 — AniList validates the whole document before running any of it, so **the good rows never executed**, and retry re-batched them for ever. Fixed by re-sending stalled rows one at a time on a validation refusal. Same case after: **2 of 3 written** |
| P23 | Undo batched | 4 restores went out as **one request**; a repeat repair is not counted as a second restored row |
| P25 | **Cancellation mid-write** | Cancelled a 14-row write in flight: stopped at exactly **8 of 14** — the batch in flight finished, the next never started, and a snapshot was offered so the partial write stays undoable |
| P26 | **Both chunk loops, forced against real data** | `MediaListCollection` at `perChunk=5` read the same 14 entries across **3 chunks** with no loss or duplication; the 50-id existing-entries loop merged 14 ids across 3 requests and returned every one |

---

## 🔎 Ground-up audit (2026-10-20)

A re-read of the whole repository against the running app, rather than against
the plan. Six real defects, four of them invisible to the test suite because
the suite itself was part of the problem.

**The test suite was reaching the live AniList API.** Two mocks patched a name
the code under test never calls:

- `test_export_state.py` patched `gui.resolve_manga_entries`, but `/api/process`
  resolves through `resolve_many_entries`. The patch did nothing, the real
  resolver ran, and the test only passed because the network happened to work.
- `test_batch_resolution.py` stubbed `_execute_anilist_batch` but left
  `_execute_anilist_request` alone, so sequel expansion walked out to AniList.

Proved by wrapping `socket.socket.connect`: the suite made two outbound
connections to AniList per run. It would have failed offline and it spent rate
limit every time. Both mocks now patch what is actually called, and
`tests/__init__.py` blocks non-loopback connections for the whole suite - a
vacuous mock now fails loudly instead of quietly phoning home. **Outbound
connections during the suite: 0.**

> [!CAUTION]
> **Any website you had open could POST to the server.** It is local and has no
> login, and a JSON body sent as `Content-Type: text/plain` is a CORS "simple
> request", so nothing preflighted it and nothing refused it. Measured against
> the running app: a POST carrying `Origin: https://evil.example` was accepted.
>
> It could not write to AniList - the token lives in the page and is never
> stored server-side - but it could spend the AniList rate limit through
> `/api/process` and overwrite the export through `/api/update-export`.
>
> `do_POST` now refuses a request whose `Origin` is present and is not this
> server. An absent `Origin` still passes, because curl and the tests send none.
>
> **The first version of that guard broke the app**, and the audit caught it:
> it compared against `127.0.0.1` only, so opening the page as
> `http://localhost:5173` - which the browser treats as a different origin -
> got a 403 on every action. Every loopback spelling is accepted now, while a
> lookalike like `127.0.0.1.evil.example` is not.

**A long write could not be stopped, and the fix needed a second pass.**
`runControls` - the Cancel and Pause buttons - was only ever shown by the
resolve run. The write, undo and copy runs
register with the same control block and check `cancel` between batches, but
the buttons never appeared, so there was no way to stop a copy of a large
account from the page. Cancel is now shown for all of them. **Pause stays
hidden** for write-type runs: only the resolve loop waits on it, and a button
that does nothing is worse than no button.

Showing it was not enough. In the browser the control turned out to live in the
**left column's panel footer**, under the Process button - sensible for the
resolve run, which starts there, but a write, an undo and a copy all start from
the right column and report into the progress card on the right. The Cancel
button was rendering 1400px down the page, half a screen below the fold, while
the user watched the progress bar. It now sits **inside the progress card**,
directly under the bar, which is the one place every run type reports to. The
resolve run gets the same benefit: its controls used to be nowhere near its own
progress.

**A half-finished copy could not be undone.** The copy writes anime and manga
as separate stages with a snapshot each. `lastWriteSnapshots` was only set on
`copy_complete`, so a copy that wrote the anime stage and then failed - or was
cancelled - left the account changed with no Undo button, even though the
snapshot was sitting on disk. Both paths now hand the collected snapshots over.

**The copy sat silent while it read.** The server emitted `stage_started` and
the page ignored it, so reading two whole lists showed nothing until the first
row was written. The page now says which list it is reading, and that nothing
has been written yet.

**Switching media type silently destroyed a resolve.** The Media Type dropdown
cleared every row - no confirmation, no toast - and `saveState` then wrote the
empty list to storage, so a 339-title resolve was gone for good on one mis-click,
unrecoverable even by reloading. Rows genuinely cannot cross media types (an
anime row has no chapter count and the wrong ids), so clearing was right;
losing them was not. They are now set aside per type and come back when you
switch back, with a toast either way. Driven in the browser: resolve 3 anime →
switch to manga ("3 anime rows set aside") → switch back → all three returned
with their totals intact.

**A fixed row still claimed to be unresolved.** The placeholder carries
`unresolved: true`; a picked match replaces the row's identity but has no such
key, so `Object.assign` left the old flag behind. Nothing in the page reads it,
so nothing was visibly wrong - but a field that states the opposite of the truth
is a trap for whatever reads it next. Cleared on both fix paths.

**Dead code.** Batching the undo removed the last caller of
`anilist_write_entry`; it was still defined, still imported, and referenced by
nothing - along with three other unused imports in `gui.py`. Removed.

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
> anime and the manga path; evidence in [measured live](#-measured-against-the-live-api)). Established: creates work,
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

Records are kept honest on purpose · every fix cites the measurement that proved it

</div>
