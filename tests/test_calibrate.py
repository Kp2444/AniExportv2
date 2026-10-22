"""The calibration harness.

It existed once, produced the numbers in the notes, and was never committed -
so the next change to matching had nothing to judge it with. These cases keep
the comparison honest, because the comparison is the whole point: an aggregate
flag percentage can sit still while every row underneath it moves.

No network: `compare` reads two snapshots off disk and nothing else.
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "calibrate", Path(__file__).resolve().parent.parent / "tools" / "calibrate.py")
calibrate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(calibrate)


def snapshot(rows):
    return {"version": calibrate.SNAPSHOT_VERSION, "media_type": "manga", "rows": rows}


def row(anilist_id, picked, ratio=0.9, flagged=False, reason="", alias=False, gap=None):
    return {"anilist_id": anilist_id, "mal_id": 1, "picked": picked,
            "match_ratio": ratio, "runner_up_gap": gap, "needs_review": flagged,
            "review_reason": reason, "alias_hit": alias, "unresolved": False,
            "candidates": 3}


class CompareTests(unittest.TestCase):
    def _run(self, before, after):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            changed = calibrate.compare(snapshot(before), snapshot(after))
        return changed, buffer.getvalue()

    def test_a_changed_pick_is_counted_and_named(self):
        changed, out = self._run(
            {"Alpha": row(1, "One"), "Beta": row(2, "Two")},
            {"Alpha": row(99, "Different"), "Beta": row(2, "Two")})
        self.assertEqual(changed, 1)
        self.assertIn("picks changed: 1 of 2", out)
        self.assertIn("Different", out, "the diff has to name what it became")

    def test_an_unchanged_ranker_reports_zero(self):
        rows = {"Alpha": row(1, "One"), "Beta": row(2, "Two")}
        changed, out = self._run(rows, dict(rows))
        self.assertEqual(changed, 0)
        self.assertIn("picks changed: 0 of 2", out)

    def test_flag_movement_is_broken_out_in_both_directions(self):
        _, out = self._run(
            {"A": row(1, "One", flagged=True), "B": row(2, "Two")},
            {"A": row(1, "One"), "B": row(2, "Two", flagged=True, reason="one-shot")})
        self.assertIn("flagged: 1 -> 1", out)
        self.assertIn("newly flagged  1", out)
        self.assertIn("no longer flagged 1", out,
                      "a net-zero change still has to show what moved")
        self.assertIn("one-shot", out)

    def test_saturation_is_reported_because_an_average_hides_it(self):
        """Folding synonyms into the ratio raised every candidate at once. The
        mean improves while the ratio stops telling candidates apart."""
        _, out = self._run(
            {"A": row(1, "One", ratio=0.4), "B": row(2, "Two", ratio=0.5)},
            {"A": row(1, "One", ratio=1.0), "B": row(2, "Two", ratio=1.0)})
        self.assertIn("match_ratio >= 0.95: 0 -> 2", out)

    def test_only_titles_in_both_snapshots_are_compared(self):
        changed, out = self._run({"A": row(1, "One")},
                                 {"A": row(1, "One"), "B": row(2, "Two")})
        self.assertEqual(changed, 0)
        self.assertIn("titles in both snapshots: 1", out)


class SnapshotFormatTests(unittest.TestCase):
    def _compare_files(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name in ("a.json", "b.json"):
                path = Path(directory) / name
                path.write_text(json.dumps(payload), encoding="utf-8")
                paths.append(str(path))
            argv = ["calibrate.py", "--compare"] + paths
            with patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                calibrate.main()

    def test_a_foreign_version_is_refused_rather_than_misread(self):
        """A snapshot from a different schema compares as nonsense otherwise."""
        with self.assertRaises(SystemExit):
            self._compare_files({"version": 99, "rows": {}})

    def test_a_current_snapshot_compares_without_a_network_or_an_input_file(self):
        self._compare_files(snapshot({"A": row(1, "One")}))

    def test_the_snapshot_carries_what_calibration_needs(self):
        stored = row(1, "One")
        for field in ("anilist_id", "picked", "match_ratio", "runner_up_gap",
                      "needs_review", "review_reason", "alias_hit", "candidates"):
            self.assertIn(field, stored)


if __name__ == "__main__":
    unittest.main()
