"""Building the MyAnimeList XML a list can be imported from.

Split out of generate_mal_xml.py, which was three thousand lines doing five
jobs. Nothing here touches the network.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Sequence


def deduplicate_entries(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicates entries preserving first occurrence order.
    Deduplicates by ID if non-zero, otherwise by title.
    """
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in entries:
        anime_id = item.get("id", 0)
        key = anime_id if anime_id != 0 else item.get("title", "").strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# AniList offers "Paused" and "Rereading"/"Rewatching"; MAL's XML has neither.
# Paused is MAL's On-Hold, and a reread is MAL's Reading plus the my_rereading flag.
REREAD_STATUSES = {"rereading", "re-reading", "rewatching", "re-watching"}


def _clamp_progress(value: Any, total: Any) -> int:
    """Never below zero, never above a known total (0 means unknown)."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    number = max(0, number)
    try:
        ceiling = int(total or 0)
    except (TypeError, ValueError):
        ceiling = 0
    return min(number, ceiling) if ceiling > 0 else number


def _mal_date(value: Any) -> str:
    """Validate an entry date for MAL XML.

    Empty/missing stays MAL's "unset" placeholder, so every existing caller is
    unaffected. Anything else must be a real YYYY-MM-DD date; a malformed value
    raises rather than silently exporting a date MAL will reject.
    """
    text = str(value or "").strip()
    if not text or text == "0000-00-00":
        return "0000-00-00"
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date {value!r}: expected YYYY-MM-DD") from None
    # strptime accepts unpadded input like 2024-3-1; MAL wants it zero-padded, so
    # round-trip the parse and reject anything that does not come back identical.
    if parsed.strftime("%Y-%m-%d") != text:
        raise ValueError(f"Invalid date {value!r}: expected zero-padded YYYY-MM-DD")
    return text


def _is_repeat_status(status: Any) -> bool:
    """True when the user picked AniList's Rereading/Rewatching state."""
    return str(status or "").strip().casefold() in REREAD_STATUSES


def _normalize_status(status: Any, media_type: str) -> str:
    value = str(status or "Completed").strip()
    folded = value.casefold()
    shared = {"paused": "On-Hold", "on hold": "On-Hold", "onhold": "On-Hold"}
    if media_type == "manga":
        aliases = {"watching": "Reading", "plan to watch": "Plan to Read", "plantowatch": "Plan to Read",
                   "plan to read": "Plan to Read", "plantoread": "Plan to Read",
                   "rereading": "Reading", "re-reading": "Reading",
                   "rewatching": "Reading", "re-watching": "Reading", **shared}
        allowed = ["Reading", "Completed", "Plan to Read", "On-Hold", "Dropped"]
    else:
        aliases = {"reading": "Watching", "plan to read": "Plan to Watch", "plantoread": "Plan to Watch",
                   "plan to watch": "Plan to Watch", "plantowatch": "Plan to Watch",
                   "rewatching": "Watching", "re-watching": "Watching",
                   "rereading": "Watching", "re-reading": "Watching", **shared}
        allowed = ["Watching", "Completed", "Plan to Watch", "On-Hold", "Dropped"]
    normalized = aliases.get(folded, value)
    canonical = {item.casefold(): item for item in allowed}
    if normalized.casefold() not in canonical:
        raise ValueError(f"Unsupported {media_type} list status: {value!r}")
    # Emit MAL's canonical status spelling even when CLI input uses different casing.
    return canonical[normalized.casefold()]


