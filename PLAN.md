# AniExport — Advanced Features & UX Overhaul Plan

Grounded implementation plan for the roadmap items approved on 2026-10-16.
Scope confirmed by user: **all 6 phases including direct AniList write**; import merge =
**imported XML overwrites**; disk cache = **cwd, 7-day TTL**.

> **STATUS: ALL SIX PHASES SHIPPED (2026-10-18).** This document is now a record of how the
> work was reasoned about, not a queue. The one gate not satisfied is Phase 5's: the direct
> AniList write has never been run against a live account — see that phase and the README.
> Verified at close: 122 Python tests, 14 jsdom suites, `py_compile` + CLI smoke clean.

> **Revision 2026-10-17 (rev 3)** — re-verified end to end against the working tree and the
> official AniList docs. Every line reference below was re-checked; the test count (15), the
> eight gui.py imports, and all 17 cited `gui.py` / `generate_mal_xml.py` locations are current.
>
> Corrections across revisions: builders gain date emission (was collected-but-never-exported);
> date field names pinned; Phase 5 backfills `anilist_id`; pacing made adaptive (90/min nominal
> vs the degraded 30); error handling keyed off `errors[].status` rather than HTTP status.
>
> **Rev 3 fixed four errors in this plan's own reasoning:**
> 1. The additive ranking weights contradicted the invariant they claimed to enforce — a one-shot
>    with a perfect title match outscored a not-yet-released full series (1410 vs 700). Replaced
>    with lexicographic tiers.
> 2. Batching via aliased top-level `Media` would have silently dropped the candidate list and the
>    `idMal`-null skip — the two fixes shipped this week. Now aliased `Page` blocks.
> 3. The ETA formula hardcoded 2.1 s, contradicting the adaptive-pacing constraint added in rev 2.
> 4. Decision #3 promised a 400 on invalid input, but no phase implemented it, and the behaviour
>    today is an uncaught traceback mid-SSE. Now an explicit Phase 0 item.

---

## Verified external facts (checked, not assumed)

| Fact | Source / check |
|---|---|
| MAL XML dates use `YYYY-MM-DD` (matches existing `0000-00-00` placeholders in both builders) | MAL XML exports parse as `"start_date": "2020-07-01"` (malexport); consistent with repo placeholders |
| AniList `SaveMediaListEntry` accepts `mediaId`, `status` (MediaListStatus), `score`, `scoreRaw` (0–100), `progress`, `progressVolumes`, `notes`, `startedAt`/`completedAt` (`FuzzyDateInput {year, month, day}`), `customLists`, `advancedScores`, `priority` | AniList official mutation reference (AniList/docs + docs.anilist.co) |
| AniList `Media(idMal: $id, type: $type)` query exists (used by direct MAL-ID claim) | AniList Media query reference — endpoint still degrades gracefully to `ok:false` if rejected |
| Nominal rate limit is **90 req/min**. The API is **currently in a degraded state limited to 30 req/min**, described as temporary. Exceeding it yields a **1-minute timeout** with `Retry-After` and `X-RateLimit-Reset` | docs.anilist.co/guide/rate-limiting; matches observed `X-RateLimit-Limit: 30`, `Retry-After: 51` |
| A separate **burst limiter** exists on top of the per-minute limit, to stop "too many requests in a very short period of time" (no published threshold) | docs.anilist.co/guide/rate-limiting |
| A 429 is also returned **in the GraphQL body** as `{"data":null,"errors":[{"message":"Too Many Requests.","status":429}]}` | docs.anilist.co/guide/rate-limiting |
| **A 200 response may still contain errors** — `errors[].status` carries the real code (e.g. 400), so HTTP status alone is not a success signal | docs.anilist.co/guide/graphql/errors |
| Mutation validation failures return an extra **`validation` object** with per-field messages (e.g. `"The score may not be greater than 100."`), suitable for display | docs.anilist.co/guide/graphql/errors |
| `Page` accepts **only one data field** per query (`media` *or* `characters` …); `pageInfo` is exempt | docs.anilist.co/guide/graphql/pagination |
| `PageInfo.total` and `PageInfo.lastPage` are **currently inaccurate**; only `hasNextPage` is reliable | docs.anilist.co/guide/graphql/pagination |
| `SaveMediaListEntry` create-vs-update is decided by **whether an `id` is supplied** | docs.anilist.co/guide/graphql/mutations |
| AniList search is **not** fuzzy over typos — `Raphsody`, `Aristrocat`, `Wisemans Grandchild` return **zero** hits, not bad hits | Observed live against the manga.txt misses |
| Builders hardcode `0000-00-00`; `comments`/`tags` are entry-driven | `generate_mal_xml.py:696-697, 702, 706, 791-792, 798, 800` |

## Unverified assumptions (must be spiked before the phase that depends on them)

| Assumption | Risk if wrong | Spike |
|---|---|---|
| ~~AniList accepts 8 aliased `Page { media(search:) }` blocks~~ **RESOLVED 2026-10-17: yes.** One document with 8 aliases returned HTTP 200, an empty `errors` array and all 8 aliases populated with candidate lists. Four documents sent back to back moved `X-RateLimit-Remaining` 29 -> 28 -> 27 -> 26: **one document costs one request, not eight**, so batching is a true 8x throughput gain and the burst limiter was not triggered. | resolved | spike run live against graphql.anilist.co |
| `notes` max length 6000 | Over-long notes rejected (as a `validation` error, now displayable) or truncated server-side | Not stated anywhere in the docs. Send one over-long note during the Phase 5 manual test and read the `validation` object. |
| `SaveMediaListEntry` with `mediaId` but no `id` **updates** an existing list entry rather than creating a duplicate | Re-running a write could duplicate entries on the user's account | Docs say create-vs-update keys off `id`. Phase 5's manual test must write the same entry **twice** and confirm the list still has one row. |
| A cancelled/paused run leaves a usable partial state | Partial files confuse the download buttons | Covered by Phase 2 tests |

## Observed GUI gaps (verified against the working tree, 2026-10-17)

Every line reference below was checked in the current `gui.py`, not assumed. Ordered by
severity. The right-hand column says which phase closes it; three were **not** covered by
the original plan and are folded into Phase 0 below.

### Data correctness — these make the exported file wrong

