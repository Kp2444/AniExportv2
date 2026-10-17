# AniExport

> **Zero-dependency plain-text anime and manga list converter for MyAnimeList-compatible XML and AniList import.**

AniExport accepts a title list, resolves titles against AniList's GraphQL API, lets you review results in a local web dashboard, and exports MAL-compatible XML and gzip files. AniList imports MAL XML by matching MAL IDs, so a resolved title without a valid MAL ID is not exportable through this path — such rows are shown as unresolved and excluded from export.

## Features

- Anime and manga media modes in the same GUI and CLI, with separate XML schemas and status vocabularies.
- Input cleanup: strips leading numbering (`1.`, `2)`), bullets (`-`, `*`, `•`), surrounding quotes, and skips `#` comment lines.
- Deduplication by MAL ID (by title when the ID is unknown).
- Import-aware resolution: search hits that carry a MAL ID are preferred over ones without, and full series are preferred over one-shots.
- An in-table match picker appears for rows with two or more importable candidates; switching a match keeps that row's status, score and progress.
- Per-entry progress inputs (watched episodes, or read chapters and volumes). Completed entries — and in-progress rereads/rewatches — default to the full known count, everything else defaults to zero. Explicitly set values, including `0`, are kept, and inputs are capped at the known total.
- Unknown episode/chapter/volume counts remain `0` (unknown); values are never invented.
- Unresolved titles are disabled in the GUI and excluded from export rather than exported with ID `0`.
- Optional direct sequel expansion (disabled by default): both media types via the CLI, anime only in the GUI.
- Review table with MAL and AniList links, filtering by title or ID, select-all with indeterminate state, and per-entry inclusion controls.
- Global status and score changes re-apply to every row; all exporting rows re-sync to the export files (debounced) as you edit.
- Live progress in the GUI: streamed step/percentage updates with an ETA, plus an AniList rate-limit wait banner.
- Unmatched titles get their own banner with a one-click "Copy unmatched titles" action.
- Standard-library-only Python implementation; XML output is validated by re-parsing before it is reported as generated.

## Requirements

- Python 3.9+
- Internet access to `https://graphql.anilist.co`
- Nothing else: the project runs entirely on the Python Standard Library (`requirements.txt` installs no extra packages).

The GUI page loads two Google Fonts for styling; if that request fails, the interface still renders with your system fonts.

## GUI

```bash
python gui.py
```

Open `http://127.0.0.1:5000` if the browser does not open automatically.

- `-p/--port` — local port (default 5000; occupied ports up to 5049 are skipped).
- `--no-browser` — do not open a browser automatically.

The server binds to `127.0.0.1` only and never signs in to or modifies any account.

The header includes a light/dark theme toggle; the choice is remembered in the browser.

Use the **Media Type** selector to choose Anime or Manga. Changing the media type clears the current selection and generated export to avoid accidentally mixing media types.

The status selector uses AniList's vocabulary; the server maps it to MAL's values on export:

| GUI status (AniList)      | Anime XML (MAL)                   | Manga XML (MAL)                |
| ------------------------- | --------------------------------- | ------------------------------ |
| Watching / Reading        | Watching                          | Reading                        |
| Plan to watch / read      | Plan to Watch                     | Plan to Read                   |
| Completed                 | Completed                         | Completed                      |
| Rewatching / Rereading    | Watching + rewatching flag        | Reading + rereading flag       |
| Paused                    | On-Hold                           | On-Hold                        |
| Dropped                   | Dropped                           | Dropped                        |

## CLI

Anime remains the default:

```bash
python generate_mal_xml.py anime_list.txt
python generate_mal_xml.py anime_list.txt --status Watching --score 8
python generate_mal_xml.py anime_list.txt --expand-sequels
```

Manga:

```bash
python generate_mal_xml.py manga_list.txt --media-type manga
python generate_mal_xml.py manga_list.txt --media-type manga --status Reading --score 8
python generate_mal_xml.py manga_list.txt --media-type manga --expand-sequels
```

Notes:

- With no positional input, `-i/--input-file` or default probing (`notes.txt`, then `animes.txt`, then `input/animes.txt`) is used.
- `--status` accepts MAL status names (`Watching`, `Plan to Watch`, `Completed`, `On-Hold`, `Dropped` for anime; `Reading`, `Plan to Read`, … for manga) plus the GUI aliases above, case-insensitively. Invalid values raise an error.
- Default output filenames depend on the media type: `myanimelist_export.xml` / `.xml.gz` for anime, `myanimelist_manga_export.xml` / `.xml.gz` for manga. When an `output/` directory exists, files are written there; `-o` and `-g` override the paths.

## Input format

One title per line. Leading numbering, bullets, and surrounding quotes are removed; blank lines and `#` comment lines are skipped:

```text
Berserk
1. One Piece
2) Vagabond
- Fullmetal Alchemist
# a comment
```

Duplicate titles resolve to a single entry. `manga.txt` in the repository is a larger example input.

## Import

1. Open [AniList import settings](https://anilist.co/settings/import).
2. Choose **Import from MyAnimeList**.
3. Upload the generated `.xml` or `.xml.gz`.
4. Review the import result on AniList before assuming every entry was added.

The generated files are MAL-compatible import files; XML parsing alone cannot guarantee that AniList has a matching entry for every MAL ID. Always review skipped or unresolved entries.

## Tests

Run the offline unit tests (no network access required — resolvers are mocked):

```bash
python -m unittest discover -s tests -v
```

Coverage includes manga and anime XML structure, status alias mapping and canonical casing, missing-ID exclusion, XML escaping, importable-candidate selection, duplicate handling, a mocked GUI manga-process endpoint that writes XML and gzip, and GUI template integrity. One test validates the GUI's JavaScript with Node.js and is skipped when `node` is not installed.

## Limitations

- Resolution ranking follows AniList's relevance order aside from preferring importable entries, and release year is not shown. A wrong match can still look plausible — review the displayed title and exclude anything you do not recognize.
- The match picker offers only candidates that carry a MAL ID; anything else cannot be imported through this route.
- A MAL ID is required for this import route. If AniList has a manga entry without a MAL ID, it will be shown but cannot be imported through MAL XML.
- Sequel expansion follows direct `SEQUEL` relations only and is not a complete franchise crawler. It is optional and should be reviewed. The GUI exposes it for anime only; the CLI supports it for manga as well.
- AniList allows 30 requests/minute. Requests are spaced accordingly, so long lists resolve at roughly one title every 2 seconds and wait out throttle windows when they occur.
- The tool generates import files; it does not sign in to or modify either account directly.

## License

See [LICENSE](LICENSE).
