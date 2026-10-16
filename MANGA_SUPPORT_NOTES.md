# Manga support implementation notes

## Design
- Preserved the existing anime resolver and XML builder API.
- Added separate AniList GraphQL queries restricted to `type: MANGA`.
- Added manga parsing for MAL ID, AniList ID, title, format, chapters and volumes.
- Added a separate manga XML builder using the manga entry element and field names:
  `manga_mangadb_id`, `manga_title`, `manga_volumes`, `manga_chapters`,
  `my_read_volumes`, `my_read_chapters`, and manga status metadata.
- Added the GUI media selector and media-specific statuses, table labels, links,
  progress metadata, XML builder selection and download names.
- The GUI disables unresolved rows (no positive MAL ID); both XML builders also
  omit invalid MAL IDs so ID `0` is not falsely exported as importable.
- Unknown chapter/volume counts remain `0`; completed manga with known totals
  defaults read progress to those totals, while other statuses default to zero.
- Status strings are validated, mapped between anime and manga vocabularies, and emitted with canonical MAL capitalization.
- Manga XML emits exactly one start-date and one finish-date element per entry.
- XML CDATA values safely handle embedded `]]>` sequences.

## Verification performed
- Python syntax compilation for generator and GUI.
- JavaScript syntax check with Node.js.
- Eight offline unit tests covering XML structure, title escaping, canonical status mapping,
  unresolved-ID exclusion, manga query selection, duplicate IDs, and a mocked GUI
  manga-process endpoint that writes XML and gzip.
- Python bytecode compilation, JavaScript syntax validation with Node.js, and CLI `--help` smoke test.
- GUI endpoint smoke test uses a mocked resolver; it does not make live AniList
  requests.

## Not verified
- No live AniList API requests or real account imports were performed in this
  environment. The generated file is structurally tested against an informal
  public description of the MAL XML format, but an actual AniList import should
  still be tested with a small sample first.
- Title resolution still accepts AniList's top search result; ambiguous title
  disambiguation and a candidate-picker UI are not implemented. The table shows the
  resolved title, format, and IDs, but not release year. Exclude uncertain matches
  manually; a correct match cannot currently be selected from alternate candidates.
- This change modifies the local ZIP copy only; it has not been pushed to the
  upstream AniExport repository.