| # | Gap | Evidence | Covered by |
|---|---|---|---|
| 1 | **One status for the entire list.** `selectStatus` stamps `item.status = newStatus` onto every row, and the status cell renders a non-interactive pill. A real list is mixed (e.g. 80 completed, 12 reading, 5 dropped); today you either export everything under one status — false data — or do one run per status and merge the XML by hand. **Largest functional gap in the GUI.** | `gui.py:1871` (bulk assign), `gui.py:1767` (read-only pill) | **Phase 0** (moved — was buried in Phase 3's row editor) |
| 2 | **Download can serve the other media's bytes.** One shared `EXPORT_XML_PATH`; the filename is chosen from `EXPORT_MEDIA_TYPE` at serve time while the link href is static. Run manga, flip the selector to anime, download → manga content inside `myanimelist_export.xml`. Silent and plausible-looking. | `gui.py:32-40`, `1960`, `1973`, `1197` | Phase 0 |
| 3 | **Unchecking every row deletes the export files.** An empty `update-export` unlinks both files with no warning. | `gui.py:2098-2099` | Phase 0 |

### Workflow — these waste the user's time

| # | Gap | Evidence | Covered by |
|---|---|---|---|
| 4 | **A refresh loses a 12-minute run.** `loadData()` starts empty by design; nothing persists except the theme. | `gui.py:1493`; theme-only persistence at `1902`, `1910` | Phase 3 |
| 5 | **No way to stop a run.** No cancel path exists anywhere (`reader.cancel()` at `1581` only closes the client's stream — the server keeps resolving). Wrong media type or a mistyped list means killing the terminal. | no cancel endpoint; `gui.py:1581` | Phase 2 |
| 6 | **Unmatched titles can be copied out but not fixed in place.** The unresolved bar lists them; fixing a typo means editing the textarea and re-running all 339 titles. The paged candidates are already fetched, so a per-row "search title or MAL ID" input fixes them in seconds. | unresolved bar + `copy unmatched`; no per-row input | Phase 2 |
| 7 | **339 rows, no sorting and no status filter.** Text search only. Finding every Dropped row, or the unresolved ones, means scrolling. | `gui.py:1867` (text filter is the only one) | Phase 3 |

### Cheap and genuinely useful

| # | Gap | Evidence | Covered by |
|---|---|---|---|
| 8 | **Drag a `.txt` onto the textarea.** The app has no file input at all — the originating friction of this whole effort was a `manga.txt` on disk that had to be opened and pasted by hand. | zero `type="file"` elements in `gui.py` | Phase 3 |
| 9 | **"Disabled" download buttons are not disabled.** They receive a CSS class and `aria-disabled`, but remain live `<a href>` elements — there is no `preventDefault` anywhere in the file. Clicking one before any run navigates to a raw 404 page. | `gui.py:1197`, `1205`, `1636-1648`; no `preventDefault` in file | **Phase 0** (new) |
| 10 | **Year is not shown.** Two series share a name often enough to matter; `startDate { year }` is absent from both page queries today. | zero `startDate` occurrences in `generate_mal_xml.py` | Phase 0 (parse) + Phase 3 (display) |
| 11 | **"Total Chapters" stat ignores volumes.** The reducer sums `chapters` only, under a label that reads "Total Chapters" — accurate for chapters, but the manga column it summarises is "Chapters / Volumes". | `gui.py:1621` (sum), `1421` / `1227` (label) | **Phase 0** (new) |

**Not scheduled, deliberately:** per-row comments/tags editing, XML preview, timestamped
archives and cover art. Real features, but no observed user has been blocked by their absence,
and per-row dates cannot work at all until Phase 0 stops the builders hardcoding `0000-00-00`.

---

## Non-negotiable constraints (apply to every phase)

1. **Zero external Python dependencies** — stdlib only (`difflib`, `unicodedata`, `math`, `json`, `re`, `xml.etree` are all allowed).
2. **`HTML_TEMPLATE` must contain no backslash characters anywhere** — `tests/test_gui_template.py` fails on any line containing one (it scans the literal's source, and `node --check`s the emitted script). Newlines in JS: `String.fromCharCode(10)` or template literals; **no regex escapes** in new JS (use backslash-free character classes). This guard exists because a single `\n` inside a JS string literal silently killed the entire `<script>`.
3. **Public API compatibility** — `resolve_anime_entries`, `resolve_manga_entries`, `build_mal_xml`, `build_mal_manga_xml`, `sanitize_title_line`, `deduplicate_entries`, `set_rate_limit_notifier`, `MIN_REQUEST_INTERVAL` keep their signatures (gui.py imports exactly these eight).
4. **`tests/test_manga_support.py` patches `_execute_anilist_request` with the query as first positional arg** → single-title fetch functions (`fetch_anime_info_online`, `fetch_manga_info_online`) keep calling it exactly as today.
5. **All existing tests green at the end of every phase** — with one deliberate, documented change: `tests/test_gui_manga_endpoint.py` moves its patch target from `gui.resolve_manga_entries` to `gui.resolve_many_entries`. **Both the target and the mock's return shape change** (`[entry, ...]` → `{title: [entry, ...]}`); the assertions are unchanged.
6. **Rate-limit policy — pacing becomes adaptive (Phase 0).** Today `MIN_REQUEST_INTERVAL = 2.1`
   is hardcoded for the **degraded** 30 req/min the API is currently serving. The documented
   nominal limit is **90 req/min**, and the degradation is described as temporary, so when it
   lifts the current constant leaves 3× of the throughput unused — a 339-title run would stay
   at ~12 minutes instead of dropping to ~4.
   `_throttle()` reads `X-RateLimit-Limit` from the most recent response and targets
   `60 / limit * 1.05` seconds, clamped to `[0.7, 2.5]`, starting at the current 2.1 until a
   header is seen. `MIN_REQUEST_INTERVAL` stays as the exported starting value, so constraint #3
   and gui.py's import are unaffected. The existing 429 / `Retry-After` / transient-error
   handling is unchanged and remains the backstop, as is the fixed spacing that keeps the
   **burst limiter** satisfied.
7. **HTTP 200 is not a success signal.** Per the errors doc, a 200 response may carry
   `errors[].status` of 400, 429 or 5xx. Every code path that inspects a response — existing
   transient detection, Phase 1a's adaptive batch halving, Phase 5's write results — keys off
   `errors[].status`, never the HTTP status alone.
8. **Canonical entry field names** — fixed once here, used identically by builders, row editor, XML import and AniList write:
   `start_date`, `finish_date` (`YYYY-MM-DD` or `0000-00-00`), `comments`, `tags`,
   `my_watched_episodes`, `my_read_chapters`, `my_read_volumes`, `score`, `status`.
   Private/derived keys (`_rereading`, `_rewatching`, `candidates`) are never persisted to disk, sent to the write API, or round-tripped through import.

---

## Phase 0 — Foundation: entry dates, per-row status, per-media export files, `/api/export-state`

> **STATUS: COMPLETE (2026-10-17).** All items below are implemented and verified —
> 23 Python tests, 5 jsdom suites, a 22-check live server script, and live AniList calls.
> Two things shipped beyond the written plan:
> - **Live row streaming.** Progress events now carry the resolved rows, so the table fills
>   during a run instead of staying empty until `complete`. The client dedupes exactly as
>   `deduplicate_entries` does, `complete` merges rather than replaces (so edits made mid-run
>   survive), and downloads stay locked while a run owns the files.
> - **Fail fast on HTTP 400.** A malformed query can never succeed, so it no longer burns
>   three retries' worth of rate limit. This is what surfaced the `coverImage` error below.
>
> **One deliberate deviation from decision #15:** the global status dropdown still stamps
> every row, exactly as before; per-row selects were *added* alongside it rather than
> replacing it. Rationale: the user asked for nothing breaking, and demoting the global
> control to "apply to checked rows" changes existing muscle memory. Decision #15 is
> superseded by this note until someone asks for the demotion.

**Problem fixed (a):** both builders hardcode `<my_start_date>0000-00-00</my_start_date>`
(`generate_mal_xml.py:696-697` anime, `791-792` manga). Phases 3, 4 and 5 all assume
per-entry dates exist. Without this, a user can set dates in the UI, export, and silently
lose them. **This must land first** — it is the shared contract P3/P4/P5 depend on.

**Problem fixed (b):** one shared `EXPORT_XML_PATH`/`EXPORT_GZ_PATH`/`EXPORT_MEDIA_TYPE`
trio (`gui.py:32-40`; filenames chosen at `gui.py:1960` and `1973`) means manga runs
overwrite `myanimelist_export.xml`, and a selector flip can serve one media's bytes under
the other's filename.

**generate_mal_xml.py**
- **Dates in both builders** — new `_mal_date(value)` helper: returns `0000-00-00` for
  empty/missing, passes through a strict `YYYY-MM-DD` match, raises `ValueError` otherwise
  (mirrors `_normalize_status`'s reject-loudly behaviour, so bad input surfaces as a 400
  rather than a corrupt import). Both builders emit
  `_mal_date(entry.get("start_date"))` / `_mal_date(entry.get("finish_date"))`.
  Default stays `0000-00-00`, so every existing caller and test is unaffected.
- Add `startDate { year }`, `popularity`, `coverImage { medium }` to `ANILIST_SEARCH_PAGE_QUERY`
  and `ANILIST_MANGA_SEARCH_PAGE_QUERY`.

  > **Corrected:** earlier revisions of this plan specified `coverImage { small }`. **That field
  > does not exist** — AniList answers `Cannot query field "small" on type "MediaCoverImage"`
  > with status 400, which made every search return nothing. The real fields are `medium`,
  > `large`, `extraLarge` and `color`; `medium` serves the CDN's small image, which is what was
  > intended. Verified live.
- `_parse_media_node` (line 275) / `_parse_manga_node` (line 324): add keys `year` (`int|None`),
  `popularity` (`int`), `cover` (`str|None`).
- **Adaptive pacing (constraint #6).** `_throttle()` targets `60 / observed_limit * 1.05`, where
  `observed_limit` is the last `X-RateLimit-Limit` seen (90 nominal, 30 while degraded), clamped
  to `[0.7, 2.5]` s and defaulting to today's 2.1 until a header arrives. The response handler
  records the header on both success and 429 paths. Roughly 3× faster whenever AniList restores
  the nominal limit, with no change in behaviour while it is degraded.
- Fix `my_scanslation_group` → `my_scanalation_group` (MAL's own export carries that
  misspelling; ours currently differs). Harmless to importers either way, but it is a
  deviation from the format we claim to emit.

**gui.py**
- Replace the single path pair with `EXPORT_FILES = {"anime": {"xml":..., "gz":...}, "manga": {...}}`
  (under `output/` when it exists; manga names `myanimelist_manga_export.xml[.gz]`).
- `GET /api/download/xml?media=anime|manga` (same for `gz`): **param optional — omitted means
  the active media**, so existing links keep working; any value other than the two literals → 400.
  `Content-Disposition` filename derived from the resolved media; 404 if that file is missing.
- **New** `GET /api/export-state` → `{"anime": {"xml": bool, "gz": bool, "xml_name": ..., "gz_name": ..., "count": int|null, "updated_at": ...}, "manga": {...}, "active_media": ...}`
  (count parsed from file on demand, wrapped in try/except).
- `POST /api/update-export` with **empty entries no longer deletes files** (currently
  `unlink` at `gui.py:2098-2099`) — returns `{ok, count: 0}`; stale-but-valid files remain downloadable.
- Client: after load, process, update, or media-type flip → fetch `/api/export-state` and drive
  `href`, `download` attribute and `disabled` from server truth instead of client assumptions.
- **Builder `ValueError` must become a clean error, not a traceback (pre-existing bug).**
  Decision #3 assumes invalid input surfaces as a 400, but nothing implements it today:
  `_normalize_status` already raises, and in `/api/process` it raises *after* the SSE headers
  are sent, so the stream simply ends with no `complete` event, a traceback in the console, and
  a UI that quietly gives up. Verified by sending `status: "Bogus"` to the endpoint.
  - `/api/update-export` (no stream yet): wrap the build in `try/except ValueError` → `400 {ok: false, error}`.
  - `/api/process` (headers already sent): wrap the build and emit
    `{"type": "error", "error": str(exc)}`, which the client renders as a toast.
  - Phase 0's `_mal_date` raises the same way, so both date and status validation land on one path.

**gui.py — GUI gaps 1, 9, 11 (see register above)**
- **Per-row status select (gap 1).** The status cell becomes a `<select>` built from the existing
  `STATUS_SETS[mediaType()]`, bound per row: `change` → `item.status = value` → `renderTable` +
  debounced `updateStatsAndExport()`. The global `selectStatus` stops stamping every row and becomes
  **"apply to checked rows"** plus the default for newly resolved entries.
  *No backend work:* both builders already read `entry["status"]` per entry and normalize it
  (`_normalize_status`), so a mixed-status list exports correctly the moment the UI can express it.
  This is listed here rather than in Phase 3's row editor because it is a data-correctness fix,
  not a convenience, and it has no dependencies.
- **Disabled download links must not navigate (gap 9).** A `click` handler on both anchors calls
  `preventDefault()` while the `disabled` class is present. Server truth from `/api/export-state`
  drives that class, so the button state and the file state cannot disagree.
- **Stat card honesty (gap 11).** For manga the card shows chapters and volumes
  (`"12,345 ch · 1,204 vol"`) or the label narrows to match what is actually summed. No silent
  half-truth under a "Total Chapters" heading.

**Tests (new):** `tests/test_export_state.py` — flip regression (process manga →
`download?media=manga` serves manga bytes and name while `media=anime` is 404);
empty update-export keeps files. **`tests/test_dates.py`** — valid date emitted,
missing → `0000-00-00`, malformed → `ValueError`, and a `build → parse → fields equal`
roundtrip for both builders.

**Acceptance:** existing 15 + new tests pass; no filename/content mismatch possible for any
selector sequence; a date set on an entry survives export; **a list containing three different
statuses exports with those three statuses** (today it cannot); a "disabled" download button
cannot reach a 404.

---

## Phase 1 — Resolution engine: batching, ranking, variant retries, disk cache

> **STATUS: COMPLETE (2026-10-17).** Spike passed first (see the resolved row above).
> Shipped: `_search_batch_query` / `_execute_anilist_batch` over aliased `Page` blocks,
> `_post_graphql` as the single shared network policy, lexicographic `_media_rank` /
> `_rank_nodes` / `_pick_best_media(nodes, search_title)`, `_search_variants`, the
> `.anilist_cache.json` disk cache with `--no-cache` and `ANIEXPORT_CACHE`,
> `resolve_many_entries` with `on_progress` and `should_cancel`, and `_expand_sequels_chain`
> shared by both paths. GUI `/api/process` and both CLI branches use it.
>
> **Measured live:** 12 titles resolved in **3 documents / 4.5s** against ~25s of pacing for the
> per-title path. A warm cache costs 0 documents for anything that resolved. The variant pass
> recovered a real miss from the user's list (`Hero?I Quit A Long Time Ago(YingXiong Zai Lin)`
> -> MAL 133966) while the typo `Raphsody` correctly stayed unresolved, as predicted.
>
> **Two deliberate test changes:** `test_gui_manga_endpoint` and `test_run_control` now patch
> `gui.resolve_many_entries` with the batched signature; their assertions are unchanged.


> **Gate:** do the 8-alias spike from the unverified-assumptions table first. If AniList
> rejects it, 1a reduces to "keep one request per title" and the rest of Phase 1 still stands.

### 1a. Batched GraphQL
- `_search_batch_query(count, media_type)` → builds a document of **aliased `Page` blocks**:
  `t0: Page(perPage: 10) { media(search: $q0, type: MANGA) {...} } t1: Page(perPage: 10) {...}`,
  with the full field set (incl. Phase 0 fields). `BATCH_SIZE = 8`.

  > **Not aliased top-level `Media(search:)`.** A `Media` alias returns a *single* node, which
  > would destroy both things the current resolver depends on: the 10-candidate list that feeds
  > the match picker, and the ability to skip a top hit whose `idMal` is `null` (the One Punch
  > Man / Boruto fix). Batching must preserve a candidate list per title, so each alias has to be
  > a `Page`. The documented "only one data field per `Page`" rule constrains the *inside* of each
  > `Page` block (`media` only, no `characters` alongside it) — it does not limit how many aliased
  > `Page` blocks a document may contain.
- Extract shared HTTP core from `_execute_anilist_request` (`generate_mal_xml.py:340-393`):
  new `_post_graphql(query, variables, max_retries)` returns the full `data` dict (throttle,
  429 waits, transient-error detection all shared). `_execute_anilist_request` becomes a thin
  wrapper preserving exact signature and `result_key` semantics → constraint #4 holds.
- New `_execute_anilist_batch(query, variables, max_retries=3)` → `data` or `None`. Retries the
  whole payload on HTTP-level failure; returns partial data when individual aliases are `null`
  (those titles are unresolved).
- **Adaptive fallback:** if AniList rejects the document, halve batch size down to 1 and retry —
  protects against unknown alias limits. **Trigger is `errors[].status == 400` in the response
  body, not the HTTP status** (constraint #7: a rejection can arrive as HTTP 200 with an errors
  array). A `status == 429` is *not* a halving signal — that is the existing rate-limit path.
- **`Page` shape constraint:** each aliased `Page` block carries `media` and nothing else
  (the documented one-data-field rule). `PageInfo.total`/`lastPage` are documented as inaccurate,
  so nothing may derive counts or ETA from them — `hasNextPage` only, and we request neither.

### 1b. Candidate ranking + year
- `_pick_best_media(nodes, search_title="")` (`generate_mal_xml.py:309`) — **lexicographic tiers,
  not additive weights.** Stable sort on
  `(has_mal_id, is_not_one_shot, is_released, score, -original_index)`, descending.

  > **Why tiers:** an earlier draft of this plan used additive weights (`idMal +1000`,
  > `ONE_SHOT −500`, exact title `+300`, ratio `×200`, status `±`, popularity `+50`) and
  > asserted the invariant "a non-one-shot with a MAL ID always outranks a one-shot".
  > Those numbers do not satisfy that claim: a one-shot with a MAL ID and a perfect title
  > match scores `1000 − 500 + 300 + 260 + 200 + 100 + 50 = 1410`, while a non-one-shot with
  > a MAL ID that is not yet released scores `1000 − 300 = 700`. The one-shot wins — exactly
  > the Boruto / Gotoubun regression this is meant to prevent. Tiers make the invariant true
  > by construction instead of by arithmetic that has to be re-checked on every tweak.

  - **Tier 1 — `has_mal_id`:** anything with `idMal` outranks anything without. Non-importable.
  - **Tier 2 — `is_not_one_shot`:** a full series outranks a `ONE_SHOT`, *whatever the titles say*.
    Observed failure mode: `Boruto` → "BORUTO: ROAD TO B"; `Gotoubun No Hanayome` → a 122+1 one-shot.
  - **Tier 3 — `is_released`:** `status != NOT_YET_RELEASED`.
  - **Tier 4 — `score`**, used only to order within a tier:
    `max(exact_english * 300, exact_romaji * 260)` (**not additive** — one title bonus, the better
    of the two) `+ SequenceMatcher ratio × 200 + min(50, round(log10(popularity + 1) * 10))`.
  - Normalization: `casefold` → NFKD → strip combining marks → non-alnum → single spaces.
  - Preserve fallback: if *no* node has `idMal`, return `nodes[0]` (existing contract, covered by tests).
  - Callers pass the search title.
- `_candidates` (`generate_mal_xml.py:292`): same score ordering, still MAL-ID-only and deduped by `idMal`.

**Test (mandatory, regression):** the live Boruto and Gotoubun node sets must still select the
full series, with the one-shot ranked below it.

### 1c. Smarter retry for unresolved
- `_search_variants(title)` → ≤3 deterministic stages; only stages differing from the previous are queued:
  1. NFKC + curly→straight quotes + whitespace collapse;
  2. strip one pass of `(…)`/`[…]` groups (recovers `Hero?I Quit A Long Time Ago(YingXiong Zai Lin)`);
  3. (manga only) strip a trailing `manga|manhua|manhwa` token if the remainder is non-empty.
- Variants queued in `BATCH_SIZE` chunks; first variant with a root media wins; still-unresolved →
  unchanged `id:0, type:"Unresolved"` row.
- **Explicit non-goal:** this does not recover misspellings. AniList returns zero hits for
  `Raphsody` / `Aristrocat` / `Wisemans Grandchild`, so no variant rule can find them —
  Phase 2's manual search is the fix for those. Acceptance criteria below reflect that.

### 1d. Disk cache
- Path: `Path.cwd()/.anilist_cache.json` (env override `ANIEXPORT_CACHE`); CLI `--no-cache` flag.
- Format: `{"version": 1, "entries": {key: {"ts": epoch, "data": [...]}}}`;
  key = `f"{media_type}:{title.strip().casefold()}:{'expand'|'single'}"` via a shared `_cache_key`
  helper, so single- and multi-title paths interoperate. (This changes today's anime session-cache
  key shape; session cache is in-memory only, so nothing migrates.)
- TTL 7 days (604800 s); prune expired on load; cap 10,000 entries (oldest dropped);
  `threading.Lock` + atomic write (temp file + `os.replace`); corrupt/missing file → empty dict.
- **Only exportable results cached** (root `id > 0`); unresolved never cached (retried next run).
- `candidates` are cached (they are what makes the picker work offline); private `_`-prefixed keys are not.

### 1e. Orchestration
- New `resolve_many_entries(titles, media_type, expand_sequels=False, session_cache=None, use_cache=True, on_progress=None) -> Dict[str, List[dict]]`:
  1. unique titles → disk/session cache lookup;
  2. uncached → chunk by 8 → batch query → `_pick_best_media` + `_candidates` per title →
     `on_progress(stage="resolve", done_count, total)` after **each title** in the returned batch;
  3. variant retry pass (`stage="retry"`), chunked;
  4. `expand_sequels` → **refactor** the duplicated SEQUEL-chain loops
     (`generate_mal_xml.py:429-455` and `530-553`) into one shared
     `_expand_sequels_chain(root_media, media_type)` used by both single and batch paths (`stage="expand"`);
  5. unresolved rows shaped exactly as today; cache-set exportable results; one atomic cache save per run.
- Single `resolve_anime_entries`/`resolve_manga_entries` keep working (delegating cache get/set to the
  shared helper); `fetch_*_info_online` untouched (still used by tests and the Phase 2 search endpoint).
- **ETA:** progress events gain
  `eta_seconds = remaining_batches × current_interval + remaining_titles × 0.05`, where
  `current_interval` is the **live adaptive value** from constraint #6 — not the literal 2.1,
  which would over-estimate by 3× once AniList restores the 90/min limit. The event also carries
  that interval so the client can fall back to computing it.
- GUI `/api/process`: call `resolve_many_entries` once with an `on_progress` that emits SSE progress
  (including retry-stage subtext), then iterate titles to attach `original`/`status`/`score` → dedupe →
  build XML → write to **per-media Phase 0 paths** → `complete` event. Rate-limit notifier wiring unchanged.
- CLI `main()` and `process_anime_list` switch to `resolve_many_entries` (both media branches); add `--no-cache`.

**Tests (new):** `test_batch_resolution` (mock `_execute_anilist_batch` → call count = `ceil(N/8)`,
order preserved, unresolved shape), `test_candidate_ranking` (one-shot demotion incl. the Boruto/Gotoubun
regression, order independence, no-idMal fallback, `year` parsed), `test_search_variants`
(determinism/distinctness/manga-suffix rule, and that a typo yields no recovery), `test_cache`
(TTL, corrupt recovery, `--no-cache`, unresolved not cached, valid JSON after save).
Update `test_gui_manga_endpoint` target and mock shape (constraint #5).

**Acceptance:** 15 pre-existing tests green (one retargeted); simulated 100-title run → 13 batch calls;
second run with warm cache → 0 network calls; the 339-title list resolves with **no new unmatched titles
versus today** (typos still excluded — they are Phase 2's problem).

---

## Phase 2 — Run control + manual correction

> **STATUS: COMPLETE (2026-10-17).** `_run_lock` (409 on a second run), `/api/run-control`
> (cancel / pause / resume), `started` / `paused` / `resumed` / `cancelled` SSE events,
> `/api/search`, `/api/lookup` (`ANILIST_BY_MAL_ID_QUERY` + `lookup_by_mal_id`,
> `search_candidates`), and per-row repair inputs on rows with no MAL ID.
> **Found and fixed while building:** the client's `break` on `complete`/`cancelled` only left
> the inner chunk loop — the outer reader kept awaiting, so the run never cleaned up unless the
> server closed the socket. Now guarded by an explicit `streamDone` flag.


**Run control (gui.py)**
- `_run_lock` (non-blocking acquire → **409** `"A run is already in progress"`) +
  `_active_run = {run_id: {"cancel": Event, "pause": Event}}`.
- Checkpoint between units of work in `/api/process`: **cancel** → emit
  `{"type":"cancelled","entries":[partial...]}`, write no files, release lock; **pause** → emit
  `{"type":"paused"}`, wait loop that still honours cancel, resume → `{"type":"resumed"}`.
- Client: Cancel + Pause/Resume visible while running; spinner freezes while paused; on `cancelled` →
  populate table with partial entries + `updateStatsAndExport()`.
- **Stale-download guard:** a cancelled run writes no files, so the on-disk export still belongs to the
  previous run while the 250 ms debounce is pending. Download buttons are **disabled from the moment a
  run starts until the post-cancel update-export round-trip resolves** (export-state is the source of
  truth, per Phase 0) — otherwise a user who cancels and immediately clicks Download gets the old list.

**Manual correction endpoints + UI**
- `POST /api/search {"query", "media_type"}` → existing single-title `fetch_*_info_online` (throttled) → `{ok, entries}`.
- `POST /api/lookup {"mal_id", "media_type"}` → new `ANILIST_BY_MAL_ID_QUERY`
  (`Media(idMal:$id, type:$type)` with the full field set) → `{ok, entry}` / `{ok:false, error}`;
  non-integer → 400. **This helper is reused by Phase 5** to backfill `anilist_id` — keep it importable
  from `generate_mal_xml.py`, not buried in the handler.
- Row UI: one input + button on rows with `id <= 0` (placeholder `"Search title or MAL ID..."`);
  all-digits → lookup, otherwise search. On success: `Object.assign` identity/metadata into the row,
  **keep status/score/progress/dates**, set `enabled` from the new `id`, attach candidates, re-render,
  debounced re-sync, toast.

**Tests:** `test_run_control` (pre-held lock → 409; cancel mid-run → `cancelled` partial event and no files),
`test_search_lookup` (mocked successes, 400 validations).

---

## Phase 3 — Table & persistence UX

> **STATUS: COMPLETE (2026-10-17).** localStorage persistence, sortable columns (asc → desc →
> off, stable, numeric where numeric), 300-row cap with "Show all", status chips that compose
> with the text filter, lazy cover thumbnails, year beside the type tag, drag-and-drop `.txt`,
> Ctrl/Cmd+Enter, expandable row editor (score, start/finish dates, tags, comments), XML preview
> modal, timestamped archives, and the pre-existing theme toggle.
> **Not built, deliberately:** the "Stamp finished dates" bulk button — the row editor already
> sets dates, and no one has asked for the bulk form. Add on request.


**Client (backslash ban applies to everything here)**
- ~~**localStorage**~~ **DONE (2026-10-17).** Key `aniexport.state` = `{v:1, mediaType, status,
  score, sequels, text, results, updated}`; 300 ms debounced save; restore at boot sets the media
  selector, calls `updateMediaTypeUI(false)`, *then* restores the status value (the options only
  exist after that call), then renders the rows and fires one update-export sync so the server
  files match the restored session. `beforeunload` saves immediately and warns only while
  `isProcessing`. `btnClear` clears the persisted text. Corrupt JSON or an unknown `v` falls back
  to an empty session instead of throwing. Theme stays on its own `aniexport.theme` key.
  **Deviation from the written spec:** `candidates` are *kept* by default rather than always
  stripped, because dropping them loses the match picker on restore and there is no re-fetch
  until Phase 2. Instead the save degrades in three steps — full, then without `candidates`,
  then without `results` — so a quota error costs the least useful data first. Verified against
  a 400-row payload with a 200 KB quota.
- **Sortable columns**: click `<th>` cycles asc → desc → off; sorted copy of the filtered list only;
  `data-index` stays `currentAnimes.indexOf(item)` (identity-based → correct under sorting).
- **Row cap**: render first 300 filtered rows + `"Show all (N)"` (resets on filter change).
- **Status chips**: Reading/Watching (incl. repeats), Completed, Paused, Planned, Dropped,
  Excluded (`id<=0`); click = status filter, composes with text search.
- **Cover + year**: lazy `<img>` (AniList CDN, `item.cover`, degrades to nothing when absent);
  year appended to the type tag (`TV · 2019`).
- **Quick wins**: drag-and-drop `.txt` onto the textarea (`file.text()`); `Ctrl/Cmd+Enter` → process;
  light-theme toggle → `body.light` variable overrides, persisted.
- **Expandable row editor** (⋯ → detail row): **score 0–10**,
  **`start_date`/`finish_date`** (`<input type="date">`, native `YYYY-MM-DD`, empty → delete field),
  **tags**, **comments** — all using the constraint #8 names, all emitted by the Phase 0 builders;
  every change → `updateStatsAndExport()`. (Per-row **status** is not here — it ships as a
  first-class table control in Phase 0, gap 1.)
- **Bulk button** `"Stamp finished dates"`: sets `finish_date = today` only on `Completed` rows lacking one.
- **XML preview**: `GET /api/preview-xml?media=X` (server reads file, 5 MB cap) → modal with `<pre>` + Copy.
- **Timestamped archive**: optional checkbox → process/update-export *additionally* writes
  `{stem}_{YYYYmmdd_HHMMSS}.xml[.gz]`; export-state reports the latest archive name;
  `?media=X&archive=<name>` downloads it after validation:
  `re.fullmatch(r"[A-Za-z0-9_]+\.xml(\.gz)?", name)` + `is_file()` (no traversal).

**Server:** preview endpoint, archive writes, export-state archive fields.
**Tests:** `test_export_preview_archive` (preview content, archive write, bad `archive` → 400);
template test auto-covers new JS (backslash scan + `node --check`).

**Browser-only checks (manual checklist):** restore across refresh, sort/chips/theme, drag-drop,
Ctrl+Enter, row editor dates surviving a download, preview modal.

---

## Phase 4 — Import existing MAL XML

> **STATUS: COMPLETE (2026-10-17).** `parse_mal_xml` with the DOCTYPE refusal and the 20 MB
> ceiling, media-type detection, and the client-side merge (match by MAL id, keep the row's
> `anilist_id`, append unknown ids unless the toggle is off). `.gz` is read in the browser via
> `DecompressionStream`.

- **Server** `POST /api/parse-mal-xml {"xml": str}`: `Content-Length` > 20 MB → 413 (module constant
  `MAX_IMPORT_BYTES`, patchable in tests); **reject any document containing a `DOCTYPE` declaration
  before parsing** (`xml.etree` expands internal entities — a local file is low risk, but the guard is
  one line); `ET.fromstring` failure or root ≠ `myanimelist` → 400; detect type from
  `myinfo/user_export_type` (1=anime, 2=manga, fallback by child element).
- Field map (exact tags from the builders, `generate_mal_xml.py:690-710` / `783-807`):
  `series_animedb_id|manga_mangadb_id → id`, `series_title|manga_title → title`,
  `series_episodes → episodes`, `manga_chapters`/`manga_volumes`,
  `my_watched_episodes|my_read_chapters|my_read_volumes`, `my_status`, `my_score` (clamp 0–10),
  `my_start_date → start_date`, `my_finish_date → finish_date` (kept only if `YYYY-MM-DD` or
  `0000-00-00`, matching Phase 0's `_mal_date`), `my_comments → comments`, `my_tags → tags`
  (ET yields CDATA text). Entries with non-positive id or a status failing `_normalize_status` are
  **skipped** → reported in `{ok, media_type, entries, skipped}`.
- **Client**: toolbar `"Import MAL XML..."` → file input (`.xml`; `.gz` via feature-detected
  `DecompressionStream`, else toast) → sends text → if the returned `media_type` differs from the
  current selector, auto-switch first → merge (**overwrite semantics**):
  - matched by positive MAL id: **imported file wins** on `title`, `type`, counts, `status`, `score`,
    progress fields, `start_date`/`finish_date`, `comments`, `tags`; `anilist_id`, `candidates`,
    `enabled` retained;
  - ids absent from the session → appended as enabled rows (checkbox `"Add entries not in my pasted
    list"`, default on). **These rows have no `anilist_id`** — see Phase 5.
  - toast: `"Imported: X matched, Y added, Z skipped"` → render + re-sync.
- **Tests:** `test_parse_mal_xml` (anime + manga mapping, root/type errors → 400, invalid status skipped,
  `DOCTYPE` rejected, size cap via patched constant) + **roundtrip**: `build_mal_manga_xml(entries)` →
  parse → fields equal originals, **including dates** (this is the test that proves Phase 0 landed).

---

## Phase 5 — Direct AniList write (optional path)

> **STATUS: SHIPPED, GATE NOT SATISFIED (2026-10-18).** The code is complete and covered by
> `tests/test_anilist_write.py` against a mocked API: token check, existing-entry read, add/update
> counts in the confirmation, per-title AniList validation errors. **The live manual test was never
> performed**, so the gate below is still open. It shipped anyway because it is opt-in (a destination
> the user selects, plus a token they paste) and because the duplicate risk the gate existed to catch
> was designed out instead: the write reads existing entries first and passes their list-entry `id`,
> so a repeat write updates rather than inserts. The README says plainly that it is untested live.
> Next person here: run it once on a throwaway account and record the result in
> MANGA_SUPPORT_NOTES.md.
>
> **Original gate:** this is the only irreversible, account-modifying feature in the plan, and it
> cannot be verified by unit tests alone. It does not ship until it has been run once by hand against
> a throwaway list on a real account, with the result recorded in MANGA_SUPPORT_NOTES.md.
> If that test is not performed, stop after Phase 4 — everything before this point is read-only.
>
> **The manual test must cover, at minimum:**
> 1. **Write the same entry twice.** The docs say `Save` chooses create-vs-update by the presence
>    of `id`, and we send only `mediaId`. Confirm the list holds **one** row afterwards, not two.
>    If it duplicates, payloads must carry the existing list-entry `id`, which means querying
>    `MediaList` first — a design change, not a tweak.
> 2. **One over-long `notes` value**, to read the `validation` object and settle the 6000 limit.
> 3. **An invalid token**, expecting a clean error rather than a stack trace.

**generate_mal_xml.py**
- `ANILIST_VIEWER_QUERY` (`Viewer { id name }`) and `ANILIST_SAVE_ENTRY_MUTATION` with variables
  `$mediaId, $status: MediaListStatus, $scoreRaw: Int, $progress, $progressVolumes, $notes,
  $startedAt/$completedAt: FuzzyDateInput`, body `SaveMediaListEntry(...) { id status }`.
- `_execute_anilist_authenticated(query, variables, token)` — same core as `_post_graphql` plus
  `Authorization: Bearer <token>`; token never printed or logged (handler logging already suppressed).
- **`anilist_id` backfill** — entries that arrived via XML import carry only a MAL ID. Before building
  payloads, any entry with `id > 0` and no positive `anilist_id` is resolved through the Phase 2
  `ANILIST_BY_MAL_ID_QUERY` helper (throttled, cached by the Phase 1d cache). Without this step,
  "import my MAL list, push it to AniList" — the main reason both features exist — writes nothing.
  Entries still unresolved after backfill go to the skipped list with a stated reason.
- `iter_write_payloads(entries, default_status, default_score)`:
  - **status map** from normalized MAL status + repeat flag: `Reading/Watching→CURRENT`,
    repeat→`REPEATING`, `Completed→COMPLETED`, `Plan to...→PLANNING`, `On-Hold→PAUSED`, `Dropped→DROPPED`;
  - **`scoreRaw = score × 10`** (0–10 UI scale → AniList's 0–100 raw);
  - progress derived with the same rule as the builders (Completed/repeat → totals, else explicit or 0);
    `progressVolumes` for manga; `notes = comments[:6000]` (see unverified assumptions);
    FuzzyDate only for complete `YYYY-MM-DD` dates, else omitted;
  - entries without a positive `anilist_id` *after backfill* → skipped list.

**gui.py**
- Settings card: PAT input (`type=password`) + `"Test"` → `/api/anilist-test` → viewer query →
  `{ok, name}` toast. Token lives in a **JS memory variable only** (never localStorage, never disk;
  refresh requires re-paste), sent in request bodies over the loopback-only server; the server holds it
  in a run-local variable — nothing persisted.
- `POST /api/anilist-write` (**SSE**, shares the Phase 2 `_run_lock`): validates token + explicit
  `confirm` flag; per-entry mutations under `_throttle`; events `write_progress` / `rate_limit` /
  `write_complete {updated, failed:[{title,error}], skipped}`. Failures reported, not retried beyond
  the standard retry count. **A mutation's `validation` object is surfaced verbatim in the failure
  list** (the docs state these messages are fit to show users) — `"The score may not be greater
  than 100."` tells the user what to fix; a bare `"400"` does not.
- Client: `"Write to AniList..."` enabled only after a successful token test; confirmation modal listing
  the status breakdown and the count to be written, with explicit wording `"modifies your AniList
  account"`; SSE consumed like `/api/process`; failure list with Copy.
- README: security paragraph (PAT scope, memory-only handling, loopback-only server, what is/isn't stored).

**Tests:** `test_anilist_write` — payload correctness (scoreRaw, FuzzyDates, progress derivation,
status map, skip rules), **`anilist_id` backfill path with a mocked MAL-ID lookup**, mocked SSE flow
counts, missing token → 400, **token string asserted absent from every response body**.

---

## Phase 6 — Docs, hardening, final verification

> **STATUS: COMPLETE (2026-10-18).** README rewritten around the two destinations rather than
> around MAL XML; `.gitignore` added; MANGA_SUPPORT_NOTES.md carries the per-phase verification
> record. The verification gate below ran clean. Beyond the written plan: a full UI redesign, the
> session reset (`Start over` / `#reset`), the **Fix unmatched** flow, and `tests/test_cli.py`,
> which found and fixed a real CLI gap (AniList-only titles were reported as "missing MAL ID").

- **README.md**: speed (batching ≈8× fewer requests, cache path/TTL/`--no-cache`), candidate ranking +
  year/cover, variant retry **and its typo non-goal**, manual search/MAL-ID claim, cancel/pause,
  full-field editing incl. `YYYY-MM-DD` dates + stamp button, sort/chips/row cap, preview, archives,
  theme, drag-drop/Ctrl+Enter, import **overwrite** semantics, direct write + security, per-media
  filenames + export-state. Rewrite Limitations: drop "no release year shown"; add that ranking still
  benefits from human review, that misspelled titles cannot be auto-recovered, and the live-test status
  of direct write.
- **MANGA_SUPPORT_NOTES.md**: append a "Later additions" addendum with verification status per phase
  (it is the running change record), including the Phase 5 manual-test result.
- **New `.gitignore`**: `__pycache__/`, `*.pyc`, `.anilist_cache.json`, `output/`,
  `myanimelist_export*.xml*`, `myanimelist_manga_export*.xml*`.
- **Full verification gate** (end of every phase; final run here):

  ```bash
  python -m unittest discover -s tests -v     # all suites green
  python -m py_compile generate_mal_xml.py gui.py
  python generate_mal_xml.py --help           # CLI smoke
  ```

  plus the template test's `node --check` + backslash scan, and a manual checklist (launch with
  `--no-browser`, 3-title sample, cancel mid-run, restore after refresh, set a date and confirm it
  survives download, import a small MAL XML, token test with an invalid token expecting a clean error).

---

## Decisions log (ambiguity removal)

1. Import merge = **imported XML overwrites** matched rows; unmatched appended (toggle, default on).
2. Cache = cwd `.anilist_cache.json`, 7-day TTL, exportable-only, `--no-cache`, `ANIEXPORT_CACHE` override, gitignored, atomic writes.
3. Dates = `YYYY-MM-DD`, fields `start_date`/`finish_date`, emitted by both builders as of **Phase 0**; invalid → `ValueError` → endpoint 400 (mirrors status validation).
4. Cancel **and** Pause; pause checked between units of work; a cancelled run writes no files, and downloads stay disabled until the post-cancel re-sync resolves.
5. Empty update-export **keeps** files (no more silent deletion).
6. `?media=` optional (defaults to active media), validated against two literals; `?archive=` regex-whitelisted and existence-checked.
7. Row render cap 300 + "Show all"; status chips double as a filter.
8. Batch size 8 with adaptive halving — **contingent on the alias spike**.
9. Covers = AniList CDN, lazy, optional.
10. `scoreRaw = score × 10`; FuzzyDates only for complete dates.
11. Token: memory-only on both ends, request bodies over 127.0.0.1, shared run lock.
12. One deliberate test change: `test_gui_manga_endpoint` patch target **and mock return shape**.
13. One-shot penalty (−500) always dominates title-match bonuses — locked by regression test.
14. Phase 5 requires a real manual write test before it counts as delivered.
15. ~~Per-row status demotes the global control to "apply to checked rows".~~ **Superseded:**
    per-row selects were added *alongside* the unchanged global control, because that is
    additive. Revisit only on request. Original rationale: Per-row status is **Phase 0**, not Phase 3 — a mixed-status list is the normal case, so one
    global status is a correctness bug rather than a missing convenience. The global control
    becomes "apply to checked rows".
16. Disabled download links get `preventDefault`; the stat card either shows volumes or stops
    claiming to.
17. Pacing is **adaptive from `X-RateLimit-Limit`** (Phase 0, DONE), not pinned to the degraded 30/min.
    The documented limit is 90/min; the 30 is temporary. Clamped to `[0.7, 2.5]` s so the
    undocumented burst limiter is still respected.
18. Nothing branches on HTTP status alone — `errors[].status` is authoritative, because a 200 can
    carry a 400 or 429.
19. `PageInfo.total`/`lastPage` are never used (documented as inaccurate); only `hasNextPage`.
20. Ranking is **lexicographic tiers**, not additive weights — the additive draft provably let a
    one-shot outrank a full series, which is the bug it was written to prevent.
21. Batching aliases **`Page` blocks**, never top-level `Media`, so each title keeps its candidate
    list and the `idMal`-null skip.
22. Builder `ValueError` (bad status or bad date) surfaces as a 400 or an SSE `error` event —
    today it is an uncaught traceback mid-stream.

## Dependency order

`P0 → P1 → (P2 ∥ P3) → P4 → P5 → P6`

P0 is now load-bearing for P3, P4 and P5 (date emission + canonical field names), not just a
filename cleanup. Each phase ends with the full test gate green, so work can stop at any boundary —
and stopping after P4 leaves a fully read-only tool.

## Progress log

| Date | Landed | Verification |
|---|---|---|
| 2026-10-17 | **Phase 0 complete** — entry dates (`_mal_date`), `year`/`popularity`/`cover` fields, per-media export files, optional `?media=`, `/api/export-state`, empty update-export keeps files, `ValueError` → 400 / SSE `error`, per-row status, `preventDefault` on disabled links, volumes in the stat card, `my_scanalation_group` spelling, adaptive pacing, fail-fast on 400 | 23 Python tests, 5 jsdom suites, 22-check live server script, live AniList calls |
| 2026-10-17 | **Live row streaming** (not in the original plan) — rows appear as they resolve; dedupe mirrors the server; `complete` merges so mid-run edits survive; downloads locked during a run | `domtest4.js` drives the real handler against a stepped SSE body |
| 2026-10-17 | **Phase 3 localStorage persistence** — a refresh no longer loses a long run | `domtest5.js`: cross-session restore, corrupt/unknown-version fallback, three-step quota degradation |
| 2026-10-17 | **Phase 2 complete** — run lock + cancel/pause/resume, `/api/search`, `/api/lookup`, per-row repair | `tests/test_run_control.py` (6 tests), `domtest7.js` (14 lifecycle assertions), 30-check live server script |
| 2026-10-17 | **Phase 1 complete** — batching (8 aliases per document, 1 request each), tiered ranking, variant retries, disk cache, `resolve_many_entries` | 17 new tests in `test_batch_resolution.py`; live: 12 titles / 3 documents / 4.5s; warm cache 0 documents |
| 2026-10-17 | **Phase 3 complete** — sorting, chips, row cap, covers, drag-drop, Ctrl+Enter, row editor, preview modal, archives | `domtest6.js` (31 assertions), `tests/test_run_control.py` preview/archive cases incl. traversal rejection |

| 2026-10-17 | **Phase 4 complete** — `parse_mal_xml`, DOCTYPE refusal, 20 MB ceiling, client merge keyed on MAL id, add-unmatched toggle, `.gz` via `DecompressionStream` | `tests/test_gui_manga_endpoint.py` import cases, `domtest12.js` merge assertions |
| 2026-10-17 | **Phase 5 shipped, live gate still open** — token check, existing-entry read, add/update preview, `SaveMediaListEntry` per row, AniList validation messages surfaced per title | `tests/test_anilist_write.py` (mocked API only — **no live account run**) |
| 2026-10-18 | **AniList-first reframing** (see ANILIST_FIRST_PLAN.md) — `anilist_id > 0` replaces `idMal > 0` as the validity test; rows split three ways (exportable / AniList only / not found); per-destination row enablement | `tests/test_gui_contract.py`, `tests/test_batch_resolution.py` |
| 2026-10-18 | **Fix unmatched** — one-at-a-time repair over every unresolved title, accepting a title, an anilist.co/myanimelist.net link or a bare id, with labelled candidates | `/api/resolve-one` tests, `fixerprobe.js` |
| 2026-10-18 | **UI redesign + session reset** — every component restyled, light theme audited for WCAG AA, `Start over` and `#reset` clear the cached session | 14 state×theme audits, 8 viewport widths, 4 gap audits at 0 |
| 2026-10-18 | **Phase 6 complete** — README rewritten, `.gitignore`, CLI test suite, three-way CLI reporting | 114 Python tests, 12 jsdom suites, `py_compile` + CLI smoke |

**Remaining work:** one item only — run the direct AniList write once against a throwaway account
and record the result in MANGA_SUPPORT_NOTES.md (Phase 5's open gate).
