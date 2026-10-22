"""Choosing which AniList entry a pasted title meant.

Split out of generate_mal_xml.py, which was three thousand lines doing five
jobs. Nothing here touches the network.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence


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


MEDIA_STATUS_LABELS = {
    "FINISHED": "Finished",
    "RELEASING": "Releasing",
    "NOT_YET_RELEASED": "Not yet released",
    "CANCELLED": "Cancelled",
    "HIATUS": "On hiatus",
}

DESCRIPTION_LIMIT = 260


def _media_details(media: Dict[str, Any]) -> Dict[str, Any]:
    """The extra context the match picker shows, under one key.

    Nested rather than flattened on purpose: the picker's result is merged into
    the user's row, and a top-level `status` or `score` here would overwrite the
    row's own list status and score.
    """
    titles = media.get("title") or {}
    start = media.get("startDate") or {}
    end = media.get("endDate") or {}
    text = re.sub(r"<[^>]+>", "", str(media.get("description") or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > DESCRIPTION_LIMIT:
        text = text[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0] + "..."
    raw_status = str(media.get("status") or "")
    mean = media.get("averageScore")
    return {
        "english": titles.get("english") or "",
        "romaji": titles.get("romaji") or "",
        "native": titles.get("native") or "",
        # Synonyms are how a pasted title often differs from the official one,
        # so they are the most useful field for judging a doubtful match.
        "synonyms": [str(x) for x in (media.get("synonyms") or [])][:6],
        "genres": [str(x) for x in (media.get("genres") or [])][:4],
        "mean_score": int(mean) if isinstance(mean, (int, float)) else None,
        "popularity": int(media.get("popularity") or 0),
        "status": MEDIA_STATUS_LABELS.get(raw_status, raw_status.replace("_", " ").title()),
        "start_year": start.get("year"),
        "end_year": end.get("year"),
        "url": media.get("siteUrl") or "",
        "description": text,
    }


def _parse_media_node(media: Dict[str, Any], fallback_title: str = "") -> Dict[str, Any]:
    titles = media.get("title") or {}
    detail = _media_details(media) if "siteUrl" in media else None
    official_title = titles.get("english") or titles.get("romaji") or fallback_title
    raw_format = (media.get("format") or "TV").upper()
    series_type = FORMAT_MAP.get(raw_format, "TV")
    mal_id = int(media.get("idMal") or 0)
    anilist_id = int(media.get("id") or 0)
    eps = int(media.get("episodes") or 0)
    entry = {
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
    # How close this was to what the user actually typed. None when nothing was
    # searched - a row read back from an AniList list was never a guess.
    if fallback_title:
        ratio = round(_title_ratio(media, fallback_title), 4)
        entry["match_ratio"] = ratio
        alias = _alias_hit(media, fallback_title)
        if alias:
            entry["alias_hit"] = True
        # An exact alias settles the question the ratio was guessing at, so it
        # clears a low-score flag. It says nothing about the entry being the
        # wrong *kind* of thing, so it does not clear the checks below.
        entry["needs_review"] = ratio < REVIEW_RATIO_FLOOR and not alias
        default_win = _won_by_default(media)
        if default_win:
            entry["needs_review"] = True
            entry["review_reason"] = default_win
    if detail:
        entry["details"] = detail
    return entry


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
    if out and out[0].get("match_ratio") is not None:
        # A tie is as suspicious as a poor match: two candidates scoring the
        # same means the ranker had no real reason to prefer either. With fewer
        # than two candidates there is no gap, which is not the same as a gap
        # of zero and must not be read as a tie.
        gap = None
        if len(out) > 1 and out[1].get("match_ratio") is not None:
            gap = round(out[0]["match_ratio"] - out[1]["match_ratio"], 4)
        out[0]["runner_up_gap"] = gap
        if (gap is not None and gap < REVIEW_TIE_GAP
                and not out[0].get("alias_hit")):
            # An inversion means a passed-over candidate matched the searched
            # title better. If the chosen one is an exact alias of it, the
            # comparison was between a fuzzy score and a fact, and the fact
            # wins.
            out[0]["needs_review"] = True
        # One hit, and it is a single chapter: AniList had nothing else to
        # offer and what it offered is a fragment. Manga only - an anime with
        # one episode is an ordinary movie or OVA, not a sign of a bad match.
        if (len(out) == 1 and media_type == "manga"
                and int(out[0].get("chapters") or 0) == 1):
            out[0]["needs_review"] = True
            out[0].setdefault("review_reason", "only match, one chapter")
    return out


def _normalize_title(value: Any) -> str:
    """Fold a title to a comparable form: case, accents and punctuation removed."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


