"""
XML Generator
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ANILIST_API_URL = "https://graphql.anilist.co"

# AniList caps at 30 requests/minute; cap how long one title may wait on 429s.
MAX_RATE_LIMIT_WAITS = 6

# ponytail: process-wide lock serialises every AniList call to stay under the
# 30/min cap. Correct because the cap is per-IP, not per-thread. Swap for a
# token bucket only if AniList ever allows bursts.
MIN_REQUEST_INTERVAL = 2.1
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """Space out requests so AniList's 30/min limit is not tripped in the first place."""
    global _last_request_at
    with _throttle_lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()

# Per-thread hook so a caller (the GUI) can tell the user why a run is stalling.
_rate_limit_hook = threading.local()


def set_rate_limit_notifier(callback: Optional[Any]) -> None:
    """Register a callback(seconds) invoked whenever a request waits out a 429."""
    _rate_limit_hook.callback = callback

ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Media (search: $search, type: ANIME) {
    id
    idMal
    title {
      english
      romaji
    }
    format
    episodes
    status
    relations {
      edges {
        relationType
        node {
          id
          idMal
          title {
            english
            romaji
          }
          format
          episodes
          type
          status
        }
      }
    }
  }
}
"""

ANILIST_BY_ID_QUERY = """
query ($id: Int) {
  Media (id: $id, type: ANIME) {
    id
    idMal
    title {
      english
      romaji
    }
    format
    episodes
    status
    relations {
      edges {
        relationType
        node {
          id
          idMal
          title {
            english
            romaji
          }
          format
          episodes
          type
          status
        }
      }
    }
  }
}
"""


ANILIST_SEARCH_PAGE_QUERY = """
query ($search: String) {
  Page (perPage: 10) {
    media (search: $search, type: ANIME) {
      id
      idMal
      title { english romaji }
      format
      episodes
      status
    }
  }
}
"""

ANILIST_MANGA_SEARCH_QUERY = """
query ($search: String) {
  Media (search: $search, type: MANGA) {
    id
    idMal
    title { english romaji }
    format
    chapters
    volumes
    status
    relations {
      edges {
        relationType
        node {
          id
          idMal
          title { english romaji }
          format
          chapters
          volumes
          type
          status
        }
      }
    }
  }
}
"""

ANILIST_MANGA_SEARCH_PAGE_QUERY = """
query ($search: String) {
  Page (perPage: 10) {
    media (search: $search, type: MANGA) {
      id
      idMal
      title { english romaji }
      format
      chapters
      volumes
      status
    }
  }
}
"""

ANILIST_MANGA_BY_ID_QUERY = """
query ($id: Int) {
  Media (id: $id, type: MANGA) {
    id
    idMal
    title { english romaji }
    format
    chapters
    volumes
    status
    relations {
      edges {
        relationType
        node {
          id
          idMal
          title { english romaji }
          format
          chapters
          volumes
          type
          status
        }
      }
    }
  }
}
"""

MANGA_FORMAT_MAP = {
    "MANGA": "Manga",
    "NOVEL": "Novel",
    "ONE_SHOT": "One-shot",
    "DOUJIN": "Doujinshi",
    "MANHWA": "Manhwa",
    "MANHUA": "Manhua",
    "OEL": "OEL",
}

FORMAT_MAP = {
    "TV": "TV",
    "TV_SHORT": "TV",
    "MOVIE": "Movie",
    "SPECIAL": "Special",
    "OVA": "OVA",
    "ONA": "ONA",
    "MUSIC": "Music",
}


def sanitize_title_line(line: str) -> Optional[str]:
    """
    Cleans a line from an anime text file:
    - Removes leading numbering or bullet points (e.g., '1. ', '1) ', '- ')
    - Removes wrapping quotation marks and extra whitespace
    - Ignores comment lines starting with '#'
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    # Remove leading numbers/bullets if any (e.g., '1. ', '1- ', '1) ', '- ')
    cleaned = re.sub(r"^\s*(?:\d+[\.\-\)]|[-*•])\s*", "", stripped).strip()

    # Remove surrounding double/single quotes if present
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()

    return cleaned if cleaned else None


