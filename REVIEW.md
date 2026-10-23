# AniExport — code and product review (2026-10-20)

A read of both modules end to end, the test suite, and the README, plus live probes against
`graphql.anilist.co` to check the claims rather than assert them.

Findings are labelled **R1–R28** so code comments and commits can cite them, the same way the
engineering notes cite their E/L/M/P evidence labels.

> **Status, 2026-10-22.** Every numbered finding has **shipped** with tests except **R24** and
> **R28**, both of which need a decision only you can make. `gui.py` is **1,610 lines, down from
> 6,662**; `generate_mal_xml.py` is 2,419 from 3,042; the script is ten numbered sections. The
> suite is **466 tests** in about nine seconds, from 333 in fifty-nine. The review's own headline
> recommendation was **measured and rejected**; the half of it that survived shipped as R13 — see
> [The matching question](#-the-matching-question).
>
> **The unnumbered sections are now closed too.** [UI and UX](#-ui-and-ux) and
> [Positioning](#-positioning) are done; [Housekeeping](#-housekeeping) is done except
> `pyproject.toml`, where the item as written turned out to be **wrong** and the measurement is
> recorded in place. **R15** is also settled, against the live API, and the documentation it
> contradicted has been corrected. What is left is listed under [Still to do](#-still-to-do) —
> three items, each blocked on a decision rather than on work.

Every claim below is marked:

| Mark | Meaning |
|---|---|
| **measured** | a command was run and its output is quoted here |
| **read** | established by reading the code, with the file and line |
| **unverified** | plausible, not checked — do not plan around it until it is |

The second half of this document ([Corrections](#-corrections-to-the-first-draft)) records what
the first draft of this review got wrong, because one of its headline recommendations did not
survive measurement.

---

## Contents

[What this product is](#-what-this-product-is) ·
[How it was measured](#-how-it-was-measured) ·
[Correctness](#-correctness) ·
[The matching question](#-the-matching-question) ·
[Performance](#-performance) ·
[Structure](#-structure) ·
[Hardening](#-hardening) ·
[Features](#-features) ·
[UI and UX](#-ui-and-ux) ·
[Positioning](#-positioning) ·
[Housekeeping](#-housekeeping) ·
[Unverified](#-unverified--do-not-plan-on-these-yet) ·
[Shipped](#-shipped) ·
[Still to do](#-still-to-do) ·
[Order of work](#-order-of-work) ·
[Corrections](#-corrections-to-the-first-draft)

---

## 🎯 What this product is

A local, zero-dependency tool that turns a **human list of titles** into a real tracker list:
resolve each line against AniList, review and correct the matches in a local web page, then either
write straight to an AniList account or emit MAL-compatible XML.

The differentiators that actually exist in the code today:

1. **It starts from a plain list**, not an export file from another tracker.
2. **The write shows a per-field diff before it sends**, folds in the changes AniList makes on its
   own, and can be undone.
3. **It reaches AniList-only titles** that MAL XML structurally cannot carry.
4. **Measured, not assumed** — `MANGA_SUPPORT_NOTES.md` records what was proved against a live
   account, which is the reason someone would trust it with an irreversible write.

Current shape:

Shape when this review was written, and now:

| File | Was | Now |
|---|---|---|
| `gui.py` | 6,660, of which **5,127 were one string literal** | **1,610** |
| `generate_mal_xml.py` | 2,931, five jobs in one file | **2,419**, plus `matching.py` 424, `malxml.py` 275, `titles.py` 259 |
| the page | inside that literal | `static/index.html` 578, `static/app.css` 1,021, `static/app/*.js` **ten** sections, 3,599 lines |
| `tests/` | 344 tests, 59 s | **466 tests, ~9 s**, still no outbound network — plus `tests/js`, run by node through `tests/test_js_behaviour.py` |

---

## 🔬 How it was measured

| Probe | Command / method | Result |
|---|---|---|
| Synonym-aware ranking | real `_media_rank` run twice over identical nodes, 16 titles from `manga.txt`, live search | 0 of 16 picks changed; saturation 3 → 10 |
| Gotoubun candidate set | live `Page(perPage:10)` search, ranked through `_rank_nodes` | exactly one candidate returned |
| Line sanitiser | `sanitize_title_line` over 8 known numeric-prefixed titles | 6 of 8 corrupted |
| Test suite wall time | `unittest` discover, then again with `serve_forever(poll_interval=0.01)` | 59 s → **7.0 s**, 344 pass both ways |
| Per-module timing | each module timed separately | `test_anilist_write` 28.1 s of the 59 |
| Sleep/throttle audit | `time.sleep` and `_throttle` instrumented across `test_anilist_write` | 0.0 s slept, 0 throttle calls |
| Curly-quote variants | `_search_variants("Kaguya-sama’s War")` | `[]` |
| Dead global | `grep -rn last_run_failures *.py tests/*.py` | 3 hits, all writes, 0 readers |
| Calibration tool | repo, scratchpad, `git log --all -- '*calibrate*'` | absent from all three — since rebuilt as `tools/calibrate.py` (R14) |

---

## 🔴 Correctness

### R1 — The line sanitiser eats a whole class of real titles

**measured.** `sanitize_title_line` (`generate_mal_xml.py:284`) strips `^\s*(?:\d+[\.\-\)]|[-*•])\s*`
as list numbering. The `\d+-` branch matches Japanese counter prefixes:

```
BROKEN  '5-toubun no Hanayome'           -> 'toubun no Hanayome'
BROKEN  '3-gatsu no Lion'                -> 'gatsu no Lion'
BROKEN  '07-Ghost'                       -> 'Ghost'
BROKEN  '4-nin wa Sorezore Uso wo Tsuku' -> 'nin wa Sorezore Uso wo Tsuku'
BROKEN  '3-nen Z-gumi Ginpachi-sensei'   -> 'nen Z-gumi Ginpachi-sensei'
BROKEN  '6-gatsu wa Ai no Kisetsu'       -> 'gatsu wa Ai no Kisetsu'
ok      '86 - Eighty Six'   (space before the dash saves it)
ok      '11eyes'  '18if'  '20th Century Boys'  '91 Days'  '7 Seeds'
```

`07-Ghost → Ghost` is the worst case: it does not fail, it resolves confidently to a different
work.

**Fix.** Require whitespace after the delimiter — `^\s*(?:\d+[.)]|\d+-\s|[-*•])\s*` or equivalent.
`1. Berserk`, `1) Berserk` and `- Berserk` keep working; `3-gatsu` is left alone. One regex, and a
test table of the eight titles above.

**Why it is first.** Everything downstream — resolution, ranking, review, both destinations —
consumes this function's output. A title corrupted here cannot be recovered by any later stage.

### R2 — The pasted line is thrown away

**read.** `/api/process` (`gui.py:5730-5734`) builds `cleaned_titles` from the sanitiser, and
`decorate(resolved, raw_title)` (`:5791`) is then called with the *cleaned* string. `item.original`
therefore holds the sanitised text, never what the user typed.

So when R1 misfires, the table shows `from gatsu no Lion` — the user cannot see that their input
was altered. Carry the untouched line alongside the cleaned one.

### R3 — A sole candidate defeats the ranking tiers

**measured.** Live search for `Gotoubun No Hanayome`, ranked through the real `_rank_nodes`:

```
title                                     format    mal   pop   ch  ratio  rank key
Go-toubun no Hanayome: 122 + 1 - Itsu...  ONE_SHOT  None  2944   1  0.430  (0, 0, 1, 121.0)
```

One result. A **1-chapter one-shot with no MAL id**. The 122-chapter main series is not in the
candidate set at all — AniList's index does not match `Gotoubun` against `Go-toubun`.

Nothing flags it:

| Guard | Why it misses |
|---|---|
| `not_one_shot` tier | `_media_rank:480` sets it to 0 correctly, but tiers demote **relative to siblings**. With one candidate there is nothing to demote below. |
| `REVIEW_RATIO_FLOOR` (0.15) | ratio is 0.430 — comfortably above |
| `REVIEW_TIE_GAP` (inversion) | `runner_up_gap` is `None` with fewer than two candidates (`_candidates:414`) |
| The UI | `can_export_xml = False` renders as **AniList only**, a legitimate-looking state |

The code comment at `_media_rank:476` names this exact title as "the Boruto / Gotoubun failure this
code exists to prevent". The tier design does prevent it — when there is a sibling to lose to.

**Fix — zero API cost, data already in the node.** Flag a row for review when the *winning* rank
key carries a demotion:

- `not_one_shot == 0` — a one-shot won anyway, so it won by default, or
- `released == 0` — an unreleased entry won anyway, or
- the candidate list has length 1 **and** the single entry is a one-shot or has `chapters == 1`.

This is higher precision than any title-similarity threshold and, unlike a ratio, it does not
saturate. See [The matching question](#-the-matching-question) for why that matters.

### R4 — The retry pass cannot produce the spelling that would have worked

**read.** `_search_variants` (`:533`) has three rules: NFKC + quote folding, strip bracketed text,
strip a trailing `manga|manhua|manhwa`. None varies hyphenation or word spacing, which is exactly
what `Gotoubun` → `Go-toubun` needed.

It also only runs for titles that resolved to **nothing** (`resolve_many_entries:1265`). R3's row
"resolved", so the retry never fires for it.

Adding a hyphen/space variant rule is cheap, but note it only helps the no-hits population.

### R5 — The curly-quote variant has never worked

**measured.**

```python
_search_variants("Kaguya-sama’s War")  ->  []

# the replace target it builds:
"’".encode().decode("unicode_escape")  ->  'â\x80\x99'
```

`generate_mal_xml.py:544` round-trips each curly quote through `encode()` / `unicode_escape`,
producing mojibake that is never present in a real title. `NFKC` does not fold U+2019 to `'`, so
no straight-quote variant is ever generated.

**Fix.** `stage1.replace(curly, straight)` — one line, and the four no-op replaces disappear.

**Impact is narrow**, and the first draft of this review oversold it: variants only run for titles
that found nothing, and AniList's fuzzy search usually finds a curly-apostrophe title anyway.
It is a correctness fix on a cold path.

### R6 — Score precision is silently lost on an account copy

**read.** `_row_from_list_entry:1500` reads `POINT_100` and stores `round(score / 10)` as 0–10.
`iter_write_payloads:1967` writes it back as `score * 10`. An account score of **87 becomes 90**.

On a normal write the diff renders `87 → 90`, so it is visible. On **Copy an account** there is no
per-row diff — only counts — so it is silent, and the README promises score travels.

**Fix, and the rule the first draft omitted.** Carry `score_raw` on rows read from AniList and
prefer it in `iter_write_payloads` — but it must be invalidated whenever the user edits the score,
or a score edit on an AniList-read row silently no-ops. Two write sites:

- the per-row detail editor, `gui.py:3991` (`item.score = ...`)
- `applyBulk`, `gui.py:4295`

Both must `delete item.score_raw`. Side effect to expect: rows that currently count as "changed"
in the copy Check become "unchanged", so the preview numbers shift.

### R7 — A cancelled run discards everything it resolved

**read.** `resolve_many_entries:1297` returns on `stopped` *before* the `if use_cache:` save at
`:1307` and before the session-cache fill at `:1314`. Cancel a 300-title run at title 250 and all
250 resolutions are thrown away; the next run re-spends that rate limit.

**Fix.** Save the `_cacheable` subset of `results` before the early return — roughly four lines.
Safe, because unresolved placeholders are only appended *after* the stopped check (`:1303`), so
nothing negative can be cached.

### R8 — Closing the tab leaves the resolve running and the lock held

**read.** `send_event` in `/api/process` raises `BrokenPipeError` once the client is gone, but it
is reached through `on_progress`, which `report()` wraps in `try/except: pass`
(`generate_mal_xml.py:1192-1198`). The resolver therefore runs to completion against a dead socket,
holding `_run_lock` for the full ~90 s. Reopening the page and pressing Process gives
*"A run is already in progress."*

The write, undo and copy endpoints are **not** affected: they call `send_event` directly inside
their own `try/finally`, so the exception propagates and the lock is released.

**Fix.** Catch the write failure in `/api/process`'s `send_event` and `control["cancel"].set()`.

**Caveat, and an ordering dependency.** SSE writes are buffered, so a short run can finish before
any write fails — this is best-effort. And it must land **with or after R7**, or a closed tab stops
merely orphaning the work and starts destroying it.

### R9 — `last_run_failures` is dead

**measured.** `grep -rn last_run_failures *.py tests/*.py` → three hits, all in
`generate_mal_xml.py`: the declaration (`:1095`), `.clear()` and `.extend()` (`:1300-1301`). No
reader anywhere, including the tests. The GUI uses the `network_error` SSE event instead.

A module-level mutable global that nothing consumes. Delete it, with its comment.

---

## 🧪 The matching question

This section exists because the first draft of this review recommended a change here as its single
highest-value item, and measurement did not support it.

### What was proposed

`_title_ratio` (`:450`) scores only against `english` and `romaji`. `_search_batch_query` (`:621`)
does not even request `synonyms`. Yet the code's own comment at `_media_details:341` says
*"Synonyms are how a pasted title often differs from the official one, so they are the most useful
field for judging a doubtful match."*

Two live examples that look decisive:

```
"First God Of War"                           -> Di Yi Zhan Shen
    en/ro 0.129   synonyms 1.000   ['First God of War']
"I Am Being Chased To Fall In Love Everyday" -> Forced to Date with BOSS Everyday
    en/ro 0.560   synonyms 1.000   ['Forced to Fall in Love With the Boss Every Day',
                                    'I Am Being Chased to Fall in Love Everyday']
```

### What measurement showed

The real ranker was run twice over identical candidate nodes — once as shipped, once with synonyms
folded into `_title_ratio` — across 16 titles from `manga.txt`:

```
pasted                                     picked now / with synonyms         r_now  r_syn gap_now gap_syn
Mushoku Tensei                             Mushoku Tensei: Redundant Reincar  0.718  0.718   0.158   0.067
Gotoubun No Hanayome                       Go-toubun no Hanayome: 122 + 1     0.430  0.430   0.000   0.000
Tsuki Ga Michibiku                         Tsukimichi: Moonlit Fantasy        0.706  0.706   0.000   0.000
Duke of Death And His Maid                 The Duke of Death and His Maid     0.929  0.929   0.000   0.000
In another world with my Smartphone        In Another World With My Smartpho  1.000  1.000   0.000   0.000
First God Of War                           Di Yi Zhan Shen                    0.129  1.000   0.000   0.000
I Am Being Chased To Fall In Love Everyday Forced to Date with BOSS Everyday  0.560  1.000   0.000   0.000
The Max Level Hero Has Returned!           The Max Level Hero Strikes Back!   0.677  1.000   0.000   0.000
Aitsura, Zettai Yatta Desho                They Definitely Had S●X            0.341  1.000   0.000   0.000
The Great Mage Returns After 4000 Years    The Archmage Returns After 4000 Y  0.895  1.000   0.000   0.000
Maxed Out Leveling                         Leveling Beyond the Max            0.579  1.000   0.000   0.000
Sweet Guy                                  He Does a Body Good                0.231  1.000   0.113   0.143
The Hero Returns                           The Hero Returns                   1.000  1.000   0.000   0.000

picks changed:                  0 of 16
candidates at ratio >= 0.95     now: 3    with synonyms: 10
```

**R10 — "it improves ranking" is false.** Zero picks changed. `_media_rank` is lexicographic:
`(has_mal_id, not_one_shot, released, score)`. The title term only breaks ties *inside* a tier, and
`has_mal_id` plus popularity already decide. The tier design is correct; it also makes the title
score nearly inert for the choice.

**R11 — it erodes the signal that works.** Saturation triples, because `max()` over ~20 extra
strings raises every candidate monotonically. The runner-up gap is the only review signal that
currently earns its place (`REVIEW_TIE_GAP = 0.0`, i.e. inversions), and it compresses — Mushoku's
halved, 0.158 → 0.067.

**R12 — the floor still cannot be raised.** With synonyms on, the *correct* matches that score low
stay low: Gotoubun 0.430, Tsukimichi 0.706, Mushoku 0.718. A floor at 0.5 flags correct rows
exactly as 0.6 did in the original calibration. Synonyms do not rescue `REVIEW_RATIO_FLOOR`.

**R13 — what does survive: a boolean, not a ratio.** The useful fact is *"does AniList list the
pasted title as an exact alias of this entry?"* That is high-precision, does not saturate, and
belongs in its own field — a badge reading **"AniList knows your title as an alias"** — rather than
as a term inside `match_ratio`. Pair it with R3's demoted-winner flag; together they cover both
"we are confident and right" and "we won by default".

**R14 — the calibration harness no longer exists.** `calibrate.py` is absent from the repo, absent
from the scratchpad, and `git log --all -- '*calibrate*'` is empty — it was never committed. Any
change in this area needs it rebuilt first, and needs to be judged on a **before/after diff of
which rows changed pick**, not on an aggregate flag percentage.

**R15 — a documented defect was not one. Settled 2026-10-22.** `MANGA_SUPPORT_NOTES.md` cited
*"I Am Being Chased To Fall In Love Everyday" → "Forced to Date with BOSS Everyday"* as the
canonical undetectable wrong match.

**measured.** A live unauthenticated search for the pasted line returns exactly one candidate,
AniList id **192823**, whose three synonyms include the pasted line verbatim:

```
id 192823 | idMal None | ch 86
  romaji  : Meitian Beipo He Dalao Tanlianai
  english : Forced to Date with BOSS Everyday
  synonyms: ['Forced to Fall in Love With the Boss Every Day',
             'I Am Being Chased to Fall in Love Everyday',
             '<native title>']
  exact synonym for the pasted line: True
```

The match is **correct**, and always was. `matching._alias_hit` returns `True` for it, so the row
the notes called invisible now carries a **known alias** badge — R13 closed this case without
anyone noticing it had been filed as a defect. The note in `MANGA_SUPPORT_NOTES.md` has been
corrected in place rather than deleted, and **no replacement example has been measured**: the
claim that some wrong matches are undetectable may well still hold, but this row was not evidence
for it.

---

## ⚡ Performance

### R16 — The test suite is 8× slower than it needs to be

**measured.**

```
whole suite    59 s  ->  7.0 s     (344 tests, 0 failures, both runs)
test_anilist_write  28.1 s  ->  1.8 s   (129 tests)
```

Per-module, before:

```
28,099 ms  test_anilist_write        1,648 ms  test_gui_manga_endpoint
12,404 ms  test_gui_contract           722 ms  test_cli
 6,446 ms  test_anilist_copy           679 ms  test_batch_resolution
 6,266 ms  test_run_control            676 ms  test_gui_template
 4,249 ms  test_anilist_list           557 ms  test_manga_support
 3,243 ms  test_export_state           476 ms  test_match_confidence
```

**Cause.** Every server fixture does `threading.Thread(target=self.server.serve_forever)`. The
default `poll_interval` is **0.5 s**, and `shutdown()` blocks until that loop notices — so each of
roughly 55 per-test servers pays up to half a second of pure waiting. Instrumentation confirms
nothing else is responsible: **0.0 s** spent in `time.sleep` and **0** `_throttle` calls across the
slowest module.

**Fix.** `target=lambda: self.server.serve_forever(poll_interval=0.01)` in the six fixtures
(`test_anilist_copy:151`, `test_anilist_list:203`, `test_anilist_write:761` and `:1315`,
`test_export_state:36`, `test_gui_contract:48` and `:332`, `test_gui_manga_endpoint:32` and `:76`,
`test_run_control:26`).

**Do not** hoist the server into `setUpClass` as an alternative — the fixtures patch module globals
(`EXPORT_XML_PATH` and friends) to per-test temp directories, and sharing the server across tests
would need that isolation reworked. Take the one-liner.

A 59-second suite is one people stop running before committing.

### R17 — `renderTable` is quadratic and rebuilds everything

**read.** `gui.py:3899` calls `currentAnimes.indexOf(item)` once per rendered row — O(n²). At the
300-row cap that is fine; under **Show all** with a few thousand rows it is not. Separately, every
checkbox toggle rebuilds the entire `<tbody>`.

**Both fixes are larger than they look:**

- The index is the row's identity across `data-index`, `data-detail`, `expandedRows`,
  `visibleIndexes()`, the keyboard navigation and `writeOverrides` — about six call sites.
- A single-row re-render must also refresh `filterCountLabel`, `updateSelectAllState()` and
  `rowCapNotice`, or the counters drift away from the table.

Not urgent at the current cap. Worth doing before raising it.

---

## 🧱 Structure

### R18 — Split the HTML template into files

`gui.py` is 6,660 lines, **5,127 of them one string literal**: 1,849 CSS, 2,596 JS, 682 markup,
against ~1,460 lines of actual Python.

**The strongest argument is not file length.** Because the template is a Python string, the
JavaScript **cannot contain a backslash**. That is why there are 8 `String.fromCharCode(10)` calls,
no regex literals and no `\n` anywhere in 2,596 lines of JS — plus a dedicated test
(`test_template_source_has_no_python_consumed_escapes`) and two source comments policing it. That
constraint dies with the split. (206 backticks already appear, so template literals work today.)

**Shape:** `static/index.html`, `static/app.css`, `static/app.js`, assembled by `_page_html()`.

- **Read per request, not at import.** `_page_html()` is already called on every GET
  (`gui.py:5596`), so reading the files there gives live CSS/JS reload with no server restart. Keep
  an import-time read only to populate `gui.HTML_TEMPLATE`, which `tests/test_gui_template.py`
  depends on — then none of those 494 lines of tests need to change.
- `_page_html()` already does the `__REVIEW_RATIO_FLOOR__` substitution, so the seam exists.
- Resolve from `BASE_DIR / "static"` so `python gui.py` still works from any cwd.
- Keep inlining into one response: the page is served `no-store` and stays a single request.

**Two honest trade-offs:**

- It **moves** the bulk rather than reducing it. `app.js` is still 2,596 lines in one IIFE with
  about 100 module-level bindings. That is the real complexity and the split does not touch it.
- `gui.py` + `generate_mal_xml.py` is currently two copyable files; afterwards it is a directory,
  which weakens the README's "no install step" framing slightly.

### R19 — Then, separately, modularise the JavaScript

Once it is a real `.js` file, `<script type="module">` plus 3–4 modules (table, write, fixer,
state) becomes cheap, and eslint/prettier/devtools line numbers all start working. **Do not do it
in the same change as R18** — one of them is a pure move, the other is a refactor, and mixing them
makes the diff unreviewable.

### R20 — `generate_mal_xml.py` can wait

2,931 lines doing five jobs (HTTP client + rate limiter, cache, ranking, XML builders, AniList
write + undo). It is navigable because of its section banners. Leave it until it next grows, then
split `anilist.py` / `matching.py` / `malxml.py`. Lower priority than R18.

---

## 🛡 Hardening

Both are local-only issues on a loopback server that already refuses cross-origin POSTs. Cheap
enough to fix anyway.

### R21 — The request body is read before any size check

**read.** `do_POST` reads `self.rfile.read(content_length)` at `gui.py:5713`; `MAX_IMPORT_BYTES`
is not enforced until `:6299`, after the whole body is already buffered. A malformed
`Content-Length` also raises `ValueError` → 500.

**Fix.** Parse and bound the header before reading. The cap must exceed `MAX_IMPORT_BYTES`
(20 MB), because `/api/process` legitimately carries large pastes — around 25 MB, not a small
round number.

### R22 — The DOCTYPE guard only covers the first 4 KB

**read.** `parse_mal_xml:278` checks `"<!DOCTYPE" in raw[:4096].upper()`. `ElementTree` expands
internal entities, so a file with a long comment prolog gets past the guard. `<!ENTITY` cannot
appear without a DOCTYPE, so scanning the whole string is sufficient — drop the `[:4096]` slice.
It is a full-string scan on a document about to be parsed anyway.

---

## 🚀 Features

Ordered by value ÷ code, **after R1 lands** — a smarter input parser on a sanitiser that turns
`07-Ghost` into `Ghost` makes things worse, not better.

### R23 — Parse the line, not just the title

The product's real differentiator is that it takes a human list — but only one bare title per line.
Real lists look like:

```
Frieren 12/28 9/10 watching
Berserk — 374 ch — dropped
1. Vinland Saga (completed) 8
```

Extracting progress, score and status is pure local string work, no API cost, fully testable
offline, and nothing comparable does it.

**Constraints it has to respect**, all of which R1 illustrates:

- Strictly additive. It may fill `score`, `status`, `my_read_chapters` — it must **never** alter
  the title unless the user confirms.
- Show what it parsed in the row, so a wrong read is visible and correctable.
- Numeric ambiguity is real and unavoidable: `86`, `20th Century Boys`, `7 Seeds`, `3-gatsu no
  Lion`, `91 Days`. Default to treating digits as part of the title; only claim them as metadata
  when a unit or separator makes it unambiguous (`12/28`, `374 ch`, `9/10`).

### R24 — Cut the token setup from five steps to two

Token setup is the biggest drop-off in the funnel. Client ids are **public by design** in the
implicit grant — the README already says "a short number, not a secret". Registering one AniExport
client and shipping the id removes steps 1–3; a **Get a token** button opens the authorize URL
directly.

**Three things that must come with it:**

- A field to paste your own client id, since the shared client is tied to one account.
- An error path that distinguishes *"the shared client was revoked"* from *"your token expired"* —
  otherwise every install sees a wrong diagnosis simultaneously.
- A decision about attribution: other people's API traffic lands on whoever owns the client.

### R25 — Save the write plan

The diff modal is the best thing in the product and it is ephemeral. "Download this plan" as JSON
before a 300-row write costs almost nothing and is exactly what someone nervous about an
irreversible operation wants to keep.

### R26 — Expose all five undo snapshots

`list_undo_snapshots()` returns five and `/api/anilist-snapshots` serves them, but the page uses
`[0]` only (`gui.py:5554`). Four recoverable snapshots are unreachable from the UI.

**Group by media type when listing them.** A copy writes **two** snapshots, one per media type, so
a flat timestamp list shows one logical operation as two unrelated entries. `list_undo_snapshots()`
already returns `media_type`.

### R27 — CSV / JSON export of the resolved list

`csv` is stdlib, roughly fifteen lines. Some people want the data, not an import file, and it makes
the resolver useful on its own. Scope it **export-only** — a re-importable format is a second
parser to maintain.

### R28 — MAL username import

The direction matrix is otherwise complete. Missing: reading a MAL list without the user exporting
a file first. **This depends on an unverified assumption — see below.**

---

## 🎨 UI and UX

- [x] **no action needed** - the 300-row cap with **Show all** is the right call. Do not add
  virtualisation until someone complains; R17 has since landed if the cap ever rises.
- [x] **done** (2026-10-22 redesign) - **a `?` shortcut sheet.** `?` or **Keys** above the table
  opens it; it lists shift-click ranges, arrow navigation, space-to-toggle and Ctrl+Enter.
- [x] **done** (2026-10-22 redesign) - **lead the empty state with the destination question.** The
  Source step now asks it above the paste box, and Resolve will not start until it is answered,
  because the server ranks candidates differently per destination.
- [x] **no action needed** - accessibility is already strong: focus management, dialog roles,
  scoped headers, `prefers-reduced-motion`. Keep the `CascadeOrderTests` guard; it caught a real
  regression.

---

## 📣 Positioning

The README leads with *Resolves / Reviews / Delivers*, which describes every importer ever written.
What is actually distinctive, in order:

1. [x] **done** (2026-10-22) - **it starts from a human list.** The tagline is now *"Start from
   the list you already wrote - not an export file from a tracker you already use"*, and the
   opening paragraph leads with the category difference and R23's line parsing.
2. [x] **done** (2026-10-22) - **it shows the diff and can undo it.** Second of the three
   columns, quoting *"episodes 224 → 142"* and the snapshot.
3. [x] **done** (2026-10-22) - **measured, not assumed.** Third column, linking
   `MANGA_SUPPORT_NOTES.md` from the top of the README rather than only from Limitations.

Claim 1 was originally written here as "no other importer takes a human list". That is a
competitive absolute this review cannot substantiate — keep the positioning, drop the absolute.

---

## 🧹 Housekeeping

- [x] **done** (2026-10-22) - **the JavaScript tests are in the repo.** `tests/js/` with
  `run.js` as the entry point (`node tests/js/run.js`) and `tests/test_js_behaviour.py` wiring it
  into `python -m unittest`, skipped rather than failed when node is absent. **111 assertions**
  across the write diff, the candidate card and the skip list.

  They no longer run against hand-made copies: `tests/js/extract.js` pulls the 19 functions out of
  `static/app/*.js` at run time and **throws** if a name is renamed, moved or deleted, so a rename
  is a failure rather than a suite that quietly tests nothing. That failure path has its own test.
- [x] **done** - **`calibrate.py` was already lost** (R14). Rebuilt as `tools/calibrate.py`, with
  `tests/test_calibrate.py` beside it.
- [x] **written, and deliberately not called green** (2026-10-22) -
  `.github/workflows/tests.yml`, matrixed over Python 3.9 / 3.12 / 3.13 on Ubuntu and Windows,
  with node for `tests/js`. **It has not run**: that needs a push, so its status is unknown and is
  not claimed here.

  A static check supports 3.9 without settling it - every module carries
  `from __future__ import annotations`, there are no `match` statements or other 3.10+ syntax, and
  every stdlib module used exists in 3.9. That is an argument, which is exactly why the matrix is
  there.
- [x] **done** - the README test count is current in both the badge and the Tests section, though
  it is still a hand-maintained number rather than pinned to the command.
- [x] **done** (2026-10-22) - `generate_mal_xml.__version__` is the single source, and
  `USER_AGENT` is built from it as `AniExport/2.0.0 (+<the repo URL>)` - a contactable name for an
  unauthenticated public API. Both entry points answer `--version`, and `VersionTests` fails if
  `MALAnimeExporter` reappears in either file.
- [ ] **not done, and the item as written is wrong** (measured 2026-10-22). A 15-line
  `pyproject.toml` with `py-modules` **does not work here**: built as a wheel it carries the five
  modules and **zero** files from `static/`, so the installed `aniexport` would start a server with
  no page to serve. The page is a directory, not a module, and `py-modules` wheels carry no data.

  Three options, each built rather than reasoned about:

  | Option | Result | Cost |
  |---|---|---|
  | `py-modules` only, as the item describes | wheel built, **0** static files | broken install |
  | plus `packages = ["static", "static.app"]` | **works** - 12 static files, and the installed `aniexport -p 5321` served the real 253,513-byte page | claims the generic top-level name `static` in site-packages: harmless under `pipx`/`uvx`, a real collision hazard under `pip install` into a shared environment |
  | restructure into an `aniexport/` package with `static/` inside | the correct answer | moves five modules and **21 import lines across 13 test files**, and weakens the README's "two copyable files" framing |

  **Not shipped, because this is a distribution decision rather than a defect.** Clone and
  `python gui.py` already works with no install step, so the marginal value is modest and the
  right fix is the restructure, not a wheel that pollutes the namespace.

---

## ⚠️ Unverified — do not plan on these yet

| Claim | Status |
|---|---|
| MAL API v2 serves **another user's** list with only `X-MAL-CLIENT-ID` (R28) | **Still unverified, and now known to be unverifiable without a client id.** See below. |
| ~~Adding `synonyms` to the batch query "costs nothing per request"~~ | **Settled.** Measured at 8 aliases × perPage 10: query 2,122 → 2,242 B, response 15,572 → 21,831 B (+40%), latency 0.60 → 0.58 s, and **the rate-limit decrement is 1 either way**. The claim was right about rate limit, wrong about bytes, and the bytes do not matter — about 270 KB across a 339-title run. |
| Python 3.9 compatibility | **Unverified.** Only one annotation-level risk found, and it is neutralised. CI would settle it. |
| "No other importer takes a plain list" | **Unverifiable competitive claim.** Dropped from the positioning. |

---

## ✅ Shipped

| # | What landed | Evidence |
|---|---|---|
| **R1** | the hyphen branch of the numbering rule now requires whitespace | 6 of 8 counter titles were corrupted before; `tests/test_input_parsing.py` |
| **R2** | rows carry `pasted` when the line was tidied, and the table shows it | `PastedLineTests` |
| **R3** | one-shot / unreleased / lone-one-chapter winners are flagged, with a reason per row | the Gotoubun node, which defeated every other guard; `WonByDefaultTests` |
| **R5** | the curly-quote variant fires at all | `_search_variants("Kaguya-sama’s War")` returns the straight form, not `[]` |
| **R6** | `score_raw` carries the exact POINT_100 value, invalidated on edit at both write sites | `ScorePrecisionTests`, including a copy round-trip that must produce an empty diff |
| **R7** | a cancelled run caches what it resolved | cancel after 1 of 3 batches: 8 cached, re-run costs 0 requests |
| **R8** | a vanished client cancels the run instead of orphaning the lock | `ClientDisconnectTests`, which fails without the fix (verified by neutering it) |
| **R9** | `last_run_failures` deleted | 3 writes, 0 readers |
| **R16** | `serve(server)` in `tests/__init__.py`, 10 fixtures | 59 s → 9.1 s |
| **R21** | the body is bounded before it is read | `RequestSizeTests` |
| **R22** | the DOCTYPE scan covers the whole document | `DoctypeScanTests` |
| **R4** | the retry pass varies hyphenation (`07-Ghost` → `07 Ghost`, `07Ghost`) | `SearchVariantTests`; the insertion direction stays out of reach and is R3's job |
| **R18** | the template lifted into `static/`, `gui.py` 6,662 → **1,610** lines | reassembly is byte-identical to the old literal, asserted both ways before anything was written |
| **R14** | `tools/calibrate.py` rebuilt, with the pick diff as its headline | `tests/test_calibrate.py`, no network |
| **R13** | the exact-alias boolean, clearing a low-ratio flag but not a won-by-default one | `AliasHitTests`; on the real list 8 of 12 rows are alias hits and 1 was being falsely flagged |
| **R23** | the whole line is read — status, score, progress, volumes | `LineParsingTests` + `LineFieldTests`; a bare number is never believed on its own, and a half-read line is abandoned whole |
| **R25** | **Save plan** writes the diff as JSON, overrides included | `KeepTheWorkTests`; without the overrides the plan does not describe what would have been sent |
| **R26** | all five undo snapshots, labelled by media type, delete box following the choice | the page read `[0]` and left four unreachable |
| **R27** | **Download CSV**, built in the browser | formula-injection guard and a UTF-8 BOM, both verified on real bytes |
| **R17** | one definition of the "Showing N of M" line | it flipped from 300 to 2000 on any toggle above the row cap; the quadratic `indexOf` went too, worth ~0 ms at realistic sizes |
| **R19** | the script in ten numbered sections, rejoined byte for byte | each section parses alone; ES modules measured and deferred — see below |
| **R20** | `matching.py`, `titles.py`, `malxml.py` split out | only code that touches no network and is never patched; the import list is explicit because `import *` skips underscored names |

Two notes on how these were checked, both of which changed the work:

- **R8's test passed without the fix twice before it was right.** First it closed the socket
  before the run reached the resolver, so the failure hit an unprotected `send_event` and released
  the lock by crashing. Then it still mocked `resolve_many_entries` itself — and the swallow that
  hides the broken pipe lives *inside* that function. It only discriminates when the real resolver
  runs with a slow `_execute_anilist_batch` underneath.
- **One assertion was wrong, not the code.** R7's "never caches a miss" test asserted the cache
  file would not exist. `save_cache` has always been called unconditionally, which is also what
  compacts expired rows; the right assertion is that `entries` is empty.
- **R18 shipped half-done at first.** The literal was replaced with a loader, but `_page_html()`
  was left returning the copy built at import — so the per-request re-read that justifies the
  whole arrangement did not exist. The live-reload test caught it, which is the only reason that
  test was worth writing.
- **R13 broke a deliberate guard, correctly.** `test_the_batch_query_stays_lean` asserted that
  `synonyms` never appears in the batch query, on the grounds that a 339-title run would carry it
  for ten candidates each and show none of it. Half of that reasoning no longer holds — the field
  is now read by `_alias_hit` — so the test keeps guarding `genres`, `description` and `siteUrl`
  and a second test records the measured exception. The guard was updated, not deleted.
- **A test scenario that did not test what it claimed.** The first version of the
  "alias beats an inversion" case built two nodes that shared a MAL id, so one was deduped away
  before ranking, and the competitor outranked the alias anyway — no inversion ever occurred. It
  now builds the real shape: the has-a-MAL-id tier putting a worse-named entry first.
- **R19 was cut back on evidence, not on preference.** The plan said "ES modules for `app.js`".
  Counting first: 246 top-level bindings, `currentAnimes` read 79 times and reassigned in
  several. An imported binding cannot be reassigned, so modules mean a state refactor across
  ~200 call sites — on the code that writes irreversibly to an account, verified by a suite that
  mostly asserts on source text. The file split shipped because it provably cannot change
  behaviour; the state refactor is listed above as its own piece of work.
- **R17 was smaller than the review claimed.** The `indexOf` really was quadratic, and at 2,000
  rows it costs about 0 ms against 240 ms of DOM work. What the look actually found was the
  counter writing two different numbers.
- **R23's status matching took three attempts.** A status can be several words ("on hold", "plan
  to watch"), so the pattern has to allow spaces inside it — and a regex that does will happily
  match `ch - dropped` or `eps completed` as a single word and strip the lot. It matches whole
  trailing words now, not a regex. The earlier versions parsed nothing at all for exactly the
  lines the feature exists for, which is how they were caught.

## 🚧 Still to do

Everything that could be settled by doing the work, or by measuring, has been. These three cannot:
each needs a decision from you, not an afternoon.

| # | Item | Blocked on |
|---|---|---|
| **R24** | ship a public AniList client id | **your call** — the client would be registered under one account and carry everyone's traffic, and a revocation would break every install at once |
| **R28** | MAL username import | **your call** — settling the API question needs one request with a real client id, and a client id is tied to a MAL account. See [what was tried](#what-was-tried-for-r28) |
| *(packaging)* | `pyproject.toml` | **your call** — the three measured options are in [Housekeeping](#-housekeeping); the correct one is a package restructure, not a 15-line file |
| *(R19, second half)* | real ES modules for the script | ~15 mutable top-level bindings moving behind a state object, ~200 call sites, on the write path. Deliberately not bundled with the file split; best done *inside* a feature that already touches that code rather than as a standalone refactor |

### What was tried, for R28

The official v2 reference page is JavaScript-rendered and serves 66 characters to a plain fetch,
so it settles nothing. The authorization page is complete but never mentions `X-MAL-CLIENT-ID` at
all, never says which endpoints accept a client id versus a user token, and says nothing about
`{user_name}`, `@me`, or list privacy. Its only worked example is a user-token *write*.

Probing the API directly gets close but cannot finish the job:

```
GET /v2/users/<name>/animelist   no auth           -> 403 {"error":"forbidden"}
GET /v2/users/<name>/animelist   invalid client id -> 400 {"message":"Invalid client id"}
GET /v2/anime?q=berserk          no auth           -> 403 {"error":"forbidden"}
GET /v2/anime?q=berserk          invalid client id -> 400 {"message":"Invalid client id"}
```

The user-list endpoint answers **identically to the search endpoint**, which is known to work
with a client id alone — it accepts the client-id scheme and fails only on the id being invalid,
rather than refusing the scheme outright. That is consistent with client-id auth being allowed
there, but it is not evidence: MAL validates the client id *before* making any authorization
decision, so both endpoints would look exactly like this whether or not a valid id would be
accepted afterwards.

**What would settle it:** one request with a real client id. Registering one takes a form and no
approval step (the authorization page describes no review), but it is tied to a MAL account, so
it is the owner's call — the same shape of decision as R24.

## 📋 Order of work

Dependencies are real here; this order is not a preference.

| # | Item | Why here | Size |
|---|---|---|---|
| 1 | **R1** sanitiser + **R2** keep the raw line | Silent corruption on the input every other stage consumes | regex + a test table |
| 2 | **R3** demoted-winner / sole-candidate flags | Catches a real wrong match in the current list, zero API cost, replaces most of what synonyms were meant to do | small |
| 3 | **R16** test speedup | Makes every later change cheap to verify | one line × 6 fixtures |
| 4 | **R7** cancel saves the cache | Must precede R8 | ~4 lines |
| 5 | **R8** cancel on a broken pipe | Destroys work if shipped before R7 | ~4 lines |
| 6 | **R6** score precision + the edit-wins rule | Silent data loss on copy | ~10 lines, 2 call sites |
| 7 | **R5** curly quotes, **R9** delete the dead global | Trivial, bundle them | 2 lines |
| 8 | **R18** template split, with the per-request read | Unblocks R19 and normal front-end tooling | mechanical |
| 9 | **R21**, **R22** hardening | Independent of everything | ~8 lines |
| 10 | **R14** rebuild the calibration harness | Prerequisite for anything in matching | small tool |
| 11 | **R13** exact-alias badge | Only meaningful once 10 exists and 2 has shipped | small |
| 12 | **R23** onward — features | All sit behind R1 | varies |

---

## 📏 Corrections to the first draft

Recorded rather than quietly edited, in the same spirit as the "what the first draft got wrong"
table in the shipped engineering notes.

| Claim in the first draft | What measurement showed |
|---|---|
| "Add synonyms to `_title_ratio` — highest value item here; it improves ranking" | **Wrong.** 0 of 16 picks changed (R10). The tier structure makes the title score nearly inert for the pick. |
| "The review floor can go back to something useful" | **Not supported.** Correct matches still land at 0.43–0.72 with synonyms on (R12). |
| "Synonyms make `match_ratio` a real signal" | **Partly true, with a cost not mentioned.** Saturation 3 → 10, and the runner-up gap compresses (R11). |
| "Costs nothing per request" | **Unverified**, asserted without checking. |
| "A cancelled run leaves stale `last_run_failures`" | **Retracted.** Nothing reads that global at all (R9). |
| "The curly-quote fix matters" | **Oversold.** Narrow cold path (R5). |
| "~5 lines" for the score fix | **Under-specified.** Missing the invalidate-on-edit rule, without which score edits silently no-op (R6). |
| "Or hoist one server per class" | **Not a drop-in.** It would break per-test temp-directory isolation (R16). |
| "Read the template files at import" | **Worse option.** Per-request reading gives live reload for free (R18). |
| "The split fixes the size problem" | **It moves it.** `app.js` is still 2,596 lines in one scope (R18). |
| "Move `calibrate.py` into the repo" (filed as housekeeping) | **It no longer exists**, and item 1 of the plan depended on it (R14). |
| "Smart line parser" as feature #1 | **Mis-ordered.** It sits on top of a sanitiser that corrupts titles (R1). |
| "65 assertions in `test_diff.js`" | **Wrong number.** Three files, 41 `assert` lines total. |
| "MAL API v2 needs no OAuth for public lists" | **Unverified**, presented as fact. |

The pattern worth keeping: two of the three biggest items in the first draft were produced by
reading code and reasoning, and both were corrected by a single probe against the live API. The
project's existing habit of recording measurements rather than inferences is the right one.
