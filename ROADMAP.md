# AniExport — roadmap

Forward-looking plan; [MANGA_SUPPORT_NOTES.md](MANGA_SUPPORT_NOTES.md) records what already
shipped and what was actually verified. The earlier planning documents (`PLAN.md`,
`ANILIST_FIRST_PLAN.md`) were removed once their contents shipped and live in git history
(`git show 8a35779:PLAN.md`); line references to them below are kept for provenance.

Scope confirmed by user (2026-10-18): all four areas below. Conflict default when a pasted
row disagrees with the account: **never go backwards**, with per-row override.

---

## Context

All six phases of the original plan shipped. The tool is correct, fast (339 titles in ~90s) and
accessible. What follows was found by reading the write path and the GUI, not by
brainstorming. Two findings set the priority.

### 1. The write silently destroys data

`iter_write_payloads` (`generate_mal_xml.py:1379-1450`) takes only `id` from the user's
existing AniList entry. `status`, `scoreRaw` and `progress` are sent on **every** row,
unconditionally:

- AniList has chapter 224, the pasted row says 142 — or is blank, which `_clamp_progress`
  turns into `0` — so 142 or 0 is written over 224.
- `scoreRaw` is always sent, and the list-wide score control stamps **every** row on
  **every keystroke** (`gui.py:3652-3657`, bound to `input`). A write after touching that
  field zeroes the score of every entry it touches on the account.

The server already fetches `status`, `progress`, `progressVolumes` and `score`
(`ANILIST_EXISTING_ENTRIES_QUERY`, `generate_mal_xml.py:1219-1232`) and never reads them.
The confirm step shows counts only (`gui.py:4553-4561`). This is the only irreversible
feature in the tool, and it has never been run against a live account.

`notes`, `startedAt`, `completedAt` and `repeat` are sent only when non-empty
(`generate_mal_xml.py:1432-1440`), so those are never blanked. Correct as-is.

### 2. The write takes ~12 minutes for 339 rows

One `SaveMediaListEntry` per row, each through `_throttle()` (~2.1s). Search solved this in
Phase 1 by aliasing 8 queries per document; the same trick applies to mutations.

### Verified externally

Introspection and unauthenticated calls against `graphql.anilist.co`. Nothing was written.

| Fact | How |
|---|---|
| `MediaListCollection(userId, userName, type, chunk, perChunk, forceSingleCompletedList, ...)` accepts a **username with no token** | introspection, plus a live unauthenticated call returning a well-formed empty collection rather than an auth error |
| `MediaList` carries `status, score, progress, progressVolumes, repeat, notes, startedAt, completedAt, media` | introspection of the `MediaList` type |
| Aliased mutations validate and resolve **per alias** | sent `m0:`/`m1: SaveMediaListEntry` with no token; got `data:{m0:null,m1:null}` plus one error per alias |
| `DeleteMediaListEntry(id)` exists | introspection — needed to undo a *created* entry |
| `UpdateMediaListEntries(ids:[Int], ...)` exists: many entries, one set of values | introspection |

**Unverified, and gating Phase 2:** whether AniList executes every alias of a real
authenticated mutation document, and whether it costs 1 request or N against the rate limit.
The spike measures this; it is not assumed.

### Defects found while auditing this plan

Recorded because each was a real defect in the first draft, not a wording change:

- **The `COMPLETED` guard would have blocked legitimate rereads.** `_is_repeat_status`
  (`:1521-1523`) maps Rereading/Rewatching to `REPEATING`; "never overwrite COMPLETED" would
  have silently refused every reread the user marked. Carve-out added.
- **Undo would have been incomplete.** The existing-entries query does not fetch `notes`,
  `repeat`, `startedAt` or `completedAt`, so a snapshot built from it cannot restore a row
  whose notes or dates the write replaced.
- **Undo of a *created* entry was missing entirely.** Restoring prior values is meaningless
  for a row that did not exist; it has to be deleted, which is destructive and needs its own
  consent.
- **Undo and retry both need a token the server no longer has** — it is cleared in the
  `finally` at `gui.py:4650`.
- **Alias→row attribution was hand-waved.** `errors[]` carries `locations`, not `path`.
- **The score rule was unimplementable as written.** "Unless the row carries an explicit
  score" assumed provenance the row does not have: the global control stamps every row, so
  an explicit score is indistinguishable from the default.

---

## Phase 1 — Make the write safe

Closes a confirmed data-loss path using data the server already fetches. GUI-only; the CLI
has no write path.

**Step 0 — fetch what safety and undo require.** Extend `ANILIST_EXISTING_ENTRIES_QUERY`
with `notes`, `repeat`, `startedAt { year month day }`, `completedAt { year month day }`.
Same request count, same chunking.

**Step 1 — stop swallowing the read error.** `anilist_existing_entries` does
`if result.get("error"): continue` (`:1358-1359`). A failed chunk yields no existing entries
for those 50 ids, so they are classified as creates with no `id` — the exact mediaId-only
behaviour the design comment at `:1209-1213` says it refuses to rely on. Propagate the
failure and refuse the preview rather than guessing.

