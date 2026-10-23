"""
XML Generator
"""

from __future__ import annotations

__version__ = "2.0.0"

import argparse
import gzip
import json
import os
from datetime import datetime
from pathlib import Path
import difflib
import math
import re
import unicodedata
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


from titles import (
    load_titles_from_txt,
    parse_title_line,
    sanitize_title_line,
    _CHAPTER_UNITS,
    _EPISODE_UNITS,
    _LINE_STATUSES,
    _LINE_STATUS_FOR_MEDIA,
    _SEPARATORS_CLASS,
    _TRAILING_SEPARATORS,
    _VOLUME_UNITS,
    _WEAK_STATUS_WORDS,
    _strip_bare_score,
    _strip_counted,
    _strip_fraction,
    _strip_status,
)
from matching import (
    DESCRIPTION_LIMIT,
    FORMAT_MAP,
    MANGA_FORMAT_MAP,
    MEDIA_STATUS_LABELS,
    REVIEW_RATIO_FLOOR,
    REVIEW_TIE_GAP,
    _alias_hit,
    _candidates,
    _carry_candidate_review,
    _media_details,
    _media_rank,
    _normalize_title,
    _parse_manga_node,
    _parse_media_node,
    _pick_best_media,
    _rank_nodes,
    _search_variants,
    _title_ratio,
    _won_by_default,
)
from malxml import (
    build_mal_manga_xml,
    build_mal_xml,
    deduplicate_entries,
    REREAD_STATUSES,
    _cdata,
    _clamp_progress,
    _is_repeat_status,
    _mal_date,
    _normalize_status,
)

ANILIST_API_URL = "https://graphql.anilist.co"

# AniList caps at 30 requests/minute; cap how long one title may wait on 429s.
MAX_RATE_LIMIT_WAITS = 6

# ponytail: process-wide lock serialises every AniList call to stay under the
# 30/min cap. Correct because the cap is per-IP, not per-thread. Swap for a
# token bucket only if AniList ever allows bursts.
# Starting interval, sized for the 30 req/min the API serves while degraded.
# AniList's documented nominal limit is 90/min, so the real spacing is derived
# from the X-RateLimit-Limit header once one has been seen.
MIN_REQUEST_INTERVAL = 2.1
MIN_INTERVAL_FLOOR = 0.7      # 90 req/min
MIN_INTERVAL_CEILING = 2.5
_throttle_lock = threading.Lock()
_last_request_at = 0.0
_observed_limit: Optional[int] = None


def _record_rate_limit(headers: Any) -> None:
    """Remember AniList's advertised per-minute limit so pacing can follow it."""
    global _observed_limit
    try:
        value = int(headers.get("X-RateLimit-Limit"))
    except (AttributeError, TypeError, ValueError):
        return
    if value > 0:
        _observed_limit = value


def current_interval() -> float:
    """Seconds to leave between requests, from the limit AniList last reported.

    The 5% margin keeps us just under the cap; the floor keeps the undocumented
    burst limiter happy even if a very large limit is ever advertised.
    """
    if not _observed_limit:
        return MIN_REQUEST_INTERVAL
    return min(MIN_INTERVAL_CEILING, max(MIN_INTERVAL_FLOOR, 60.0 / _observed_limit * 1.05))


def _throttle() -> None:
    """Space out requests so AniList's per-minute limit is not tripped at all."""
    global _last_request_at
    with _throttle_lock:
        wait = current_interval() - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()

# Per-thread hook so a caller (the GUI) can tell the user why a run is stalling.
_rate_limit_hook = threading.local()


def set_rate_limit_notifier(callback: Optional[Any]) -> None:
    """Register a callback(seconds) invoked whenever a request waits out a 429."""
    _rate_limit_hook.callback = callback


# The five queries below each select the fields the match picker shows, so a
# human can tell two similar entries apart. Deliberately NOT in
# _search_batch_query: a 339-title run would carry all of this for ten
# candidates per title and never display any of it.
ANILIST_BY_ID_QUERY = """
query ($id: Int) {
  Media (id: $id, type: ANIME) {
    id
    idMal
    format
    episodes
    title { english romaji native }
    synonyms
    genres
    averageScore
    popularity
    status
    startDate { year month day }
    endDate { year }
    coverImage { medium }
    siteUrl
    description(asHtml: false)
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
      format
      episodes
      title { english romaji native }
      synonyms
      genres
      averageScore
      popularity
      status
      startDate { year month day }
      endDate { year }
      coverImage { medium }
      siteUrl
      description(asHtml: false)
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
      format
      chapters
      volumes
      title { english romaji native }
      synonyms
      genres
      averageScore
      popularity
      status
      startDate { year month day }
      endDate { year }
      coverImage { medium }
      siteUrl
      description(asHtml: false)
    }
  }
}
"""

ANILIST_MANGA_BY_ID_QUERY = """
query ($id: Int) {
  Media (id: $id, type: MANGA) {
    id
    idMal
    format
    chapters
    volumes
    title { english romaji native }
    synonyms
    genres
    averageScore
    popularity
    status
    startDate { year month day }
    endDate { year }
    coverImage { medium }
    siteUrl
    description(asHtml: false)
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

ANILIST_BY_MAL_ID_QUERY = """
query ($idMal: Int, $type: MediaType) {
  Media (idMal: $idMal, type: $type) {
    id
    idMal
    format
    episodes
    chapters
    volumes
    title { english romaji native }
    synonyms
    genres
    averageScore
    popularity
    status
    startDate { year month day }
    endDate { year }
    coverImage { medium }
    siteUrl
    description(asHtml: false)
  }
}
"""

def _post_graphql(query: str, variables: Dict[str, Any], max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Send one GraphQL document and return its ``data`` object.

    Owns throttling, 429 waits, transient-error detection and fail-fast on 400.
    `_execute_anilist_request` and `_execute_anilist_batch` are thin wrappers so
    every caller shares exactly one network policy.
    """
    return _request_graphql(query, variables, max_retries)