def build_mal_xml(
    entries: Sequence[Dict[str, Any]],
    default_status: str = "Completed",
    default_score: int = 0,
) -> str:
    """
    Builds the MAL-compatible anime XML. Entries without a positive MAL ID are omitted.
    """
    entries = [dict(e,
                    status=_normalize_status(e.get("status", default_status), "anime"),
                    _rewatching=_is_repeat_status(e.get("status", default_status)))
               for e in entries if int(e.get("id") or 0) > 0]
    total_anime = len(entries)
    completed_count = sum(1 for e in entries if e.get("status", default_status).lower() == "completed")
    watching_count = sum(1 for e in entries if e.get("status", default_status).lower() == "watching")
    onhold_count = sum(1 for e in entries if e.get("status", default_status).lower() in ("on-hold", "onhold"))
    dropped_count = sum(1 for e in entries if e.get("status", default_status).lower() == "dropped")
    plantowatch_count = sum(1 for e in entries if e.get("status", default_status).lower() in ("plan to watch", "plantowatch"))

    lines: List[str] = [
        '<?xml version="1.0" encoding="UTF-8" ?>',
        '<!--',
        '  Created by XML Export Feature',
        '  Program: MyAnimeList.net',
        f'  Entry count: {total_anime}',
        '-->',
        '<myanimelist>',
        '  <myinfo>',
        '    <user_id></user_id>',
        '    <user_name></user_name>',
        '    <user_export_type>1</user_export_type>',
        f'    <user_total_anime>{total_anime}</user_total_anime>',
        f'    <user_total_watching>{watching_count}</user_total_watching>',
        f'    <user_total_completed>{completed_count}</user_total_completed>',
        f'    <user_total_onhold>{onhold_count}</user_total_onhold>',
        f'    <user_total_dropped>{dropped_count}</user_total_dropped>',
        f'    <user_total_plantowatch>{plantowatch_count}</user_total_plantowatch>',
        '  </myinfo>',
    ]

    for entry in entries:
        anime_id = entry.get("id", 0)
        title = entry.get("title", "")
        series_type = entry.get("type", "TV")
        eps = entry.get("episodes", 0)
        # Only a finished (or in-progress rewatch) entry has every episode watched;
        # Plan to Watch / Dropped must not import as fully watched.
        fully_watched = str(entry.get("status", default_status)).casefold() == "completed" or entry.get("_rewatching")
        watched_eps = _clamp_progress(entry.get("my_watched_episodes", eps if fully_watched else 0), eps)
        status = entry.get("status", default_status)
        score = max(0, min(10, int(entry.get("score", default_score) or 0)))
        comments = entry.get("comments", "")
        tags = entry.get("tags", "")

        lines.extend([
            '  <anime>',
            f'    <series_animedb_id>{anime_id}</series_animedb_id>',
            f'    <series_title>{_cdata(title)}</series_title>',
            f'    <series_type>{series_type}</series_type>',
            f'    <series_episodes>{eps}</series_episodes>',
            f'    <my_watched_episodes>{watched_eps}</my_watched_episodes>',
            f'    <my_start_date>{_mal_date(entry.get("start_date"))}</my_start_date>',
            f'    <my_finish_date>{_mal_date(entry.get("finish_date"))}</my_finish_date>',
            '    <my_rated></my_rated>',
            f'    <my_score>{score}</my_score>',
            '    <my_storage></my_storage>',
            f'    <my_status>{status}</my_status>',
            f'    <my_comments>{_cdata(comments)}</my_comments>',
            f'    <my_times_watched>{1 if fully_watched else 0}</my_times_watched>',
            '    <my_rewatch_value></my_rewatch_value>',
            '    <my_priority>LOW</my_priority>',
            f'    <my_tags>{_cdata(tags)}</my_tags>',
            f'    <my_rewatching>{1 if entry.get("_rewatching") else 0}</my_rewatching>',
            '    <my_rewatching_ep>0</my_rewatching_ep>',
            '    <update_on_import>1</update_on_import>',
            '  </anime>',
        ])

    lines.append('</myanimelist>')
    lines.append('')
    return '\r\n'.join(lines)


