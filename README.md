# AniExport

> **Zero-dependency plain-text anime and manga list converter for MyAnimeList-compatible XML and AniList import.**

AniExport accepts a title list, resolves titles against AniList's GraphQL API, lets you review results in a local web dashboard, and exports MAL-compatible XML and gzip files. AniList imports MAL XML by matching MAL IDs, so a resolved title without a valid MAL ID is not exportable through this path.

## Features

- Anime and manga media modes in the same GUI and CLI.
- Anime metadata: MAL/AniList IDs, title, format, episode count.
- Manga metadata: MAL/AniList IDs, title, format, chapter count, volume count.
- Separate anime and manga XML schemas and status vocabularies.
- Numbering/bullet cleanup for plain-text input.
- Optional direct sequel relation expansion (disabled by default).
- Review table with MAL and AniList links, filters and per-entry inclusion controls.
- Unresolved results have no valid MAL ID, are disabled in the GUI, and are excluded from export rather than exported with ID `0`.
- Unknown episode/chapter/volume counts remain `0` (unknown); values are not invented.
- Standard-library-only Python implementation.

## Requirements

- Python 3.9+
- Internet access to `https://graphql.anilist.co`

## GUI

```bash
python gui.py
```

Open `http://127.0.0.1:5000` if the browser does not open automatically. Use the **Media Type** selector to choose Anime or Manga. Changing the media type clears the current selection and generated export to avoid accidentally mixing media types.

Manga statuses are `Reading`, `Completed`, `Plan to Read`, `On-Hold`, and `Dropped`. Anime statuses are `Watching`, `Completed`, `Plan to Watch`, `On-Hold`, and `Dropped`.

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

The manga CLI defaults to `myanimelist_manga_export.xml` and `myanimelist_manga_export.xml.gz` (or corresponding filenames inside `output/` when that directory exists). `-o` and `-g` override the output paths.

## Input format

One title per line. Leading numbering, bullets, and surrounding quotes are removed:

```text
Berserk
1. One Piece
2) Vagabond
- Fullmetal Alchemist
```

## Import

1. Open [AniList import settings](https://anilist.co/settings/import).
2. Choose **Import from MyAnimeList**.
3. Upload the generated `.xml` or `.xml.gz`.
4. Review the import result on AniList before assuming every entry was added.

The generated files are MAL-compatible import files; XML parsing alone cannot guarantee that AniList has a matching entry for every MAL ID. Always review skipped or unresolved entries.

## Tests

Run the offline unit tests (no network access required):

```bash
python -m unittest discover -s tests -v
```

The tests cover manga XML structure, status mapping, missing IDs, XML escaping, anime regression, and duplicate handling.

## Limitations

- Title resolution currently accepts AniList's top search result; it does not score multiple candidates for ambiguity. Review the displayed title, format, and MAL/AniList IDs before exporting. The current UI does not show release year or offer a candidate picker, so exclude any questionable match rather than assuming it is correct.
- A MAL ID is required for this import route. If AniList has a manga entry without a MAL ID, it will be shown but cannot be imported through MAL XML.
- Sequel expansion follows direct `SEQUEL` relations and is not a complete franchise crawler. It is optional and should be reviewed.
- The tool generates import files; it does not sign in to or modify either account directly.

## License

See [LICENSE](LICENSE).