# Calibrated against the user's own 339-title list, not guessed. The plan's
# proposed floor of 0.6 would have flagged **51.5% of resolved rows**, because
# this list is full of translated titles whose official romaji name shares
# almost no characters with the English one the user pasted - "First God Of
# War" resolves correctly to "Di Yi Zhan Shen" at a ratio of 0.129. Title
# similarity is therefore close to useless as a correctness signal here.
#
# What does carry signal is an *inversion*: the entry the ranker chose matched
# the searched title worse than a candidate it passed over, which means a tier
# above the title score (usually "has a MAL id") overrode the better name. All
# five inversions in the calibration look wrong on inspection. A plain tie does
# not count - "One Punch Man" ties with "One-Punch Man" at 1.0 and is right.
#
# Together these flag 3.4% of rows (9 of 268). Numbers and method are recorded
# in MANGA_SUPPORT_NOTES under "Match confidence".
REVIEW_RATIO_FLOOR = 0.15      # a floor of last resort; flagged 4 rows, all of them correct
REVIEW_TIE_GAP = 0.0           # gap < 0 means an inversion, which is the real signal


def _alias_hit(media: Dict[str, Any], search_title: str) -> bool:
    """Does AniList list the searched title as another name for this entry?

    Deliberately a boolean and deliberately not part of `_title_ratio`. Folding
    synonyms into the ratio was measured over 16 real titles and rejected: it
    changed 0 of 16 picks - the ranking tiers decide, not the title score -
    while tripling the number of candidates sitting at a ratio above 0.95 and
    halving the runner-up gap, which is the signal that actually works.

    As a separate fact it is worth a great deal, because it is exact rather
    than fuzzy. "First God Of War" scores 0.129 against "Di Yi Zhan Shen" and
    is correct; AniList lists it verbatim as a synonym. A row like that was
    being flagged for review on a similarity score that cannot describe it.
    """
    wanted = _normalize_title(search_title)
    if not wanted:
        return False
    return any(_normalize_title(name) == wanted
               for name in (media.get("synonyms") or []))


def _won_by_default(media: Dict[str, Any]) -> str:
    """Why the chosen entry may have won by default, or "" when it did not.

    A ranking tier only demotes an entry *relative to its siblings*, so a
    demotion needs something to lose to. Measured: "Gotoubun No Hanayome"
    returns exactly one AniList hit - a 1-chapter ONE_SHOT with no MAL id and
    not the 122-chapter series - and every guard misses it. The one-shot tier
    has nothing to rank it below, the ratio floor passes it at 0.430, and the
    runner-up gap is None because there is no runner-up. It then renders as
    "AniList only", which looks like a legitimate state rather than a miss.

    Both traits are read off the winning node itself, so unlike a gap they
    need no second candidate to be true.
    """
    if (media.get("format") or "").upper() == "ONE_SHOT":
        return "one-shot"
    if (media.get("status") or "").upper() == "NOT_YET_RELEASED":
        return "not yet released"
    return ""


