#!/usr/bin/env python3
"""Calibrate the match-review thresholds against a real title list.

This existed once, produced the numbers in MANGA_SUPPORT_NOTES under "Match
confidence", and was never committed - so when the next change to matching came
along there was nothing to judge it with. It is here now.

Two things it does, and the second is the important one:

  * **report** - what the current rules flag, and what a different ratio floor
    *would* flag. A threshold is only defensible against a distribution.
  * **compare** - which titles a change made the ranker pick differently. An
    aggregate flag percentage can stay still while every row underneath it
    moves; the only honest measure of a ranking change is the pick diff.

Resolution goes through the ordinary disk cache, so the first run costs the
usual rate limit and every run after it is free and offline.

    python tools/calibrate.py manga.txt --media-type manga
    python tools/calibrate.py manga.txt --media-type manga --snapshot before.json
    #   ... change the ranker ...
    python tools/calibrate.py manga.txt --media-type manga --snapshot after.json
    python tools/calibrate.py --compare before.json after.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate_mal_xml as gen          # noqa: E402

SNAPSHOT_VERSION = 1
# The floors worth seeing the curve at. 0.6 is the value the original plan
# proposed; it flagged half the list, which is why the shipped floor is 0.15.
TRIAL_FLOORS = (0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)


def resolve(titles: List[str], media_type: str, use_cache: bool) -> Dict[str, Any]:
    """One row per title, in the shape a snapshot stores."""
    done = {"n": 0}

    def progress(stage: str, n: int, total: int, info=None) -> None:
        if n != done["n"]:
            done["n"] = n
            print("\r  %s %d/%d" % (stage, n, total), end="", file=sys.stderr)

    resolved = gen.resolve_many_entries(
        titles, media_type=media_type, use_cache=use_cache, on_progress=progress)
    print("", file=sys.stderr)

    rows: Dict[str, Any] = {}
    for title in titles:
        entries = resolved.get(title) or []
        if not entries:
            continue
        top = entries[0]
        rows[title] = {
            "anilist_id": int(top.get("anilist_id") or 0),
            "mal_id": int(top.get("id") or 0),
            "picked": top.get("title") or "",
            "match_ratio": top.get("match_ratio"),
            "runner_up_gap": top.get("runner_up_gap"),
            "needs_review": bool(top.get("needs_review")),
            "review_reason": top.get("review_reason") or "",
            "alias_hit": bool(top.get("alias_hit")),
            "unresolved": bool(top.get("unresolved")),
            "candidates": len(top.get("candidates") or []),
        }
    return {"version": SNAPSHOT_VERSION, "media_type": media_type, "rows": rows}


def report(snapshot: Dict[str, Any]) -> None:
    rows = snapshot["rows"]
    resolved = {t: r for t, r in rows.items() if not r["unresolved"]}
    flagged = {t: r for t, r in resolved.items() if r["needs_review"]}
    print("titles          %d" % len(rows))
    print("resolved        %d" % len(resolved))
    print("unresolved      %d" % (len(rows) - len(resolved)))
    if not resolved:
        return
    print("flagged         %d  (%.1f%% of resolved)"
          % (len(flagged), 100.0 * len(flagged) / len(resolved)))

    print("\nwhy they were flagged")
    reasons: Dict[str, int] = {}
    for row in flagged.values():
        if row["review_reason"]:
            key = row["review_reason"]
        elif row["runner_up_gap"] is not None and row["runner_up_gap"] < gen.REVIEW_TIE_GAP:
            key = "inversion (a passed-over candidate matched better)"
        else:
            key = "ratio below the floor (%.2f)" % gen.REVIEW_RATIO_FLOOR
        reasons[key] = reasons.get(key, 0) + 1
    for key in sorted(reasons, key=lambda k: -reasons[key]):
        print("  %-52s %4d" % (key, reasons[key]))

    ratios = sorted(r["match_ratio"] for r in resolved.values()
                    if r["match_ratio"] is not None)
    if ratios:
        print("\nmatch_ratio distribution (%d rows)" % len(ratios))
        for pct in (0, 10, 25, 50, 75, 90, 100):
            index = min(len(ratios) - 1, pct * (len(ratios) - 1) // 100)
            print("  p%-3d %.3f" % (pct, ratios[index]))
        print("\nwhat a ratio floor alone would flag")
        for floor in TRIAL_FLOORS:
            hit = sum(1 for value in ratios if value < floor)
            print("  < %.2f  %4d  (%.1f%%)" % (floor, hit, 100.0 * hit / len(ratios)))
        print("\n  The shipped floor is %.2f. A floor is only useful where correct"
              % gen.REVIEW_RATIO_FLOOR)
        print("  matches do not live: on a translated-title list they sit as low as")
        print("  0.13, which is why this curve matters more than any single number.")

    lone = sum(1 for r in resolved.values() if r["candidates"] <= 1)
    print("\nrows AniList returned a single candidate for: %d" % lone)

    # The point of the alias boolean is the rows it rescues: a correct match
    # whose title looks nothing like what was pasted. Counting them is how you
    # tell whether the floor can move.
    alias = [r for r in resolved.values() if r.get("alias_hit")]
    rescued = [r for r in alias
               if r["match_ratio"] is not None
               and r["match_ratio"] < gen.REVIEW_RATIO_FLOOR]
    print("rows AniList knows the pasted title as an alias of: %d" % len(alias))
    print("  of which the ratio floor alone would have flagged:  %d" % len(rescued))


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> int:
    """Pick diff first, flag rates second. Returns the number of changed picks."""
    old, new = before["rows"], after["rows"]
    shared = [t for t in old if t in new]
    print("titles in both snapshots: %d" % len(shared))

    changed = [t for t in shared if old[t]["anilist_id"] != new[t]["anilist_id"]]
    print("\npicks changed: %d of %d (%.1f%%)"
          % (len(changed), len(shared), 100.0 * len(changed) / max(1, len(shared))))
    for title in changed[:40]:
        print("  %-38s %s" % (title[:38], old[title]["picked"][:28]))
        print("  %-38s %s" % ("", "-> " + new[title]["picked"][:28]))
    if len(changed) > 40:
        print("  ... and %d more" % (len(changed) - 40))

    was = sum(1 for t in shared if old[t]["needs_review"])
    now = sum(1 for t in shared if new[t]["needs_review"])
    print("\nflagged: %d -> %d" % (was, now))
    gained = [t for t in shared if new[t]["needs_review"] and not old[t]["needs_review"]]
    lost = [t for t in shared if old[t]["needs_review"] and not new[t]["needs_review"]]
    print("  newly flagged  %d" % len(gained))
    for title in gained[:15]:
        print("    %-38s %s" % (title[:38], new[title]["review_reason"] or "ratio/gap"))
    print("  no longer flagged %d" % len(lost))
    for title in lost[:15]:
        print("    %s" % title[:38])

    # A ratio that rises for every candidate at once tells you nothing: what
    # matters is whether the distribution still separates them.
    def saturated(rows):
        return sum(1 for t in shared
                   if (rows[t]["match_ratio"] or 0) >= 0.95)
    print("\nrows at match_ratio >= 0.95: %d -> %d  (a jump means the ratio stopped"
          % (saturated(old), saturated(new)))
    print("  discriminating, even if the average looks better)")
    return len(changed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("input", nargs="?", help="a text file, one title per line")
    parser.add_argument("--media-type", choices=("anime", "manga"), default="manga")
    parser.add_argument("--snapshot", metavar="FILE",
                        help="write the resolved rows here instead of only reporting")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="diff two snapshots; no network, no input file needed")
    parser.add_argument("--no-cache", action="store_true",
                        help="refetch rather than reuse the 7-day cache")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N titles, for a quick pass")
    args = parser.parse_args()

    if args.compare:
        loaded = []
        for name in args.compare:
            data = json.loads(Path(name).read_text(encoding="utf-8"))
            if data.get("version") != SNAPSHOT_VERSION:
                parser.error("%s is not a snapshot this version understands" % name)
            loaded.append(data)
        compare(*loaded)
        return

    if not args.input:
        parser.error("a title list is required unless --compare is used")
    titles = gen.load_titles_from_txt(Path(args.input))
    if args.limit:
        titles = titles[:args.limit]
    if not titles:
        parser.error("no titles found in %s" % args.input)
    print("resolving %d titles (%s)..." % (len(titles), args.media_type), file=sys.stderr)

    snapshot = resolve(titles, args.media_type, use_cache=not args.no_cache)
    if args.snapshot:
        Path(args.snapshot).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
        print("snapshot written to %s" % args.snapshot, file=sys.stderr)
    report(snapshot)


if __name__ == "__main__":
    main()