**Step 2 — conflict policy.** New `_reconcile_with_existing(variables, known, overrides)`
in `generate_mal_xml.py`, called from `iter_write_payloads` where `known` is already in
scope. All rules are no-ops when there is no existing entry:

| Field | Rule |
|---|---|
| `progress`, `progressVolumes` | never write a value lower than the existing one |
| `status` | never replace `COMPLETED` **except** with `REPEATING`. All other transitions pass — `DROPPED` → `CURRENT` is a real edit, and no further ordering is invented |
| `scoreRaw` | never write `0` over a non-zero existing score; any non-zero score passes |
| `notes`, `startedAt`, `completedAt`, `repeat` | unchanged — already sent only when non-empty |

Policies: `"safe"` (default) and `"overwrite"`, plus a per-row/per-field `overrides` map so
one row can be forced without changing the policy. An overridden row is written exactly as
the row says, downgrades included.

**Step 3 — per-row diff.** `iter_write_payloads` returns
`diff: [{field, from, to, applied, blocked_by_policy}]` per payload, covering only fields
the write sends and the existing query fetches. `/api/anilist-preview` returns it with the
current counts. The confirm modal gains a scrollable change list: only rows that change,
each conflict with a "keep mine / keep AniList" toggle, unchanged rows collapsed to a count.

**Step 4 — undo.** Before the first mutation, write `.aniexport_undo_<timestamp>.json`
(gitignored, beside `.anilist_cache.json`, **never containing the token**) holding per row:
`anilist_id`, whether it was a create, and the full prior field set from Step 0. Keep the
last 5, prune oldest. `/api/anilist-undo` replays prior values through the same write path.
Rows that were *creates* have no prior state, so undoing them means
`DeleteMediaListEntry(id)` — destructive, therefore **off by default**, listed separately in
the undo confirm as "N entries this run created", deleted only if ticked. Undo requires the
token to be supplied again, exactly like the write.

**Step 5 — retry failed rows.** `write_complete` carries only `{title, error}`
(`gui.py:4642-4648`), so failures cannot be retried. Keep the failed *payloads* for the last
run only, keyed by run id, and add "Retry N failed" that re-sends just those.

## Phase 2 — Batch the write

Mirrors `_search_batch_query`. New `_write_batch_query(n)` emitting `m0..m7` aliased
`SaveMediaListEntry` blocks with numbered variables (`$id0, $mediaId0, $status0, ...`);
`_execute_anilist_authenticated` reused unchanged.

**Attribution of partial failure.** Mutations in one document execute serially, and a
failing alias does not stop later ones. The primary signal for "row k failed" is
`data.mk === null`, not the error list. Error *messages* are matched to aliases by the
character offset recorded for each alias when the document is built, compared against
`errors[].locations`; when an offset cannot be matched, the batch-level message is reported
for every null alias in that batch rather than guessed at. Per-row `write_progress` events
are still emitted one per row so the UI does not jump 8 at a time.

**Spike first**, on the throwaway account, 2 rows: confirm both aliases apply, and read
`X-RateLimit-Remaining` either side to learn whether the document costs 1 or 8.

- costs 1 → ship it; ~12 min becomes ~90s.
- costs 8 → **abandon Phase 2**, keep the per-row path. Nothing else depends on it.

Also add an ETA to the write phase. The resolve phase has one (`gui.py:4398-4412`); the
write has none.

## Phase 3 — Read an AniList list

New `ANILIST_LIST_COLLECTION_QUERY` using `MediaListCollection(userName:/userId:, type:,
chunk:, perChunk:)`, looping while `hasNextChunk`. Start at `perChunk: 500`, halve on error.
Public lists need no token; with a token, resolve `userId` through the existing
`anilist_viewer` so private lists work. A private list queried by name returns nothing — say
so plainly instead of reporting an empty list.

**De-duplicate.** `lists` includes custom lists, so one entry can appear more than once.
Pass `forceSingleCompletedList: true` and dedupe by `mediaId` on the way in.

**Inverse status map.** `ANILIST_STATUS_MAP` (`:1251-1259`) is one-way and not invertible
(`CURRENT` ← reading *or* watching), so the reverse must be media-type aware, with
`REPEATING` → Rereading/Rewatching. Add `ANILIST_STATUS_REVERSE[media_type]` beside it so
the two cannot drift.

Unlocks three things that are impossible today:

- **AniList → MAL XML.** Entries carry `media { idMal }` and feed the existing
  `build_mal_xml` / `build_mal_manga_xml`. Rows with a null `idMal` are the existing
  AniList-only case: reported, not dropped silently.
- **Backup** before a write, independent of the undo snapshot.
- **Diff against the real list** — "40 of these 339 are already on your account".

Reuse the MAL-XML import merge (match on MAL id, keep the row's `anilist_id`, honour the
add-unmatched toggle) rather than writing a second one. New `/api/anilist-import` beside
`/api/parse-mal-xml`. Worth a CLI flag too, since it is read-only.

