# AniList-first export — logic and plan

**Problem statement (user, 2026-10-17):** the target is **AniList**, not MyAnimeList. MAL XML is
only a transport. Today every title without a MAL ID is dropped, even when AniList itself knows
the title. Those titles must become exportable.

> **STATUS: IMPLEMENTED (2026-10-18).** This document is kept as the reasoning record. Phase A
> shipped as written; the GUI-side repair flow the user asked for next (**Fix unmatched**) shipped
> on top of it. The one open item is unrelated to this plan: the direct write has not been run
> against a live account (see PLAN.md, Phase 5).

---

## 1. The reframing, stated precisely

The tool currently has **one** export artifact and **one** validity test:

```
valid(entry)  ==  entry.idMal > 0
```

That single test gates: ranking (`_media_rank`, generate_mal_xml.py:387), candidate lists
(`_candidates`, :362-364), row checkboxes and badges (gui.py:2866, 2871, 2891), the
"Included in Export" count (gui.py:2374, 2442), and both XML builders (which skip `id <= 0`).

For an AniList-targeted tool that test is **wrong**, because there are two destinations with
different identity requirements:

| Destination | Identity required | Auth | Loses anything? |
|---|---|---|---|
| **MAL-compatible XML file** | `idMal` | none | yes — every AniList-only title |
| **Direct AniList write** (`SaveMediaListEntry`) | `anilist_id` | user token | no |

Every title we resolve already has an `anilist_id` — that is how we found it. So the set of
titles exportable by direct write is **strictly larger** than the set exportable by XML.

A row with `anilist_id > 0, idMal = 0` is therefore **not "unresolved"**. It is **AniList-only**.
Calling it "No MAL ID" and disabling its checkbox is the mislabel at the centre of this problem.

---

## 2. Evidence (measured this session, not assumed)

| Fact | How it was established |
|---|---|
| `MediaListStatus` enum is exactly `CURRENT, PLANNING, COMPLETED, DROPPED, PAUSED, REPEATING` | GraphQL introspection against graphql.anilist.co |
| Several of the user's "unresolvable" titles **do** exist on AniList with `idMal: null` — `Gotoubun No Hanayome` (AL 148167), `My Wife Is From a Thousand Years Ago` (AL 146267), `I Am Being Chased To Fall In Love Everyday` (AL 192823), `Aitsura, Zettai Yatta Desho` | live `Page { media(search:) }` queries returning hits whose `idMal` is null |
| Some titles do **not** exist on AniList under the pasted string at all — `Wisemans Grandchild`, `Death March To The Parallel World Raphsody` | same queries returning zero hits |
| One document with 8 aliased `Page` blocks costs **1 request**, not 8 | burst spike: `X-RateLimit-Remaining` 29→28→27→26 |
| Rate limit: 90/min nominal, **30/min while degraded**; separate undocumented burst limiter | docs.anilist.co/guide/rate-limiting + observed headers |
| A 200 response may still carry `errors[].status` | docs.anilist.co/guide/graphql/errors |
| Mutations require an access token; `Save*` chooses create vs update by whether `id` is supplied | docs.anilist.co/guide/graphql/mutations |

### Measured split of the user's 8 unmatched titles

All eight were re-queried without requiring a MAL ID. The split is exactly even:

| Recoverable by direct AniList write (4) | AniList id | Not on AniList under that string (4) |
|---|---|---|
| Gotoubun No Hanayome | 148167 | Wisemans Grandchild |
| My Wife Is From a Thousand Years Ago | 146267 | Death March To The Parallel World Raphsody |
| I Am Being Chased To Fall In Love Everyday | 192823 | Reincarnated As An Aristrocat With Appraisal Skill |
| Aitsura, Zettai Yatta Desho | (hit, idMal null) | Tensei Kenja No Isekai Raifu |

So this plan recovers **half** of the current misses automatically. The other half are
misspellings or English fan-titles and need a human to supply the right string or ID — no
destination change rescues them. One caveat on the recoverable column: `I Am Being Chased To Fall
In Love Everyday` matched `Forced to Date with BOSS Everyday`, which may be wrong. Recovering a
title and recovering the *correct* title are different problems; see section 6.