def _cdata(value: Any) -> str:
    """Return a CDATA section that remains valid even if the value contains ]]>."""
    return "<![CDATA[" + str(value or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_mal_manga_xml(
    entries: Sequence[Dict[str, Any]],
    default_status: str = "Completed",
    default_score: int = 0,
) -> str:
    """Build a MAL-compatible manga list XML document for AniList import.

    Entries without a positive MAL manga ID are omitted because AniList's MAL
    import route matches by MAL ID; exporting ID 0 would create a false success.
    """
    valid_entries = [
        dict(e,
             status=_normalize_status(e.get("status", default_status), "manga"),
             _rereading=_is_repeat_status(e.get("status", default_status)))
        for e in entries if int(e.get("id") or 0) > 0
    ]
    total = len(valid_entries)
    status_of = lambda e: str(e.get("status", default_status)).strip().lower()
    counts = {
        "reading": sum(status_of(e) == "reading" for e in valid_entries),
        "completed": sum(status_of(e) == "completed" for e in valid_entries),
        "onhold": sum(status_of(e) in ("on-hold", "onhold") for e in valid_entries),
        "dropped": sum(status_of(e) == "dropped" for e in valid_entries),
        "plantoread": sum(status_of(e) in ("plan to read", "plantoread") for e in valid_entries),
    }
    lines = [
        '<?xml version="1.0" encoding="UTF-8" ?>',
        '<!--',
        '  Created by AniExport',
        '  Program: MyAnimeList.net',
        f'  Entry count: {total}',
        '-->',
        '<myanimelist>',
        '  <myinfo>',
        '    <user_id></user_id>',
        '    <user_name></user_name>',
        '    <user_export_type>2</user_export_type>',
        f'    <user_total_manga>{total}</user_total_manga>',
        f'    <user_total_reading>{counts["reading"]}</user_total_reading>',
        f'    <user_total_completed>{counts["completed"]}</user_total_completed>',
        f'    <user_total_onhold>{counts["onhold"]}</user_total_onhold>',
        f'    <user_total_dropped>{counts["dropped"]}</user_total_dropped>',
        f'    <user_total_plantoread>{counts["plantoread"]}</user_total_plantoread>',
        '  </myinfo>',
    ]
    for entry in valid_entries:
        manga_id = int(entry.get("id") or 0)
        title = entry.get("title", "")
        chapters = int(entry.get("chapters") or 0)
        volumes = int(entry.get("volumes") or 0)
        status = entry.get("status", default_status)
        fully_read = str(status).casefold() == "completed" or entry.get("_rereading")
        default_chapters_read = chapters if fully_read else 0
        default_volumes_read = volumes if fully_read else 0
        # Clamp here, not only in the browser: a hand-built payload could claim
        # 999 of 10 chapters, which MAL and AniList both reject.
        read_chapters = _clamp_progress(entry.get("my_read_chapters", default_chapters_read), chapters)
        read_volumes = _clamp_progress(entry.get("my_read_volumes", default_volumes_read), volumes)
        score = max(0, min(10, int(entry.get("score", default_score) or 0)))
        comments = entry.get("comments", "")
        tags = entry.get("tags", "")
        lines.extend([
            '  <manga>',
            f'    <manga_mangadb_id>{manga_id}</manga_mangadb_id>',
            f'    <manga_title>{_cdata(title)}</manga_title>',
            f'    <manga_volumes>{volumes}</manga_volumes>',
            f'    <manga_chapters>{chapters}</manga_chapters>',
            '    <my_id>0</my_id>',
            '    <my_read_volumes>' + str(read_volumes) + '</my_read_volumes>',
            '    <my_read_chapters>' + str(read_chapters) + '</my_read_chapters>',
            f'    <my_start_date>{_mal_date(entry.get("start_date"))}</my_start_date>',
            f'    <my_finish_date>{_mal_date(entry.get("finish_date"))}</my_finish_date>',
            '    <my_scanalation_group></my_scanalation_group>',
            f'    <my_score>{score}</my_score>',
            '    <my_storage></my_storage>',
            '    <my_retail_volumes></my_retail_volumes>',
            f'    <my_status>{status}</my_status>',
            f'    <my_comments>{_cdata(comments)}</my_comments>',
            '    <my_times_read>0</my_times_read>',
            f'    <my_tags>{_cdata(tags)}</my_tags>',
            '    <my_priority>LOW</my_priority>',
            '    <my_reread_value></my_reread_value>',
            f'    <my_rereading>{"YES" if entry.get("_rereading") else "NO"}</my_rereading>',
            '    <my_discuss>YES</my_discuss>',
            '    <my_sns>default</my_sns>',
            '    <update_on_import>1</update_on_import>',
            '  </manga>',
        ])
    lines.extend(['</myanimelist>', ''])
    return '\r\n'.join(lines)
