<div align="center">

# AniExport

**Turn a plain list of titles into a real AniList or MyAnimeList list.**

[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-success)](requirements.txt)
[![Tests](https://img.shields.io/badge/tests-333%20passing-success)](#-tests)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

Paste a list of anime or manga titles. AniExport resolves each one against AniList's GraphQL API,
lets you review and correct the matches in a local web page, and then either writes them straight
to your AniList account or exports a MAL-compatible XML file.

```bash
python gui.py          # opens http://127.0.0.1:5000
```

No install step, no packages, no account needed to try it.

<table>
<tr>
<td width="33%" valign="top">

**🔎 Resolves**

339 titles in about 90 seconds, 8 per request, paced off AniList's own rate-limit headers.
Cached for 7 days, so a repeat run is near-instant.

</td>
<td width="33%" valign="top">

**✅ Reviews**

Rows stream in as they resolve. Pick between candidates, fix what matched nothing, edit status,
progress, score and dates per row.

</td>
<td width="33%" valign="top">

**🚀 Delivers**

Straight to your AniList account, out as MAL-compatible XML, or copied wholesale from one AniList
account to another — anime and manga, every field.

</td>
</tr>
</table>

### Contents

[Two destinations](#-two-destinations-and-why-it-matters) ·
[Install](#-install) ·
[GUI](#-gui) ·
[Copy an account](#copying-one-account-onto-another) ·
[CLI](#-cli) ·
[Data notes](#-data-notes) ·
[Limitations](#-limitations) ·
[Tests](#-tests) ·
[Project docs](#-project-docs)

---

## 🎯 Two destinations, and why it matters

AniList is the primary target. MAL XML is only a transport, and it is a lossy one:

| Destination | Identity required | Needs a token | Carries |
|---|---|---|---|
| **AniList (direct write)** | AniList id | yes | every title AniList knows |
| **MAL-compatible XML** | MyAnimeList id | no | only titles that also exist on MAL |

Plenty of titles exist on AniList with no MyAnimeList id at all. Those rows are labelled
**AniList only**: they cannot be written into the XML file, but they can be sent directly to your
account. A row is only called **Not found** when AniList does not know it either.

You pick the destination on first run; it is remembered after that.

## 📦 Install

Python 3.9+ and an internet connection. Nothing else: `requirements.txt` installs no packages.

## 🖥️ GUI

```bash
python gui.py              # opens http://127.0.0.1:5000
```

| Flag | Meaning |
|---|---|
| `-p/--port` | local port (default 5000, scanning up to 5049) |
| `--no-browser` | do not open a browser |

### Resolving a list

Paste titles (one per line), or drag a `.txt` file onto the box, or press `Ctrl/Cmd+Enter` to
start. Numbering (`1.`, `2)`), bullets and surrounding quotes are stripped; `#` lines are ignored.

Rows appear **as they resolve**, not at the end. A run can be **paused or cancelled**; a cancelled
run writes no files and keeps whatever it already resolved.

Titles are resolved in batches of 8 per request. AniList allows 90 requests/minute (currently
degraded to 30), and the pacing follows the rate-limit headers automatically, so a 339-title list
takes roughly a minute and a half rather than twelve.

Results are cached in `.anilist_cache.json` for 7 days, so a repeat run is close to instant. Turn
on **Ignore saved results** to refetch, `--no-cache` on the CLI, or point `ANIEXPORT_CACHE`
elsewhere.

### Reviewing and fixing matches

- Every row shows the chosen title, its MAL and AniList ids, type, year and cover.
- A **match picker** appears when a search returned several plausible entries. Switching keeps
  that row's status, score, progress and dates.
- **Fix unmatched** steps through every title that matched nothing, one at a time. For each it
  shows what you pasted, a prefilled search box, and the **candidates it found** so you choose
  rather than the tool guessing. The box accepts:
  - a title to search,
  - a pasted **anilist.co** or **myanimelist.net** link, which resolves to exactly that entry,
  - a bare id, looked up as both an AniList id and a MAL id (a dropdown pins it to one if you
    know which you have).

  Candidates are labelled `MAL 102255` or `AniList 148167 - no MAL ID`, so an AniList-only match
  is visibly usable rather than looking broken. Choosing one keeps that row's status, score,
  progress and dates, and moves to the next unmatched title.
- Per-row **status** and **read/watched progress**, plus an expandable editor (`⋯`) for score,
  start and finish dates, tags and comments.
- Sortable columns, status chips, text filter, and a 300-row cap with "Show all".
- The window can be narrowed to a phone width: the table scrolls sideways rather than crushing the
  title column, and the action rows stack instead of running off the edge.
- Keyboard and screen-reader usable throughout: sortable headers take focus and respond to
  Enter/Space while announcing their direction, every control has a name, the dialogs move focus
  in and hand it back on close, and `prefers-reduced-motion` turns the transitions off.
- Everything survives a refresh. **Start over** clears the saved session (two clicks, on purpose);
  loading `#reset` clears it too if the page is ever stuck.
- Unchecking a row excludes it from *this* list. **Always skip unchecked** makes that stick, so the
  same titles stay unchecked the next time you paste them — kept per media type, and undone by
  **Forget N skipped** beside it. Unchecking alone never changes anything permanently.

Nothing disappears silently: the notice bar accounts for titles with no MAL id, titles found
nowhere, lines merged into an existing row, and any title AniList could not be reached for.

### Getting an AniList token

AniList has no "copy my API key" button — a token is minted by its OAuth implicit grant, which
takes about a minute and no server of your own. Tokens are valid for **one year**; AniList does
not issue refresh tokens, so when one expires you repeat step 4.

| # | Where | What to do |
|---|---|---|
| 1 | [anilist.co/settings/developer](https://anilist.co/settings/developer) | **Create New Client** |
| 2 | the client form | Name it anything. Set **Redirect URL** to `https://anilist.co/api/v2/oauth/pin` |
| 3 | after saving | Copy the **Client ID** — a short number, not a secret |
| 4 | your browser | Open `https://anilist.co/api/v2/oauth/authorize?client_id=YOUR_ID&response_type=token` and approve |
| 5 | the pin page | AniList prints the token on screen. Copy all of it — it is several hundred characters |

Paste it into the token box and press **Check**. A working token answers with
*"Connected as YourName."* — that round trip is the only confirmation that the token is live and
bound to the account you expect, so do it before every write.

> [!TIP]
> The redirect URL is why step 2 matters. The implicit grant returns the token in the *fragment*
> of whatever URL you registered, so a normal site would need a page there to read it. AniList's
> own `/oauth/pin` endpoint simply displays it instead, which is what makes a local-only tool
> like this one workable.

### Writing to AniList

With the destination set to AniList, paste an access token and press **Check**. The token is held
in that browser tab only: it is never saved to disk, never written to the export files, and never
appears in a server response.

**Write to AniList…** first reads your existing list, then shows **exactly which fields change
on which rows** before anything is sent — *"episodes 224 → 142"*, not *"1 update"*. Entries you
already have are updated in place by their existing list-entry id, so running the write twice
cannot create duplicates. Failures are reported per title using AniList's own validation messages.

Nothing goes backwards unless you say so. The confirm step applies a conflict policy and shows it:

| Field | What the write will not do |
|---|---|
| Episodes / chapters / volumes | Never write a number lower than the account's |
| Score | Never write `0` over a score you already have. Any real score replaces another |
| Rereads / rewatches | Never lower the account's count |
| Status | Never replace **Completed** with anything except a reread |
| Notes, start date, finish date | Only ever sent when your row carries one, so a blank row never blanks them |

Rows go out **eight per request**, so a 339-row write takes about ninety seconds rather than
twelve minutes. One request, eight entries, one unit of rate limit — measured, not assumed.

**Cancel run** stops a write, an undo or a copy. Because rows travel eight at a time a cancel
lands *between* batches: the eight already in flight still get written, the next batch never
starts, and what did land stays undoable. Measured live — cancelling a 14-row write mid-flight
stopped it at exactly 8.

Each blocked field carries a **keep mine** tick, so a downgrade you actually want is one click
away. Rows that already match collapse to a count. A row that stops being completed is flagged,
because AniList keeps the old finish date otherwise — **clear the finish date** is offered there,
and is the only thing in this tool that sends an empty value.

The diff shows what your account will end up holding, not merely what gets sent, because those
are not always the same thing. AniList changes two fields on its own, whatever it is asked for,
and both are measured rather than assumed ([measured live](MANGA_SUPPORT_NOTES.md#-measured-against-the-live-api), L7–L8):

- While an entry is **Completed**, progress and volumes are forced to the work's total. So
  "keep mine" on a lower chapter count does nothing unless you also move the status off Completed.
- Finishing a reread bumps the reread count by one.

Fields in that situation are labelled **"AniList sets this itself on this status change"**, so the
confirm step never promises a change the API will quietly discard.

> [!TIP]
> **Undo last write…** puts the account back the way it was. Every write first saves what it is
> about to replace to `.aniexport_undo_<timestamp>.json` beside the cache — notes and dates
> included, never the token — and the five most recent are kept. Entries the run *created* have no
> previous state, so undoing those means deleting them: that is listed separately and happens only
> if you tick it. **Retry failed** re-sends just the rows that failed, without rebuilding them.

> [!WARNING]
> **What the guard does not cover.**
>
> - **Undo is itself a write and can fail halfway.** Each row is reported on its own; if you
>   deleted an entry on AniList after the write, that row cannot be restored and says so.
> - **A restored "no start date" may not stick.** AniList stamps a start date itself when an entry
>   becomes in-progress without one — measured, not assumed ([measured live](MANGA_SUPPORT_NOTES.md#-measured-against-the-live-api), M7).
> - **Two things have still never run against a real account:** cancelling a write part-way, and
>   a write big enough to need more than one 50-id lookup batch. Everything else here — the guard,
>   every override, the stale-date clear, undo in both directions, snapshot pruning, deleting a
>   created entry, a per-row rejection and the retry — was measured on a throwaway account
>   ([measured live](MANGA_SUPPORT_NOTES.md#-measured-against-the-live-api), P1–P12), on top of the mocked test suite.

### Reading a list back out of AniList

**From AniList…** pulls an existing list straight in — status, score, progress, notes and dates —
and merges it with whatever is already on screen. Nothing is written to any account.

- A **public** list needs nothing but the username.
- Leave the username blank and it reads **your own** list using the token, which is the only way
  to get at a private one.
- Entries AniList has but MyAnimeList does not are counted and kept, not dropped: they are the
  same *AniList only* rows the rest of the tool reports.

The CLI has the read-only half of this as `--from-anilist USERNAME`, which writes the list
straight to MAL XML:

```bash
python generate_mal_xml.py --from-anilist someone --media-type manga
```

That makes **AniList → MyAnimeList** possible, which it was not before: previously titles could
only travel in one direction.

### Copying one account onto another

**Copy an account…** reads **every anime and every manga** from one AniList account and writes
them to the account whose token is in the panel above. Use it to move to a new account, or to
mirror one you control.

What travels: status, score, progress, volumes, rereads, notes, start and finish dates, and the
`private`, `hidden from status lists` and `priority` flags — everything AniList will both hand
back and accept.

**Check** first. It names both accounts and, per media type, shows what will be added, what will
be overwritten, and what the destination has that the source does not:

```
From Razed2Ground to second-account.
Anime: 3 on the source, 3 to add, 0 to overwrite.
Manga: 14 on the source, 11 to add, 3 to overwrite, 1 already identical.
       6 on the destination are not on the source and will be left as they are.
```

> [!WARNING]
> A copy **mirrors**: where both accounts already have a title, the source wins, including values
> that go backwards. This is the one place the never-go-backwards guard is deliberately switched
> off, because "an exact copy" means exactly that. It is saved and undoable like any other write —
> as two snapshots, one per media type, and Undo replays both.
>
> Nothing is ever deleted. Entries the destination has and the source does not are untouched, so
> the destination ends up a **superset** rather than an identical twin. The Check step says how
> many.

Three things cannot be copied, because AniList will not hand them back in a form it will accept:

| Not copied | Why |
|---|---|
| Custom lists | Read as a JSON object, written as a list of names, and the destination would need the same lists defined first |
| Advanced scores | Read keyed by category; the destination's categories may differ, so there is no safe mapping |
| "No start date" on an in-progress entry | AniList stamps today's date itself when an entry becomes in-progress without one — measured, and not something this tool can override |

### Importing an existing MAL export

**Import XML** reads a MyAnimeList export (`.xml`, or `.gz` in browsers with
`DecompressionStream`). Matching rows are updated from the file; unknown ids are appended unless
you turn that off. Imported rows carry no AniList id until you resolve or search for them.

### Export files

`Download XML` / `Download .XML.GZ` serve the current selection as a normal browser download, so
**your browser picks where the file goes** — the copy kept beside the script is a convenience, not
the delivery. Anime and manga write to separate files, so one media type can never overwrite or
impersonate the other. **Preview XML** shows the generated file, and **Keep timestamped copies**
also writes `..._YYYYmmdd_HHMMSS.xml` on every save.

**List Status** and **Score (0-10)** apply to the rows you have selected, or to every row the
filter is showing when nothing is selected. Each says which, under the control. They used to stamp
every row in the list on every keystroke.

There is deliberately no "export to this path" field. The server would have to accept a filesystem
path from a web page, and any page in your browser can post to a local port.

## ⌨️ CLI

```bash
python generate_mal_xml.py manga.txt --media-type manga --status Reading
python generate_mal_xml.py animes.txt --expand-sequels --no-cache
```

| Flag | Meaning |
|---|---|
| `-o/--output`, `-g/--gz-output` | output paths (default depends on media type) |
| `--status` | default list status (`Reading`/`Watching`, `Plan to read/watch`, `Completed`, `On-Hold`/`Paused`, `Dropped`, `Rereading`/`Rewatching`) |
| `--score` | default score, 0–10 |
| `--media-type` | `anime` (default) or `manga` |
| `--expand-sequels` | follow direct sequel relations |
| `--no-cache` | ignore and do not write the on-disk cache |

The CLI reports three outcomes rather than two: titles in the export, titles that exist on AniList
without a MyAnimeList id (named with their AniList id, since only the GUI's direct write can send
them), and titles that were not found at all.

> [!NOTE]
> The CLI has no write path — it produces XML files. Sending titles to an AniList account is
> GUI-only.

## 📐 Data notes

- Unknown chapter, volume and episode counts stay `0`. Counts are never invented.
- Completed entries (and in-progress rereads) default to the full known count; everything else
  starts at zero. An explicit `0` is kept.
- Dates must be `YYYY-MM-DD`. Invalid input is rejected with an error rather than silently dropped.
- `Paused` maps to MAL's `On-Hold`; `Rereading`/`Rewatching` map to `Reading`/`Watching` plus the
  repeat flag in XML, and to `REPEATING` on AniList.

## ⚠️ Limitations

- **Misspellings are not auto-recoverable.** AniList returns zero hits for `Raphsody` or
  `Aristrocat`, so no destination, retry or ranking change finds them on its own. Fix unmatched is
  the answer: search the right title, or paste the AniList link.
- **A confident match can still be wrong, and the tool can only sometimes tell.** A
  **Needs review** chip flags rows worth a second look, but it is deliberately quiet: calibrated
  against a real 339-title list it flags about 3%. Flagging on title similarity alone would have
  flagged **half the list**, because a correct match often shares almost no characters with what
  you pasted — `First God Of War` is really `Di Yi Zhan Shen`. What it does catch reliably is a
  match that scored *worse* on your title than one the ranker passed over. Some wrong matches are
  not detectable at all; check the picker on rows that matter.
- **An overwrite you confirm is still an overwrite.** The policy blocks a value going backwards,
  not a value going forwards: a row at chapter 300 replaces an account at 224 without comment,
  because that is an ordinary edit. The diff shows it either way.
- **Deleting an entry from your AniList account is only possible as part of an undo.** The write
  creates and updates; undo can delete the entries that same run created, if you tick it. Nothing
  else here removes anything from your list, and unchecking a row never does.
- **One unwritable row used to take its whole batch down.** AniList validates a batched mutation
  document before running any of it, so a single stale media id made all eight fail. Fixed: the
  rows that did not execute are re-sent one at a time, so the good ones land and only the bad one
  is reported. It costs extra requests, but only when something is actually wrong.
- **The page refuses requests from other websites.** The server is local and has no login, so any
  page you had open could otherwise POST to it — not to your AniList account, which needs the
  token that only this page holds, but enough to spend your rate limit or overwrite an export.
- AniList's own rate limit is the floor on speed; nothing here can make a cold run faster than the
  API allows.

Known defects, each with the code that causes them, are listed in
[MANGA_SUPPORT_NOTES.md](MANGA_SUPPORT_NOTES.md#-known-defects).

## 🧪 Tests

```bash
python -m unittest discover -s tests -t .
```

**333 tests** covering the XML builders, status and date handling, batched resolution, the disk
cache, candidate ranking, run control, the GUI event contract, the AniList write payloads, the
conflict policy (one case per rule, including the blank-progress and score-zero cases that
motivate it), the per-row diff, the undo snapshot and its restore and delete paths, retrying
failed rows, the MAL XML importer, the unmatched-title resolver (links, ids and free text), the
command line (both media types, input discovery, cache flags, error paths), the no-store headers
on the page and the downloads, the persistent skip list's wiring, and the markup's accessibility
contract (header scope, focusable sortable headers,
dialog roles, named fields) and its copy (the primary action naming the chosen destination, no
stale claims about unbuilt features).

The browser JavaScript is additionally syntax-checked, and the HTML template is scanned for
backslash escapes that Python would consume before the browser ever sees them.

## 📚 Project docs

| Document | What it holds |
|---|---|
| [MANGA_SUPPORT_NOTES.md](MANGA_SUPPORT_NOTES.md) | Every change, what was measured against the live API to prove it, and the defects found along the way |

The planning document that drove the write-safety, batching, list-reading and review work was
deleted once all four phases shipped and were verified; it lives in git history
(`git show 7e90450:ROADMAP.md`). Its evidence tables moved into the notes above, which is what the
code comments cite.

<div align="center">

MIT licensed · Built against AniList's public GraphQL API

</div>
