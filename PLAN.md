# AniExport — Advanced Features & UX Overhaul Plan

Grounded, gap-free implementation plan for the roadmap items approved on 2026-10-16.
Scope confirmed by user: **all 6 phases including direct AniList write**; import merge =
**imported XML overwrites**; disk cache = **cwd, 7-day TTL**.

---

## Verified external facts (no guessing)

| Fact | Source |
|---|---|
| MAL XML dates use `YYYY-MM-DD` (matches existing `0000-00-00` placeholders in both builders) | MAL XML exports parse as `"start_date": "2020-07-01"` (malexport); consistent with repo placeholders |
| AniList `SaveMediaListEntry` accepts `mediaId`, `status` (MediaListStatus), `score` (user's scale), `scoreRaw` (0–100), `progress`, `progressVolumes`, `notes` (≤6000), `startedAt`/`completedAt` (`FuzzyDateInput {year, month, day}`), `customLists`, `advancedScores`, `priority` | AniList official mutation reference (AniList/docs + docs.anilist.co) |
| AniList `Media(idMal: $id, type: $type)` query exists (used by direct MAL-ID claim) | AniList Media query reference — endpoint still degrades gracefully to `ok:false` if rejected |

## Non-negotiable constraints (apply to every phase)

1. **Zero external Python dependencies** — stdlib only (`difflib`, `unicodedata`, `json`, `re`, `xml.etree` are all allowed).
2. **`HTML_TEMPLATE` must contain no backslash characters anywhere** — `tests/test_gui_template.py:26-36` fails on any line containing one. Newlines in JS: `String.fromCharCode(10)` or template literals; **no regex escapes** in new JS (use backslash-free character classes).
3. **Public API compatibility** — `resolve_anime_entries`, `resolve_manga_entries`, `build_mal_xml`, `build_mal_manga_xml`, `sanitize_title_line`, `deduplicate_entries`, `set_rate_limit_notifier`, `MIN_REQUEST_INTERVAL` keep their signatures (gui.py imports them).
4. **`tests/test_manga_support.py` patches `_execute_anilist_request` with the query as first positional arg** → single-title fetch functions (`fetch_anime_info_online`, `fetch_manga_info_online`) keep calling it exactly as today.
5. **All existing tests green at the end of every phase** — with one deliberate, documented change: `tests/test_gui_manga_endpoint.py` patch target moves from `gui.resolve_manga_entries` to `gui.resolve_many_entries` (because `/api/process` becomes batched); its assertions stay identical.
6. **Rate-limit policy unchanged** — `MIN_REQUEST_INTERVAL = 2.1`, existing 429/`Retry-After`/transient-in-200-body logic reused.

---

## Phase 0 — Foundation: per-media export files + `/api/export-state`

**Problem fixed:** one shared `EXPORT_XML_PATH`/`EXPORT_MEDIA_TYPE` pair (gui.py:32-40, 1841, 1854) means manga runs overwrite `myanimelist_export.xml`, and a selector flip can offer wrong filenames or 404s.

**generate_mal_xml.py**
- Add `startDate { year }`, `popularity`, `coverImage { small }` to `ANILIST_SEARCH_PAGE_QUERY` and `ANILIST_MANGA_SEARCH_PAGE_QUERY`.
- `_parse_media_node` (line 275) / `_parse_manga_node` (line 324): add keys `year` (`int|None`), `popularity` (`int`), `cover` (`str|None`).

**gui.py**
- Replace the single path pair with `EXPORT_FILES = {"anime": {"xml":..., "gz":...}, "manga": {"xml":..., "gz":...}}` (under `output/` when it exists; manga names `myanimelist_manga_export.xml[.gz]`).
- `GET /api/download/xml?media=anime|manga` (same for `gz`): param validated against the two literals → 400 otherwise; `Content-Disposition` filename derived from the param; 404 if that media's file is missing. Explicit param = filename always matches served bytes.
- **New** `GET /api/export-state` → `{"anime": {"xml": bool, "gz": bool, "xml_name": ..., "gz_name": ..., "count": int|null, "updated_at": ...}, "manga": {...}, "active_media": ...}` (count parsed from file on demand, wrapped in try/except).
- `POST /api/update-export` with **empty entries no longer deletes files** (currently `unlink` at gui.py:1978-1982) — returns `{ok, count: 0}`; stale-but-valid files remain downloadable.
- Client: after load, process, update, or media-type flip → fetch `/api/export-state` and drive `href`, `download` attribute, and `disabled` from server truth instead of client assumptions.

**Tests (new):** `tests/test_export_state.py` — flip scenario regression (process manga → `download?media=manga` serves manga file/name while `media=anime` is 404); empty update-export keeps files.

**Acceptance:** existing 15 + new tests pass; no filename/content mismatch possible for any selector sequence.

---

## Phase 1 — Resolution engine: batching, ranking, variant retries, disk cache

### 1a. Batched GraphQL
- `_search_batch_query(count, media_type)` → builds aliased document `t0: Media(search: $q0, type: MANGA) {...} t1: ...` with the full field set (incl. Phase 0 fields). `BATCH_SIZE = 8`.
- Extract shared HTTP core from `_execute_anilist_request` (generate_mal_xml.py:340-393): new `_post_graphql(query, variables, max_retries)` returns the full `data` dict (throttle, 429 waits, transient-error detection all shared). `_execute_anilist_request` becomes a thin wrapper preserving exact signature/semantics (`result_key` extraction) → test contract #4 holds.
- New `_execute_anilist_batch(query, variables, max_retries=3)` → `data` or `None`. Retries whole payload on HTTP-level failure; returns partial data when individual aliases are `null` (those titles are unresolved).
- **Adaptive fallback:** if AniList rejects the document with a validation error (HTTP 400 with GraphQL errors), halve batch size down to 1 and retry — protects against unknown alias-count limits.

### 1b. Candidate ranking + year
- `_pick_best_media(nodes, search_title="")` (generate_mal_xml.py:309) — deterministic score, stable sort `(-score, original_index)`:
  - `idMal` present **+1000**; normalized exact English title **+300**, exact romaji **+260**; `difflib.SequenceMatcher` ratio on normalized strings **×200**; format ≠ `ONE_SHOT` **+150**; status ≠ `NOT_YET_RELEASED` **+100** else **−300**; popularity `min(50, round(log10(pop+1) * 10))`.
  - Normalization: `casefold` → NFKD → strip combining marks → non-alnum → single spaces.
  - Preserve fallback: if *no* node has `idMal`, return `nodes[0]` (existing contract, covered by tests).
  - Callers pass the search title.
- `_candidates` (generate_mal_xml.py:292): same score ordering, still MAL-ID-only and deduped by `idMal`.

### 1c. Smarter retry for unresolved
- `_search_variants(title)` → ≤3 deterministic stages; only stages that differ from the previous are queued:
  1. NFKC + curly→straight quotes + whitespace collapse;
  2. strip one pass of `(…)`/`[…]` groups (e.g. `(YingXiong Zai Lin)`);
  3. (manga only) strip trailing `manga|manhua|manhwa` token if remainder non-empty.
- Variants queued in `BATCH_SIZE` chunks; first variant with a root media wins; still-unresolved → unchanged `id:0, type:"Unresolved"` row.

### 1d. Disk cache
- Path: `Path.cwd()/.anilist_cache.json` (env override `ANIEXPORT_CACHE`); CLI `--no-cache` flag.
- Format: `{"version": 1, "entries": {key: {"ts": epoch, "data": [...]}}}`; key = `f"{media_type}:{title.strip().casefold()}:{'expand'|'single'}"` (shared `_cache_key` helper — single- and multi-title paths use the same helper so they interoperate).
- TTL 7 days (604800 s); prune expired on load; cap 10,000 entries (oldest dropped); `threading.Lock` + atomic write (temp file + `os.replace`); corrupt/missing file → empty dict.
- **Only exportable results cached** (root `id > 0`); unresolved never cached (retried next run).

### 1e. Orchestration
- New `resolve_many_entries(titles, media_type, expand_sequels=False, session_cache=None, use_cache=True, on_progress=None) -> Dict[str, List[dict]]`:
  1. unique titles → disk/session cache lookup;
  2. uncached → chunk by 8 → batch query → `_pick_best_media` + `_candidates` per title → `on_progress(stage="resolve", done_count, total)` after **each title** in the returned batch (smooth bar);
  3. variant retry pass (`stage="retry"`), chunked;
  4. `expand_sequels` → **refactor** the duplicated SEQUEL-chain loops (generate_mal_xml.py:429-455 and 530-553) into one shared `_expand_sequels_chain(root_media, media_type)` used by both single and batch paths (`stage="expand"`);
  5. unresolved rows shaped exactly as today; cache-set exportable results; one atomic cache save per run.
- Single `resolve_anime_entries`/`resolve_manga_entries` keep working (delegating cache get/set to the shared helper); `fetch_*_info_online` untouched (still used by tests and the Phase 2 search endpoint).
- **ETA:** progress events gain `eta_seconds = remaining_batches × 2.1 + remaining_titles × 0.05`; client prefers it, falls back to the existing `interval` formula.
- GUI `/api/process`: call `resolve_many_entries` once with an `on_progress` that emits SSE progress (including retry-stage subtext), then iterate titles to attach `original`/`status`/`score` → dedupe → build XML → write to **per-media Phase 0 paths** → `complete` event. Rate-limit notifier wiring unchanged.
- CLI `main()` and `process_anime_list` switch to `resolve_many_entries` (both media branches); add `--no-cache`.

**Tests (new):** `test_batch_resolution` (mock `_execute_anilist_batch` → call count = `ceil(N/8)`, order preserved, unresolved shape), `test_candidate_ranking` (order independence, one-shot demotion, fallbacks, `year` parsed), `test_search_variants` (determinism/distinctness/manga-suffix rule), `test_cache` (TTL, corrupt recovery, no-cache flag, unresolved not cached, valid JSON after save). Update `test_gui_manga_endpoint` patch target (per constraint #5).

**Acceptance:** 15 pre-existing tests green (one retargeted); simulated 100-title run → 13 batch calls; second run with warm cache → 0 network calls.

---

## Phase 2 — Run control + manual correction

**Run control (gui.py)**
- `_run_lock` (non-blocking acquire → **409** `"A run is already in progress"`) + `_active_run = {run_id: {"cancel": Event, "pause": Event}}`.
- Checkpoint between units of work in `/api/process`: **cancel** → emit `{"type":"cancelled","entries":[partial...]}`, write no files, release lock; **pause** → emit `{"type":"paused"}`, wait loop that still honors cancel, resume → `{"type":"resumed"}`.
- Client: Cancel + Pause/Resume buttons visible while running; spinner freezes while paused; on `cancelled` → populate table with partial entries + normal `updateStatsAndExport()` (the debounced update-export then builds files from exactly what the user sees); toast wording `"Run cancelled — N titles resolved"`.

**Manual correction endpoints + UI**
- `POST /api/search {"query", "media_type"}` → existing single-title `fetch_*_info_online` (throttled) → `{ok, entries}`.
- `POST /api/lookup {"mal_id", "media_type"}` → new `ANILIST_BY_MAL_ID_QUERY` (`Media(idMal:$id, type:$type)` with the full field set) → `{ok, entry}` / `{ok:false, error}`; non-integer → 400.
- Row UI: one input + button on rows with `id <= 0` (placeholder `"Search title or MAL ID..."`); all-digits → lookup, otherwise search. On success: `Object.assign` identity/metadata into the row, **keep status/score/progress**, set `enabled` from new `id`, attach candidates, re-render, debounced re-sync, toast.

**Tests:** `test_run_control` (pre-held lock → 409; cancel mid-run → `cancelled` partial event and no files), `test_search_lookup` (mocked successes, 400 validations).

---

## Phase 3 — Table & persistence UX

**Client (backslash ban applies to everything here)**
- **localStorage**: key `aniexport.state` = `{v:1, mediaType, status, score, sequels, text, results, updated}`; debounced save (300 ms) on every relevant change; quota failure → retry without `results`; restore at boot **before** wiring (set selector value, then `updateMediaTypeUI(false)`, then render restored results and fire one update-export sync so server files match the restored session). Separate key `aniexport.theme`. `beforeunload` prompt while `isProcessing`. `btnClear` also clears the persisted text.
- **Sortable columns**: click `<th>` cycles asc → desc → off; sorted copy of the filtered list only; `data-index` stays `currentAnimes.indexOf(item)` (identity-based → correct under sorting).
- **Row cap**: render first 300 filtered rows + `"Show all (N)"` button (resets on filter change) — no true virtualization needed at these sizes.
- **Status chips** above table: Reading/Watching (incl. repeats), Completed, Paused, Planned, Dropped, Excluded (`id<=0`); click = status filter (works alongside text search).
- **Cover + year**: lazy `<img>` (AniList CDN, `item.cover`, degrades to nothing when absent); year appended to the type tag (`TV · 2019`).
- **Quick wins**: drag-and-drop `.txt` onto textarea (`file.text()`); `Ctrl/Cmd+Enter` → process; light-theme toggle button → `body.light` CSS variable overrides, persisted.
- **Expandable row editor** (⋯ button → detail row): per-row **status select**, **score 0–10**, **start/finish dates** (`<input type="date">`, native `YYYY-MM-DD`, empty → delete field), **tags**, **comments**; every change → `updateStatsAndExport()`.
- **Bulk button** `"Stamp finished dates"`: sets `finish_date = today (YYYY-MM-DD)` only on `Completed` rows lacking one; toast with count.
- **XML preview**: button → `GET /api/preview-xml?media=X` (server reads file, 5 MB cap) → modal with `<pre>` + Copy + Close.
- **Timestamped archive**: optional checkbox → process/update-export *additionally* writes `{stem}_{YYYYmmdd_HHMMSS}.xml[.gz]`; export-state reports the latest archive name; `?media=X&archive=<name>` downloads it after validation: `re.fullmatch(r"[A-Za-z0-9_]+\.xml(\.gz)?", name)` + `is_file()` (no traversal).

**Server:** preview endpoint, archive writes, export-state archive fields.
**Tests:** `test_export_preview_archive` (preview content, archive write, bad `archive` param → 400); template test auto-covers new JS (backslash scan + `node --check`).

**Browser-only checks (manual checklist, documented):** restore across refresh, sort/chips/theme, drag-drop, Ctrl+Enter, row editor, preview modal.

---

## Phase 4 — Import existing MAL XML

- **Server** `POST /api/parse-mal-xml {"xml": str}`: `Content-Length` > 20 MB → 413 (module constant `MAX_IMPORT_BYTES`, patchable in tests); `ET.fromstring` failure or root ≠ `myanimelist` → 400; detect type from `myinfo/user_export_type` (1=anime, 2=manga, fallback by child element).
- Field map (exact tags from the builders, generate_mal_xml.py:689-711 / 782-808): `series_animedb_id|manga_mangadb_id → id`, `series_title|manga_title → title`, `series_episodes → episodes`, `manga_chapters`/`manga_volumes`, `my_watched_episodes|my_read_chapters|my_read_volumes`, `my_status`, `my_score` (clamp 0–10), `my_start_date|my_finish_date` (kept only if `YYYY-MM-DD` or `0000-00-00`), `my_comments|my_tags` (ET yields CDATA text). Entries with non-positive id or status failing `_normalize_status` are **skipped** → reported in `{ok, media_type, entries, skipped}`.
- **Client**: toolbar `"Import MAL XML..."` → file input (`.xml`; `.gz` via feature-detected `DecompressionStream`, else toast) → sends text → if returned `media_type` differs from current selector, auto-switch first → merge (**overwrite semantics**, per user decision):
  - matched by positive MAL id: **imported file wins** on `title`, `type`, counts, `status`, `score`, progress fields, `start_date`/`finish_date`, `comments`, `tags`; `anilist_id`, `candidates`, `enabled` retained;
  - ids not present in the session → appended as enabled rows (checkbox `"Add entries not in my pasted list"`, default on);
  - toast: `"Imported: X matched, Y added, Z skipped"` → render + re-sync.
- **Tests:** `test_parse_mal_xml` (valid anime + manga mapping, root/type errors → 400, invalid status skipped, size cap via patched constant) + **roundtrip test**: `build_mal_manga_xml(entries)` → parse → fields equal originals.

---

## Phase 5 — Direct AniList write (optional path)

**generate_mal_xml.py**
- `ANILIST_VIEWER_QUERY` (`Viewer { id name }`) and `ANILIST_SAVE_ENTRY_MUTATION` with variables `$mediaId, $status: MediaListStatus, $scoreRaw: Int, $progress, $progressVolumes, $notes, $startedAt/$completedAt: FuzzyDateInput`, body `SaveMediaListEntry(mediaId:..., scoreRaw:..., ...) { id status }`.
- `_execute_anilist_authenticated(query, variables, token)` — same core as `_post_graphql` plus `Authorization: Bearer <token>`; token never printed/logged (handler logging already suppressed).
- `iter_write_payloads(entries, default_status, default_score)`:
  - **status map** from normalized MAL status + repeat flag: `Reading/Watching→CURRENT`, repeat→`REPEATING`, `Completed→COMPLETED`, `Plan to...→PLANNING`, `On-Hold→PAUSED`, `Dropped→DROPPED`;
  - **`scoreRaw = score × 10`** (avoids the user's score-format ambiguity);
  - progress derived with the same rule as the builders (Completed/repeat → totals, else explicit or 0); `progressVolumes` for manga; `notes = comments[:6000]`; FuzzyDate only for complete `YYYY-MM-DD` dates, else omitted;
  - entries without positive `anilist_id` → skipped list.

**gui.py**
- Settings card: PAT input (`type=password`) + `"Test"` → `/api/anilist-test` → viewer query → `{ok, name}` toast. Token lives in a **JS memory variable only** (never localStorage, never disk; refresh requires re-paste) and is sent in request bodies over the loopback-only server; server holds it in a run-local variable, garbage-collected after — nothing persisted.
- `POST /api/anilist-write` (**SSE**, shares the Phase 2 `_run_lock` — never two concurrent network runs): validates token + explicit `confirm` flag; per-entry mutations under `_throttle`; events `write_progress` / `rate_limit` / `write_complete {updated, failed:[{title,error}], skipped}`. Failed entries reported, not retried beyond the standard retry count.
- Client: `"Write to AniList..."` button only enabled after a successful token test; confirmation modal with status breakdown and explicit wording `"modifies your AniList account"`; SSE consumed like `/api/process`; failure list with Copy button.
- README: security paragraph (PAT scope from AniList Settings → Developer, memory-only handling, loopback-only server, what is/isn't stored).

**Tests:** `test_anilist_write` — payload correctness (scoreRaw, FuzzyDates, progress derivation, skip rules, status map), mocked SSE flow counts, missing token → 400, **token string asserted absent from response bodies**.

---

## Phase 6 — Docs, hardening, final verification

- **README.md**: new/updated sections — speed (batching ≈8× fewer waits, cache path/TTL/`--no-cache`), candidate ranking + year/cover, variant retry, manual search/MAL-ID claim, cancel/pause, full-field editing + date format `YYYY-MM-DD` + stamp button, sort/chips/row cap, preview, timestamped archives, theme, drag-drop/Ctrl+Enter, import-XML **overwrite** semantics, direct AniList write + security, per-media filenames + export-state. Rewrite Limitations (remove "no release year shown"; add: batch ranking still benefits from human review; direct write is untested live; franchise/sequel note stays). Update Tests section coverage list.
- **MANGA_SUPPORT_NOTES.md**: append a short "Later additions" addendum with verification status (keeps it the running change record).
- **New `.gitignore`**: `__pycache__/`, `*.pyc`, `.anilist_cache.json`, `output/`, `myanimelist_export*.xml*`, `myanimelist_manga_export*.xml*` (already-committed `.pyc` files left untouched unless asked).
- **Full verification gate** (run at end of every phase, final run in Phase 6):

  ```bash
  python -m unittest discover -s tests -v     # all suites green
  python -m py_compile generate_mal_xml.py gui.py
  python generate_mal_xml.py --help           # CLI smoke
  ```

  plus the template test's built-in `node --check` and backslash scan, and a manual smoke checklist (launch GUI with `--no-browser`, run a 3-title sample, cancel mid-run, restore after refresh, import a small MAL XML, token test with an invalid token expecting a clean error).

---

## Decisions log (ambiguity removal)

1. Import merge = **imported XML overwrites** matched rows; unmatched appended (toggle, default on).
2. Cache = cwd `.anilist_cache.json`, 7-day TTL, exportable-only, `--no-cache`, `ANIEXPORT_CACHE` env override, gitignored, atomic writes.
3. Dates = `YYYY-MM-DD` (native date inputs); invalid → `ValueError` in builders → endpoint 400 (mirrors existing status validation).
4. Cancel **and** Pause; pause checked between units of work; cancelled run writes no files (client re-syncs what's shown via update-export).
5. Empty update-export **keeps** files (no more silent deletion).
6. Downloads require explicit `?media=`; `?archive=` regex-whitelisted, existence-checked.
7. Row render cap 300 + "Show all"; status chips double as a filter.
8. Batch size 8 with adaptive halving on GraphQL validation errors.
9. Covers = AniList CDN, lazy, optional (`cover` field may be absent).
10. `scoreRaw = score × 10`; FuzzyDates only for complete dates.
11. Token: memory-only on both ends, request bodies over 127.0.0.1, shared run lock.
12. One deliberate test change: `test_gui_manga_endpoint` patch target → `gui.resolve_many_entries`.

## Dependency order

`P0 → P1 → (P2 ∥ P3) → P4 → P5 → P6` — each phase ends with the full test gate green, so work can stop at any boundary.
