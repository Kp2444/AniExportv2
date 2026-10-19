# AniExport — roadmap

Forward-looking plan. [MANGA_SUPPORT_NOTES.md](MANGA_SUPPORT_NOTES.md) records what already
shipped and what was actually verified; [README.md](README.md) is the user-facing description.
The earlier planning documents (`PLAN.md`, `ANILIST_FIRST_PLAN.md`) were deleted once their
contents shipped and live in git history (`git show 8a35779:PLAN.md`).

Scope confirmed by user: all four phases. Conflict default when a pasted row disagrees with the
account: **never go backwards**, with per-row override.

## How to read this

Every claim below is in one of three states, and they are never mixed:

| Marker | Meaning |
|---|---|
| **verified** | Observed directly — a line read in this repo, or a live response from `graphql.anilist.co` quoted in the Evidence section |
| **inferred** | Follows from something verified, by stated reasoning. Could still be wrong if the reasoning is wrong |
| **open** | Not established. Listed in [Open questions](#9-open-questions) with the experiment that would settle it |

Line references are `file:line` against this repo at the commit that introduced this file. They
were re-checked in this working tree, not carried over from notes.

---

## 1. Context

### 1.1 The write silently overwrites the account — verified

`iter_write_payloads` (`generate_mal_xml.py:1379`) builds the mutation variables. Three fields are
sent on **every** row, unconditionally (`:1422-1427`):

```python
variables: Dict[str, Any] = {
    "mediaId": anilist_id,
    "status": status,
    "scoreRaw": max(0, min(10, int(entry.get("score", default_score) or 0))) * 10,
    "progress": progress,
}
```

The only thing taken from the user's existing AniList entry is its id (`:1442-1446`):

```python
known = existing.get(anilist_id)
if known and known.get("id"):
    variables["id"] = int(known["id"])
```

Consequences, all **verified** by reading the path end to end:

- **Progress is replaced, not merged.** Account at chapter 224, pasted row says 142 → 142 is
  written. A row with no progress value becomes `0` via `_clamp_progress`
  (`generate_mal_xml.py:1486`) and `0` is written over 224.
- **Score is replaced.** `scoreRaw` is always present. The list-wide score box
  (`gui.py:3653-3657`) is bound to `input` and does `currentAnimes.forEach(...)`, so it stamps
  every row on every keystroke. Typing a score before a write therefore sets that score on every
  entry the write touches, and typing then deleting it leaves `0` on every row.
- **Status is replaced,** including `COMPLETED` → `PLANNING`.
- **The data needed to prevent all of this is already fetched and discarded.**
  `ANILIST_EXISTING_ENTRIES_QUERY` (`generate_mal_xml.py:1219-1232`) selects
  `id, mediaId, status, progress, progressVolumes, score`. Only `id` is ever read; the other four
  fields are dead in the response.
- **The confirm step cannot show any of it.** `/api/anilist-preview` (`gui.py:4530-4562`) returns
  `ok, viewer, backfilled, updates, creates, skipped, total` — `updates` and `creates` are
  `sum(1 for p in payloads if p["update"])` and its inverse. No field-level data exists to show.

Not affected, **verified** at `generate_mal_xml.py:1428-1440`: `progressVolumes`, `repeat`,
`notes`, `startedAt` and `completedAt` are added only when non-empty, so a blank row never blanks
them on the account. They can still be *replaced* when the row does carry a value, which is why
they belong in the diff even though they need no guard.

That protection has a cost the live run exposed (§2.3, M3). Because `completedAt` is never sent
empty, a write that takes an entry from `COMPLETED` to `CURRENT` **leaves the old completion date
behind**, so the account is left holding an entry that is in progress and also finished on a date.
Nothing in this tool can clear it, because clearing it means sending an empty value, which is
precisely what the guard above prevents. `_reconcile_with_existing`-style field guards do not fix
this; the diff has to offer "clear the completion date" as an explicit action when the status
moves off `COMPLETED`. Scoped into Phase 1, Step 2.

This is the only irreversible feature in the tool. It has now been **run against a live
throwaway account** and the overwrite was reproduced exactly as described above — see §2.3, L3.
Unit coverage in `tests/test_anilist_write.py` remains mock-only.

### 1.2 The write takes ~12 minutes for 339 rows — verified

`gui.py:4620-4640` loops one row at a time calling `anilist_write_entry`
(`generate_mal_xml.py:1453`), which calls `_execute_anilist_authenticated`
(`:1262`), which calls `_throttle()` (`:77`) before every request. `current_interval()` (`:66`)
returns `60/limit*1.05` clamped to `[0.7, 2.5]`, defaulting to 2.1s when no limit has been
observed. 339 × 2.1s ≈ 12 minutes.

Resolution already solved the identical problem by aliasing 8 queries into one document
(`_search_batch_query`, `generate_mal_xml.py:462`), which is what took a 339-title resolve from
~12 minutes to ~90 seconds.

---

## 2. Evidence

### 2.1 AniList API — verified by live probes

Introspection and unauthenticated queries against `https://graphql.anilist.co`. No token was used
and nothing was written.

| # | Fact | Probe and result |
|---|---|---|
| E1 | `MediaListCollection(userId, userName, type, status, chunk, perChunk, forceSingleCompletedList, status_in, status_not_in, sort, ...)` exists | `__type(name:"Query"){fields{name args{name}}}` |
| E2 | It accepts a **username with no token** | `MediaListCollection(userName:"…", type:ANIME, perChunk:10, chunk:1)` returned `hasNextChunk: true` and four list groups, with no `Authorization` header |
| E3 | `MediaList` fields: `id, userId, mediaId, status, score, progress, progressVolumes, repeat, priority, private, notes, hiddenFromStatusLists, customLists, advancedScores, startedAt, completedAt, updatedAt, createdAt, media, user` | `__type(name:"MediaList"){fields{name}}` |
| E4 | **`MediaList.score` takes a `format: ScoreFormat` argument.** `ScoreFormat` is `POINT_100, POINT_10_DECIMAL, POINT_10, POINT_5, POINT_3` | `__type(name:"MediaList"){fields{name args{name type{name}}}}`; `score(format: POINT_100)` returned `80` on a live entry |
| E5 | `Viewer.mediaListOptions.scoreFormat` exists, so the account's display format is readable | `__type(name:"MediaListOptions"){fields{name}}` → `scoreFormat, rowOrder, animeList, mangaList` |
| E6 | **`progressVolumes` is `null` on anime entries** | live `MediaListCollection(type:ANIME)` entry returned `'progressVolumes': None` |
| E7 | `MediaListGroup` carries `name, status, isCustomList, isSplitCompletedList, entries` | `__type(name:"MediaListGroup"){fields{name}}` |
| E8 | **Split completed lists appear as separate groups with the same status**, and `forceSingleCompletedList: true` merges them | same account, same chunk: without the flag, `Completed TV` (7 entries) and `Completed TV Short` (1), both `status=COMPLETED, isSplitCompletedList=true`; with the flag, one `Completed` group of 8 |
| E9 | `MediaListStatus` is exactly `CURRENT, PLANNING, COMPLETED, DROPPED, PAUSED, REPEATING` | `__type(name:"MediaListStatus"){enumValues{name}}` |
| E10 | `FuzzyDate` and `FuzzyDateInput` are both `{year, month, day}` as `Int` | `__type(name:"FuzzyDate")`, `__type(name:"FuzzyDateInput")` |
| E11 | **Aliased mutations parse, validate and resolve per alias** | posted `mutation{ m0: SaveMediaListEntry(mediaId:1, progress:1){id} m1: SaveMediaListEntry(mediaId:2, progress:2){id} }` with no token → `{"data":{"m0":null,"m1":null},"errors":[{…401, locations:[{line:1,column:11}]},{…400 validation, locations:[{line:1,column:72}]}]}`. Both aliases present in `data`; one error per alias, each with its own `locations` |
| E12 | `errors[]` carries `message`, `status`, `locations` and (on validation failures) `validation`. **No `path` field** | same response as E11 |
| E13 | `DeleteMediaListEntry(id)` exists | `__type(name:"Mutation"){fields{name args{name}}}` |
| E14 | `UpdateMediaListEntries(ids:[Int], status, score, scoreRaw, progress, …)` exists — many entries, one set of values | same introspection |
| E15 | `SaveMediaListEntry` args: `id, mediaId, status, score, scoreRaw, progress, progressVolumes, repeat, priority, private, notes, hiddenFromStatusLists, customLists, advancedScores, startedAt, completedAt` | same introspection |

### 2.2 This repo — verified by reading

| # | Fact | Location |
|---|---|---|
| R1 | Only `id` is read from the existing entry | `generate_mal_xml.py:1442-1446` |
| R2 | `status`, `scoreRaw`, `progress` always sent | `:1422-1427` |
| R3 | `progressVolumes`, `repeat`, `notes`, `startedAt`, `completedAt` sent only when truthy | `:1428-1440` |
| R4 | Row score is 0–10, multiplied by 10 → `scoreRaw` is 0–100 | `:1425` |
| R5 | A failed existing-entries chunk is skipped silently: `if result.get("error"): continue` | `:1358-1359` |
| R6 | `anilist_write_entry` returns `{"ok", "id", "status"}`; the caller keeps none of it | `:1453-1462`, caller `gui.py:4620-4640` |
| R7 | Write failures are recorded as `{"title", "error"}` only — the payload is not kept, so a retry cannot be reconstructed | `gui.py:4642-4648` |
| R8 | The write holds the global `_run_lock` and releases it in a `finally`; the token is cleared there too | `gui.py:4576`, `:4650-4652` |
| R9 | `_media_rank` computes `difflib.SequenceMatcher(...).ratio()` and returns it inside a sort key that is used once and discarded | `generate_mal_xml.py:328-364`, consumed at `:367-372` |
| R10 | `_candidates` calls `parse(node, fallback_title=fallback_title)` — the parser already receives the searched title | `:296-317` |
| R11 | Parsed entry dicts contain `id, anilist_id, title, type, episodes` (or `chapters`/`volumes`), `year, popularity, cover, can_export_xml, can_write_anilist` — no confidence field | `:272-293`, `:421-439` |
| R12 | `CACHE_VERSION = 1`; `load_cache` discards the entire file on a version mismatch | `:951`, `:975-976` |
| R13 | `selectStatus` and `inputScore` both do `currentAnimes.forEach(...)`; `inputScore` is bound to `input` | `gui.py:3646-3657` |
| R14 | `selectAllCheckbox` correctly scopes to `isActionable(item) && matchesFilters(item)` | `gui.py:3413-3421` |
| R15 | `repairRow` applies `entries[0]` without rendering the candidate list | `gui.py:3500-3535` |
| R16 | `CHIPS` has 8 entries | `gui.py:3210-3219` |
| R17 | `ANILIST_STATUS_MAP` maps MAL vocabulary → AniList enum and is **not invertible** (`CURRENT` ← `reading` or `watching`) | `:1251-1259` |
| R18 | `_is_repeat_status` drives `REPEATING` and `repeat: 1` | `:1521-1523`, used at `:1430-1431` |
| R19 | `/api/anilist-preview` calls `backfill_anilist_ids(entries, …)` which **mutates the caller's rows** before payloads are built | `gui.py:4543` |

### 2.3 The live write — verified on a throwaway account, 2026-10-19

Run against an AniList account that was **empty before the test** (confirmed: `MediaListCollection`
returned 0 anime and 0 manga entries). The three media ids were resolved through the repo's own
`fetch_anime_info_online`, so nothing depended on search ranking: Serial Experiments Lain `339`,
Texhnolyze `26`, Kaiba `3701`. L1–L4 went through the running server's `/api/anilist-write`, so
the whole handler path was exercised, not just the library. Account state was read back after each
step through `anilist_existing_entries`.

#### The anime path

| # | Step | Result |
|---|---|---|
| L1 | Write Lain at episode 3, `Watching`, onto an empty account | `creates: 1`; entry `604576747` created, `status=CURRENT progress=3`. **The create path works live — this had never been run before** |
| L2 | Write the identical row again | Classified `updates: 1, creates: 0`; entry id **unchanged** at `604576747`. The one guarantee the README makes — a repeated write cannot duplicate — holds live |
| L3 | Write Lain at episode 10, then at episode 3 | Account went `3 → 10 → 3`. **§1.1 reproduced: the downgrade was written and reported as `"ok": true`.** No warning, no failure, no trace |
| L4 | `/api/anilist-preview` for the episode-3 row while the account was at 10 | Returned exactly `{"ok":true,"viewer":"…","backfilled":0,"updates":1,"creates":0,"skipped":[],"total":1}`. **The user is told "1 update" and nothing else** — confirming §1.1's claim that no field-level data exists to show |
| L5 | Rate-limit cost of a 2-alias `SaveMediaListEntry` document | Control: three single-operation requests decremented `X-RateLimit-Remaining` `15 → 14 → 13`, one each. The aliased document then took it `13 → 12`: **cost 1 request for 2 mutations.** `X-RateLimit-Limit` read `30`, so this was measured in the degraded state |
| L6 | Did both aliases apply? | Yes — `data.m0` and `data.m1` each returned a distinct new entry (`604576843` mediaId 26 progress 4; `604576844` mediaId 3701 progress 5), both confirmed present on a fresh read |

#### The manga path

Same account, same day, same route through `/api/anilist-write` with `media_type: "manga"`.
Media ids resolved through `fetch_manga_info_online`: Goodnight Punpun `34632` (147 ch / 13 vol),
Vinland Saga `30642` (224 / 29). Read back with a **fuller query than the repo's own**, adding
`repeat`, `notes`, `startedAt`, `completedAt` and `score(format: POINT_100)` — fields
`ANILIST_EXISTING_ENTRIES_QUERY` does not fetch, so the repo could not have observed them.

| # | Step | Result |
|---|---|---|
| M1 | Create Punpun at chapter 40 / volume 4, `Reading` | `ch=40 vol=4`. **`progressVolumes` works live** — the one field the anime path never sends. AniList also stamped `startedAt` with today's date, which was never sent (see M7) |
| M2 | Same row at chapter 147 / volume 13, `Completed`, score 9 | `COMPLETED ch=147 vol=13 score=90`. The `finished` branch and `scoreRaw` both correct. Read back as `90` only because the query asked for `format: POINT_100` — E4's trap, confirmed |
| M3 | Same row with **chapters blank**, `Reading` | `COMPLETED 147/13 score 90` → **`CURRENT 0/0 score 0`.** One blank row destroyed four fields in a single write, reported `"ok": true`. This is the worst case in §1.1 and it is worse than the prose suggested — the score goes too |
| M4 | `completedAt` after M3 | **Still `2026-10-19`.** The entry is now `CURRENT` *and* carries a completion date. The "never sent empty" guard (§1.1) is what prevents clearing it |
| M5 | Set `repeat: 5` directly, then write the row as `Rereading` | `repeat` `5` → **`1`**. Defect D9 reproduced: `repeat` is the literal `1` (R18), so a reread write resets the account's count |
| M6 | Create Vinland Saga with `comments`, `start_date: 2024-03-01`, `finish_date: 0000-00-00` | `notes='live test note'`, `startedAt=2024-3-1`, `completedAt` unset. **`notes` and dates all work, and `0000-00-00` is correctly skipped** by `_fuzzy_date` (`:1367-1376`) |
| M7 | Did AniList invent a `startedAt`? | Yes on M1, where none was sent; no on M6, where one was. So AniList stamps a start date itself when the entry becomes `CURRENT` without one — an account setting, not this tool's write. It matters for undo: restoring "no start date" may not be possible |

#### Phase 1 itself, verified after it shipped — 2026-10-19

Same throwaway account (`Razed2Ground`), through the running server's own endpoints. §12 steps
2–5. Account state was read before and after every step with a **fuller query than the code under
test uses**, so the verification is not limited to the fields the implementation happens to fetch.

| # | Step | Result |
|---|---|---|
| P1 | 3-row write with deliberate conflicts: progress 40 vs 147, volumes 4 vs 13, score 0 vs 90, repeat 1 vs 5, `COMPLETED` → `Reading`, `COMPLETED` → `Plan to read` | **8 fields blocked across 3 rows**, every one named in the preview first with the exact value it would keep. Account did not move. Two blocks were ones the plan never listed: the `Plan to read` row would have zeroed 249 chapters *and* 22 volumes — M3's catastrophe on a second row |
| P2 | `CURRENT` → `REPEATING` on the same write | **Allowed and applied.** The `COMPLETED` carve-out (D1) does not over-block |
| P3 | Override: "keep mine" on progress and volumes | Policy block cleared, payload sent 40 — see P5 for what the account then did |
| P4 | Override the status off `COMPLETED`, leaving the date | Finish date **stranded** exactly as defect 7 describes: `CURRENT` *and* finished 2026-10-19. The preview flagged `stale_completed_at` beforehand |
| P5 | **Does an all-`null` `FuzzyDateInput` clear a date?** | **Yes — the date cleared.** This was the last open question in Phase 1 and it is now measured, not assumed |
| P6 | Undo the write that cleared the date | Date **restored**. The field D2 would have lost, put back |
| P7 | Undo a 3-row write | All three rows restored field for field against the snapshot's own `prior` — status, progress, volumes, score, repeat, notes, both dates |
| P8 | Snapshot retention | The step-2 snapshot was **gone** when step 4 asked for it: six writes had happened and five are kept. Pruning works, and undo 404s cleanly instead of guessing |
| P9 | Create + undo, box unticked | 1 create + 1 update; `created_entry_id` **604614624** recorded in the snapshot as the write ran (D3). Undo restored the update and **left the created entry alone** |
| P10 | Same snapshot, box ticked | `DeleteMediaListEntry` removed the created entry. **First live exercise of the delete path** |
| P11 | A row AniList rejects, mid-write | `"The selected media id is invalid."` reported against that row by name; the other row still wrote. **First live failure path** |
| P12 | Retry | Re-sent **1 row, not 2**, with no fresh account read. A spent run id is refused with 404 rather than silently re-run |
| P13 | Undo after a reread write (`COMPLETED` → `REPEATING`) | **Found a defect in Phase 1 itself.** Undo restored `repeat` as 5, not the prior 4: L8's increment beats the value sent alongside the status. A second write carrying only `repeat` does stick, so `iter_undo_payloads` now emits one for a row that has a reread count to lose. Re-run end to end: `repeat` restored exactly |
| P14 | Two rows resolving to one AniList entry | **Found a second defect.** Both were written, the later over the earlier, and the snapshot held two rows for one entry with one recorded id. `iter_write_payloads` now keeps the first and skips the rest with a reason. Verified: 1 row attempted, not 2 |

#### What P1–P12 found that no code reading had

Three AniList-side behaviours, each measured by direct probes that bypass this tool entirely. They
matter because they make the confirm step lie if it ignores them:

| # | Behaviour | Evidence |
|---|---|---|
| L7 | **While `status` is `COMPLETED`, AniList forces `progress` and `progressVolumes` to the media total** — when a lower value is sent explicitly, *and* when none is sent at all | Sent `COMPLETED` + `progress: 40` against a 147-chapter work → stored **147**. Separately, an entry sitting at `CURRENT, 40` sent only `status: COMPLETED` → stored **147**. `CURRENT` + `progress: 40` stores 40, so it is the status that does it |
| L8 | **`REPEATING` → `COMPLETED` increments `repeat` by one**, whatever is sent | Sent `repeat: 5` on that transition → stored **6**. Sent no `repeat` at all → also **6**. This is also why the test baseline came back `repeat: 2` after being set to 3 |
| L9 | A transition **to** `REPEATING` zeroes `progress` **only when no progress is sent** | `COMPLETED` → `REPEATING` with `progress: 100` → kept **100**; the same transition sending no progress → **0**. Every payload this tool builds sends a progress, so this one does not bite here |

L7 and L8 are now folded into the diff by `_anilist_side_effects`, and `applied` means **what the
account will hold**, not merely what gets sent. Without that, P3 would have promised "progress →
40" while the account kept 147 — a confirm step that lies, which is the exact failure Phase 1
exists to remove. L9 is recorded and deliberately **not** acted on, because it cannot reach this
write path.

Phase 1's conflict guard therefore has to cover the manga path specifically: `progressVolumes`
needs the same never-lower rule as `progress`, and `repeat` needs it too.

What this changes: **§1.1's data loss is no longer a code reading, it is a measurement**, **O1/O4
are answered** (§9), and **D9 and the manga write path are confirmed live** rather than inferred.
M3 and M4 found damage the earlier prose did not describe: the score is lost with the progress, and
a stale `completedAt` is left behind.

What it does not change: every automated test is still against a mocked API. Untested live:
`DeleteMediaListEntry`, a document of 8 aliases rather than 2, a multi-row write, cancellation
mid-write, and any failure path — every row in both runs returned `"ok": true`.

---

### 2.4 Defects found while auditing this plan

Each was wrong in an earlier draft of this document and is corrected below. Kept visible because
every one of them would have produced broken code.

| # | Defect | Correction |
|---|---|---|
| D1 | "Never overwrite `COMPLETED`" would have blocked legitimate rereads — `_is_repeat_status` turns Rereading into `REPEATING` (R18) | Carve-out: `COMPLETED` → `REPEATING` is allowed |
| D2 | The undo snapshot would have been incomplete — the existing query fetches neither `notes`, `repeat`, `startedAt` nor `completedAt` (R2/R3 vs `:1219-1232`) | Step 0 extends the query |
| D3 | **Undo of a *created* entry is impossible from a pre-write snapshot.** A create has no prior state, and its new entry id only exists in the mutation *response* (R6) | The snapshot is appended to as creates succeed, recording each returned id; undo deletes those via `DeleteMediaListEntry` (E13) |
| D4 | Undo and retry both need a token the server no longer holds (R8) | Both require the token to be re-supplied by the client |
| D5 | Alias→row attribution by `path` is impossible — `errors[]` has no `path` (E12) | Attribute by `data.mk === null`; use `locations` only as a secondary hint |
| D6 | The score rule "unless the row carries an explicit score" assumed provenance that does not exist — the global control stamps every row (R13), so an explicit score is indistinguishable from the default | Rule restated purely in terms of values: never write `0` over a non-zero existing score |
| D7 | **Comparing the fetched `score` to our `scoreRaw` compares different scales.** Bare `score` is returned in the account's own format (E4) — a user on `POINT_5` with a real 4 would be compared against a 0–100 value | Step 0 fetches `score(format: POINT_100)` so both sides are 0–100 |
| D8 | **`progressVolumes` is `None` on anime (E6).** `None < 5` raises `TypeError` in Python 3, so a naive "never lower" comparison crashes the preview for every anime row | Every comparison coerces with `int(x or 0)`, and `progressVolumes` is only considered for manga |
| D9 | `repeat` was listed as "needs no guard". It is sent as the literal `1` (R18), so writing it over an existing `repeat: 3` is a downgrade | `repeat` gets the same never-lower rule |
| D10 | The `needs_review` threshold was described as "tuned against the user's 339-title list". No such tuning has happened | Stated as a starting value with a calibration task attached |

---

## 3. Phase 1 — Make the write safe

> [!NOTE]
> **Shipped and verified live 2026-10-19.** All six steps, together, because they are one feature.
> 209 tests pass (140 before), plus a node suite of 57 assertions on the diff renderer, and
> **§12's live steps 2–6 all passed against the throwaway account** — see §2.3, P1–P12. What
> landed is recorded in `MANGA_SUPPORT_NOTES.md` under "Write safety"; defects 1, 3, 4, 7 and 8
> are closed.
>
> Two deviations from the plan below, both deliberate:
> - **Step 1 raises rather than returning `(entries, error)`.** `AniListReadError` keeps
>   `anilist_existing_entries`' signature, so the existing callers and their tests were untouched.
> - **Step 3 puts `diff` on each payload** rather than adding a third return value, for the same
>   reason: `iter_write_payloads` still returns `(payloads, skipped)`.
>
> Three things the live run forced, none of them in the plan. The third is the argument for
> running these steps at all: two were defects in Phase 1 as shipped, found only by exercising it
> against a real account (§2.3, P13/P14) — undo could not restore `repeat` after a reread, and two
> rows resolving to one entry were both written.
>
> `_anilist_side_effects` is the first:
> AniList edits `progress` and `repeat` itself on certain status transitions (L7, L8), so a diff
> that reported only what the payload says would have promised changes the API discards. `applied`
> therefore means *what the account will hold*, and `by_anilist` marks the fields that are not
> this tool's doing. Found by the override test (P3) failing against a real account.

Closes §1.1. GUI-only: the CLI has no write path. No new API requests — Step 0 adds fields to a
query that is already being issued.

### Step 0 — fetch what safety and undo require

Extend `ANILIST_EXISTING_ENTRIES_QUERY` (`generate_mal_xml.py:1219-1232`) to:

```graphql
query ($userId: Int, $type: MediaType, $mediaIds: [Int]) {
  Page (perPage: 50) {
    mediaList (userId: $userId, type: $type, mediaId_in: $mediaIds) {
      id
      mediaId
      status
      progress
      progressVolumes
      score (format: POINT_100)
      repeat
      notes
      startedAt { year month day }
      completedAt { year month day }
    }
  }
}
```

`score (format: POINT_100)` is load-bearing, not cosmetic — see D7. Same request count, same
50-id chunking.

### Step 1 — stop swallowing the read error

`anilist_existing_entries` (`:1345-1363`) currently does:

```python
if result.get("error"):
    continue
```

A failed chunk yields no existing entries for those 50 ids, so every one of them is classified as
a create with no `id`. The design comment at `:1209-1213` states the code specifically refuses to
rely on `mediaId`-only behaviour — this path does exactly that, silently. Change the function to
return `(entries, error)` or raise; `/api/anilist-preview` and `/api/anilist-write` then refuse
with a clear message rather than guessing. **Refusing is correct here**: proceeding risks creating
duplicates of entries the user already has, which is the one failure mode the design calls out.

### Step 2 — conflict policy

New function in `generate_mal_xml.py`, called from `iter_write_payloads` at the point where
`known` is already in scope (`:1442`):

```python
def _reconcile_with_existing(variables, known, media_type, overrides=None):
    """Apply the conflict policy in place. Returns the diff rows.

    `known` is the existing AniList entry or None (a create — every rule is a
    no-op). `overrides` is {field: "mine"} for fields the user chose to force.
    """
```

Rules. Every comparison coerces with `int(x or 0)` first, because `progressVolumes` is `None` on
anime (D8) and AniList may return `null` for an unset numeric field:

| Field | Rule | Why |
|---|---|---|
| `progress` | never write a value lower than the existing one | §1.1 |
| `progressVolumes` | same, **manga only**; skipped entirely for anime | E6 |
| `scoreRaw` | never write `0` over a non-zero existing score. Any non-zero value passes | D6 — a `0` is indistinguishable from "unset", a real score is not |
| `repeat` | never write a value lower than the existing one | D9 |
| `status` | never replace `COMPLETED`, **except with `REPEATING`**. Every other transition passes | D1. `DROPPED` → `CURRENT` is a real edit; no ordering beyond the `COMPLETED` guard is invented, because none is defensible |
| `notes`, `startedAt`, `completedAt` | no guard — already only sent when the row supplies them (R3). Still reported in the diff | Replacing a note the user typed is a change worth seeing, even if it is not a regression |

Policy values: `"safe"` (default) and `"overwrite"`. `overrides` is per-row, per-field, so one row
can be forced without weakening the policy for the rest. An overridden field is written exactly as
the row says, downgrades included.

**Known consequence, stated rather than hidden:** a user who finished a series, started it again,
and marked the row `Reading` rather than `Rereading` will have that row blocked by the `status`
rule. The diff shows it as blocked, and the per-row override is the escape hatch. The alternative
— guessing that `COMPLETED` → `CURRENT` means a reread — would silently discard a completion.

### Step 3 — per-row diff

`iter_write_payloads` gains a third return value, or each payload gains a `diff` key:

```python
{
  "title": "Vinland Saga",
  "anilist_id": 30642,
  "update": True,
  "variables": {...},
  "diff": [
    {"field": "progress", "from": 224, "to": 142, "applied": 224, "blocked": True},
    {"field": "scoreRaw", "from": 80,  "to": 90,  "applied": 90,  "blocked": False}
  ]
}
```

- `from` — the account's value, `None` on a create.
- `to` — what the row asked for.
- `applied` — **what the account will hold** after the write. That is what the policy sends, except
  where AniList overrides it on a status change (L7–L8), which `by_anilist: true` marks.
- `blocked` — `True` when `applied != to` because of the policy.
- Fields whose `from` equals `to` are **omitted**; a payload with an empty `diff` is a no-op row.

`/api/anilist-preview` response gains `diff` and two counts. Full contract after this phase:

```jsonc
{
  "ok": true,
  "viewer": "name",
  "backfilled": 0,
  "total": 120, "creates": 31, "updates": 89,
  "unchanged": 44,          // payloads whose diff is empty
  "conflicts": 7,           // payloads with at least one blocked field
  "skipped": [{"title": "...", "reason": "no AniList id"}],
  "rows": [ {"title": "...", "anilist_id": 1, "update": true, "diff": [...]} ]
}
```

Note R19: `backfill_anilist_ids` mutates the caller's rows before payloads are built, so the diff
must be computed after it — which it naturally is, since both happen inside the endpoint.

Confirm modal: a scrollable list of changing rows only, unchanged collapsed to "44 already match",
each blocked field offering **keep mine / keep AniList**. Choosing "keep mine" adds to `overrides`
and the write re-sends them. If the account's score format is not `POINT_100`, label the score row
with the account's format (E5) so "80" is not mistaken for the user's own 1–10 scale.

### Step 4 — undo

Snapshot file `.aniexport_undo_<unix_ts>.json`, written next to `.anilist_cache.json`, added to
`.gitignore`, and **never containing the token**:

```jsonc
{
  "version": 1,
  "written_at": 1760000000,
  "media_type": "manga",
  "viewer_id": 12345,
  "rows": [
    {"anilist_id": 30642, "was_create": false,
     "prior": {"id": 998, "status": "CURRENT", "progress": 224, "progressVolumes": 18,
               "score": 80, "repeat": 0, "notes": "...",
               "startedAt": {"year": 2024, "month": 3, "day": 1}, "completedAt": null}},
    {"anilist_id": 30013, "was_create": true, "created_entry_id": null}
  ]
}
```

**`created_entry_id` is filled in after the fact.** A created entry's id exists only in the
mutation response (R6, D3), so the writer updates the row and re-saves the snapshot as each create
succeeds. A crash mid-write therefore leaves the snapshot accurate up to the last completed row,
which is exactly what undo needs. Write it with the same atomic `os.replace` pattern `save_cache`
already uses (`generate_mal_xml.py:987`).

Retention: keep the 5 most recent, prune oldest.

`POST /api/anilist-undo`:

```jsonc
// request
{"token": "...", "snapshot": ".aniexport_undo_1760000000.json",
 "confirm": true, "delete_created": false}
```

- Rows with `was_create: false` → `SaveMediaListEntry` with the `prior` values and the existing
  `id`.
- Rows with `was_create: true` → `DeleteMediaListEntry(id: created_entry_id)` (E13). **Destructive,
  therefore off by default**, listed separately in the confirm as "N entries this run created",
  and performed only when `delete_created` is `true`. A row with `created_entry_id: null` (the
  create failed, or the snapshot predates the fix) is reported as unrecoverable, not guessed at.
- Takes `_run_lock` (R8), returns the existing 409 when busy.
- Streams the same SSE shape as the write, so the client reuses the progress UI.

### Step 5 — retry failed rows

Keep the failed **payloads** for the most recent run only, keyed by run id, in the same module-level
registry the run lock already uses. `write_complete` gains `failed_count` and a `run_id`; the
client posts `{"token": ..., "run_id": ..., "retry": true}` to re-send exactly those payloads. The
stored payloads are dropped when a new run starts, so nothing accumulates.

### Phase 1 tests

New table-driven cases in `tests/test_anilist_write.py`, one per rule, each asserting both the
sent value and the diff row:

- progress 142 vs existing 224 → sends 224, diff `blocked: true`
- progress blank (→0) vs existing 224 → sends 224
- progress 300 vs existing 224 → sends 300, not blocked
- `progressVolumes` on an **anime** row where the existing entry has `None` → no crash, field absent
- score 0 vs existing 80 → sends 80; score 90 vs existing 80 → sends 90
- repeat 1 vs existing 3 → sends 3
- `COMPLETED` → `PLANNING` blocked; `COMPLETED` → `REPEATING` allowed; `DROPPED` → `CURRENT` allowed
- a create (no `known`) → every rule a no-op, diff rows all have `from: null`
- `overrides: {"progress": "mine"}` → sends 142 despite the rule
- a failed existing-entries chunk → preview returns an error, no payloads built

Existing tests that will need updating, since the preview response and payload shape change:
`test_preview_counts_adds_and_updates` (`tests/test_anilist_write.py:129`) and any assertion on
the exact payload dict.

---

## 4. Phase 2 — Batch the write

Depends on Phase 1 (it re-batches Phase 1's payloads). **May be abandoned at the spike.**

### The spike comes first

Two rows, throwaway account, standalone script — **not** through
`_execute_anilist_authenticated`, because that helper does not expose response headers to its
caller and the spike needs to read `X-RateLimit-Remaining` before and after. Use `urllib` directly.

Question: does one document containing aliased mutations cost **1** request or **N** against the
rate limit?

- **costs 1** → build it. 339 rows: ~12 min → ~90s.
- **costs N** → **abandon Phase 2.** Batching would buy nothing and the undocumented burst limiter
  (noted in `generate_mal_xml.py:69-71`) makes it actively riskier. Nothing else depends on this.

> [!NOTE]
> **The spike has been run. It costs 1 request.** A two-alias `SaveMediaListEntry` document
> decremented `X-RateLimit-Remaining` by exactly 1, against a control of three single-operation
> requests that each decremented by 1, and both aliases applied — see §2.3, L5 and L6.
>
> **Phase 2 is cleared to build.** Two caveats the spike also surfaced:
> - It was measured at 2 aliases, not 8. The decrement is per document rather than per field
>   resolver, so 8 should behave identically, but the first 8-alias document should re-read the
>   header and confirm before the batch size is raised.
> - `X-RateLimit-Limit` read **30, not 90**, so the spike ran while the API was in its degraded
>   mode (`generate_mal_xml.py:66-88` already adapts the interval to the observed limit). Batching
>   matters *more* in that state, not less: at 30/min the per-row write would take ~34 minutes.

### If the spike passes

`_write_batch_query(n)` mirroring `_search_batch_query` (`generate_mal_xml.py:462`):

```graphql
mutation ($id0: Int, $mediaId0: Int, $status0: MediaListStatus, $scoreRaw0: Int, $progress0: Int, ...) {
  m0: SaveMediaListEntry(id: $id0, mediaId: $mediaId0, status: $status0,
                         scoreRaw: $scoreRaw0, progress: $progress0, ...) { id status }
  m1: SaveMediaListEntry(id: $id1, ...) { id status }
  ...
}
```

Per-alias variables are numbered. Optional fields (`progressVolumes`, `repeat`, `notes`,
`startedAt`, `completedAt`) are only declared for aliases that carry them, so the document is built
per batch rather than from a fixed template — unlike the search batcher, where every alias has the
same shape.

**Partial failure (E11, E12, D5).** Mutations in one document execute serially and a failing alias
does not stop later ones. The authoritative signal that row *k* failed is `data["mk"] is None`.
Error *messages* have no `path`, only `locations`, so they are matched to aliases by the character
offset recorded for each alias when the document is built; when an offset cannot be matched, the
batch-level message is attributed to every null alias in that batch rather than guessed at.
Successful aliases return `{id, status}` — that `id` is what Step 4's snapshot needs for creates.

**Cancellation** currently checks between rows (`gui.py:4621-4623`). With batching it can only
check between *batches*: a cancel arriving mid-batch still writes the remaining rows of that batch.
Say so in the UI rather than implying row-level granularity.

Emit one `write_progress` event per row even though they complete 8 at a time, so the progress bar
does not jump. Add an ETA to the write phase; the resolve phase has one (`gui.py:4398-4412`), the
write has none.

---

## 5. Phase 3 — Read an AniList list

Independent of Phases 1, 2 and 4. Read-only, so it is the safest of the four.

### The query

```graphql
query ($name: String, $userId: Int, $type: MediaType, $chunk: Int, $perChunk: Int) {
  MediaListCollection (userName: $name, userId: $userId, type: $type,
                       chunk: $chunk, perChunk: $perChunk,
                       forceSingleCompletedList: true) {
    hasNextChunk
    lists {
      name status isCustomList
      entries {
        id status score (format: POINT_100) progress progressVolumes repeat notes
        startedAt { year month day } completedAt { year month day }
        media { id idMal title { english romaji } format chapters volumes episodes
                status startDate { year } popularity coverImage { medium } }
      }
    }
  }
}
```

- Loop while `hasNextChunk` (E2). One call per media type — `type` is a single enum (E9).
- `forceSingleCompletedList: true` merges split completed lists; **verified** (E8): without it one
  account returned `Completed TV` (7) and `Completed TV Short` (1) as separate `COMPLETED` groups,
  with it a single `Completed` of 8.
- **Skip groups where `isCustomList` is true** (E7) and dedupe by `media.id` regardless. A custom
  list repeats entries that already appear under their status group; skipping the custom groups is
  the precise fix, and the dedupe is the belt-and-braces.
- `score (format: POINT_100)` for the same reason as D7 — the import must not inherit the account's
  display format.
- `perChunk`: start at 500 and halve on error. The documented maximum is **open**; 500 is the
  commonly used value and the halving loop makes a wrong guess self-correcting.
- Public lists need no token (E2). With a token, resolve `userId` through the existing
  `anilist_viewer` (`generate_mal_xml.py:1334`) so private lists work. A private list queried by
  name returns no lists rather than an error — say that plainly instead of reporting an empty list.

### Inverse status map

`ANILIST_STATUS_MAP` is one-way and not invertible (R17). Add beside it:

```python
ANILIST_STATUS_REVERSE = {
    "anime": {"CURRENT": "Watching", "PLANNING": "Plan to Watch", "COMPLETED": "Completed",
              "PAUSED": "On-Hold", "DROPPED": "Dropped", "REPEATING": "Rewatching"},
    "manga": {"CURRENT": "Reading",  "PLANNING": "Plan to Read",  "COMPLETED": "Completed",
              "PAUSED": "On-Hold", "DROPPED": "Dropped", "REPEATING": "Rereading"},
}
```

Covers all six enum members (E9). Add a test asserting every `MediaListStatus` value has a reverse
mapping for both media types, so a future AniList addition fails loudly rather than silently
producing `None`.

### Mapping an entry to a row

`score` 0–100 → row score 0–10 (divide by 10, round). `startedAt`/`completedAt` `{year, month,
day}` → `YYYY-MM-DD`, and **only when all three are present** — a FuzzyDate may be partial (E10),
and `_mal_date` rejects anything that is not a complete padded date. A partial date becomes blank
rather than a fabricated day.

### What it unlocks

- **AniList → MAL XML**, impossible today. Entries carry `media { idMal }` and feed the existing
  `build_mal_xml` / `build_mal_manga_xml`. Rows with `idMal: null` are the existing **AniList only**
  case and are reported by the three-way counter, not dropped silently.
- **Backup** before a write, independent of the Step 4 snapshot.
- **Diff against the real list** — "40 of these 339 are already on your account" — before resolving.

### Surface

`POST /api/anilist-import`, beside `/api/parse-mal-xml`, taking `{username | token, media_type}`.
Takes `_run_lock` (R8) — an import landing rows into `currentAnimes` while a write iterates them
would be a real race. Reuses the MAL-XML import merge (match on MAL id, keep the row's
`anilist_id`, honour the add-unmatched toggle) rather than growing a second merge.

Also worth a CLI flag — `--from-anilist USERNAME` — since the whole phase is read-only and the CLI
already knows how to write both XML files.

---

## 6. Phase 4 — Review and bulk-edit UX

Independent of Phases 1–3.

### 6.1 Surface the confidence that is already computed

`_media_rank` runs `difflib.SequenceMatcher` against both title forms and keeps `best_ratio`, then
returns it inside a sort key that is used once and dropped (R9). Nothing about how confident a
match was reaches the UI, so a confidently-wrong match renders identically to a perfect one (R11).

Mechanism, precise because the ratio and the runner-up gap live in different places:

1. Extract the ratio into a helper — `_title_ratio(node, wanted) -> float` — and call it from
   `_media_rank` so the logic exists once.
2. `_parse_media_node` / `_parse_manga_node` already receive `fallback_title` (R10), so each parsed
   entry can carry `match_ratio` computed there.
3. The **gap to the runner-up** is only knowable where the ranked list exists, so `_candidates`
   (`:296-317`) sets `runner_up_gap` on the first entry after ranking. With fewer than two
   candidates the gap is `None`, which is not the same as `0.0` and must not be treated as a tie.
4. `needs_review = match_ratio < REVIEW_RATIO_FLOOR or (gap is not None and gap < REVIEW_TIE_GAP)`.

Starting values `REVIEW_RATIO_FLOOR = 0.6`, `REVIEW_TIE_GAP = 0.05`, as module constants.

> **These are guesses until calibrated** (D10). Before shipping: run the ranker over the user's
> 339-title list, dump `(pasted_title, matched_title, ratio, gap)` to a CSV, and pick the floor from
> the actual distribution. The one known-bad case — `I Am Being Chased To Fall In Love Everyday`
> resolving to `Forced to Date with BOSS Everyday`, recorded as unsolvable in the deleted
> AniList-first plan — must land above the floor, and a sample of known-good matches must land
> below it. Report both numbers when the phase lands.

A **"Needs review"** chip joins `CHIPS` (`gui.py:3210-3219`). A manually picked match clears
`needs_review`: the ratio describes the original search and is meaningless once a human has chosen.

**Cache interaction.** Parsed entries gain fields, so bump `CACHE_VERSION` to 2 (R12). `load_cache`
already discards the whole file on a mismatch, so stale rows cannot leak in without the new fields —
no migration code. **Cost:** every user's cache is invalidated once, so the next run is a cold
resolve (~90s for 339 titles). That is the right trade against silently serving entries that have
no confidence data.

### 6.2 Stop the inline Find button guessing

`repairRow` (`gui.py:3500-3535`) applies `entries[0]` without rendering candidates (R15), which is
the same "confidently wrong" failure the fixer modal was built to prevent. Show the candidate list
when there is more than one hit, or when the top hit's `match_ratio` is below the floor. The modal
already has the UI; this is a routing change, not new interface.

### 6.3 Scope the bulk controls

`selectStatus` and `inputScore` both do `currentAnimes.forEach(...)` (R13), overwriting unchecked
rows, rows hidden by the filter, and any per-row value the user hand-edited. `inputScore` is bound
to `input`, so it re-stamps on every keystroke — which is the second half of the score data-loss
path in §1.1.

- Scope both to selected-and-visible rows. `selectAllCheckbox` (R14) already does this correctly —
  follow its predicate rather than inventing a second one.
- Move `inputScore` from `input` to `change`.
- Label each control with the count it affects: "Apply to 12 selected".
- With nothing selected, apply to all **visible** rows and say so in the label. (Chosen over
  disabling the control: a user who has filtered to "Dropped" and wants to stamp all of them should
  not have to select them first. The label makes the scope explicit either way.)

This reverses a decision recorded in the deleted `PLAN.md` (#15), which deferred exactly this with
"revisit only on request". It will break the jsdom assertions that rely on stamp-all behaviour;
those are expected updates, not regressions.

### 6.4 Smaller items

Shift-click range selection; arrow-key row navigation with space to toggle; a **Back** button in the
fixer (`fixPos` only ever increments, `gui.py:3865`); debounced search-as-you-type in the fixer,
which today requires pressing Search.

---

## 7. Sequencing

```
Phase 1 ──► Phase 2 (spike gate; may be abandoned)
Phase 3   (independent)
Phase 4   (independent)
```

Phase 2 re-batches Phase 1's payloads, so it must come second. Phases 3 and 4 touch different code
and can be built in any order, including before Phase 1 — but Phase 1 is the one that stops data
loss, so it goes first on merit, not dependency.

## 8. Edge cases that need explicit handling

- **A stale undo snapshot.** If the user deleted an entry on AniList after the write, the
  snapshot's `id` no longer resolves and the restore fails for that row. Report it per row like any
  other write failure; do not abort the rest of the undo.
- **A create whose id was never recorded.** `created_entry_id: null` means the create failed or the
  snapshot predates Step 4. Report as unrecoverable; never guess an id to delete.
- **The global run lock.** Write, undo, retry and import all take `_run_lock` (R8) and return the
  existing 409 when busy.
- **Cancel during a batched write.** Resolution is row-level today; batching makes it batch-level.
  The UI must not imply otherwise.
- **An anime row's `progressVolumes`.** `None` from AniList (E6); never compared, never sent.
- **A partial FuzzyDate on import.** Missing month or day → blank, not an invented date.

## 9. Open questions

| # | Question | How it gets settled |
|---|---|---|
| ~~O5~~ | ~~Does AniList honour an all-`null` `FuzzyDateInput` as "clear this date"?~~ | **Settled: yes.** Measured — §2.3, P5. It is how the stale finish date is cleared |
| ~~O1~~ | ~~Does a document of aliased mutations cost 1 request or N against the rate limit?~~ | **Settled: 1 request.** Measured at 2 aliases — §2.3, L5. Re-confirm at 8 before raising the batch size |
| O2 | What is `perChunk`'s maximum on `MediaListCollection`? | Start at 500, halve on error — self-correcting, so it needs no answer up front |
| O3 | Where should `REVIEW_RATIO_FLOOR` sit? | Calibration run described in §6.1, against the real 339-title list |
| ~~O4~~ | ~~Does AniList rate-limit authenticated mutations differently from anonymous queries?~~ | **Settled: no separate mutation budget.** Queries and mutations drew from one counter on the same token — §2.3, L5 |

## 10. Risks

| Risk | Mitigation |
|---|---|
| The conflict policy blocks an edit the user genuinely wanted | Per-row override, and every blocked field is visible in the diff before anything is sent |
| Undo is itself a write, and can fail halfway | Per-row reporting, same as the write; the snapshot is not deleted until the undo completes |
| Deleting created entries is irreversible | Off by default, listed separately, requires an explicit tick |
| The batched write makes a partial-failure bug harder to diagnose | `data.mk is None` is an exact per-row signal (E11); per-row progress events are kept |
| `CACHE_VERSION` bump costs every user one cold run | Stated in the release notes; the alternative is serving entries with no confidence data |

## 11. Files

| File | Work |
|---|---|
| `generate_mal_xml.py` | `ANILIST_EXISTING_ENTRIES_QUERY` (+5 fields, `score` format), `anilist_existing_entries` (error propagation), new `_reconcile_with_existing`, `iter_write_payloads` (diff), new `_write_batch_query`, new `ANILIST_LIST_COLLECTION_QUERY` + reader, `ANILIST_STATUS_REVERSE`, new `_title_ratio`, `_parse_media_node` / `_parse_manga_node` (+`match_ratio`), `_candidates` (+`runner_up_gap`), `CACHE_VERSION` → 2 |
| `gui.py` | `/api/anilist-preview` (diff in the response), `/api/anilist-write` (snapshot, retry registry), new `/api/anilist-undo`, new `/api/anilist-import`; `HTML_TEMPLATE` for the diff modal, the Needs-review chip, bulk-edit scoping, `repairRow` candidate rendering, selection and keyboard work |
| `.gitignore` | `.aniexport_undo_*.json` |
| `tests/` | `test_anilist_write.py` (conflict table, diff shape, undo, retry), `test_batch_resolution.py` (batched mutation document, alias attribution), new suite for the list reader and the reverse status map |
| `README.md`, `MANGA_SUPPORT_NOTES.md` | Per the project's convention of recording what shipped and what was verified. The README's write warning is narrowed once Phase 1 lands |

## 12. Verification

- `python -m unittest discover -s tests -t .` — **203 pass today** (132 when this plan was
  written, 140 after the skip list and match picker). Every item above lands with tests; the
  conflict policy is table-driven, one case per rule per §3 "Phase 1 tests".
- jsdom suites in the scratchpad for the diff modal (**done** — 57 assertions, 16 suites and 352
  assertions in total), the Needs-review chip and the bulk-edit scoping.
- Chrome screenshots at 1440 and 430 plus the width probe. **Not done for the diff modal** — the
  Chrome extension would not connect (2026-10-19). The modal follows `fixModal`'s proven shape (a
  `max-height` scroll box inside `.modal-card`, which caps at `min(900px, 100%)`), every diff line
  wraps, and `overflow-wrap: anywhere` guards the one unbreakable value a diff can hold, a 60-char
  note. That is reasoning, not a measurement, and the screenshots are still owed.
- **Live, on the throwaway account, in this order.** Steps 1 and 2's baseline are **done** — see
  §2.3. The remaining steps all test behaviour Phase 1 has yet to build:
  1. ~~**Spike (O1).** Two aliased mutations; read `X-RateLimit-Remaining` either side; read both
     entries back to confirm both applied.~~ **Done — §2.3, L5/L6. Costs 1 request; both applied.**
     Still to do once `_write_batch_query` exists: re-read the header on a full 8-alias document.
  2. ~~**Guard.** A 3-row write with deliberate conflicts.~~ **Done — §2.3, P1/P2.** 8 fields
     blocked across 3 rows, every one predicted by the preview first.
  3. ~~**Override.** "Keep mine" on a downgrade.~~ **Done — §2.3, P3/P5.** The override works; it
     also found L7, which the diff now reports.
  4. ~~**Undo.**~~ **Done — §2.3, P6/P7.** Including a cleared date put back, and retention
     pruning verified by a real prune (P8).
  5. ~~**Create + undo, both ways.**~~ **Done — §2.3, P9/P10.** `created_entry_id` recorded as the
     write ran, and the delete path exercised for the first time.
  6. ~~**Failure and retry.**~~ **Done — §2.3, P11/P12.** Not in the original list; added because
     Phase 1 Step 5 had never seen a real failure.

  Still unexercised live: **cancellation mid-write**, and a write large enough to need more than
  one 50-id existing-entries chunk.