def _execute_anilist_batch(query: str, variables: Dict[str, Any], max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Run an aliased multi-search document. Returns ``data`` or None.

    Verified against the live API: one document carrying eight aliased
    ``Page { media(search:) }`` blocks returns HTTP 200 with all eight aliases
    populated and decrements X-RateLimit-Remaining by 1, not 8.
    """
    return _post_graphql(query, variables, max_retries=max_retries)


def _search_batch_query(count: int, media_type: str) -> str:
    """Build `t0..tN` aliased Page blocks, one per title.

    Aliased `Page` blocks rather than aliased top-level `Media`: a `Media` alias
    returns a single node, which would lose both the candidate list behind the
    match picker and the ability to skip a top hit whose idMal is null.
    """
    media_enum = "MANGA" if media_type == "manga" else "ANIME"
    count_fields = "chapters\n      volumes" if media_type == "manga" else "episodes"
    args = ", ".join(f"$q{i}: String" for i in range(count))
    blocks = "".join(
        f"""
  t{i}: Page(perPage: 10) {{
    media(search: $q{i}, type: {media_enum}) {{
      id
      idMal
      title {{ english romaji }}
      synonyms
      format
      {count_fields}
      status
      startDate {{ year }}
      popularity
      coverImage {{ medium }}
    }}
  }}"""
        for i in range(count)
    )
    return f"query ({args}) {{{blocks}\n}}"


def _execute_anilist_request(query: str, variables: Dict[str, Any], max_retries: int = 3, result_key: str = "Media") -> Optional[Dict[str, Any]]:
    data = _request_graphql(query, variables, max_retries)
    if data is None:
        return None
    return (data or {}).get(result_key)


def _request_graphql(query: str, variables: Dict[str, Any], max_retries: int = 3) -> Optional[Dict[str, Any]]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    attempt = 0
    rate_limit_waits = 0
    while attempt < max_retries:
        try:
            _throttle()
            req = urllib.request.Request(ANILIST_API_URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                _record_rate_limit(response.headers)
                result = json.loads(response.read().decode("utf-8"))
                data = result.get("data")
                # AniList can report throttling/outages in a 200 body; retry those
                # instead of mistaking them for "no such title".
                statuses = {e.get("status") for e in (result.get("errors") or []) if isinstance(e, dict)}
                if not data and any(st == 429 or (isinstance(st, int) and st >= 500) for st in statuses):
                    raise RuntimeError(f"AniList transient error: {sorted(st for st in statuses if st)}")
                return data or {}
        except urllib.error.HTTPError as e:
            _record_rate_limit(e.headers)
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
            # 400 means the query itself is wrong - retrying cannot fix it and only
            # burns rate limit, so fail fast like a 404.
            if e.code in (400, 404) or attempt >= max_retries:
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
    root_media = _pick_best_media(nodes, title)
    if not root_media:
        return []

    root_entry = _parse_media_node(root_media, fallback_title=title)
    root_entry["candidates"] = _candidates(nodes, "anime", title)
    _carry_candidate_review(root_entry, root_entry["candidates"])
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
    root_media = _pick_best_media(nodes, title)
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
    _carry_candidate_review(root_entry, root_entry["candidates"])
    root_entry.setdefault("can_write_anilist", int(root_entry.get("anilist_id") or 0) > 0)
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


def lookup_by_mal_id(mal_id: Any, media_type: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Claim a specific MAL ID directly, bypassing title search.

    The only reliable fix when a title cannot be matched by name: the user reads
    the ID off MyAnimeList and we ask AniList for exactly that entry.
    """
    try:
        numeric = int(str(mal_id).strip())
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    media = _execute_anilist_request(
        ANILIST_BY_MAL_ID_QUERY,
        {"idMal": numeric, "type": "MANGA" if media_type == "manga" else "ANIME"},
        max_retries=max_retries,
    )
    if not media:
        return None
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    return parse(media)


ANILIST_BY_MAL_IDS_QUERY = """
query ($idsMal: [Int], $type: MediaType) {
  Page (perPage: 50) {
    media (idMal_in: $idsMal, type: $type) {
      id
      idMal
      title { english romaji }
      format
      episodes
      chapters
      volumes
      status
      startDate { year }
      popularity
      coverImage { medium }
    }
  }
}
"""


def backfill_anilist_ids(entries: Sequence[Dict[str, Any]], media_type: str,
                         max_retries: int = 3) -> int:
    """Give MAL-only rows their AniList id, in place. Returns how many were filled.

    A list imported from a MyAnimeList export carries MAL ids and nothing else,
    so without this every imported row is unwritable and the AniList route is
    closed to it. One request per 50 rows via idMal_in.
    """
    pending = [e for e in entries
               if int(e.get("id") or 0) > 0 and int(e.get("anilist_id") or 0) <= 0]
    if not pending:
        return 0
    media_enum = "MANGA" if media_type == "manga" else "ANIME"
    by_mal: Dict[int, Dict[str, Any]] = {}
    ids = [int(e["id"]) for e in pending]
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        page = _execute_anilist_request(
            ANILIST_BY_MAL_IDS_QUERY, {"idsMal": chunk, "type": media_enum},
            max_retries=max_retries, result_key="Page",
        )
        for node in ((page or {}).get("media") or []):
            if node and node.get("idMal"):
                by_mal[int(node["idMal"])] = node
    filled = 0
    for entry in pending:
        node = by_mal.get(int(entry["id"]))
        if not node:
            continue
        entry["anilist_id"] = int(node.get("id") or 0)
        entry["can_write_anilist"] = int(entry["anilist_id"]) > 0
        if not entry.get("cover"):
            entry["cover"] = (node.get("coverImage") or {}).get("medium")
        if not entry.get("year"):
            entry["year"] = (node.get("startDate") or {}).get("year")
        filled += 1
    return filled


def lookup_by_anilist_id(anilist_id: Any, media_type: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Claim a specific AniList entry. The only route for a title MAL does not have."""
    try:
        numeric = int(str(anilist_id).strip())
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    query = ANILIST_MANGA_BY_ID_QUERY if media_type == "manga" else ANILIST_BY_ID_QUERY
    media = _execute_anilist_request(query, {"id": numeric}, max_retries=max_retries)
    if not media:
        return None
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    return parse(media)


_ANILIST_URL = re.compile(r"anilist\.co/(anime|manga)/(\d+)", re.IGNORECASE)
_MAL_URL = re.compile(r"myanimelist\.net/(anime|manga)/(\d+)", re.IGNORECASE)


def parse_media_reference(text: str) -> Dict[str, Any]:
    """Work out what the user pasted.

    Returns {"kind": "anilist"|"mal"|"digits"|"text", "value": ...}. A pasted
    link is unambiguous, so it is honoured exactly; a bare number is not, and is
    reported as such rather than guessed at.
    """
    raw = str(text or "").strip()
    if not raw:
        return {"kind": "text", "value": ""}
    found = _ANILIST_URL.search(raw)
    if found:
        return {"kind": "anilist", "value": int(found.group(2)), "media": found.group(1).lower()}
    found = _MAL_URL.search(raw)
    if found:
        return {"kind": "mal", "value": int(found.group(2)), "media": found.group(1).lower()}
    if raw.isdigit():
        return {"kind": "digits", "value": int(raw)}
    return {"kind": "text", "value": raw}


def resolve_one(query: str, media_type: str, id_kind: Optional[str] = None,
                max_retries: int = 3) -> Dict[str, Any]:
    """Everything the GUI needs to fix a single unmatched row.

    A link resolves to exactly one entry. A bare number is looked up as both a
    MAL id and an AniList id when the caller has not said which, and whatever
    exists comes back labelled, so the user chooses instead of the tool guessing.
    Free text returns ranked candidates.
    """
    reference = parse_media_reference(query)
    kind = reference["kind"]

    if kind in ("anilist", "mal"):
        getter = lookup_by_anilist_id if kind == "anilist" else lookup_by_mal_id
        entry = getter(reference["value"], media_type, max_retries=max_retries)
        if not entry:
            label = "AniList" if kind == "anilist" else "MyAnimeList"
            linked = reference.get("media")
            if linked and linked != media_type:
                # The link says what it is; say so rather than "not found".
                return {"ok": False,
                        "error": (f"That link is for {'an' if linked == 'anime' else 'a'} "
                                  f"{linked}, but this list is {media_type}. "
                                  f"Switch Media Type, or paste a {media_type} link.")}
            return {"ok": False, "error": f"No {media_type} with {label} id {reference['value']}."}
        entry["match_source"] = kind
        return {"ok": True, "kind": kind, "entries": [entry]}

    if kind == "digits":
        wanted = [id_kind] if id_kind in ("anilist", "mal") else ["anilist", "mal"]
        entries: List[Dict[str, Any]] = []
        for which in wanted:
            getter = lookup_by_anilist_id if which == "anilist" else lookup_by_mal_id
            entry = getter(reference["value"], media_type, max_retries=max_retries)
            if entry and not any(e.get("anilist_id") == entry.get("anilist_id") for e in entries):
                entry["match_source"] = which
                entries.append(entry)
        if not entries:
            return {"ok": False,
                    "error": f"{reference['value']} is not a {media_type} id on AniList or MyAnimeList."}
        return {"ok": True, "kind": "digits", "entries": entries}

    if not reference["value"]:
        return {"ok": False, "error": "Type a title, an id, or paste an AniList link."}
    entries = search_candidates(reference["value"], media_type, max_retries=max_retries)
    if not entries:
        return {"ok": False, "error": f"AniList has no {media_type} matching that text."}
    for entry in entries:
        entry["match_source"] = "search"
    return {"ok": True, "kind": "text", "entries": entries}


def search_candidates(query: str, media_type: str, max_retries: int = 3) -> List[Dict[str, Any]]:
    """Importable matches for a free-text query, best first.

    Reuses the single-title resolver so the ranking stays identical to a normal run.
    """
    fetch = fetch_manga_info_online if media_type == "manga" else fetch_anime_info_online
    results = fetch(query, max_retries=max_retries)
    if not results:
        return []
    root = results[0]
    candidates = root.get("candidates") or []
    if candidates:
        return candidates
    # Keep an AniList-only hit: it cannot go in MAL XML but it is writable.
    return [root] if int(root.get("anilist_id") or 0) > 0 else []


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
BATCH_SIZE = 8
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_MAX_ENTRIES = 10000
# Sent on every AniList request. A contactable project name is the courtesy
# an unauthenticated public API is owed.
USER_AGENT = f"AniExport/{__version__} (+https://github.com/Kp2444/AniExportv2)"

CACHE_VERSION = 4          # entries gained alias_hit alongside review_reason
_cache_lock = threading.Lock()


def cache_path() -> Path:
    return Path(os.environ.get("ANIEXPORT_CACHE") or (Path.cwd() / ".anilist_cache.json"))


def _cache_key(title: str, media_type: str, expand_sequels: bool,
               prefer_mal_id: bool = True) -> str:
    # The destination changes which entry wins, so it must be part of the key or
    # an XML-mode result would be served to an AniList-mode run.
    rank = "mal" if prefer_mal_id else "al"
    return (f"{media_type}:{str(title).strip().casefold()}:"
            f"{'expand' if expand_sequels else 'single'}:{rank}")


def load_cache() -> Dict[str, Any]:
    """Read the cache, dropping expired rows. Any damage means "no cache"."""
    path = cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    cutoff = time.time() - CACHE_TTL_SECONDS
    return {
        key: row for key, row in entries.items()
        if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)) and row["ts"] >= cutoff
    }


def save_cache(entries: Dict[str, Any]) -> None:
    """Atomic write: a crash mid-save must not leave an unreadable cache."""
    if len(entries) > CACHE_MAX_ENTRIES:
        oldest = sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0))
        entries = dict(oldest[-CACHE_MAX_ENTRIES:])
    path = cache_path()
    payload = json.dumps({"version": CACHE_VERSION, "entries": entries}, ensure_ascii=False)
    tmp = path.with_name(path.name + ".tmp")
    with _cache_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _cacheable(entries: Sequence[Dict[str, Any]]) -> bool:
    """Cache anything we actually identified on AniList.

    Keyed on the AniList id, not the MAL id: an AniList-only result is a real
    result, and treating it as a miss meant re-fetching it on every single run.
    """
    return bool(entries) and int(entries[0].get("anilist_id") or 0) > 0


def resolve_many_entries(
    titles: Sequence[str],
    media_type: str = "anime",
    expand_sequels: bool = False,
    session_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    use_cache: bool = True,
    on_progress: Optional[Any] = None,
    should_cancel: Optional[Any] = None,
    prefer_mal_id: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """Resolve many titles with one request per batch instead of one per title.

    Returns {original title: [entry, ...]}. Unresolved titles keep exactly the
    shape the single-title resolvers produce, so callers need no special case.
    """
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    unique: List[str] = []
    for title in titles:
        if title not in unique:
            unique.append(title)

    disk = load_cache() if use_cache else {}
    session = session_cache if session_cache is not None else {}
    results: Dict[str, List[Dict[str, Any]]] = {}
    failures: List[List[str]] = []
    pending: List[str] = []
    done = 0
    total = len(unique)

    def report(stage: str, info: Optional[Dict[str, Any]] = None) -> None:
        """A caller's callback must never be able to kill a long run."""
        if on_progress:
            try:
                on_progress(stage, done, total, info)
            except Exception:
                pass

    def cancelled() -> bool:
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    for title in unique:
        key = _cache_key(title, media_type, expand_sequels, prefer_mal_id)
        cached = session.get(key) or (disk.get(key) or {}).get("data")
        if cached:
            results[title] = [dict(entry) for entry in cached]
            done += 1
            # Carry the rows: a warm cache used to emit a blank progress event,
            # so re-running a list left the table empty until the run finished.
            report("cache", {"title": title, "entries": results[title]})
        else:
            pending.append(title)

    def run_batch(batch: Sequence[str], stage: str) -> Dict[str, List[Dict[str, Any]]]:
        nonlocal done
        found: Dict[str, List[Dict[str, Any]]] = {}
        if not batch:
            return found
        query = _search_batch_query(len(batch), media_type)
        variables = {f"q{i}": name for i, name in enumerate(batch)}
        data = _execute_anilist_batch(query, variables)
        if data is None:
            # The request failed outright. Saying these titles "have no MAL ID"
            # would blame the list for a network problem, so say so explicitly.
            failures.append(list(batch))
            report("network_error", {"titles": list(batch)})
            data = {}
        for i, name in enumerate(batch):
            nodes = ((data.get(f"t{i}") or {}).get("media")) or []
            best = _pick_best_media(nodes, name, prefer_mal_id)
            # An AniList id is enough to be resolved. Requiring a MAL id here is
            # what made AniList-only titles look like failures: the single-title
            # resolver never applied that rule, so the two paths disagreed.
            if best and int(best.get("id") or 0) > 0:
                entry = parse(best, fallback_title=name)
                candidates = _candidates(nodes, media_type, name, prefer_mal_id)
                entry["candidates"] = candidates
                # The runner-up gap only exists where the ranked list does, and
                # the chosen entry is parsed from the node rather than taken
                # from that list, so it has to be carried across explicitly.
                _carry_candidate_review(entry, candidates)
                found[name] = [entry]
            if stage == "resolve":
                done += 1
                report(stage, {"title": name, "entries": found.get(name)})
        return found

    stopped = False
    for start in range(0, len(pending), BATCH_SIZE):
        if cancelled():
            stopped = True
            break
        results.update(run_batch(pending[start:start + BATCH_SIZE], "resolve"))

    # Second pass: deterministic rewrites for whatever is still missing.
    unmatched = [] if stopped else [t for t in pending if t not in results]
    if unmatched:
        attempts: List[tuple] = []
        for title in unmatched:
            for variant in _search_variants(title, media_type):
                attempts.append((title, variant))
        for start in range(0, len(attempts), BATCH_SIZE):
            if cancelled():
                stopped = True
                break
            chunk = attempts[start:start + BATCH_SIZE]
            chunk = [(orig, var) for orig, var in chunk if orig not in results]
            if not chunk:
                continue
            found = run_batch([var for _, var in chunk], "retry")
            for (orig, var) in chunk:
                if var in found and orig not in results:
                    results[orig] = found[var]
                    report("retry", {"title": orig, "entries": results[orig]})
            report("retry")

    if expand_sequels and not stopped:
        for title, entries in list(results.items()):
            anilist_id = int(entries[0].get("anilist_id") or 0)
            if anilist_id <= 0:
                continue
            by_id = ANILIST_MANGA_BY_ID_QUERY if media_type == "manga" else ANILIST_BY_ID_QUERY
            detailed = _execute_anilist_request(by_id, {"id": anilist_id})
            if detailed:
                results[title] = entries + _expand_sequels_chain(detailed, media_type, title)
            report("expand")

    def remember(found: Dict[str, List[Dict[str, Any]]]) -> None:
        """Keep whatever was actually resolved.

        A cancelled run used to return before reaching this, so stopping a
        300-title run at title 250 threw all 250 away and the next attempt
        re-spent the rate limit on every one of them. Unresolved placeholders
        are only filled in after the cancel check, and `_cacheable` rejects
        them in any case, so a miss is never written here.
        """
        stamp = time.time()
        for title, entries in found.items():
            if not _cacheable(entries):
                continue
            key = _cache_key(title, media_type, expand_sequels, prefer_mal_id)
            session[key] = entries
            if use_cache:
                disk[key] = {"ts": stamp, "data": entries}
        if use_cache:
            save_cache(disk)

    if stopped:
        remember(results)
        return {title: [dict(e) for e in results[title]] for title in titles if title in results}

    for title in unique:
        if title not in results:
            results[title] = [_unresolved_entry(title, media_type)]

    remember(results)

    return {title: [dict(e) for e in results[title]] for title in titles if title in results}


def _unresolved_entry(title: str, media_type: str) -> Dict[str, Any]:
    """Exactly the shape the single-title resolvers produce for a miss."""
    base = {"id": 0, "anilist_id": 0, "title": title, "type": "Unresolved", "unresolved": True,
            "can_export_xml": False, "can_write_anilist": False}
    if media_type == "manga":
        base.update({"chapters": 0, "volumes": 0})
    else:
        base.update({"episodes": 0})
    return base


def _expand_sequels_chain(root_media: Dict[str, Any], media_type: str, fallback_title: str = "") -> List[Dict[str, Any]]:
    """Follow direct SEQUEL relations. Shared by the single and batch paths."""
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    by_id = ANILIST_MANGA_BY_ID_QUERY if media_type == "manga" else ANILIST_BY_ID_QUERY
    wanted_type = "MANGA" if media_type == "manga" else "ANIME"
    out: List[Dict[str, Any]] = []
    visited = {root_media.get("id")}
    current = root_media
    for _ in range(15):
        nxt = None
        for edge in (current.get("relations") or {}).get("edges", []):
            if edge.get("relationType") != "SEQUEL":
                continue
            candidate = edge.get("node") or {}
            if candidate.get("type") == wanted_type and candidate.get("id") not in visited:
                if candidate.get("status") != "NOT_YET_RELEASED" and candidate.get("idMal"):
                    nxt = candidate
                    break
        if not nxt:
            break
        detailed = _execute_anilist_request(by_id, {"id": nxt["id"]})
        if not detailed:
            break
        visited.add(detailed.get("id"))
        out.append(parse(detailed, fallback_title=fallback_title))
        current = detailed
    return out


# ---------------------------------------------------------------------------
# Writing to AniList
#
# Identity note: SaveMediaListEntry decides create-vs-update by whether `id` is
# supplied. Rather than rely on undocumented upsert behaviour for `mediaId`
# alone, every payload carries the existing list-entry id when the user already
# has one, which is read in bulk first. Re-running a write therefore cannot
# duplicate rows. All three shapes below were confirmed by introspection.
# ---------------------------------------------------------------------------
ANILIST_VIEWER_QUERY = """
query { Viewer { id name } }
"""

ANILIST_EXISTING_ENTRIES_QUERY = """
query ($userId: Int, $type: MediaType, $mediaIds: [Int]) {
  Page (perPage: 50) {
    mediaList (userId: $userId, type: $type, mediaId_in: $mediaIds) {
      id
      mediaId
      status
      progress
      progressVolumes
      score (format: POINT_100)
      repeat
      notes
      startedAt { year month day }
      completedAt { year month day }
    }
  }
}
"""

# Confirmed by introspection: DeleteMediaListEntry(id: Int) -> Deleted { deleted }.
ANILIST_DELETE_ENTRY_MUTATION = """
mutation ($id: Int) {
  DeleteMediaListEntry (id: $id) { deleted }
}
"""

ANILIST_SAVE_ENTRY_MUTATION = """
mutation ($id: Int, $mediaId: Int, $status: MediaListStatus, $scoreRaw: Int,
          $progress: Int, $progressVolumes: Int, $repeat: Int, $notes: String,
          $startedAt: FuzzyDateInput, $completedAt: FuzzyDateInput) {
  SaveMediaListEntry (id: $id, mediaId: $mediaId, status: $status, scoreRaw: $scoreRaw,
                      progress: $progress, progressVolumes: $progressVolumes,
                      repeat: $repeat, notes: $notes,
                      startedAt: $startedAt, completedAt: $completedAt) {
    id
    mediaId
    status
  }
}
"""

# MAL list status -> AniList MediaListStatus. Enum values confirmed by
# introspection: CURRENT, PLANNING, COMPLETED, DROPPED, PAUSED, REPEATING.
ANILIST_STATUS_MAP = {
    "reading": "CURRENT",
    "watching": "CURRENT",
    "completed": "COMPLETED",
    "plan to read": "PLANNING",
    "plan to watch": "PLANNING",
    "on-hold": "PAUSED",
    "dropped": "DROPPED",
}


# AniList's own vocabulary back into MAL's. ANILIST_STATUS_MAP above is not
# invertible - CURRENT is reached from both "reading" and "watching" - so the
# reverse has to be media-type aware, and lives next to it so the two cannot
# drift apart.
ANILIST_STATUS_REVERSE = {
    "anime": {"CURRENT": "Watching", "PLANNING": "Plan to Watch", "COMPLETED": "Completed",
              "PAUSED": "On-Hold", "DROPPED": "Dropped", "REPEATING": "Rewatching"},
    "manga": {"CURRENT": "Reading", "PLANNING": "Plan to Read", "COMPLETED": "Completed",
              "PAUSED": "On-Hold", "DROPPED": "Dropped", "REPEATING": "Rereading"},
}

ANILIST_LIST_COLLECTION_QUERY = """
query ($name: String, $userId: Int, $type: MediaType, $chunk: Int, $perChunk: Int) {
  MediaListCollection (userName: $name, userId: $userId, type: $type,
                       chunk: $chunk, perChunk: $perChunk,
                       forceSingleCompletedList: true) {
    hasNextChunk
    lists {
      name
      status
      isCustomList
      entries {
        id status progress progressVolumes repeat notes
        private hiddenFromStatusLists priority
        score (format: POINT_100)
        startedAt { year month day }
        completedAt { year month day }
        media {
          id idMal format status popularity
          title { english romaji }
          episodes chapters volumes
          startDate { year }
          coverImage { medium }
        }
      }
    }
  }
}
"""

# 500 is the commonly used value; the documented maximum is not published, so a
# wrong guess is handled by halving rather than by asking.
LIST_PER_CHUNK = 500


def _full_date(value: Any) -> str:
    """A FuzzyDate as YYYY-MM-DD, but only when all three parts are present.

    A partial date becomes blank rather than an invented day: `_mal_date`
    rejects anything that is not a complete padded date, and guessing the 1st
    would put a date on the account the user never set.
    """
    if not isinstance(value, dict):
        return ""
    year, month, day = value.get("year"), value.get("month"), value.get("day")
    if not (year and month and day):
        return ""
    return "%04d-%02d-%02d" % (int(year), int(month), int(day))


def _row_from_list_entry(entry: Dict[str, Any], media_type: str) -> Optional[Dict[str, Any]]:
    """One MediaList entry as an export row, the shape the resolvers produce."""
    media = entry.get("media") or {}
    if not media.get("id"):
        return None
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    row = parse(media)
    status = ANILIST_STATUS_REVERSE[media_type].get(str(entry.get("status") or ""))
    if status:
        row["status"] = status
    # Scores come back as POINT_100 by request, so the account's display format
    # cannot leak in; the rows this app works with are 0-10. The exact value is
    # kept beside the rounded one - see _score_raw for why.
    row["score_raw"] = int(entry.get("score") or 0)
    row["score"] = int(round(row["score_raw"] / 10))
    if media_type == "manga":
        row["my_read_chapters"] = int(entry.get("progress") or 0)
        row["my_read_volumes"] = int(entry.get("progressVolumes") or 0)
        row["_rereading"] = str(entry.get("status") or "") == "REPEATING"
    else:
        row["my_watched_episodes"] = int(entry.get("progress") or 0)
        row["_rewatching"] = str(entry.get("status") or "") == "REPEATING"
    row["comments"] = str(entry.get("notes") or "")
    row["start_date"] = _full_date(entry.get("startedAt"))
    row["finish_date"] = _full_date(entry.get("completedAt"))
    # AniList-only flags. Always set, so "absent" (a MAL-XML row) stays
    # distinguishable from "present and false" (an AniList row that is public).
    row["private"] = bool(entry.get("private"))
    row["hidden_from_status_lists"] = bool(entry.get("hiddenFromStatusLists"))
    row["priority"] = int(entry.get("priority") or 0)
    row["original"] = row.get("title", "")
    return row


def anilist_list_collection(media_type: str, username: str = "", token: str = "",
                            user_id: int = 0, per_chunk: int = LIST_PER_CHUNK) -> Dict[str, Any]:
    """Read a whole AniList list. Read-only, and a public list needs no token.

    Returns {ok, entries, skipped, chunks, per_chunk} or {ok: False, error}.
    A private list queried by name comes back with no lists rather than an
    error, so that case is named plainly instead of being reported as empty.
    """
    media_enum = "MANGA" if media_type == "manga" else "ANIME"
    variables: Dict[str, Any] = {"type": media_enum}
    if user_id:
        variables["userId"] = int(user_id)
    elif username:
        variables["name"] = str(username)
    else:
        return {"ok": False, "error": "A username or a token is needed to read a list."}

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    skipped = 0
    chunk = 1
    halvings = 0
    saw_any_list = False
    while True:
        payload = dict(variables, chunk=chunk, perChunk=per_chunk)
        if token:
            result = _execute_anilist_authenticated(ANILIST_LIST_COLLECTION_QUERY, payload, token)
            data, error = result.get("data"), result.get("error")
        else:
            # No error detail comes back from this path, only None, and a
            # wrong name fails exactly like an unreachable API - so leave the
            # message to the handler below, which names both possibilities.
            data = _request_graphql(ANILIST_LIST_COLLECTION_QUERY, payload)
            error = None
        if error or data is None:
            # A perChunk the server will not accept is indistinguishable from
            # any other failure here, so halve and retry - but only twice. A
            # wrong username fails identically, and retrying it five times just
            # delays a wrong answer.
            if per_chunk > 125 and halvings < 2:
                per_chunk //= 2
                halvings += 1
                continue
            if error:
                return {"ok": False, "error": error}
            return {"ok": False,
                    "error": ("AniList would not return that list. The name may be wrong or "
                              "the list private - a private list can only be read with a "
                              "token - or AniList may be unreachable.")}
        collection = (data or {}).get("MediaListCollection") or {}
        for group in collection.get("lists") or []:
            # A custom list repeats entries that already appear under their
            # status group. Skipping the custom groups is the precise fix; the
            # dedupe below is the belt and braces.
            if group.get("isCustomList"):
                continue
            saw_any_list = True
            for entry in group.get("entries") or []:
                media_id = ((entry or {}).get("media") or {}).get("id")
                if not media_id or media_id in seen:
                    continue
                seen.add(media_id)
                row = _row_from_list_entry(entry, media_type)
                if row:
                    rows.append(row)
                else:
                    skipped += 1
        if not collection.get("hasNextChunk"):
            break
        chunk += 1
        if chunk > 100:                      # 50,000 entries; nobody has that
            break
    if not rows and not saw_any_list and not token:
        return {"ok": False,
                "error": ("AniList returned no lists for that name. The list may be "
                          "private, which only a token can read, or the name may be wrong.")}
    return {"ok": True, "media_type": media_type, "entries": rows,
            "skipped": skipped, "chunks": chunk, "per_chunk": per_chunk}


def _execute_anilist_authenticated(query: str, variables: Dict[str, Any], token: str,
                                   max_retries: int = 3) -> Dict[str, Any]:
    """Authenticated request. Returns {"data": ...} or {"error": "..."}.

    Never logs or echoes the token.
    """
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }
    attempt = 0
    rate_limit_waits = 0
    while attempt < max_retries:
        try:
            _throttle()
            req = urllib.request.Request(ANILIST_API_URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                _record_rate_limit(response.headers)
                result = json.loads(response.read().decode("utf-8"))
            errors = result.get("errors") or []
            if errors and not result.get("data"):
                return {"error": _describe_anilist_error(errors)}
            # A batched mutation can come back with data *and* errors: one alias
            # failed and the rest applied. Callers that do not batch ignore this.
            return {"data": result.get("data") or {}, "errors": errors}
        except urllib.error.HTTPError as exc:
            _record_rate_limit(exc.headers)
            if exc.code == 429:
                if rate_limit_waits >= MAX_RATE_LIMIT_WAITS:
                    return {"error": "AniList rate limit reached; try again shortly."}
                rate_limit_waits += 1
                try:
                    wait = float(exc.headers.get("Retry-After") or 60)
                except ValueError:
                    wait = 60.0
                time.sleep(min(max(wait, 1.0) + 1.0, 70.0))
                continue
            if exc.code in (400, 401, 403):
                body = {}
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    pass
                if exc.code in (401, 403):
                    return {"error": "AniList rejected the token. Paste a fresh one."}
                # A 400 is AniList refusing the document on argument validation.
                # It validates the whole document before running any of it, so a
                # batched caller learns nothing about which row is at fault here.
                return {"error": _describe_anilist_error(body.get("errors") or []),
                        "validation": True}
            attempt += 1
            if attempt >= max_retries:
                return {"error": f"AniList returned HTTP {exc.code}."}
            time.sleep(1.0)
        except Exception as exc:  # network, JSON, timeouts
            attempt += 1
            if attempt >= max_retries:
                return {"error": f"Could not reach AniList: {exc}"}
            time.sleep(1.0)
    return {"error": "Could not reach AniList."}


def _describe_anilist_error(errors: Sequence[Dict[str, Any]]) -> str:
    """Prefer AniList's own validation text; the docs say it is fit to show."""
    for err in errors or []:
        validation = err.get("validation") or {}
        messages = [m for field in validation.values() for m in (field or [])]
        if messages:
            return "; ".join(messages)
    for err in errors or []:
        if err.get("message"):
            return str(err["message"])
    return "AniList rejected the request."


def anilist_viewer(token: str) -> Dict[str, Any]:
    """Who the token belongs to. Read-only, used to confirm a token works."""
    result = _execute_anilist_authenticated(ANILIST_VIEWER_QUERY, {}, token)
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    viewer = (result.get("data") or {}).get("Viewer") or {}
    if not viewer.get("id"):
        return {"ok": False, "error": "AniList did not recognise that token."}
    return {"ok": True, "id": int(viewer["id"]), "name": viewer.get("name") or ""}


class AniListReadError(RuntimeError):
    """A bulk read of the user's existing list entries failed.

    Raised rather than skipped. With no existing entries for a chunk, every id
    in it looks like a create and carries no entry id -- exactly the
    mediaId-only behaviour the identity note above refuses to rely on. Guessing
    would duplicate rows the user already has, so the caller refuses instead.
    """


def anilist_existing_entries(token: str, user_id: int, media_type: str,
                             anilist_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """Map AniList media id -> the user's existing list entry, 50 at a time.

    Raises AniListReadError if any chunk fails: a partial answer is worse than
    none, because the ids it is missing would be written as creates.
    """
    found: Dict[int, Dict[str, Any]] = {}
    ids = [int(i) for i in anilist_ids if int(i or 0) > 0]
    media_enum = "MANGA" if media_type == "manga" else "ANIME"
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        result = _execute_anilist_authenticated(
            ANILIST_EXISTING_ENTRIES_QUERY,
            {"userId": user_id, "type": media_enum, "mediaIds": chunk},
            token,
        )
        if result.get("error"):
            raise AniListReadError(str(result["error"]))
        rows = (((result.get("data") or {}).get("Page") or {}).get("mediaList")) or []
        for row in rows:
            if row and row.get("mediaId"):
                found[int(row["mediaId"])] = row
    return found


def _score_raw(entry: Dict[str, Any], default_score: int = 0) -> int:
    """The POINT_100 score to send for a row.

    AniList stores every score out of 100 whatever the account displays, and
    this app works in 0-10. A row read back from an account therefore carries
    both: the exact raw value, and the rounded one the interface edits.
    Sending `score * 10` unconditionally turned an account's 87 into a 90.
    That is visible in a normal write's diff, and silent in a copy, which
    shows counts only - so "copy an account" quietly rewrote scores.

    The rounded score wins the moment the two disagree, because disagreement
    means a human edited it. The page deletes `score_raw` whenever it writes
    `score` for the same reason; this is the belt to that pair of braces, and
    it is what protects a hand-built payload that carries a stale raw value.
    """
    score = max(0, min(10, int(entry.get("score", default_score) or 0)))
    raw = entry.get("score_raw")
    if raw is None:
        return score * 10
    try:
        raw = max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return score * 10
    return raw if int(round(raw / 10)) == score else score * 10


def _fuzzy_date(value: Any) -> Optional[Dict[str, int]]:
    """AniList wants {year, month, day}; only complete dates qualify."""
    text = str(value or "").strip()
    if not text or text == "0000-00-00":
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return {"year": parsed.year, "month": parsed.month, "day": parsed.day}


# Fields the write can send, paired with the existing-entry field holding the
# account's current value. The order here is the order the diff is shown in.
DIFF_FIELDS = (
    ("status", "status"),
    ("progress", "progress"),
    ("progressVolumes", "progressVolumes"),
    ("scoreRaw", "score"),
    ("repeat", "repeat"),
    ("notes", "notes"),
    ("startedAt", "startedAt"),
    ("completedAt", "completedAt"),
    ("private", "private"),
    ("hiddenFromStatusLists", "hiddenFromStatusLists"),
    ("priority", "priority"),
)
_NUMERIC_DIFF_FIELDS = {"progress", "progressVolumes", "scoreRaw", "repeat"}
_DATE_DIFF_FIELDS = {"startedAt", "completedAt"}
_EMPTY_FUZZY_DATE = {"year": None, "month": None, "day": None}


def _fuzzy_tuple(value: Any) -> tuple:
    """A FuzzyDate as a comparable tuple. A missing part stays missing."""
    if not isinstance(value, dict):
        return (None, None, None)
    return tuple((value.get(key) or None) for key in ("year", "month", "day"))


def _fuzzy_or_empty(value: Any) -> Dict[str, Any]:
    """A stored FuzzyDate shaped for a write. All-None clears the date."""
    year, month, day = _fuzzy_tuple(value)
    return {"year": year, "month": month, "day": day}


def _same_value(field: str, left: Any, right: Any) -> bool:
    """Compare one field the way AniList means it.

    Numerics coerce with int(x or 0): progressVolumes is absent on anime and
    AniList returns null for a numeric field the user never set, so a bare
    comparison would either raise or report a change that is not one.
    """
    if field in _NUMERIC_DIFF_FIELDS:
        return int(left or 0) == int(right or 0)
    if field in _DATE_DIFF_FIELDS:
        return _fuzzy_tuple(left) == _fuzzy_tuple(right)
    if field == "notes":
        return str(left or "") == str(right or "")
    return (left or None) == (right or None)


def _guarded_value(field: str, before: Any, wanted: Any, media_type: str) -> Any:
    """What the "safe" policy actually sends. One rule per field, no ordering invented."""
    if field == "status":
        # A reread is the one real transition off COMPLETED. Everything else
        # passes: DROPPED -> CURRENT is an edit, not a regression.
        return before if before == "COMPLETED" and wanted != "REPEATING" else wanted
    if field == "scoreRaw":
        # A 0 is indistinguishable from "no score"; a real score is not.
        if int(wanted or 0) == 0 and int(before or 0) != 0:
            return before
        return wanted
    if field in ("progress", "repeat") or (field == "progressVolumes" and media_type == "manga"):
        return before if int(wanted or 0) < int(before or 0) else wanted
    # private, hiddenFromStatusLists and priority have no direction: a lower
    # priority is not a regression and neither boolean is a quantity, so there
    # is nothing here to guard against.
    return wanted


def _force_diff_row(diff: List[Dict[str, Any]], field: str, before: Any, outcome: Any) -> None:
    """Record what AniList will do to a field regardless of what we send."""
    for row in diff:
        if row["field"] != field:
            continue
        if not _same_value(field, row["applied"], outcome):
            row["applied"] = outcome
            row["by_anilist"] = True
        return
    if not _same_value(field, before, outcome):
        diff.append({"field": field, "from": before, "to": None, "applied": outcome,
                     "blocked": False, "by_anilist": True})


def _anilist_side_effects(variables: Dict[str, Any], known: Optional[Dict[str, Any]],
                          diff: List[Dict[str, Any]], totals: Dict[str, int]) -> None:
    """Fold AniList's own edits into the diff, so it predicts the account.

    AniList changes some fields itself on a status transition, whatever the
    write asks for. Both of these were **measured** against a live account
    (MANGA_SUPPORT_NOTES, L7-L8), not inferred from docs:

    - While `status` is `COMPLETED`, `progress` and `progressVolumes` are forced
      to the media total, even when a lower value is sent explicitly, and even
      when none is sent at all.
    - `REPEATING` -> `COMPLETED` increments `repeat` by one, whatever is sent.

    So `applied` in a diff row means *what the account will hold*, which is what
    the user is being asked to approve. Where that is not what we sent,
    `by_anilist` says so. Without this the confirm step would promise a change
    the API quietly discards -- the exact dishonesty this phase exists to remove.

    Not folded in, because it does not apply to this write path: a transition to
    `REPEATING` zeroes `progress` only when no progress is sent, and every
    payload here sends one (measured alongside the two above).
    """
    status = variables.get("status")
    if status != "COMPLETED":
        return
    for field, ceiling in totals.items():
        # A total of 0 means unknown - an ongoing series - so nothing to predict.
        if ceiling and field in variables:
            _force_diff_row(diff, field, (known or {}).get(field), ceiling)
    if (known or {}).get("status") == "REPEATING":
        was = int((known or {}).get("repeat") or 0)
        _force_diff_row(diff, "repeat", was, was + 1)


def _reconcile_with_existing(variables: Dict[str, Any], known: Optional[Dict[str, Any]],
                             media_type: str, policy: str = "safe",
                             overrides: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Apply the conflict policy in place and return the diff rows.

    `known` is the user's existing AniList entry, or None for a create -- every
    rule is then a no-op, because there is nothing to overwrite. `overrides` is
    per field: "mine" forces the row's own value through, downgrades included,
    and "clear" on completedAt sends an empty date, which is the only way to
    shift a completion date off an entry that is no longer completed.

    A diff row is omitted when the account already holds the value, so an empty
    diff means the row changes nothing.
    """
    overrides = overrides or {}
    guard = policy != "overwrite"
    diff: List[Dict[str, Any]] = []
    for field, source in DIFF_FIELDS:
        forced = overrides.get(field)
        before = (known or {}).get(source)
        if field == "completedAt" and forced == "clear":
            variables["completedAt"] = dict(_EMPTY_FUZZY_DATE)
            if not _same_value(field, before, None):
                diff.append({"field": field, "from": before, "to": None,
                             "applied": None, "blocked": False})
            continue
        if field not in variables:
            continue
        wanted = variables[field]
        if known is None:
            if not _same_value(field, None, wanted):
                diff.append({"field": field, "from": None, "to": wanted,
                             "applied": wanted, "blocked": False})
            continue
        if _same_value(field, before, wanted):
            continue
        applied = wanted
        if guard and forced != "mine":
            applied = _guarded_value(field, before, wanted, media_type)
        blocked = not _same_value(field, applied, wanted)
        if blocked:
            variables[field] = applied
        diff.append({"field": field, "from": before, "to": wanted,
                     "applied": applied, "blocked": blocked})
    return diff


def iter_write_payloads(entries: Sequence[Dict[str, Any]], media_type: str,
                        existing: Optional[Dict[int, Dict[str, Any]]] = None,
                        default_status: str = "Completed",
                        default_score: int = 0, policy: str = "safe",
                        overrides: Optional[Dict[Any, Dict[str, str]]] = None) -> tuple:
    """Build (payloads, skipped) for a direct AniList write.

    Progress derivation matches the XML builders exactly, so the same list
    produces the same numbers whichever route it travels.

    Each payload carries a `diff` of the fields that change against the user's
    existing entry, and the conflict policy has already been applied to
    `variables`, so what the diff says is what gets sent. `overrides` is keyed
    by AniList media id: {30642: {"progress": "mine"}}.
    """
    existing = existing or {}
    payloads: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    seen: Dict[int, str] = {}
    for entry in entries:
        anilist_id = int(entry.get("anilist_id") or 0)
        title = str(entry.get("title") or "")
        if anilist_id <= 0:
            skipped.append({"title": title, "reason": "no AniList id"})
            continue
        if anilist_id in seen:
            # Two rows can land on one entry - fixing two unmatched titles to
            # the same series does it. The XML path already refuses this
            # (gui.py, /api/update-export); without the same refusal here the
            # second row would write over the first, and the undo snapshot
            # would hold two rows for one entry with only one recorded id.
            # deduplicate_entries cannot do this job: it keys on MAL id or
            # title, and these rows differ in both.
            skipped.append({"title": title,
                            "reason": f"same AniList entry as {seen[anilist_id]!r}"})
            continue
        seen[anilist_id] = title
        raw_status = entry.get("status", default_status)
        try:
            normalized = _normalize_status(raw_status, media_type)
        except ValueError as exc:
            skipped.append({"title": title, "reason": str(exc)})
            continue
        status = ANILIST_STATUS_MAP.get(normalized.casefold())
        if not status:
            skipped.append({"title": title, "reason": f"cannot map status {normalized!r}"})
            continue
        repeating = _is_repeat_status(raw_status)
        if repeating:
            status = "REPEATING"

        finished = normalized.casefold() == "completed" or repeating
        if media_type == "manga":
            total_ch = int(entry.get("chapters") or 0)
            total_vol = int(entry.get("volumes") or 0)
            progress = _clamp_progress(entry.get("my_read_chapters", total_ch if finished else 0), total_ch)
            progress_volumes = _clamp_progress(entry.get("my_read_volumes", total_vol if finished else 0), total_vol)
            totals = {"progress": total_ch, "progressVolumes": total_vol}
        else:
            total_eps = int(entry.get("episodes") or 0)
            progress = _clamp_progress(entry.get("my_watched_episodes", total_eps if finished else 0), total_eps)
            progress_volumes = None
            totals = {"progress": total_eps}

        variables: Dict[str, Any] = {
            "mediaId": anilist_id,
            "status": status,
            "scoreRaw": _score_raw(entry, default_score),
            "progress": progress,
        }
        if progress_volumes is not None:
            variables["progressVolumes"] = progress_volumes
        if repeating:
            variables["repeat"] = 1
        notes = str(entry.get("comments") or "")
        if notes:
            variables["notes"] = notes[:6000]
        started = _fuzzy_date(entry.get("start_date"))
        if started:
            variables["startedAt"] = started
        completed = _fuzzy_date(entry.get("finish_date"))
        if completed:
            variables["completedAt"] = completed
        # Only a row read back from AniList carries these, so a MAL-XML row is
        # unaffected: the keys are absent and nothing is sent.
        if "private" in entry:
            variables["private"] = bool(entry["private"])
        if "hidden_from_status_lists" in entry:
            variables["hiddenFromStatusLists"] = bool(entry["hidden_from_status_lists"])
        if "priority" in entry:
            variables["priority"] = int(entry.get("priority") or 0)

        known = existing.get(anilist_id)
        row_overrides = (overrides or {}).get(anilist_id) or (overrides or {}).get(str(anilist_id)) or {}
        diff = _reconcile_with_existing(variables, known, media_type, policy, row_overrides)
        _anilist_side_effects(variables, known, diff, totals)
        if known and known.get("id"):
            # Supplying the existing entry id makes this an update by contract,
            # so a repeated write cannot create a second row.
            variables["id"] = int(known["id"])

        # completedAt is only ever sent when the row carries one, so taking an
        # entry off COMPLETED otherwise leaves the old completion date behind
        # and the account holds a row that is in progress and also finished.
        # Flagged here; clearing it is an explicit override, never automatic.
        stale_completed_at = bool(
            known
            and variables.get("status") != "COMPLETED"
            and _fuzzy_tuple(known.get("completedAt"))[0] is not None
            and "completedAt" not in variables)

        payloads.append({"title": title, "anilist_id": anilist_id,
                         "update": bool(known), "variables": variables,
                         "diff": diff, "stale_completed_at": stale_completed_at})
    return payloads, skipped


def anilist_delete_entry(token: str, entry_id: int) -> Dict[str, Any]:
    """Remove a list entry. Only ever used to undo an entry this tool created."""
    result = _execute_anilist_authenticated(
        ANILIST_DELETE_ENTRY_MUTATION, {"id": int(entry_id)}, token)
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    deleted = ((result.get("data") or {}).get("DeleteMediaListEntry")) or {}
    if not deleted.get("deleted"):
        return {"ok": False, "error": "AniList did not confirm the delete."}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Undo snapshots
#
# The write is the only irreversible thing this tool does, so every run records
# what it is about to replace. A snapshot never contains the token: undo needs
# one re-supplied, exactly like the write. A created entry has no prior state -
# its new entry id exists only in the mutation response - so the writer fills
# that in as each create succeeds, which leaves a snapshot that is accurate up
# to the last completed row even if the run crashes.
# ---------------------------------------------------------------------------
UNDO_SNAPSHOT_VERSION = 1
UNDO_SNAPSHOT_KEEP = 5
# Unix seconds are 10 digits until 2286, so a plain name sort is a time sort.
_UNDO_NAME_RE = re.compile(r"^\.aniexport_undo_\d+\.json$")
_UNDO_GLOB = ".aniexport_undo_*.json"
# Everything the write can touch, so a restore can put all of it back.
UNDO_PRIOR_FIELDS = ("id", "status", "progress", "progressVolumes", "score",
                     "repeat", "notes", "startedAt", "completedAt")


def undo_snapshot_dir() -> Path:
    """Beside the response cache, so one env var moves both."""
    return cache_path().parent


def _undo_snapshot_path(name: Any) -> Optional[Path]:
    """Resolve a snapshot name. Arrives over HTTP, so only a bare name passes."""
    base = str(name or "").strip()
    if not _UNDO_NAME_RE.match(base):
        return None
    return undo_snapshot_dir() / base


def build_undo_snapshot(media_type: str, viewer_id: int,
                        payloads: Sequence[Dict[str, Any]],
                        existing: Optional[Dict[int, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The state to restore, captured before the first mutation."""
    rows: List[Dict[str, Any]] = []
    for payload in payloads:
        anilist_id = int(payload.get("anilist_id") or 0)
        known = (existing or {}).get(anilist_id)
        row = {"anilist_id": anilist_id, "title": str(payload.get("title") or "")}
        if payload.get("update") and known:
            row["was_create"] = False
            row["prior"] = {field: known.get(field) for field in UNDO_PRIOR_FIELDS}
        else:
            row["was_create"] = True
            row["created_entry_id"] = None
        rows.append(row)
    return {"version": UNDO_SNAPSHOT_VERSION, "written_at": int(time.time()),
            "media_type": media_type, "viewer_id": int(viewer_id or 0), "rows": rows}


def save_undo_snapshot(snapshot: Dict[str, Any], name: Optional[str] = None) -> Optional[str]:
    """Atomic write, same pattern as save_cache. Returns the file name, or None."""
    directory = undo_snapshot_dir()
    name = name or ".aniexport_undo_%d.json" % int(snapshot.get("written_at") or time.time())
    path = directory / name
    tmp = path.with_name(path.name + ".tmp")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return name


def load_undo_snapshot(name: Any) -> Optional[Dict[str, Any]]:
    """Read one snapshot back. Any damage or version mismatch means "no snapshot"."""
    path = _undo_snapshot_path(name)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != UNDO_SNAPSHOT_VERSION:
        return None
    if not isinstance(data.get("rows"), list):
        return None
    return data


def list_undo_snapshots() -> List[Dict[str, Any]]:
    """Newest first, for the undo picker."""
    out: List[Dict[str, Any]] = []
    try:
        names = sorted((q.name for q in undo_snapshot_dir().glob(_UNDO_GLOB)), reverse=True)
    except OSError:
        return out
    for name in names:
        snapshot = load_undo_snapshot(name)
        if not snapshot:
            continue
        rows = snapshot.get("rows") or []
        out.append({"name": name, "written_at": snapshot.get("written_at"),
                    "media_type": snapshot.get("media_type"), "rows": len(rows),
                    "creates": sum(1 for r in rows if r.get("was_create")),
                    "deletable": sum(1 for r in rows
                                     if r.get("was_create") and r.get("created_entry_id"))})
    return out


def prune_undo_snapshots(keep: int = UNDO_SNAPSHOT_KEEP) -> None:
    """Keep the newest few; a snapshot stops being useful once a later one exists."""
    try:
        found = sorted(undo_snapshot_dir().glob(_UNDO_GLOB))
    except OSError:
        return
    for path in (found[:-keep] if keep > 0 else found):
        try:
            path.unlink()
        except OSError:
            pass


def record_created_entry(snapshot: Dict[str, Any], anilist_id: int, entry_id: int) -> None:
    """Remember the id of an entry the write just created, so undo can delete it."""
    for row in snapshot.get("rows") or []:
        if int(row.get("anilist_id") or 0) == int(anilist_id or 0) and row.get("was_create"):
            row["created_entry_id"] = int(entry_id)
            return


def iter_undo_payloads(snapshot: Dict[str, Any], delete_created: bool = False) -> tuple:
    """Build (restores, deletes, unrecoverable) from a snapshot.

    A restore sends every prior field, the empty ones included - that is the
    point. Putting back a note the write replaced, or a date it set, needs the
    empty value the write path deliberately never sends.

    One caveat that cannot be coded around: AniList stamps `startedAt` itself
    when an entry becomes CURRENT without one, so restoring "no start date" may
    not stick. Measured, not assumed - see MANGA_SUPPORT_NOTES, M7.
    """
    media_type = "manga" if snapshot.get("media_type") == "manga" else "anime"
    restores: List[Dict[str, Any]] = []
    deletes: List[Dict[str, Any]] = []
    unrecoverable: List[Dict[str, str]] = []
    for row in snapshot.get("rows") or []:
        title = str(row.get("title") or "")
        anilist_id = int(row.get("anilist_id") or 0)
        if row.get("was_create"):
            entry_id = int(row.get("created_entry_id") or 0)
            if not entry_id:
                unrecoverable.append(
                    {"title": title,
                     "reason": "the create never reported an entry id, so there is "
                               "nothing safe to delete"})
            elif delete_created:
                deletes.append({"title": title, "anilist_id": anilist_id,
                                "entry_id": entry_id})
            continue
        prior = row.get("prior") or {}
        entry_id = int(prior.get("id") or 0)
        if not entry_id:
            unrecoverable.append({"title": title, "reason": "no prior entry id was recorded"})
            continue
        variables: Dict[str, Any] = {
            "id": entry_id,
            "mediaId": anilist_id,
            "scoreRaw": int(prior.get("score") or 0),
            "progress": int(prior.get("progress") or 0),
            "repeat": int(prior.get("repeat") or 0),
            "notes": str(prior.get("notes") or ""),
            "startedAt": _fuzzy_or_empty(prior.get("startedAt")),
            "completedAt": _fuzzy_or_empty(prior.get("completedAt")),
        }
        if prior.get("status"):
            # Absent rather than guessed: an invented status is a second write.
            variables["status"] = prior["status"]
        if media_type == "manga":
            variables["progressVolumes"] = int(prior.get("progressVolumes") or 0)
        restores.append({"title": title, "anilist_id": anilist_id,
                         "update": True, "variables": variables})
        # L8: restoring COMPLETED over an entry the write left REPEATING makes
        # AniList increment `repeat`, and it beats the value sent alongside the
        # status - measured. A second write carrying only `repeat` does stick,
        # so the restore is followed by one. Emitted only for a row that has a
        # reread count to lose, so an ordinary undo costs no extra requests.
        if prior.get("status") == "COMPLETED" and int(prior.get("repeat") or 0) > 0:
            restores.append({"title": title, "anilist_id": anilist_id, "update": True,
                             "repair": "repeat",
                             "variables": {"id": entry_id, "mediaId": anilist_id,
                                           "repeat": int(prior.get("repeat") or 0)}})
    return restores, deletes, unrecoverable


# ---------------------------------------------------------------------------
# Batched writes
#
# One document carrying several aliased SaveMediaListEntry calls costs a single
# request against the rate limit - measured, not assumed. A two-alias document
# decremented X-RateLimit-Remaining by exactly 1 against a control of three
# single-operation requests that took 1 each, and an eight-alias one did the
# same (MANGA_SUPPORT_NOTES, L5 and P15). At 339 rows that turns roughly twelve
# minutes of writing into about ninety seconds.
# ---------------------------------------------------------------------------
WRITE_BATCH_SIZE = 8

# Every argument a payload may carry, with its GraphQL type. Only the arguments
# an alias actually has are declared, so a batch never sends an explicit null
# for a field the row left alone - which would blank it on the account.
WRITE_FIELD_TYPES = {
    "id": "Int",
    "mediaId": "Int",
    "status": "MediaListStatus",
    "scoreRaw": "Int",
    "progress": "Int",
    "progressVolumes": "Int",
    "repeat": "Int",
    "notes": "String",
    "startedAt": "FuzzyDateInput",
    "completedAt": "FuzzyDateInput",
    "private": "Boolean",
    "hiddenFromStatusLists": "Boolean",
    "priority": "Int",
}


def _write_batch_query(payloads: Sequence[Dict[str, Any]]) -> tuple:
    """Build one document of m0..mN aliased saves.

    Returns (query, variables, spans). `spans[i]` is the (start, end) character
    range of alias i inside the finished document, which is the only way to map
    an error back to a row: GraphQL errors carry `locations`, never a `path`.

    Built per batch rather than from a fixed template, because the optional
    arguments differ row by row - unlike the search batcher, where every alias
    has the same shape.
    """
    declarations: List[str] = []
    variables: Dict[str, Any] = {}
    blocks: List[str] = []
    for index, payload in enumerate(payloads):
        fields = [f for f in WRITE_FIELD_TYPES if f in (payload.get("variables") or {})]
        for field in fields:
            declarations.append(f"${field}{index}: {WRITE_FIELD_TYPES[field]}")
            variables[f"{field}{index}"] = payload["variables"][field]
        arguments = ", ".join(f"{field}: ${field}{index}" for field in fields)
        blocks.append(f"\n  m{index}: SaveMediaListEntry({arguments}) {{ id mediaId status }}")
    head = f"mutation ({', '.join(declarations)}) {{"
    spans: List[tuple] = []
    cursor = len(head)
    for block in blocks:
        spans.append((cursor, cursor + len(block)))
        cursor += len(block)
    return head + "".join(blocks) + "\n}", variables, spans


def _offset_of(query: str, line: int, column: int) -> Optional[int]:
    """Turn a GraphQL error location into a character offset in the document."""
    try:
        line, column = int(line), int(column)
    except (TypeError, ValueError):
        return None
    if line < 1 or column < 1:
        return None
    offset = 0
    for number, text in enumerate(query.split("\n"), start=1):
        if number == line:
            return offset + column - 1
        offset += len(text) + 1
    return None


def _attribute_batch_errors(errors: Sequence[Dict[str, Any]], query: str,
                            spans: Sequence[tuple]) -> Dict[int, str]:
    """Map alias index -> message, using each error's location.

    An error whose location falls outside every alias - or carries no usable
    location at all - is not guessed at. The caller reports the batch-level
    message against every failed alias instead.
    """
    found: Dict[int, str] = {}
    for error in errors or []:
        message = _describe_anilist_error([error])
        for location in (error.get("locations") or []):
            offset = _offset_of(query, location.get("line"), location.get("column"))
            if offset is None:
                continue
            for index, (start, end) in enumerate(spans):
                if start <= offset < end:
                    found.setdefault(index, message)
                    break
    return found


def anilist_write_batch(token: str, payloads: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Write several entries in one request. One result per payload, in order.

    Aliases execute serially and a failing one does not stop the rest, so the
    authoritative signal that row k failed is `data["mk"] is None` - not the
    error list, which may be shorter, longer, or unattributable.
    """
    if not payloads:
        return []
    query, variables, spans = _write_batch_query(payloads)
    result = _execute_anilist_authenticated(query, variables, token)
    if result.get("error"):
        out = [{"ok": False, "error": result["error"]} for _ in payloads]
        # Validation refuses the whole document and names no alias, so the
        # faulty row is only findable one row at a time. Auth and transport
        # failures are not retried: every row would fail the same way.
        if result.get("validation") and len(payloads) > 1:
            for index in range(len(payloads)):
                out[index] = anilist_write_batch(token, [payloads[index]])[0]
        return out
    data = result.get("data") or {}
    errors = result.get("errors") or []
    per_alias = _attribute_batch_errors(errors, query, spans)
    batch_message = _describe_anilist_error(errors) if errors else ""
    out: List[Dict[str, Any]] = []
    for index, _payload in enumerate(payloads):
        saved = data.get(f"m{index}")
        if saved and saved.get("id"):
            out.append({"ok": True, "id": int(saved["id"]), "status": saved.get("status")})
        else:
            out.append({"ok": False,
                        "error": per_alias.get(index) or batch_message
                        or "AniList did not confirm the save."})

    # AniList validates the arguments of the whole document before executing
    # any of it, so one unwritable row nulls *every* alias in its batch -
    # measured against the live API, not inferred from the docs. Without this
    # fallback a single stale media id would take the other seven rows down
    # with it, and "Retry failed" would re-batch the same eight for ever.
    # Only aliases that came back null are re-sent, so a row that did execute
    # is never written twice.
    stalled = [i for i, row in enumerate(out) if not row["ok"]]
    if stalled and len(payloads) > 1:
        for index in stalled:
            out[index] = anilist_write_batch(token, [payloads[index]])[0]
    return out


def _report_populations(entries: Sequence[Dict[str, Any]], media_type: str) -> None:
    """Three populations, not two.

    A title that exists on AniList without a MAL id is not a failure: it simply
    cannot travel inside MAL XML. Reporting it as "missing MAL ID" next to
    titles that were never found reads as the list being wrong.
    """
    exportable = [e for e in entries if int(e.get("id") or 0) > 0]
    anilist_only = [e for e in entries
                    if int(e.get("id") or 0) <= 0 and int(e.get("anilist_id") or 0) > 0]
    missing = [e for e in entries
               if int(e.get("id") or 0) <= 0 and int(e.get("anilist_id") or 0) <= 0]
    print(f"[*] Unique {media_type} candidates: {len(entries)}; "
          f"in this export: {len(exportable)}; "
          f"on AniList without a MAL ID: {len(anilist_only)}; "
          f"not found: {len(missing)}")
    if anilist_only:
        print(f"[!] {len(anilist_only)} title(s) exist on AniList but have no MyAnimeList ID, "
              f"so MAL XML cannot carry them. Use the GUI's direct AniList write:")
        for entry in anilist_only[:10]:
            print(f"      AniList {entry.get('anilist_id')}  {entry.get('title')}")
        if len(anilist_only) > 10:
            print(f"      ... and {len(anilist_only) - 10} more")
    if missing:
        print(f"[!] {len(missing)} title(s) were not found on AniList at all "
              f"(check spelling, or try the romaji title):")
        for entry in missing[:10]:
            print(f"      {entry.get('title')}")
        if len(missing) > 10:
            print(f"      ... and {len(missing) - 10} more")


def process_anime_list(
    titles: Sequence[str],
    output_xml: Optional[Path] = None,
    output_gz: Optional[Path] = None,
    status: str = "Completed",
    score: int = 0,
    expand_sequels: bool = False,
    use_cache: bool = True,
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

    resolved_map = resolve_many_entries(
        titles, media_type="anime", expand_sequels=expand_sequels,
        session_cache=session_cache, use_cache=use_cache,
    )
    for idx, raw_title in enumerate(titles, start=1):
        for resolved in resolved_map.get(raw_title, []):
            resolved_entries.append(resolved)
            mal_id = resolved["id"]
            norm_title = resolved["title"]
            eps = resolved.get("episodes", 0)
            stype = resolved["type"]

            if raw_title.lower() != norm_title.lower():
                print(f"  [{idx}/{total_titles}] '{raw_title}' -> '{norm_title}' (MAL ID: {mal_id}, {stype}, {eps} eps)")
            else:
                print(f"  [{idx}/{total_titles}] '{norm_title}' (MAL ID: {mal_id}, {stype}, {eps} eps)")

    # Deduplicate
    unique_entries = deduplicate_entries(resolved_entries)
    print()
    _report_populations(unique_entries, "anime")

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


def _cli_output_paths(args) -> tuple:
    """Where the CLI writes, for whichever route produced the rows."""
    manga = args.media_type == "manga"
    stem = "myanimelist_manga_export" if manga else "myanimelist_export"
    if args.output:
        output_xml = Path(args.output)
    elif Path("output").is_dir():
        output_xml = Path("output/%s.xml" % stem)
    else:
        output_xml = None
    if args.gz_output:
        output_gz = Path(args.gz_output)
    elif Path("output").is_dir():
        output_gz = Path("output/%s.xml.gz" % stem)
    else:
        output_gz = None
    return output_xml, output_gz


def _export_anilist_list(args) -> None:
    """--from-anilist: read a public list and write it straight to MAL XML.

    Read-only, so it needs no token and takes none. Entries AniList has but
    MyAnimeList does not are counted rather than dropped quietly - they are the
    same "AniList only" rows the GUI reports.
    """
    media_type = args.media_type
    print("[*] Reading %s's %s list from AniList..." % (args.from_anilist, media_type))
    result = anilist_list_collection(media_type, username=args.from_anilist)
    if not result.get("ok"):
        print("Error: %s" % result.get("error", "could not read that list."))
        sys.exit(1)
    rows = result["entries"]
    if not rows:
        print("That list is empty: nothing to export.")
        sys.exit(1)
    exportable = [r for r in rows if r.get("can_export_xml")]
    anilist_only = len(rows) - len(exportable)
    print("[*] %d entries read, %d exportable as MAL XML, %d AniList-only (no MAL id)"
          % (len(rows), len(exportable), anilist_only))

    output_xml, output_gz = _cli_output_paths(args)
    builder = build_mal_manga_xml if media_type == "manga" else build_mal_xml
    stem = "myanimelist_manga_export" if media_type == "manga" else "myanimelist_export"
    xml_bytes = builder(rows, args.status, args.score).encode("utf-8")
    target_xml = output_xml or (Path.cwd() / ("%s.xml" % stem))
    target_gz = output_gz or (Path.cwd() / ("%s.xml.gz" % stem))
    target_xml.parent.mkdir(parents=True, exist_ok=True)
    target_gz.parent.mkdir(parents=True, exist_ok=True)
    target_xml.write_bytes(xml_bytes)
    target_gz.write_bytes(gzip.compress(xml_bytes, compresslevel=9))
    root = ET.fromstring(xml_bytes)
    count = len(root.findall("manga" if media_type == "manga" else "anime"))
    print("[OK] XML validated. Exportable %s entries: %d" % (media_type, count))
    print("[+] XML: %s" % target_xml.resolve())
    print("[+] GZ:  %s" % target_gz.resolve())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a plain-text anime or manga title list into MAL-compatible XML for AniList import."
    )
    parser.add_argument("--version", action="version", version=f"AniExport {__version__}")
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
        "--no-cache",
        action="store_true",
        help="Ignore and do not write the on-disk AniList cache (default: .anilist_cache.json in the current directory, 7-day TTL; override the path with ANIEXPORT_CACHE).",
    )
    parser.add_argument(
        "--expand-sequels",
        action="store_true",
        help="Automatically include direct sequel relations (anime sequels/movies or manga sequels).",
    )
    parser.add_argument(
        "--from-anilist",
        metavar="USERNAME",
        default=None,
        help="Read an existing AniList list instead of resolving a text file, and export it as "
             "MAL XML. Read-only. The list must be public; a private one needs the GUI, which "
             "can supply a token.",
    )

    args = parser.parse_args()

    if args.from_anilist:
        _export_anilist_list(args)
        return

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

    output_xml, output_gz = _cli_output_paths(args)

    titles = load_titles_from_txt(input_file)
    if not titles:
        print(f"Error: No {args.media_type} titles found in {input_file}.")
        sys.exit(1)

    if args.media_type == "manga":
        cache: Dict[str, List[Dict[str, Any]]] = {}
        resolved: List[Dict[str, Any]] = []

        def show(stage: str, done: int, total_count: int, info=None) -> None:
            label = (info or {}).get("title") or ""
            print(f"[*] {stage} {done}/{total_count}: {label}")

        resolved_map = resolve_many_entries(
            titles, media_type="manga", expand_sequels=args.expand_sequels,
            session_cache=cache, use_cache=not args.no_cache, on_progress=show,
        )
        for title in titles:
            for entry in resolved_map.get(title, []):
                item = dict(entry)
                item["status"] = args.status
                item["score"] = args.score
                resolved.append(item)
        unique = deduplicate_entries(resolved)
        _report_populations(unique, "manga")
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
            use_cache=not args.no_cache,
        )


if __name__ == "__main__":
    main()