def _carry_candidate_review(entry: Dict[str, Any],
                            candidates: Sequence[Dict[str, Any]]) -> None:
    """Move the candidate list's verdict onto the entry that was chosen.

    The chosen entry is parsed from its own node, so it knows nothing about
    ties, inversions, or how many alternatives there were. The batch path
    carried two of the three fields across inline; the single-title path
    carried none, so the two paths disagreed about the same search.
    """
    if not candidates:
        return
    entry["runner_up_gap"] = candidates[0].get("runner_up_gap")
    if candidates[0].get("needs_review"):
        entry["needs_review"] = True
    if candidates[0].get("alias_hit"):
        entry["alias_hit"] = True
    reason = candidates[0].get("review_reason")
    if reason and not entry.get("review_reason"):
        entry["review_reason"] = reason


def _title_ratio(node: Dict[str, Any], search_title: str) -> float:
    """How close a node's titles are to what was searched, 0.0 to 1.0.

    The better of the English and romaji forms, both folded to a comparable
    shape first. Lives here rather than inside the ranking key because it is
    wanted twice: to sort, and to tell the user how sure the sort was.
    """
    wanted = _normalize_title(search_title)
    if not wanted:
        return 0.0
    titles = node.get("title") or {}
    english = _normalize_title(titles.get("english"))
    romaji = _normalize_title(titles.get("romaji"))
    return max(
        difflib.SequenceMatcher(None, wanted, english).ratio() if english else 0.0,
        difflib.SequenceMatcher(None, wanted, romaji).ratio() if romaji else 0.0,
    )


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
        score += _title_ratio(node, search_title) * 200
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
        stage1 = stage1.replace(curly, straight)
    stage1 = " ".join(stage1.split())
    add(stage1)

    stage2 = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stage1)
    add(stage2)

    if media_type == "manga":
        stage3 = re.sub(r"\s+(manga|manhua|manhwa)\s*$", "", stage2 or stage1, flags=re.IGNORECASE)
        add(stage3)

    # Hyphenation differs between AniList's romaji and what people type:
    # "07-Ghost" is also written "07 Ghost", "Re-Zero" as "ReZero", "K-On" as
    # "K On". Removing a hyphen and replacing it with a space are both
    # deterministic; putting one *in* is not, so "Gotoubun No Hanayome" ->
    # "Go-toubun no Hanayome" is out of reach here. That direction is covered
    # by the won-by-default review flag instead, which notices that the single
    # hit AniList did return is a one-shot.
    #
    # Appended last on purpose: the cap below is a real cost. Variants only run
    # for titles that matched nothing, batched eight to a request, so a list
    # with 70 misses spends a request for every eight attempts.
    if "-" in stage1:
        add(stage1.replace("-", " "))
        add(stage1.replace("-", ""))

    return out[:3]


def _parse_manga_node(media: Dict[str, Any], fallback_title: str = "") -> Dict[str, Any]:
    """Convert an AniList manga node into a MAL-compatible export entry."""
    titles = media.get("title") or {}
    detail = _media_details(media) if "siteUrl" in media else None
    official_title = titles.get("english") or titles.get("romaji") or fallback_title
    raw_format = (media.get("format") or "MANGA").upper()
    series_type = MANGA_FORMAT_MAP.get(raw_format, "Manga")
    entry = {
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
    # How close this was to what the user actually typed. None when nothing was
    # searched - a row read back from an AniList list was never a guess.
    if fallback_title:
        ratio = round(_title_ratio(media, fallback_title), 4)
        entry["match_ratio"] = ratio
        alias = _alias_hit(media, fallback_title)
        if alias:
            entry["alias_hit"] = True
        # An exact alias settles the question the ratio was guessing at, so it
        # clears a low-score flag. It says nothing about the entry being the
        # wrong *kind* of thing, so it does not clear the checks below.
        entry["needs_review"] = ratio < REVIEW_RATIO_FLOOR and not alias
        default_win = _won_by_default(media)
        if default_win:
            entry["needs_review"] = True
            entry["review_reason"] = default_win
    if detail:
        entry["details"] = detail
    return entry