### What this means in practice for the user's 185-title list

Three distinct populations, which the UI currently merges into one red bucket:

1. **MAL-linked** — exports today by either route.
2. **AniList-only** (`anilist_id > 0, idMal = 0`) — *currently discarded*, would export by direct
   write. This is the population this plan recovers.
3. **Not found on AniList** — exports by neither route. No amount of engineering fixes these;
   they need a human to supply the right title or ID. The plan must not pretend otherwise.

---

## 3. Unverified assumptions — spikes required before the code they gate

| # | Assumption | Risk if wrong | Spike |
|---|---|---|---|
| S1 | `SaveMediaListEntry(mediaId:)` **without** `id` updates an existing list entry rather than creating a duplicate | **Can corrupt a real account**: a re-run could double every row | Write one entry twice on a throwaway account; confirm the list holds one row. If it duplicates, every payload must first read the existing entry id via `MediaList(userId:, mediaId:)` — a design change, not a tweak |
| S2 | Mutations can be aliased in one document like queries | Only affects speed: 339 writes at 2.1s ≈ 12 min vs ≈ 1.5 min | Send 2 aliased `SaveMediaListEntry` in one document against a throwaway account |
| S3 | Full argument list of `SaveMediaListEntry` (`scoreRaw`, `progressVolumes`, `startedAt`, `notes`) | Payload rejected at write time | Introspect `__type(name: "Mutation")` for that single field (the bulk mutation introspection returned HTTP 500; query the field alone) |
| S4 | How a user obtains a token, and what it is called in AniList's UI | Dead end in the setup instructions | Confirm on anilist.co/settings/developer. The known mechanism is creating a client and using the implicit grant (`response_type=token`); **do not write setup copy until this is confirmed by looking at the page** |
| S5 | Whether AniList's web importer accepts a MAL **XML file** at all (versus importing by MAL username) | If it does not, the XML route is useless for AniList and only serves MAL itself | User can confirm on anilist.co/settings/import in seconds |

S1 is the only one that can damage data. Nothing writes to a real account until S1 passes.

---

## 4. The model to implement

### 4.1 Per-entry capability flags

Replace the single `id > 0` test with two explicit, computed flags carried on every entry:

```
can_export_xml    = int(entry["id"]) > 0            # has a MAL ID
can_write_anilist = int(entry["anilist_id"]) > 0    # has an AniList ID
```

An entry is **actionable** when `can_export_xml or can_write_anilist`. "Unresolved" shrinks to its
true meaning: **neither** flag set.

### 4.2 Destination as explicit state

A single selector, default **AniList (direct)**, because that is the stated target:

- `anilist` — direct write. Row is enabled when `can_write_anilist`.
- `xml` — MAL-compatible file. Row is enabled when `can_export_xml`.

The destination drives, in one place rather than scattered:

| Driven thing | `anilist` | `xml` |
|---|---|---|
| row enabled / checkbox | `can_write_anilist` | `can_export_xml` |
| row badge | `AniList-only` (neutral) when no MAL ID | `No MAL ID` (warning) |
| "Included in Export" stat | count of writable rows | count of XML-able rows |
| `_pick_best_media` ranking | MAL ID is **not** a tier | MAL ID stays the top tier |
| `_candidates` | include `idMal: null` entries | MAL-ID-only (today's behaviour) |
| primary action | "Write to AniList…" | "Download XML / .gz" |

Both artifacts stay available regardless; only the emphasis and the counts change.

### 4.3 Ranking must follow the destination

`_media_rank` currently puts `has_mal_id` in the top tier. For a direct write that is actively
wrong: it can pick a MAL-linked duplicate over the canonical AniList entry. Signature becomes
`_pick_best_media(nodes, search_title, prefer_mal_id: bool)`:

- `prefer_mal_id=True` (xml): tiers `(has_mal_id, not_one_shot, is_released, score)` — unchanged.
- `prefer_mal_id=False` (anilist): tiers `(not_one_shot, is_released, score)`; `has_mal_id`
  drops out entirely so the best *AniList* match wins.

The Boruto/Gotoubun regression test must pass under **both** modes, with the expected pick stated
separately for each.

### 4.4 Manual correction must accept AniList identity

`/api/lookup` accepts a MAL ID only (gui.py:3614). For a title with no MAL ID that is a dead end —
the user can see the right entry on AniList and still cannot enter it. The input must accept:

- a MAL ID (digits, current behaviour)
- an **AniList ID**
- a pasted **AniList URL** (`anilist.co/manga/30002/...`, `/anime/`)
- free text (search, current behaviour)

Disambiguation rule, stated so it is not guesswork: a pasted URL is parsed for its kind and id; a
bare number is ambiguous, so the row's input gets an explicit "MAL / AniList" toggle rather than a
silent guess. New query `Media(id: $id, type: $type)` alongside the existing `Media(idMal:)`.

---

## 5. Phases

Ordered so that every phase is independently useful and nothing destructive happens early.

### Phase A — Make AniList-only titles visible and selectable (no writing, no risk)

> **STATUS: COMPLETE (2026-10-17).** Verified live against the user's own unmatched titles:
> `Gotoubun No Hanayome` (AL 148167), `My Wife Is From a Thousand Years Ago` (AL 146267) and
> `Aitsura, Zettai Yatta Desho` (AL 124629) now resolve as AniList-only rows instead of failures;
> `Wisemans Grandchild` is still correctly reported as not found.
>
> **The actual defect** was narrower than the plan assumed: `run_batch` required
> `idMal > 0` to count a title as resolved, while the single-title resolver never applied that
> rule. The two code paths disagreed, and the batch path is the one every run uses.
>
> Shipped: `can_export_xml` / `can_write_anilist` on every entry (including unresolved ones);
> `prefer_mal_id` threaded through `_media_rank` / `_rank_nodes` / `_pick_best_media` /
> `_candidates` / `resolve_many_entries`; candidates keep AniList-only entries, deduped by MAL id
> when present and AniList id otherwise; `_cacheable` keyed on the AniList id so AniList-only
> results stop being re-fetched every run; the cache key carries the destination; a first-run
> destination prompt with the selector persisted and cleared by Start over; three row badges
> (`Manga` / `AniList only` / `Not found`) replacing the single `No MAL ID`; per-destination
> counts; a third notice-bar category; and an export card that states how many of the selected
> rows can actually travel in MAL XML.
>
> 63 Python tests and 9 jsdom suites pass. Phases B, C and D remain unbuilt.


1. `_parse_media_node` / `_parse_manga_node`: add `can_export_xml`, `can_write_anilist`.
2. `_candidates` (:362): stop filtering out `idMal: null`; mark each candidate instead.
3. `_pick_best_media` / `_media_rank`: add `prefer_mal_id`, threaded from the destination.
4. `resolve_many_entries`: `_cacheable` currently requires `id > 0` — change to "cache anything
   with an AniList ID", so AniList-only results stop being re-fetched every run.
   **Cache key must gain the destination**, or a list resolved for XML would poison an AniList run.
5. GUI: destination selector; all six gates in 4.2 driven from it; badge copy changed; the notice
   bar gains a third category ("N titles are on AniList but have no MAL ID — they can only be sent
   by direct write").
6. XML builders unchanged: they keep skipping `id <= 0`, which remains correct for that artifact.

**Acceptance:** for the user's real list, the "no MAL ID" count drops to the genuinely-not-found
titles, and the AniList-only ones appear as selectable rows. Tests: ranking under both modes, the
cache key change, counts per destination.

### Phase B — Read-only AniList account integration (token, still no writes)
> **STATUS: COMPLETE (2026-10-17).** `anilist_viewer` (Viewer query) behind `/api/anilist-test`,
> `anilist_existing_entries` reading `Page { mediaList(userId:, type:, mediaId_in:) }` 50 ids at a
> time, and `/api/anilist-preview` reporting adds vs updates before anything is sent. Token is
> memory-only in the tab, never persisted, and asserted absent from every response body.


1. Token entry (memory-only, never localStorage, never disk) + `/api/anilist-test` calling
   `Viewer { id name }`.
2. Show which of the selected rows are **already on the user's list** via
   `MediaList(userId:, mediaId:)` or `Page { mediaList(...) }`, so a write shows "will add N, will
   update M" before anything happens.

This phase is what makes S1 answerable with real data instead of a guess, and it is read-only.

### Phase C — Direct write (gated on S1)
> **STATUS: BUILT, NOT YET RUN LIVE (2026-10-17).** `iter_write_payloads`, `anilist_write_entry`
> and `/api/anilist-write` (SSE, shares the run lock, cancellable).
>
> **S1 is designed out rather than gated on.** Introspection confirmed `SaveMediaListEntry`
> accepts `id`, and `Page.mediaList` accepts `mediaId_in`, so every payload for a title already
> on the list carries that entry's id. Update-vs-create is therefore explicit and a repeated
> write cannot duplicate rows. No reliance on undocumented upsert behaviour remains.
>
> Still unverified: the whole path against a real account. Unit tests mock the API.


1. `iter_write_payloads` — status map to the **verified** enum; `scoreRaw = score * 10`;
   `progress` / `progressVolumes`; `startedAt` / `completedAt` as `FuzzyDateInput` only for
   complete dates; `notes` from comments.
2. `/api/anilist-write` as SSE, sharing the existing `_run_lock`, reusing `_throttle`.
3. Confirmation step stating exact counts: "adds N, updates M, skips K" — built from Phase B data,
   not estimated.
4. Per-entry failures surfaced with AniList's own `validation` messages, which the docs say are
   fit to display.

**Does not ship** until S1 passes on a throwaway account, and the result is recorded.

### Phase D — Honesty about what is left
> **Extended 2026-10-18:** a **Fix unmatched** workflow now steps through every title that matched
> nothing. `/api/resolve-one` accepts free text, a bare id (tried as both AniList and MAL unless
> pinned), or a pasted anilist.co / myanimelist.net link, and returns ranked candidates that the
> user picks from. `search_candidates` no longer drops AniList-only hits, which previously made the
> very titles this is meant to rescue invisible to the fixer.
>
> **STATUS: COMPLETE.** The notice bar separates titles with no MAL id, titles found nowhere,
> lines merged into an existing row, and titles AniList could not be reached for. Rows that
> matched nothing carry an inline search box.


Titles in population 3 (not on AniList at all) get a dedicated list and a one-click path into the
row-level search/ID input. No automated rescue is promised, because none exists.

---

## 6. What this plan explicitly does **not** solve

- **Misspellings.** `Raphsody`, `Aristrocat`, `Wisemans Grandchild` return **zero** AniList hits.
  Neither destination helps; a human must supply the right title or ID.
- **Wrong-but-confident matches.** `I Am Being Chased To Fall In Love Everyday` resolved to
  `Forced to Date with BOSS Everyday`. Ranking cannot detect this; only the candidate picker and a
  human eye can. Phase A makes the picker show more candidates, which helps, but does not fix it.
- **Making MAL XML carry AniList-only titles.** Impossible: that file format is keyed on MAL IDs.
  The only route for those titles is the direct write.

---

## 7. Decisions needed before implementation

1. **Default destination** — AniList (direct) or MAL XML? Affects defaults, not capability.
2. **Scope now** — Phase A alone (safe, immediately recovers the AniList-only titles in the UI and
   XML-adjacent flows), or A+B, or all the way through C?
3. **S1 testing** — is there a throwaway AniList account available, or must the duplicate question
   be answered against the real one? If the real one, Phase C stays unbuilt.
4. **S5** — does anilist.co/settings/import accept a MAL XML *file*? One look settles whether the
   XML artifact matters for AniList at all.