## Phase 4 — Review and bulk-edit UX

**Confidence is already computed and discarded.** `_media_rank` (`:328-364`) runs
`difflib.SequenceMatcher` against both titles, keeps `best_ratio`, and returns a sort key
used once and dropped. Carry `match_ratio` and the runner-up gap into `_parse_media_node` /
`_parse_manga_node`; derive `needs_review` when `match_ratio < 0.6` or the runner-up is
within `0.05`. Both thresholds are module constants, tuned against the user's own 339-title
list rather than invented and frozen. Add a **"Needs review"** chip to `CHIPS`
(`gui.py:3190-3199`).

This addresses a case the AniList-first plan recorded as unsolved: `I Am Being Chased To
Fall In Love Everyday` resolved to `Forced to Date with BOSS Everyday`, which scores far
below 0.6 and would surface immediately. A manually picked match clears `needs_review` — the
ratio describes the original search and is meaningless once a human has chosen.

**Cache:** entries gain a field, so bump `CACHE_VERSION` to 2 (`:951`). `load_cache` already
discards the whole file on a version mismatch (`:975-976`), so stale rows cannot leak in
without the new field. One line, no migration.

**The inline Find button silently auto-takes `entries[0]`** (`repairRow`,
`gui.py:3500-3535`) with no candidates shown. Show the candidate list when there is more
than one hit or the ratio is low; the fixer modal already has that UI.

**Bulk controls stamp every row.** `selectStatus` and `inputScore` (`gui.py:3646-3657`) do
`currentAnimes.forEach(...)`, overwriting unchecked rows, rows hidden by the filter, and any
per-row value the user hand-edited. Scope both to selected-and-visible rows — `select-all`
at `gui.py:3413-3421` already does this correctly, so follow it — move score from `input` to
`change`, and label each control with the count it affects ("Apply to 12 selected"). With
nothing selected it applies to all *visible* rows and says so. This revisits decision #15 in
the original plan, which deferred exactly this with "revisit only on request", and it will require
updating the existing tests that assert the stamp-all behaviour.

**Smaller:** shift-click range selection, arrow-key row navigation with space to toggle, a
Back button in the fixer (`fixPos` only ever increments, `gui.py:3865`), and debounced
search-as-you-type in the fixer, which today requires pressing Search.

---

## Sequencing

Phase 1 → Phase 2; Phase 2 re-batches Phase 1's payloads, so it must come second, and it may
be abandoned at the spike. Phases 3 and 4 are independent of both and of each other.

## Edge cases that need explicit handling

- **A stale undo snapshot.** If the user deleted an entry on AniList after the write, the
  snapshot's `id` no longer resolves and `SaveMediaListEntry` fails for that row. Report it
  per row like any other write failure; do not abort the rest of the undo.
- **The global run lock.** `/api/anilist-write` holds `_run_lock` (`gui.py:4576`). Undo,
  retry and the list import must take the same lock, or an import could land rows into
  `currentAnimes` while a write iterates them. Each returns the existing 409 when busy.

## Files

- `generate_mal_xml.py` — `iter_write_payloads`, `anilist_existing_entries`,
  `ANILIST_EXISTING_ENTRIES_QUERY`, `_media_rank`, `_parse_media_node` /
  `_parse_manga_node`, `CACHE_VERSION`, plus the new batch-write and list-collection queries
  and the reverse status map.
- `gui.py` — `/api/anilist-preview`, `/api/anilist-write`, new `/api/anilist-undo` and
  `/api/anilist-import`; `HTML_TEMPLATE` for the diff modal, the review chip, bulk-edit
  scoping and the selection/keyboard work.
- `.gitignore` — `.aniexport_undo_*.json`.
- `README.md`, `MANGA_SUPPORT_NOTES.md` — per the project's convention of recording what
  shipped and what was verified.
- `tests/` — extend `test_anilist_write.py`; batched-mutation cases in
  `test_batch_resolution.py`; a new suite for the list reader.

## Verification

- `python -m unittest discover -s tests -t .` — 127 pass today. The conflict policy needs a
  table-driven case per field, including blank-progress-becomes-0, score-0-over-a-real-score
  and the COMPLETED→REPEATING carve-out.
- jsdom suites for the diff modal, the review chip and the bulk-edit scoping, run like the
  existing 15.
- Chrome screenshots at 1440 and 430 plus the width probe; the diff modal is the largest UI
  added since the narrow breakpoint landed.
- **Live, on the throwaway account, in this order.** This is also the long-outstanding live
  test of the write path, which has only ever run against mocks:
  1. Phase 2's two-row alias spike, reading `X-RateLimit-Remaining` either side.
  2. A 3-row write with a deliberate conflict — a row's progress below the account's, and a
     row's score at 0 against a real score. Confirm the guard holds and the diff told the
     truth beforehand.
  3. Undo it; confirm the account is back to its prior state, notes and dates included.
  4. A write that creates one entry, then undo with the delete box ticked, then again with
     it unticked, to confirm both paths.