def load_titles_from_txt(file_path: Path) -> List[str]:
    """
    Reads a text file with one anime title per line (no numbering required).
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"Anime list file not found: {file_path}")

    titles: List[str] = []
    with open(file_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            cleaned = sanitize_title_line(line)
            if cleaned:
                titles.append(cleaned)
    return titles


def _parse_media_node(media: Dict[str, Any], fallback_title: str = "") -> Dict[str, Any]:
    titles = media.get("title") or {}
    official_title = titles.get("english") or titles.get("romaji") or fallback_title
    raw_format = (media.get("format") or "TV").upper()
    series_type = FORMAT_MAP.get(raw_format, "TV")
    mal_id = int(media.get("idMal") or 0)
    anilist_id = int(media.get("id") or 0)
    eps = int(media.get("episodes") or 0)
    return {
        "id": mal_id,
        "anilist_id": anilist_id,
        "title": official_title,
        "type": series_type,
        "episodes": eps,
    }


def _candidates(nodes: Sequence[Dict[str, Any]], media_type: str, fallback_title: str) -> List[Dict[str, Any]]:
    """Exportable alternatives for a search, so a wrong auto-pick can be corrected.

    Only nodes carrying a MAL ID are offered: anything else cannot be imported,
    so presenting it as a choice would be a dead end.
    """
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for node in nodes:
        if not node or not node.get("idMal") or node.get("idMal") in seen:
            continue
        seen.add(node["idMal"])
        out.append(parse(node, fallback_title=fallback_title))
    return out


def _pick_best_media(nodes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose the most importable search hit.

    AniList's top relevance hit is frequently a duplicate entry or a one-shot
    spin-off with ``idMal: null``, which cannot be imported into MAL/AniList at
    all. Prefer any candidate that carries a MAL ID, and among those prefer a
    full series over a one-shot, keeping AniList's relevance order otherwise.
    """
    usable = [n for n in nodes if n and n.get("idMal")]
    if not usable:
        return nodes[0] if nodes else None
    full_series = [n for n in usable if (n.get("format") or "").upper() != "ONE_SHOT"]
    return (full_series or usable)[0]


def _parse_manga_node(media: Dict[str, Any], fallback_title: str = "") -> Dict[str, Any]:
    """Convert an AniList manga node into a MAL-compatible export entry."""
    titles = media.get("title") or {}
    official_title = titles.get("english") or titles.get("romaji") or fallback_title
    raw_format = (media.get("format") or "MANGA").upper()
    series_type = MANGA_FORMAT_MAP.get(raw_format, "Manga")
    return {
        "id": int(media.get("idMal") or 0),
        "anilist_id": int(media.get("id") or 0),
        "title": official_title,
        "type": series_type,
        "chapters": int(media.get("chapters") or 0),
        "volumes": int(media.get("volumes") or 0),
    }


