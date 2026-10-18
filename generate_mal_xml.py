"""
XML Generator
"""

from __future__ import annotations

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
      startDate { year }
      popularity
      coverImage { medium }
      format
      episodes
      status
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
      startDate { year }
      popularity
      coverImage { medium }
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

ANILIST_BY_MAL_ID_QUERY = """
query ($idMal: Int, $type: MediaType) {
  Media (idMal: $idMal, type: $type) {
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
        "year": (media.get("startDate") or {}).get("year"),
        "popularity": int(media.get("popularity") or 0),
        "cover": (media.get("coverImage") or {}).get("medium"),
        # Two destinations, two identity requirements. A title with an AniList id
        # but no MAL id is not unresolved, it simply cannot travel inside MAL XML.
        "can_export_xml": mal_id > 0,
        "can_write_anilist": anilist_id > 0,
    }


def _candidates(nodes: Sequence[Dict[str, Any]], media_type: str, fallback_title: str,
                prefer_mal_id: bool = True) -> List[Dict[str, Any]]:
    """Alternatives for a search, so a wrong auto-pick can be corrected.

    Includes entries without a MAL ID: they are exportable by a direct AniList
    write even though MAL XML cannot carry them, and hiding them left the user
    no way to choose the right entry.
    """
    parse = _parse_manga_node if media_type == "manga" else _parse_media_node
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for node in _rank_nodes(nodes, fallback_title, prefer_mal_id):
        if not node:
            continue
        # Dedupe on MAL id when there is one, otherwise on the AniList id, so
        # AniList-only entries stay selectable instead of being filtered out.
        key = ("mal", node["idMal"]) if node.get("idMal") else ("al", node.get("id"))
        if key[1] in (None, 0) or key in seen:
            continue
        seen.add(key)
        out.append(parse(node, fallback_title=fallback_title))
    return out


def _normalize_title(value: Any) -> str:
    """Fold a title to a comparable form: case, accents and punctuation removed."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def _media_rank(node: Dict[str, Any], search_title: str = "", prefer_mal_id: bool = True) -> tuple:
    """Lexicographic ranking key, highest first.

    Tiers rather than additive weights, deliberately. An earlier draft scored
    `idMal +1000, ONE_SHOT -500, exact title +300, ...` and claimed a full
    series always beat a one-shot; it did not. A one-shot with a perfect title
    match scored 1410 against 700 for a not-yet-released full series, which is
    exactly the Boruto / Gotoubun failure this code exists to prevent. Tiers
    make that invariant true by construction.
    """
    has_mal_id = 1 if node.get("idMal") else 0
    not_one_shot = 0 if (node.get("format") or "").upper() == "ONE_SHOT" else 1
    released = 0 if (node.get("status") or "").upper() == "NOT_YET_RELEASED" else 1

    titles = node.get("title") or {}
    wanted = _normalize_title(search_title)
    english = _normalize_title(titles.get("english"))
    romaji = _normalize_title(titles.get("romaji"))

    score = 0.0
    if wanted:
        if english and english == wanted:
            score += 300
        elif romaji and romaji == wanted:
            score += 260
        best_ratio = max(
            difflib.SequenceMatcher(None, wanted, english).ratio() if english else 0.0,
            difflib.SequenceMatcher(None, wanted, romaji).ratio() if romaji else 0.0,
        )
        score += best_ratio * 200
    popularity = int(node.get("popularity") or 0)
    score += min(50, round(math.log10(popularity + 1) * 10))
    if not prefer_mal_id:
        # Writing straight to AniList needs the best AniList entry. Ranking a
        # MAL-linked duplicate above the canonical one would pick the wrong row.
        return (not_one_shot, released, score)
    return (has_mal_id, not_one_shot, released, score)


def _rank_nodes(nodes: Sequence[Dict[str, Any]], search_title: str = "",
                prefer_mal_id: bool = True) -> List[Dict[str, Any]]:
    """Best first, stable: equal candidates keep AniList's own relevance order."""
    decorated = [(_media_rank(n, search_title, prefer_mal_id), i, n) for i, n in enumerate(nodes) if n]
    decorated.sort(key=lambda item: (tuple(-v for v in item[0]), item[1]))
    return [item[2] for item in decorated]


def _pick_best_media(nodes: Sequence[Dict[str, Any]], search_title: str = "",
                     prefer_mal_id: bool = True) -> Optional[Dict[str, Any]]:
    """Choose the most importable search hit.

    AniList's top relevance hit is frequently a duplicate entry or a one-shot
    spin-off with ``idMal: null``, which cannot be imported at all.
    """
    if not nodes:
        return None
    if prefer_mal_id and not any(n and n.get("idMal") for n in nodes):
        # No MAL id anywhere: still return the best AniList match rather than
        # whatever AniList listed first, so the row is usable for a direct write.
        return _rank_nodes(nodes, search_title, prefer_mal_id=False)[0]
    return _rank_nodes(nodes, search_title, prefer_mal_id)[0]


# Deterministic rewrites tried when a title resolves to nothing. These fix
# formatting, never spelling: AniList returns zero hits for "Raphsody" or
# "Aristrocat", so no rule here can recover a typo.


