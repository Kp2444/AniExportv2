# AniExport

> **Zero-dependency converter from a plain list of titles to an AniList or MyAnimeList list.**

Paste a list of anime or manga titles. AniExport resolves each one against AniList's GraphQL API,
lets you review and correct the matches in a local web page, and then either writes them straight
to your AniList account or exports a MAL-compatible XML file.

## Two destinations, and why it matters

AniList is the primary target. MAL XML is only a transport, and it is a lossy one:

| Destination | Identity required | Needs a token | Carries |
|---|---|---|---|
| **AniList (direct write)** | AniList id | yes | every title AniList knows |
| **MAL-compatible XML** | MyAnimeList id | no | only titles that also exist on MAL |

Plenty of titles exist on AniList with no MyAnimeList id at all. Those rows are labelled
**AniList only**: they cannot be written into the XML file, but they can be sent directly to your
account. A row is only called **Not found** when AniList does not know it either.

You pick the destination on first run; it is remembered after that.

## Install

Python 3.9+ and an internet connection. Nothing else: `requirements.txt` installs no packages.

## GUI

```bash
python gui.py              # opens http://127.0.0.1:5000
```

- `-p/--port` — local port (default 5000, scanning up to 5049)
- `--no-browser` — do not open a browser

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

Nothing disappears silently: the notice bar accounts for titles with no MAL id, titles found
nowhere, lines merged into an existing row, and any title AniList could not be reached for.

### Writing to AniList

With the destination set to AniList, paste an access token and press **Check**. The token is held
in that browser tab only: it is never saved to disk, never written to the export files, and never
appears in a server response.

**Write to AniList…** first reads which of the selected titles are already on your list, then
shows exactly what will happen (*"N entries will be added, M already on your list will be
updated"*) before anything is sent. Entries you already have are updated in place by their
existing list-entry id, so running the write twice cannot create duplicates. Failures are reported
per title using AniList's own validation messages.

> **Status:** the write path is covered by unit tests against a mocked API but has **not** yet been
> exercised against a live AniList account. Try it on a throwaway account, or with a handful of
> rows, before pointing it at a list you care about.

### Importing an existing MAL export

**Import XML** reads a MyAnimeList export (`.xml`, or `.gz` in browsers with
`DecompressionStream`). Matching rows are updated from the file; unknown ids are appended unless
you turn that off. Imported rows carry no AniList id until you resolve or search for them.

### Export files

`Download XML` / `Download .XML.GZ` serve the current selection. Anime and manga write to separate
files, so one media type can never overwrite or impersonate the other. **Preview XML** shows the
generated file, and **Keep timestamped copies** also writes `..._YYYYmmdd_HHMMSS.xml` on every save.

## CLI

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

## Data notes

- Unknown chapter, volume and episode counts stay `0`. Counts are never invented.
- Completed entries (and in-progress rereads) default to the full known count; everything else
  starts at zero. An explicit `0` is kept.
- Dates must be `YYYY-MM-DD`. Invalid input is rejected with an error rather than silently dropped.
- `Paused` maps to MAL's `On-Hold`; `Rereading`/`Rewatching` map to `Reading`/`Watching` plus the
  repeat flag in XML, and to `REPEATING` on AniList.

## Limitations

- **Misspellings are not auto-recoverable.** AniList returns zero hits for `Raphsody` or
  `Aristrocat`, so no destination, retry or ranking change finds them on its own. Fix unmatched is
  the answer: search the right title, or paste the AniList link.
- **A confident match can still be wrong.** Ranking prefers exact titles, full series over
  one-shots and released entries, but it cannot know your intent. Check the picker on rows that
  matter.
- The direct AniList write has not been run against a live account (see above).
- AniList's own rate limit is the floor on speed; nothing here can make a cold run faster than the
  API allows.

## Tests

```bash
python -m unittest discover -s tests -t .
```

127 tests covering the XML builders, status and date handling, batched resolution, the disk cache,
candidate ranking, run control, the GUI event contract, the AniList write payloads, the MAL XML importer,
the unmatched-title resolver (links, ids and free text)
the command line (both media types, input discovery, cache flags, error paths), the
no-store headers on the page and the downloads, and the markup's accessibility contract
(header scope, focusable sortable headers, dialog roles, named fields) and its copy (the primary
action naming the chosen destination, no stale claims about unbuilt features). The browser JavaScript is
additionally syntax-checked, and the HTML template is scanned for backslash escapes that Python
would consume before the browser ever sees them.