def _execute_anilist_request(query: str, variables: Dict[str, Any], max_retries: int = 3, result_key: str = "Media") -> Optional[Dict[str, Any]]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MALAnimeExporter/2.0",
    }

    attempt = 0
    rate_limit_waits = 0
    while attempt < max_retries:
        try:
            _throttle()
            req = urllib.request.Request(ANILIST_API_URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                result = json.loads(response.read().decode("utf-8"))
                media = (result.get("data") or {}).get(result_key)
                # AniList can report throttling/outages in a 200 body; retry those
                # instead of mistaking them for "no such title".
                statuses = {e.get("status") for e in (result.get("errors") or []) if isinstance(e, dict)}
                if media is None and any(st == 429 or (isinstance(st, int) and st >= 500) for st in statuses):
                    raise RuntimeError(f"AniList transient error: {sorted(st for st in statuses if st)}")
                return media
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # AniList allows 30 requests/minute. Honour Retry-After and do not
                # spend a retry attempt on it, or long lists lose most of their titles.
                if rate_limit_waits >= MAX_RATE_LIMIT_WAITS:
                    return None
                rate_limit_waits += 1
                try:
                    wait = float(e.headers.get("Retry-After") or 60)
                except ValueError:
                    wait = 60.0
                delay = min(max(wait, 1.0) + 1.0, 70.0)
                callback = getattr(_rate_limit_hook, "callback", None)
                if callback:
                    try:
                        callback(delay)
                    except Exception:
                        pass
                time.sleep(delay)
                continue
            attempt += 1
            if e.code == 404 or attempt >= max_retries:
                return None
            time.sleep(1.0)
        except Exception:
            attempt += 1
            if attempt >= max_retries:
                return None
            time.sleep(1.0)

    return None


def fetch_anime_info_online(
    title: str,
    max_retries: int = 3,
    expand_sequels: bool = False,
) -> List[Dict[str, Any]]:
    """
    Queries AniList GraphQL API to fetch the official English title, MAL ID,
    format (type), and total episode count for the given anime title.
    If expand_sequels is True, automatically traverses canonical sequels and sequel movies.
    """
    page = _execute_anilist_request(
        ANILIST_SEARCH_PAGE_QUERY, {"search": title},
        max_retries=max_retries, result_key="Page",
    )
    nodes = (page or {}).get("media") or []
    root_media = _pick_best_media(nodes)
    if not root_media:
        return []

    root_entry = _parse_media_node(root_media, fallback_title=title)
    root_entry["candidates"] = _candidates(nodes, "anime", title)
    results: List[Dict[str, Any]] = [root_entry]

    if not expand_sequels:
        return results

    # The paged query omits relations; fetch the full node only when expanding.
    detailed = _execute_anilist_request(
        ANILIST_BY_ID_QUERY, {"id": root_media["id"]}, max_retries=max_retries
    )
    if detailed:
        root_media = detailed

    # Traverse SEQUEL chain (subsequent seasons and canon sequel movies)
    visited_ids = {root_media.get("id")}
    current_media = root_media

    for _ in range(15):  # Limit chain traversal to avoid potential loops
        sequel_node = None
        for edge in current_media.get("relations", {}).get("edges", []):
            if edge.get("relationType") == "SEQUEL":
                candidate = edge.get("node", {})
                if candidate.get("type") == "ANIME" and candidate.get("id") not in visited_ids:
                    # Ignore unreleased/unannounced placeholders without MAL ID
                    if candidate.get("status") != "NOT_YET_RELEASED" and candidate.get("idMal"):
                        sequel_node = candidate
                        break

        if not sequel_node:
            break

        time.sleep(0.3)
        nxt_media = _execute_anilist_request(ANILIST_BY_ID_QUERY, {"id": sequel_node["id"]}, max_retries=max_retries)
        if not nxt_media:
            break

        visited_ids.add(nxt_media.get("id"))
        results.append(_parse_media_node(nxt_media, fallback_title=title))
        current_media = nxt_media

    return results


def resolve_anime_entries(
    title: str,
    session_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    expand_sequels: bool = False,
) -> List[Dict[str, Any]]:
    """
    Resolves an anime title to official MAL details.
    If expand_sequels is True, returns all canonical sequels and sequel movies.
    Uses an in-memory session cache only for duplicate titles during execution.
    Does NOT write any cache to disk.
    """
    cache = session_cache if session_cache is not None else {}
    key = f"{title.strip().lower()}:{'expand' if expand_sequels else 'single'}"
    if key in cache:
        return [dict(e) for e in cache[key]]

    online_info = fetch_anime_info_online(title, expand_sequels=expand_sequels)
    time.sleep(0.2)

    if online_info:
        entries = online_info
    else:
        # Never invent metadata for a title AniList could not match: a fabricated
        # "TV, 12 episodes" row looks resolved in the table but exports nothing.
        entries = [{
            "id": 0,
            "anilist_id": 0,
            "title": title,
            "type": "Unresolved",
            "episodes": 0,
            "unresolved": True,
        }]

    cache[key] = entries
    return entries


def fetch_manga_info_online(
    title: str,
    max_retries: int = 3,
    expand_sequels: bool = False,
) -> List[Dict[str, Any]]:
    """
    Resolve a title against AniList's MANGA media type.
    When requested, follows direct SEQUEL relations that are themselves manga.
    Unknown chapter/volume counts remain 0 (unknown), never fabricated.
    """
    page = _execute_anilist_request(
        ANILIST_MANGA_SEARCH_PAGE_QUERY, {"search": title},
        max_retries=max_retries, result_key="Page",
    )
    nodes = (page or {}).get("media") or []
    root_media = _pick_best_media(nodes)
    if not root_media:
        return []

    # The paged query omits relations to keep it at one request per title; only
    # re-fetch the full node when sequel expansion actually needs them.
    if expand_sequels:
        detailed = _execute_anilist_request(
            ANILIST_MANGA_BY_ID_QUERY, {"id": root_media["id"]}, max_retries=max_retries
        )
        if detailed:
            root_media = detailed

    root_entry = _parse_manga_node(root_media, fallback_title=title)
    root_entry["candidates"] = _candidates(nodes, "manga", title)
    results: List[Dict[str, Any]] = [root_entry]
    if not expand_sequels:
        return results

    visited_ids = {root_media.get("id")}
    current_media = root_media
    for _ in range(15):
        sequel_node = None
        for edge in (current_media.get("relations") or {}).get("edges", []):
            if edge.get("relationType") != "SEQUEL":
                continue
            candidate = edge.get("node") or {}
            if candidate.get("type") == "MANGA" and candidate.get("id") not in visited_ids:
                if candidate.get("status") != "NOT_YET_RELEASED" and candidate.get("idMal"):
                    sequel_node = candidate
                    break
        if not sequel_node:
            break
        time.sleep(0.3)
        next_media = _execute_anilist_request(
            ANILIST_MANGA_BY_ID_QUERY, {"id": sequel_node["id"]}, max_retries=max_retries
        )
        if not next_media:
            break
        visited_ids.add(next_media.get("id"))
        results.append(_parse_manga_node(next_media, fallback_title=title))
        current_media = next_media
    return results


def resolve_manga_entries(
    title: str,
    session_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    expand_sequels: bool = False,
) -> List[Dict[str, Any]]:
    """Resolve a manga title; unresolved items are returned as visibly invalid rows."""
    cache = session_cache if session_cache is not None else {}
    key = f"manga:{title.strip().lower()}:{'expand' if expand_sequels else 'single'}"
    if key in cache:
        return [dict(e) for e in cache[key]]

    online_info = fetch_manga_info_online(title, expand_sequels=expand_sequels)
    time.sleep(0.2)
    if online_info:
        entries = online_info
    else:
        entries = [{
            "id": 0,
            "anilist_id": 0,
            "title": title,
            "type": "Unresolved",
            "chapters": 0,
            "volumes": 0,
            "unresolved": True,
        }]
    cache[key] = entries
    return [dict(e) for e in entries]


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
        watched_eps = entry.get("my_watched_episodes", eps if fully_watched else 0)
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
            '    <my_start_date>0000-00-00</my_start_date>',
            '    <my_finish_date>0000-00-00</my_finish_date>',
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
        read_chapters = int(entry.get("my_read_chapters", default_chapters_read) or 0)
        read_volumes = int(entry.get("my_read_volumes", default_volumes_read) or 0)
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
            '    <my_start_date>0000-00-00</my_start_date>',
            '    <my_finish_date>0000-00-00</my_finish_date>',
            '    <my_scanslation_group></my_scanslation_group>',
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


def process_anime_list(
    titles: Sequence[str],
    output_xml: Optional[Path] = None,
    output_gz: Optional[Path] = None,
    status: str = "Completed",
    score: int = 0,
    expand_sequels: bool = False,
) -> tuple[Path, Path, int]:
    """
    Normalizes anime titles to official English MAL metadata,
    writes the XML and GZ files, and validates output.
    Uses in-memory session resolution without saving cache to disk.
    If expand_sequels is True, automatically includes subsequent seasons and sequel movies.
    """
    base_dir = Path.cwd()
    target_xml = output_xml if output_xml else base_dir / "myanimelist_export.xml"
    target_gz = output_gz if output_gz else base_dir / "myanimelist_export.xml.gz"

    session_cache: Dict[str, List[Dict[str, Any]]] = {}
    resolved_entries: List[Dict[str, Any]] = []

    total_titles = len(titles)
    print(f"[*] Processing {total_titles} anime titles (expand_sequels={expand_sequels})...")

    for idx, raw_title in enumerate(titles, start=1):
        entries = resolve_anime_entries(raw_title, session_cache=session_cache, expand_sequels=expand_sequels)
        for resolved in entries:
            resolved_entries.append(resolved)
            mal_id = resolved["id"]
            norm_title = resolved["title"]
            eps = resolved["episodes"]
            stype = resolved["type"]

            if raw_title.lower() != norm_title.lower():
                print(f"  [{idx}/{total_titles}] '{raw_title}' -> '{norm_title}' (MAL ID: {mal_id}, {stype}, {eps} eps)")
            else:
                print(f"  [{idx}/{total_titles}] '{norm_title}' (MAL ID: {mal_id}, {stype}, {eps} eps)")

    # Deduplicate
    unique_entries = deduplicate_entries(resolved_entries)
    print(f"\n[*] Total unique anime entries: {len(unique_entries)}")

    # Build XML
    xml_content = build_mal_xml(unique_entries, default_status=status, default_score=score)
    xml_bytes = xml_content.encode("utf-8")

    # Save XML without BOM
    target_xml.write_bytes(xml_bytes)
    print(f"[+] Successfully generated: {target_xml.resolve()}")

    # Save GZ compressed file
    with open(target_gz, "wb") as f_out:
        f_out.write(gzip.compress(xml_bytes, compresslevel=9))
    print(f"[+] Successfully generated GZ: {target_gz.resolve()}")

    # Validate XML
    tree = ET.parse(target_xml)
    root = tree.getroot()
    anime_nodes = len(root.findall("anime"))
    print(f"[OK] XML validation passed! Total parsed anime nodes: {anime_nodes}\n")

    return target_xml, target_gz, anime_nodes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a plain-text anime or manga title list into MAL-compatible XML for AniList import."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Path to a text file containing one anime or manga title per line (defaults to notes.txt or animes.txt).",
    )
    parser.add_argument(
        "-i", "--input-file",
        dest="input_flag",
        type=str,
        default=None,
        help="Alternative flag to specify the input text file.",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Path for the generated XML file (default filename depends on media type).",
    )
    parser.add_argument(
        "-g", "--gz-output",
        type=str,
        default=None,
        help="Path for the compressed GZ file (default filename depends on media type).",
    )
    parser.add_argument(
        "--status",
        type=str,
        default="Completed",
        help="Default list status (anime: Watching/Plan to Watch; manga: Reading/Plan to Read; both support Completed, On-Hold, Dropped).",
    )
    parser.add_argument(
        "--score",
        type=int,
        default=0,
        help="Default score for entries (0-10, default is 0).",
    )
    parser.add_argument(
        "--media-type", choices=("anime", "manga"), default="anime",
        help="Media type to resolve and export (default: anime).",
    )
    parser.add_argument(
        "--expand-sequels",
        action="store_true",
        help="Automatically include direct sequel relations (anime sequels/movies or manga sequels).",
    )

    args = parser.parse_args()

    input_path_str = args.input_flag or args.input
    if input_path_str:
        input_file = Path(input_path_str)
    else:
        # Check default candidate
        if Path("notes.txt").is_file():
            input_file = Path("notes.txt")
        elif Path("animes.txt").is_file():
            input_file = Path("animes.txt")
        elif Path("input/animes.txt").is_file():
            input_file = Path("input/animes.txt")
        else:
            parser.error("No input file specified and no 'notes.txt' or 'animes.txt' found in current directory.")

    if args.output:
        output_xml = Path(args.output)
    elif Path("output").is_dir():
        output_xml = Path("output/myanimelist_manga_export.xml" if args.media_type == "manga" else "output/myanimelist_export.xml")
    else:
        output_xml = None

    if args.gz_output:
        output_gz = Path(args.gz_output)
    elif Path("output").is_dir():
        output_gz = Path("output/myanimelist_manga_export.xml.gz" if args.media_type == "manga" else "output/myanimelist_export.xml.gz")
    else:
        output_gz = None

    titles = load_titles_from_txt(input_file)
    if not titles:
        print(f"Error: No {args.media_type} titles found in {input_file}.")
        sys.exit(1)

    if args.media_type == "manga":
        cache: Dict[str, List[Dict[str, Any]]] = {}
        resolved: List[Dict[str, Any]] = []
        for idx, title in enumerate(titles, 1):
            print(f"[*] Resolving manga {idx}/{len(titles)}: {title}")
            for entry in resolve_manga_entries(title, cache, args.expand_sequels):
                item = dict(entry)
                item["status"] = args.status
                item["score"] = args.score
                resolved.append(item)
        unique = deduplicate_entries(resolved)
        unresolved_count = sum(1 for entry in unique if int(entry.get("id") or 0) <= 0)
        exportable_count = sum(1 for entry in unique if int(entry.get("id") or 0) > 0)
        print(f"[*] Unique manga candidates: {len(unique)}; exportable: {exportable_count}; omitted (missing MAL ID): {unresolved_count}")
        xml_text = build_mal_manga_xml(unique, args.status, args.score)
        xml_bytes = xml_text.encode("utf-8")
        target_xml = output_xml or (Path.cwd() / "myanimelist_manga_export.xml")
        target_gz = output_gz or (Path.cwd() / "myanimelist_manga_export.xml.gz")
        target_xml.parent.mkdir(parents=True, exist_ok=True)
        target_gz.parent.mkdir(parents=True, exist_ok=True)
        target_xml.write_bytes(xml_bytes)
        target_gz.write_bytes(gzip.compress(xml_bytes, compresslevel=9))
        root = ET.fromstring(xml_bytes)
        count = len(root.findall("manga"))
        print(f"[OK] Manga XML validated. Exportable manga entries: {count}")
        print(f"[+] XML: {target_xml.resolve()}")
        print(f"[+] GZ:  {target_gz.resolve()}")
    else:
        process_anime_list(
            titles=titles,
            output_xml=output_xml,
            output_gz=output_gz,
            status=args.status,
            score=args.score,
            expand_sequels=args.expand_sequels,
        )


if __name__ == "__main__":
    main()
