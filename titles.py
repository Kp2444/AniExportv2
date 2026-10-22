"""Reading a pasted line: the title, and what the line says about it.

Split out of generate_mal_xml.py, which was three thousand lines doing five
jobs. Nothing here touches the network.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Optional


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
    #
    # The hyphen form must be followed by whitespace. Without that rule the
    # `\d+-` branch ate the counter prefix off a whole class of real titles:
    # "3-gatsu no Lion" became "gatsu no Lion", "5-toubun no Hanayome" became
    # "toubun no Hanayome", and "07-Ghost" became "Ghost" - which does not
    # fail, it resolves confidently to a different work. Numbering written as
    # "1-Berserk" with no space is given up in exchange; it is far rarer than
    # the titles, and "1- Berserk" still works.
    cleaned = re.sub(r"^\s*(?:\d+[.)]\s*|\d+-\s+|[-*•]\s*)", "", stripped).strip()

    # Remove surrounding double/single quotes if present
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()

    return cleaned if cleaned else None


# Every spelling of a list status a person might type, folded to the canonical
# one. Built from the same vocabulary _normalize_status accepts, plus the forms
# that only ever appear in a hand-written list ("on hold", "ptw", "re-reading").
_LINE_STATUSES = {
    "watching": "Watching", "reading": "Reading", "completed": "Completed",
    "complete": "Completed", "done": "Completed", "finished": "Completed",
    "dropped": "Dropped", "drop": "Dropped",
    "paused": "Paused", "on hold": "On-Hold", "on-hold": "On-Hold", "onhold": "On-Hold",
    "hold": "On-Hold", "hiatus": "On-Hold",
    "plan to watch": "Plan to Watch", "plantowatch": "Plan to Watch", "ptw": "Plan to Watch",
    "plan to read": "Plan to Read", "plantoread": "Plan to Read", "ptr": "Plan to Read",
    "planned": "Plan to Watch", "plan": "Plan to Watch",
    "rereading": "Rereading", "re-reading": "Rereading", "reread": "Rereading",
    "rewatching": "Rewatching", "re-watching": "Rewatching", "rewatch": "Rewatching",
}

# One list's vocabulary is the other's nonsense: a manga list has no
# "Watching", an anime list no "Rereading". The parser knows the media type,
# so it hands back a status the list can actually hold.
_LINE_STATUS_FOR_MEDIA = {
    "anime": {"Reading": "Watching", "Plan to Read": "Plan to Watch",
              "Rereading": "Rewatching", "On-Hold": "Paused"},
    "manga": {"Watching": "Reading", "Plan to Watch": "Plan to Read",
              "Rewatching": "Rereading", "On-Hold": "Paused"},
}

# A unit word is what makes a bare number safe to read as progress. Without one
# this parser leaves the number alone, because "86", "91 Days", "7 Seeds" and
# "20th Century Boys" are titles and a guess there is unrecoverable.
_CHAPTER_UNITS = ("ch", "chs", "chap", "chaps", "chapter", "chapters")
_EPISODE_UNITS = ("ep", "eps", "episode", "episodes")
_VOLUME_UNITS = ("vol", "vols", "volume", "volumes")

_TRAILING_SEPARATORS = " \t-\u2013\u2014,|;:\u00b7"


# Words that end real titles often enough to be dangerous on their own, so
# they are only read as a status when something separates them from the title
# ("Berserk - dropped", not "Speed Reading"). The rest - "completed",
# "watching", "ptw", "on hold" - effectively never end a title.
_WEAK_STATUS_WORDS = frozenset(
    ("complete", "done", "plan", "planned", "hold", "drop", "finished"))

_SEPARATORS_CLASS = r"\-\u2013\u2014,|;"


def _strip_status(text: str) -> tuple:
    """Take a trailing status off the line: bracketed anywhere, bare at the end.

    Matched as whole trailing words rather than by one regex. A status can be
    several words ("on hold", "plan to watch"), so the pattern has to allow
    spaces inside it - and a regex that does will happily match "ch - dropped"
    or "eps completed" as a single word and strip the lot.
    """
    bracketed = re.search(r"[\(\[]\s*([A-Za-z][A-Za-z \-]*?)\s*[\)\]]", text)
    if bracketed:
        found = _LINE_STATUSES.get(bracketed.group(1).strip().casefold())
        if found:
            return (text[:bracketed.start()] + " " + text[bracketed.end():]), found

    tokens = text.split()
    for count in range(min(4, len(tokens)), 0, -1):
        phrase = " ".join(tokens[-count:]).strip(_TRAILING_SEPARATORS)
        found = _LINE_STATUSES.get(phrase.casefold())
        if not found:
            continue
        head = tokens[:-count]
        if not head:
            # The whole line is a status word. Far more likely to be a title
            # this parser does not know than a line with no title at all.
            return text, None
        previous = head[-1]
        separated = previous[-1] in "-\u2013\u2014,|;" or previous in "-\u2013\u2014,|;"
        if phrase.casefold() in _WEAK_STATUS_WORDS and not separated:
            continue
        return " ".join(head), found
    return text, None


def _strip_counted(text: str, media_type: str) -> tuple:
    """Take a trailing "<n> <unit>" or "<unit> <n>" off the line."""
    units = "|".join(_CHAPTER_UNITS + _EPISODE_UNITS + _VOLUME_UNITS)
    found = re.search(r"(?:^|[\s" + _SEPARATORS_CLASS + r"])(\d{1,5})\s*(" + units
                      + r")\.?\s*$", text, flags=re.IGNORECASE)
    if found:
        number, unit = found.group(1), found.group(2)
    else:
        found = re.search(r"(?:^|[\s" + _SEPARATORS_CLASS + r"])(" + units
                          + r")\.?\s*(\d{1,5})\s*$", text, flags=re.IGNORECASE)
        if not found:
            return text, None, None
        unit, number = found.group(1), found.group(2)
    unit = unit.casefold()
    if unit in _VOLUME_UNITS and media_type != "manga":
        # Volumes mean nothing on an anime list, and reading them as episodes
        # would invent progress out of a word.
        return text, None, None
    field = "volumes" if unit in _VOLUME_UNITS else "progress"
    return text[:found.start()], field, int(number)


def _strip_fraction(text: str) -> tuple:
    """Take a trailing "n/m" off the line.

    `9/10` is a score - nobody writes progress out of exactly ten by accident
    and everybody writes a score that way. Any other denominator is progress
    against a total, and the total is dropped: AniList knows it, and the number
    in a hand-written list is usually out of date.
    """
    found = re.search(r"(?:^|[\s" + _SEPARATORS_CLASS + r"\(\[])(\d{1,5})\s*/\s*"
                      r"(\d{1,5})[\)\]]?\s*$", text)
    if not found:
        return text, None, None
    left, right = int(found.group(1)), int(found.group(2))
    if right == 10 and left <= 10:
        return text[:found.start()], "score", left
    if right == 0 or left > right:
        return text, None, None
    return text[:found.start()], "progress", left


def _strip_bare_score(text: str, found_already: bool) -> tuple:
    """A lone 0-10 at the end, but only once the line has proved its shape.

    "Vinland Saga (completed) 8" is a score. "Attack on Titan 139" and "86" are
    titles. The difference is whether anything else on the line was readable,
    so a bare number is never the first thing this parser believes.
    """
    if not found_already:
        return text, None
    found = re.search(r"(?:^|[\s" + _SEPARATORS_CLASS + r"])(\d{1,2})\s*$", text)
    if not found or int(found.group(1)) > 10:
        return text, None
    return text[:found.start()], int(found.group(1))


def parse_title_line(line: str, media_type: str = "anime") -> Optional[Dict[str, Any]]:
    """Read a whole pasted line: the title, and whatever it says about it.

    Returns None for a blank or comment line, exactly as `sanitize_title_line`
    does, so one can be swapped for the other.

    Everything is taken off the **end** of the line and only when it is
    unambiguous. A bare trailing number is never the first thing believed:
    "86", "91 Days", "7 Seeds" and "20th Century Boys" are titles, and being
    wrong here is unrecoverable, because the search then resolves confidently
    to something else. A number needs a unit ("374 ch"), a denominator
    ("12/28"), or the company of something else already read off the line.

        Frieren 12/28 9/10 watching    -> Frieren | progress 12 | score 9 | Watching
        Berserk - 374 ch - dropped     -> Berserk | progress 374 | Dropped
        1. Vinland Saga (completed) 8  -> Vinland Saga | Completed | score 8
        86 - Eighty Six                -> unchanged, nothing read
        Attack on Titan 139            -> unchanged, nothing read

    Two bail-outs, because a half-read line is worse than an unread one: if
    stripping would leave no title, or would leave a title ending in a bare
    number, the whole line is kept and nothing is reported.
    """
    cleaned = sanitize_title_line(line)
    if not cleaned:
        return None

    out: Dict[str, Any] = {}
    text = cleaned
    # Several passes: one line can carry a status, a score and a count at once,
    # in any order, and each pass removes at most one of each.
    for _ in range(6):
        before = text
        text, status = _strip_status(text)
        if status and "status" not in out:
            out["status"] = status
        text = text.rstrip(_TRAILING_SEPARATORS)
        text, field, value = _strip_fraction(text)
        if field and field not in out:
            out[field] = value
        text = text.rstrip(_TRAILING_SEPARATORS)
        text, field, value = _strip_counted(text, media_type)
        if field and field not in out:
            out[field] = value
        text = text.rstrip(_TRAILING_SEPARATORS)
        text, score = _strip_bare_score(text, bool(out))
        if score is not None and "score" not in out:
            out["score"] = score
        text = text.rstrip(_TRAILING_SEPARATORS)
        if text == before:
            break

    if "status" in out:
        out["status"] = _LINE_STATUS_FOR_MEDIA[
            "manga" if media_type == "manga" else "anime"].get(
                out["status"], out["status"])

    title = text.strip().strip(_TRAILING_SEPARATORS).strip()
    if not out:
        return {"title": cleaned}
    if not title or re.search(r"(?:^|\s)\d{1,5}$", title):
        # Half-read: keep the line whole rather than search for "Vinland Saga 8".
        return {"title": cleaned}
    out["title"] = title
    return out


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