def _search_variants(title: str, media_type: str = "manga") -> List[str]:
    original = " ".join(str(title or "").split())
    out: List[str] = []

    def add(value: str) -> None:
        value = " ".join(value.split())
        if value and value != original and value not in out:
            out.append(value)

    stage1 = unicodedata.normalize("NFKC", original)
    for curly, straight in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"')):
        stage1 = stage1.replace(curly.encode().decode("unicode_escape"), straight)
    stage1 = " ".join(stage1.split())
    add(stage1)

    stage2 = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stage1)
    add(stage2)

    if media_type == "manga":
        stage3 = re.sub(r"\s+(manga|manhua|manhwa)\s*$", "", stage2 or stage1, flags=re.IGNORECASE)
        add(stage3)

    return out[:3]


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
        "year": (media.get("startDate") or {}).get("year"),
        "popularity": int(media.get("popularity") or 0),
        "cover": (media.get("coverImage") or {}).get("medium"),
        "can_export_xml": int(media.get("idMal") or 0) > 0,
        "can_write_anilist": int(media.get("id") or 0) > 0,
    }


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
        "User-Agent": "MALAnimeExporter/2.0",
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
# Batches whose request failed outright in the most recent resolve_many_entries
# call. Lets a caller tell "AniList was unreachable" from "no such title".
last_run_failures: List[List[str]] = []
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_MAX_ENTRIES = 10000
CACHE_VERSION = 1
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
                entry["candidates"] = _candidates(nodes, media_type, name, prefer_mal_id)
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

    if stopped:
        return {title: [dict(e) for e in results[title]] for title in titles if title in results}

    last_run_failures.clear()
    last_run_failures.extend(failures)

    for title in unique:
        if title not in results:
            results[title] = [_unresolved_entry(title, media_type)]

    if use_cache:
        stamp = time.time()
        for title in unique:
            if _cacheable(results[title]):
                disk[_cache_key(title, media_type, expand_sequels, prefer_mal_id)] = {
                    "ts": stamp, "data": results[title]}
        save_cache(disk)
    for title in unique:
        if _cacheable(results[title]):
            session[_cache_key(title, media_type, expand_sequels, prefer_mal_id)] = results[title]

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
      score
    }
  }
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


def _execute_anilist_authenticated(query: str, variables: Dict[str, Any], token: str,
                                   max_retries: int = 3) -> Dict[str, Any]:
    """Authenticated request. Returns {"data": ...} or {"error": "..."}.

    Never logs or echoes the token.
    """
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MALAnimeExporter/2.0",
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
            return {"data": result.get("data") or {}}
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
                return {"error": _describe_anilist_error(body.get("errors") or [])}
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


def anilist_existing_entries(token: str, user_id: int, media_type: str,
                             anilist_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """Map AniList media id -> the user's existing list entry, 50 at a time."""
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
            continue
        rows = (((result.get("data") or {}).get("Page") or {}).get("mediaList")) or []
        for row in rows:
            if row and row.get("mediaId"):
                found[int(row["mediaId"])] = row
    return found


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


def iter_write_payloads(entries: Sequence[Dict[str, Any]], media_type: str,
                        existing: Optional[Dict[int, Dict[str, Any]]] = None,
                        default_status: str = "Completed",
                        default_score: int = 0) -> tuple:
    """Build (payloads, skipped) for a direct AniList write.

    Progress derivation matches the XML builders exactly, so the same list
    produces the same numbers whichever route it travels.
    """
    existing = existing or {}
    payloads: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for entry in entries:
        anilist_id = int(entry.get("anilist_id") or 0)
        title = str(entry.get("title") or "")
        if anilist_id <= 0:
            skipped.append({"title": title, "reason": "no AniList id"})
            continue
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
        else:
            total_eps = int(entry.get("episodes") or 0)
            progress = _clamp_progress(entry.get("my_watched_episodes", total_eps if finished else 0), total_eps)
            progress_volumes = None

        variables: Dict[str, Any] = {
            "mediaId": anilist_id,
            "status": status,
            "scoreRaw": max(0, min(10, int(entry.get("score", default_score) or 0))) * 10,
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

        known = existing.get(anilist_id)
        if known and known.get("id"):
            # Supplying the existing entry id makes this an update by contract,
            # so a repeated write cannot create a second row.
            variables["id"] = int(known["id"])

        payloads.append({"title": title, "anilist_id": anilist_id,
                         "update": bool(known), "variables": variables})
    return payloads, skipped


def anilist_write_entry(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write one entry. Returns {"ok": True, "id": ...} or {"ok": False, "error": ...}."""
    result = _execute_anilist_authenticated(
        ANILIST_SAVE_ENTRY_MUTATION, payload["variables"], token)
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    saved = ((result.get("data") or {}).get("SaveMediaListEntry")) or {}
    if not saved.get("id"):
        return {"ok": False, "error": "AniList did not confirm the save."}
    return {"ok": True, "id": int(saved["id"]), "status": saved.get("status")}


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
        "--no-cache",
        action="store_true",
        help="Ignore and do not write the on-disk AniList cache (default: .anilist_cache.json in the current directory, 7-day TTL; override the path with ANIEXPORT_CACHE).",
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
