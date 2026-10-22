"""Direct AniList write and MAL XML import.

The write path never relies on undocumented upsert behaviour: it reads the
user's existing list entries first and supplies the entry id, so a repeated
write updates in place instead of creating a second row.
"""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import generate_mal_xml as gen
import gui
from tests import serve


class PayloadTests(unittest.TestCase):
    def test_existing_entry_is_updated_not_duplicated(self):
        rows = [{"anilist_id": 30642, "title": "Vinland Saga", "chapters": 224,
                 "volumes": 29, "status": "Completed", "score": 9}]
        payloads, _ = iter_payloads = gen.iter_write_payloads(
            rows, "manga", {30642: {"id": 999, "mediaId": 30642}})
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["update"])
        self.assertEqual(payloads[0]["variables"]["id"], 999,
                         "the existing entry id must be sent, or AniList may create a duplicate")

    def test_new_entry_carries_no_id(self):
        rows = [{"anilist_id": 148167, "title": "AniList only", "status": "Reading"}]
        payloads, _ = gen.iter_write_payloads(rows, "manga", {})
        self.assertNotIn("id", payloads[0]["variables"])
        self.assertFalse(payloads[0]["update"])

    def test_status_and_score_mapping(self):
        cases = {
            "Reading": "CURRENT", "Completed": "COMPLETED", "Plan to read": "PLANNING",
            "Paused": "PAUSED", "Dropped": "DROPPED", "Rereading": "REPEATING",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                payloads, skipped = gen.iter_write_payloads(
                    [{"anilist_id": 1, "title": "x", "status": status, "score": 7}], "manga", {})
                self.assertEqual(skipped, [])
                self.assertEqual(payloads[0]["variables"]["status"], expected)
                self.assertEqual(payloads[0]["variables"]["scoreRaw"], 70)

    def test_progress_matches_the_xml_builders(self):
        row = {"anilist_id": 1, "title": "x", "status": "Completed",
               "chapters": 100, "volumes": 10}
        payloads, _ = gen.iter_write_payloads([row], "manga", {})
        self.assertEqual(payloads[0]["variables"]["progress"], 100)
        self.assertEqual(payloads[0]["variables"]["progressVolumes"], 10)

        partial = dict(row, status="Reading", my_read_chapters=42, my_read_volumes=5)
        payloads, _ = gen.iter_write_payloads([partial], "manga", {})
        self.assertEqual(payloads[0]["variables"]["progress"], 42)
        self.assertEqual(payloads[0]["variables"]["progressVolumes"], 5)

    def test_dates_become_fuzzy_dates_only_when_complete(self):
        payloads, _ = gen.iter_write_payloads(
            [{"anilist_id": 1, "title": "x", "status": "Completed",
              "start_date": "2024-03-01", "finish_date": "0000-00-00"}], "manga", {})
        self.assertEqual(payloads[0]["variables"]["startedAt"],
                         {"year": 2024, "month": 3, "day": 1})
        self.assertNotIn("completedAt", payloads[0]["variables"])

    def test_rows_without_an_anilist_id_are_skipped_with_a_reason(self):
        payloads, skipped = gen.iter_write_payloads(
            [{"anilist_id": 0, "title": "Nowhere", "status": "Reading"}], "manga", {})
        self.assertEqual(payloads, [])
        self.assertEqual(skipped[0]["title"], "Nowhere")
        self.assertIn("AniList id", skipped[0]["reason"])

    def test_anime_payloads_use_episodes_and_omit_volumes(self):
        payloads, _ = gen.iter_write_payloads(
            [{"anilist_id": 1, "title": "x", "status": "Completed", "episodes": 12}], "anime", {})
        self.assertEqual(payloads[0]["variables"]["progress"], 12)
        self.assertNotIn("progressVolumes", payloads[0]["variables"])

    def test_validation_messages_are_surfaced(self):
        errors = [{"message": "validation", "status": 400,
                   "validation": {"score": ["The score may not be greater than 100."]}}]
        self.assertEqual(gen._describe_anilist_error(errors),
                         "The score may not be greater than 100.")


def batch_of(*outcomes, **fixed):
    """A stand-in for anilist_write_batch: one result per payload in the batch.

    `batch_of(ok=True, id=1)` answers every row the same way; passing explicit
    outcomes answers them in order and repeats the last one.
    """
    def run(token, payloads):
        if outcomes:
            return [outcomes[min(i, len(outcomes) - 1)] for i in range(len(payloads))]
        return [dict(fixed) for _ in payloads]
    return run


def _payload(row, media_type="manga", existing=None, **kwargs):
    payloads, skipped = gen.iter_write_payloads([row], media_type, existing or {}, **kwargs)
    assert not skipped, skipped
    return payloads[0]


def _diff_for(payload, field):
    for row in payload["diff"]:
        if row["field"] == field:
            return row
    return None


class ConflictPolicyTests(unittest.TestCase):
    """The write must never send a value backwards without being told to.

    Each case is a measured data loss turned into a test (MANGA_SUPPORT_NOTES):
    the account's value, the row's value, and what actually goes over the wire.
    """

    MANGA = {"anilist_id": 30642, "title": "Vinland Saga", "chapters": 224, "volumes": 29}

    def existing(self, **fields):
        base = {"id": 999, "mediaId": 30642, "status": "CURRENT", "progress": 0,
                "progressVolumes": 0, "score": 0, "repeat": 0, "notes": None,
                "startedAt": None, "completedAt": None}
        base.update(fields)
        return {30642: base}

    def test_progress_never_goes_backwards(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=142)
        payload = _payload(row, existing=self.existing(progress=224))
        self.assertEqual(payload["variables"]["progress"], 224)
        self.assertEqual(_diff_for(payload, "progress"),
                         {"field": "progress", "from": 224, "to": 142,
                          "applied": 224, "blocked": True})

    def test_a_blank_progress_cannot_zero_the_account(self):
        # my_read_chapters absent on a Reading row clamps to 0 (M3 live).
        row = dict(self.MANGA, status="Reading")
        payload = _payload(row, existing=self.existing(progress=224))
        self.assertEqual(payload["variables"]["progress"], 224)
        self.assertTrue(_diff_for(payload, "progress")["blocked"])

    def test_progress_forward_passes_unblocked(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=224)
        payload = _payload(row, existing=self.existing(progress=142))
        self.assertEqual(payload["variables"]["progress"], 224)
        self.assertFalse(_diff_for(payload, "progress")["blocked"])

    def test_volumes_get_the_same_rule_as_chapters(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=224, my_read_volumes=4)
        payload = _payload(row, existing=self.existing(progress=224, progressVolumes=29))
        self.assertEqual(payload["variables"]["progressVolumes"], 29)
        self.assertTrue(_diff_for(payload, "progressVolumes")["blocked"])

    def test_an_anime_row_never_touches_volumes(self):
        # AniList returns null for progressVolumes on anime; a bare comparison
        # against it would raise, and the field must not be sent at all.
        row = {"anilist_id": 339, "title": "Lain", "episodes": 13, "status": "Watching",
               "my_watched_episodes": 3}
        existing = {339: {"id": 7, "mediaId": 339, "status": "CURRENT", "progress": 1,
                          "progressVolumes": None, "score": None, "repeat": None}}
        payload = _payload(row, media_type="anime", existing=existing)
        self.assertNotIn("progressVolumes", payload["variables"])
        self.assertIsNone(_diff_for(payload, "progressVolumes"))
        self.assertEqual(payload["variables"]["progress"], 3)

    def test_a_zero_score_cannot_erase_a_real_one(self):
        row = dict(self.MANGA, status="Completed", score=0)
        payload = _payload(row, existing=self.existing(status="COMPLETED", progress=224,
                                                       progressVolumes=29, score=80))
        self.assertEqual(payload["variables"]["scoreRaw"], 80)
        self.assertTrue(_diff_for(payload, "scoreRaw")["blocked"])

    def test_a_real_score_replaces_another_real_score(self):
        row = dict(self.MANGA, status="Completed", score=9)
        payload = _payload(row, existing=self.existing(status="COMPLETED", progress=224,
                                                       progressVolumes=29, score=80))
        self.assertEqual(payload["variables"]["scoreRaw"], 90)
        self.assertFalse(_diff_for(payload, "scoreRaw")["blocked"])

    def test_repeat_never_goes_backwards(self):
        # The write sends the literal 1 for any reread, so an account at 5
        # would be reset without this rule (M5 live).
        row = dict(self.MANGA, status="Rereading", my_read_chapters=224, my_read_volumes=29)
        payload = _payload(row, existing=self.existing(progress=224, progressVolumes=29, repeat=5))
        self.assertEqual(payload["variables"]["repeat"], 5)
        self.assertTrue(_diff_for(payload, "repeat")["blocked"])

    def test_completed_is_not_replaced_by_anything_but_a_reread(self):
        completed = self.existing(status="COMPLETED", progress=224, progressVolumes=29)
        blocked = {"Plan to read": "PLANNING", "Reading": "CURRENT",
                   "Dropped": "DROPPED", "On-hold": "PAUSED"}
        for label in blocked:
            with self.subTest(status=label):
                row = dict(self.MANGA, status=label, my_read_chapters=224, my_read_volumes=29)
                payload = _payload(row, existing=completed)
                self.assertEqual(payload["variables"]["status"], "COMPLETED")
                self.assertTrue(_diff_for(payload, "status")["blocked"])

        row = dict(self.MANGA, status="Rereading", my_read_chapters=224, my_read_volumes=29)
        payload = _payload(row, existing=completed)
        self.assertEqual(payload["variables"]["status"], "REPEATING")
        self.assertFalse(_diff_for(payload, "status")["blocked"])

    def test_every_other_transition_passes(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=10)
        payload = _payload(row, existing=self.existing(status="DROPPED", progress=10))
        self.assertEqual(payload["variables"]["status"], "CURRENT")
        self.assertFalse(_diff_for(payload, "status")["blocked"])

    def test_a_create_blocks_nothing_and_has_no_before(self):
        row = dict(self.MANGA, status="Completed", score=9)
        payload = _payload(row, existing={})
        self.assertFalse(payload["update"])
        self.assertTrue(payload["diff"], "a create still reports what it will set")
        for entry in payload["diff"]:
            self.assertIsNone(entry["from"])
            self.assertFalse(entry["blocked"])
        self.assertEqual(payload["variables"]["progress"], 224)

    def test_an_unchanged_row_produces_an_empty_diff(self):
        row = dict(self.MANGA, status="Completed", score=8)
        payload = _payload(row, existing=self.existing(status="COMPLETED", progress=224,
                                                       progressVolumes=29, score=80))
        self.assertEqual(payload["diff"], [])

    def test_an_override_forces_the_row_through(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=142)
        payload = _payload(row, existing=self.existing(progress=224),
                           overrides={30642: {"progress": "mine"}})
        self.assertEqual(payload["variables"]["progress"], 142)
        self.assertFalse(_diff_for(payload, "progress")["blocked"])

    def test_an_override_key_may_arrive_as_a_string(self):
        # JSON object keys are strings, so the wire format must still match.
        row = dict(self.MANGA, status="Reading", my_read_chapters=142)
        payload = _payload(row, existing=self.existing(progress=224),
                           overrides={"30642": {"progress": "mine"}})
        self.assertEqual(payload["variables"]["progress"], 142)

    def test_the_overwrite_policy_drops_every_guard(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=1, score=0)
        payload = _payload(row, existing=self.existing(status="COMPLETED", progress=224,
                                                       progressVolumes=29, score=80),
                           policy="overwrite")
        self.assertEqual(payload["variables"]["progress"], 1)
        self.assertEqual(payload["variables"]["scoreRaw"], 0)
        self.assertEqual(payload["variables"]["status"], "CURRENT")

    def test_a_stale_completion_date_is_flagged_not_cleared(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=224, my_read_volumes=29,
                   score=9)
        existing = self.existing(status="COMPLETED", progress=224, progressVolumes=29,
                                 score=90, completedAt={"year": 2026, "month": 10, "day": 19})
        # Forcing the status off COMPLETED is what leaves the date behind.
        payload = _payload(row, existing=existing, overrides={30642: {"status": "mine"}})
        self.assertEqual(payload["variables"]["status"], "CURRENT")
        self.assertTrue(payload["stale_completed_at"])
        self.assertNotIn("completedAt", payload["variables"],
                         "the date must not be touched unless the user asks")

    def test_clearing_the_completion_date_sends_an_empty_fuzzy_date(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=224, my_read_volumes=29,
                   score=9)
        existing = self.existing(status="COMPLETED", progress=224, progressVolumes=29,
                                 score=90, completedAt={"year": 2026, "month": 10, "day": 19})
        payload = _payload(row, existing=existing,
                           overrides={30642: {"status": "mine", "completedAt": "clear"}})
        self.assertEqual(payload["variables"]["completedAt"],
                         {"year": None, "month": None, "day": None})
        self.assertFalse(payload["stale_completed_at"])
        cleared = _diff_for(payload, "completedAt")
        self.assertEqual((cleared["to"], cleared["applied"]), (None, None))

    def test_a_still_completed_row_is_not_called_stale(self):
        row = dict(self.MANGA, status="Completed", my_read_chapters=224, my_read_volumes=29,
                   score=9)
        existing = self.existing(status="COMPLETED", progress=224, progressVolumes=29,
                                 score=90, completedAt={"year": 2026, "month": 10, "day": 19})
        self.assertFalse(_payload(row, existing=existing)["stale_completed_at"])

    def test_notes_and_dates_are_reported_but_never_guarded(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=224,
                   comments="mine", start_date="2024-03-01")
        existing = self.existing(progress=224, notes="theirs",
                                 startedAt={"year": 2020, "month": 1, "day": 1})
        payload = _payload(row, existing=existing)
        self.assertEqual(payload["variables"]["notes"], "mine")
        self.assertEqual(payload["variables"]["startedAt"],
                         {"year": 2024, "month": 3, "day": 1})
        for field in ("notes", "startedAt"):
            with self.subTest(field=field):
                self.assertFalse(_diff_for(payload, field)["blocked"])


class AniListSideEffectTests(unittest.TestCase):
    """AniList edits some fields itself on a status change, whatever is sent.

    Measured live, not inferred (MANGA_SUPPORT_NOTES, L7-L8). The diff has to predict
    the account rather than the payload, or the confirm step promises changes
    the API discards.
    """

    MANGA = {"anilist_id": 34632, "title": "Punpun", "chapters": 147, "volumes": 13}

    def existing(self, **fields):
        base = {"id": 999, "mediaId": 34632, "status": "COMPLETED", "progress": 147,
                "progressVolumes": 13, "score": 90, "repeat": 0, "notes": None,
                "startedAt": None, "completedAt": None}
        base.update(fields)
        return {34632: base}

    def test_a_forced_downgrade_under_completed_predicts_the_total(self):
        # The live 3a case: "keep mine" sends 40, AniList stores 147 anyway
        # because the status guard kept the entry COMPLETED.
        row = dict(self.MANGA, status="Reading", my_read_chapters=40, my_read_volumes=4, score=9)
        payload = _payload(row, existing=self.existing(),
                           overrides={34632: {"progress": "mine", "progressVolumes": "mine"}})
        self.assertEqual(payload["variables"]["progress"], 40, "the payload still says 40")
        progress = _diff_for(payload, "progress")
        self.assertEqual(progress["applied"], 147, "but the account will hold 147")
        self.assertTrue(progress["by_anilist"])
        self.assertEqual(_diff_for(payload, "progressVolumes")["applied"], 13)

    def test_no_clamp_once_the_status_is_actually_moved_off_completed(self):
        row = dict(self.MANGA, status="Reading", my_read_chapters=40, my_read_volumes=4, score=9)
        payload = _payload(row, existing=self.existing(),
                           overrides={34632: {"progress": "mine", "progressVolumes": "mine",
                                              "status": "mine"}})
        self.assertEqual(payload["variables"]["status"], "CURRENT")
        self.assertEqual(_diff_for(payload, "progress")["applied"], 40)
        self.assertNotIn("by_anilist", _diff_for(payload, "progress"))

    def test_a_blocked_progress_needs_no_clamp_note(self):
        # Policy already keeps 147, which is what AniList would force anyway.
        row = dict(self.MANGA, status="Reading", my_read_chapters=40, my_read_volumes=4, score=9)
        payload = _payload(row, existing=self.existing())
        progress = _diff_for(payload, "progress")
        self.assertEqual(progress["applied"], 147)
        self.assertTrue(progress["blocked"])
        self.assertNotIn("by_anilist", progress, "do not blame AniList for the policy's doing")

    def test_finishing_a_reread_predicts_the_repeat_increment(self):
        row = dict(self.MANGA, status="Completed", my_read_chapters=147,
                   my_read_volumes=13, score=9)
        payload = _payload(row, existing=self.existing(status="REPEATING", repeat=5))
        self.assertNotIn("repeat", payload["variables"], "a non-reread row sends no repeat")
        bumped = _diff_for(payload, "repeat")
        self.assertEqual((bumped["from"], bumped["applied"]), (5, 6))
        self.assertTrue(bumped["by_anilist"])
        self.assertFalse(bumped["blocked"])

    def test_an_ongoing_series_predicts_nothing_it_cannot_know(self):
        # chapters 0 means unknown, so there is no total to clamp to.
        row = {"anilist_id": 34632, "title": "Berserk", "chapters": 0, "volumes": 0,
               "status": "Completed", "score": 9}
        payload = _payload(row, existing=self.existing(progress=10, progressVolumes=1))
        progress = _diff_for(payload, "progress")
        # The policy still keeps the account's 10 over the unknown-total 0; what
        # it must not do is claim AniList will raise it to a total nobody knows.
        self.assertEqual(progress["applied"], 10)
        self.assertTrue(progress["blocked"])
        self.assertNotIn("by_anilist", progress)

    def test_an_anime_row_clamps_episodes_and_never_volumes(self):
        row = {"anilist_id": 339, "title": "Lain", "episodes": 13, "status": "Watching",
               "my_watched_episodes": 3}
        existing = {339: {"id": 7, "mediaId": 339, "status": "COMPLETED", "progress": 13,
                          "progressVolumes": None, "score": 0, "repeat": 0}}
        payload = _payload(row, media_type="anime", existing=existing,
                           overrides={339: {"progress": "mine"}})
        self.assertEqual(_diff_for(payload, "progress")["applied"], 13)
        self.assertIsNone(_diff_for(payload, "progressVolumes"))


class DuplicateRowTests(unittest.TestCase):
    """Two rows can resolve to one AniList entry - fixing two unmatched titles
    to the same series does it. The XML path already refuses that; this one has
    to as well, or the second row writes over the first and the undo snapshot
    holds two rows for one entry with only one recorded id."""

    def test_the_second_row_on_one_entry_is_skipped_with_a_reason(self):
        rows = [{"anilist_id": 30642, "title": "Vinland Saga", "chapters": 224,
                 "status": "Reading", "my_read_chapters": 100},
                {"anilist_id": 30642, "title": "Vinland Saga: Slave Arc", "chapters": 224,
                 "status": "Reading", "my_read_chapters": 10}]
        payloads, skipped = gen.iter_write_payloads(rows, "manga", {})
        self.assertEqual(len(payloads), 1, "one payload per AniList entry")
        self.assertEqual(payloads[0]["title"], "Vinland Saga", "the first row wins")
        self.assertEqual(len(skipped), 1)
        self.assertIn("Vinland Saga", skipped[0]["reason"])
        self.assertEqual(skipped[0]["title"], "Vinland Saga: Slave Arc")

    def test_deduplicate_entries_could_not_have_done_this(self):
        # Different MAL ids and different titles, same AniList entry: the
        # existing helper keys on neither, which is why the check lives here.
        rows = [{"anilist_id": 1, "id": 11, "title": "A", "status": "Reading"},
                {"anilist_id": 1, "id": 22, "title": "B", "status": "Reading"}]
        self.assertEqual(len(gen.deduplicate_entries(rows)), 2)
        payloads, skipped = gen.iter_write_payloads(rows, "manga", {})
        self.assertEqual((len(payloads), len(skipped)), (1, 1))

    def test_distinct_entries_are_all_kept(self):
        rows = [{"anilist_id": 1, "title": "A", "status": "Reading"},
                {"anilist_id": 2, "title": "B", "status": "Reading"}]
        payloads, skipped = gen.iter_write_payloads(rows, "manga", {})
        self.assertEqual((len(payloads), len(skipped)), (2, 0))


class UndoRepeatRepairTests(unittest.TestCase):
    """Restoring COMPLETED over an entry the write left REPEATING makes AniList
    increment `repeat`, and its increment beats the value sent alongside the
    status - measured live (MANGA_SUPPORT_NOTES, L8). A second write carrying only
    `repeat` does stick, so the restore is followed by one."""

    def snapshot(self, **prior):
        base = {"id": 500, "status": "COMPLETED", "progress": 249, "progressVolumes": 22,
                "score": 70, "repeat": 4, "notes": "", "startedAt": None, "completedAt": None}
        base.update(prior)
        return {"version": 1, "written_at": 1, "media_type": "manga", "viewer_id": 1,
                "rows": [{"anilist_id": 30003, "title": "20th Century Boys",
                          "was_create": False, "prior": base}]}

    def test_a_reread_count_is_repaired_by_a_second_write(self):
        restores, _, _ = gen.iter_undo_payloads(self.snapshot())
        self.assertEqual(len(restores), 2, "the restore is followed by a repeat repair")
        self.assertEqual(restores[1]["repair"], "repeat")
        self.assertEqual(restores[1]["variables"],
                         {"id": 500, "mediaId": 30003, "repeat": 4})
        self.assertNotIn("status", restores[1]["variables"],
                         "sending the status again would re-trigger the increment")

    def test_an_ordinary_undo_costs_no_extra_request(self):
        for prior in ({"repeat": 0}, {"status": "CURRENT", "repeat": 4}):
            with self.subTest(prior=prior):
                restores, _, _ = gen.iter_undo_payloads(self.snapshot(**prior))
                self.assertEqual(len(restores), 1)
                self.assertNotIn("repair", restores[0])


class BatchWriteTests(unittest.TestCase):
    """One document, several aliased saves, one request against the rate limit.

    Aliases execute serially and a failing one does not stop the rest, so the
    authoritative per-row signal is data["mk"] is None. Errors carry no `path`,
    only `locations`, so messages are matched back by character offset.
    """

    def payloads(self):
        return [
            {"title": "A", "anilist_id": 1,
             "variables": {"mediaId": 1, "status": "CURRENT", "progress": 3}},
            {"title": "B", "anilist_id": 2,
             "variables": {"id": 99, "mediaId": 2, "status": "COMPLETED", "scoreRaw": 90,
                           "progress": 12, "notes": "n",
                           "completedAt": {"year": 2024, "month": 1, "day": 2}}},
        ]

    def test_only_the_arguments_a_row_carries_are_declared(self):
        query, variables, _ = gen._write_batch_query(self.payloads())
        # Sending an explicit null for a field the row left alone would blank it.
        self.assertNotIn("$notes0", query)
        self.assertNotIn("$completedAt0", query)
        self.assertNotIn("notes0", variables)
        self.assertIn("$notes1: String", query)
        self.assertIn("$completedAt1: FuzzyDateInput", query)
        self.assertEqual(variables["completedAt1"], {"year": 2024, "month": 1, "day": 2})

    def test_each_alias_is_numbered_and_self_contained(self):
        query, variables, spans = gen._write_batch_query(self.payloads())
        self.assertIn("m0: SaveMediaListEntry(", query)
        self.assertIn("m1: SaveMediaListEntry(", query)
        self.assertEqual(variables["mediaId0"], 1)
        self.assertEqual(variables["id1"], 99)
        for index, (start, end) in enumerate(spans):
            self.assertIn("m%d:" % index, query[start:end])
            self.assertNotIn("m%d:" % (1 - index), query[start:end])

    def test_an_empty_batch_sends_nothing(self):
        self.assertEqual(gen.anilist_write_batch("t", []), [])

    def test_a_null_alias_is_the_signal_that_a_row_failed(self):
        # The stalled alias is re-sent on its own and fails there too, which is
        # what a genuinely unwritable row does.
        def api(query, variables, token, *a, **k):
            if query.count("SaveMediaListEntry(") > 1:
                return {"data": {"m0": {"id": 5, "mediaId": 1, "status": "CURRENT"}, "m1": None},
                        "errors": []}
            return {"data": {"m0": None}, "errors": []}

        with patch.object(gen, "_execute_anilist_authenticated", side_effect=api):
            out = gen.anilist_write_batch("t", self.payloads())
        self.assertEqual(out[0], {"ok": True, "id": 5, "status": "CURRENT"})
        self.assertFalse(out[1]["ok"])
        self.assertTrue(out[1]["error"], "a failed row must carry some message")

    def test_an_error_is_matched_to_its_own_alias_by_location(self):
        query, _, spans = gen._write_batch_query(self.payloads())
        # Point the error at the second alias the way AniList does: 1-based
        # line and column into the document it was sent.
        target = spans[1][0] + 4
        line = query[:target].count(chr(10)) + 1
        column = target - (query.rfind(chr(10), 0, target) + 1) + 1
        reply = {"data": {"m0": {"id": 5, "mediaId": 1, "status": "CURRENT"}, "m1": None},
                 "errors": [{"message": "The selected media id is invalid.",
                             "locations": [{"line": line, "column": column}]}]}
        single = {"data": {"m0": None},
                  "errors": [{"message": "The selected media id is invalid."}]}

        def api(q, variables, token, *a, **k):
            return reply if q.count("SaveMediaListEntry(") > 1 else single

        with patch.object(gen, "_execute_anilist_authenticated", side_effect=api):
            out = gen.anilist_write_batch("t", self.payloads())
        self.assertTrue(out[0]["ok"])
        self.assertIn("media id is invalid", out[1]["error"])

    def test_an_unplaceable_error_is_reported_not_guessed(self):
        reply = {"data": {"m0": None, "m1": None},
                 "errors": [{"message": "Something went wrong.", "locations": []}]}
        single = {"data": {"m0": None},
                  "errors": [{"message": "Something went wrong.", "locations": []}]}

        def api(query, variables, token, *a, **k):
            return reply if query.count("SaveMediaListEntry(") > 1 else single

        with patch.object(gen, "_execute_anilist_authenticated", side_effect=api):
            out = gen.anilist_write_batch("t", self.payloads())
        for row in out:
            self.assertFalse(row["ok"])
            self.assertIn("Something went wrong", row["error"],
                          "an error that cannot be placed goes to every failed alias")

    def test_one_bad_row_does_not_take_its_whole_batch_down(self):
        # Measured live: AniList validates the entire document before running
        # any of it, so an invalid media id nulls every alias in the batch -
        # the good rows never execute. Without the per-row fallback a single
        # stale id would lose seven good writes and the retry would re-batch
        # the same eight for ever.
        sent = []

        def api(query, variables, token, *a, **k):
            sent.append(query.count("SaveMediaListEntry("))
            if sent[-1] > 1:
                return {"data": {"m0": None, "m1": None},
                        "errors": [{"message": "validation",
                                    "locations": [{"line": 3, "column": 3}]}]}
            # Alone, the good row writes and the bad one does not.
            good = "mediaId: $mediaId0" in query and variables.get("mediaId0") == 1
            if good:
                return {"data": {"m0": {"id": 5, "mediaId": 1, "status": "CURRENT"}}, "errors": []}
            return {"data": {"m0": None},
                    "errors": [{"message": "The selected media id is invalid."}]}

        with patch.object(gen, "_execute_anilist_authenticated", side_effect=api):
            out = gen.anilist_write_batch("t", self.payloads())
        self.assertEqual(sent, [2, 1, 1], "the batch, then each stalled row on its own")
        self.assertTrue(out[0]["ok"], "the good row must be rescued by the fallback")
        self.assertEqual(out[0]["id"], 5)
        self.assertFalse(out[1]["ok"])

    def test_a_row_that_did_execute_is_never_re_sent(self):
        calls = []

        def api(query, variables, token, *a, **k):
            calls.append(query.count("SaveMediaListEntry("))
            if calls[-1] > 1:
                return {"data": {"m0": {"id": 5, "mediaId": 1, "status": "CURRENT"}, "m1": None},
                        "errors": [{"message": "nope"}]}
            return {"data": {"m0": None}, "errors": [{"message": "nope"}]}

        with patch.object(gen, "_execute_anilist_authenticated", side_effect=api):
            out = gen.anilist_write_batch("t", self.payloads())
        self.assertEqual(calls, [2, 1], "only the null alias is retried")
        self.assertTrue(out[0]["ok"])

    def test_a_single_row_batch_does_not_recurse(self):
        with patch.object(gen, "_execute_anilist_authenticated",
                          return_value={"data": {"m0": None}, "errors": []}) as api:
            out = gen.anilist_write_batch("t", self.payloads()[:1])
        self.assertEqual(api.call_count, 1)
        self.assertFalse(out[0]["ok"])

    def test_a_document_level_failure_fails_every_row_in_it(self):
        with patch.object(gen, "_execute_anilist_authenticated",
                          return_value={"error": "AniList rejected the token. Paste a fresh one."}):
            out = gen.anilist_write_batch("t", self.payloads())
        self.assertEqual(len(out), 2)
        for row in out:
            self.assertFalse(row["ok"])
            self.assertIn("token", row["error"])

    def test_offsets_round_trip_through_line_and_column(self):
        text = "abc" + chr(10) + "defgh" + chr(10) + "ij"
        self.assertEqual(gen._offset_of(text, 1, 1), 0)
        self.assertEqual(gen._offset_of(text, 2, 1), 4)
        self.assertEqual(gen._offset_of(text, 2, 5), 8)
        self.assertEqual(gen._offset_of(text, 3, 1), 10)
        self.assertIsNone(gen._offset_of(text, 0, 1))
        self.assertIsNone(gen._offset_of(text, None, 1))


class ExistingReadFailureTests(unittest.TestCase):
    def test_a_failed_chunk_raises_instead_of_looking_like_creates(self):
        with patch.object(gen, "_execute_anilist_authenticated",
                          return_value={"error": "AniList returned HTTP 500."}):
            with self.assertRaises(gen.AniListReadError) as caught:
                gen.anilist_existing_entries("t", 7, "manga", [1, 2, 3])
        self.assertIn("500", str(caught.exception))

    def test_a_good_read_still_maps_by_media_id(self):
        page = {"data": {"Page": {"mediaList": [{"id": 5, "mediaId": 9, "progress": 3}]}}}
        with patch.object(gen, "_execute_anilist_authenticated", return_value=page):
            found = gen.anilist_existing_entries("t", 7, "manga", [9])
        self.assertEqual(found[9]["id"], 5)

    def test_the_query_fetches_what_safety_and_undo_need(self):
        query = gen.ANILIST_EXISTING_ENTRIES_QUERY
        for field in ("repeat", "notes", "startedAt", "completedAt"):
            self.assertIn(field, query, field)
        self.assertIn("score (format: POINT_100)", query,
                      "a bare score comes back in the account's own scale, not 0-100")


class UndoSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        env = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(self.dir / ".anilist_cache.json")})
        env.start()
        self.addCleanup(env.stop)

    def snapshot(self):
        payloads = [
            {"title": "Vinland Saga", "anilist_id": 30642, "update": True, "variables": {}},
            {"title": "Punpun", "anilist_id": 34632, "update": False, "variables": {}},
        ]
        existing = {30642: {"id": 998, "status": "CURRENT", "progress": 224,
                            "progressVolumes": 18, "score": 80, "repeat": 2,
                            "notes": "mine", "startedAt": {"year": 2024, "month": 3, "day": 1},
                            "completedAt": None}}
        return gen.build_undo_snapshot("manga", 12345, payloads, existing)

    def test_a_snapshot_keeps_every_field_an_undo_has_to_restore(self):
        prior = self.snapshot()["rows"][0]["prior"]
        for field in ("id", "status", "progress", "progressVolumes", "score", "repeat",
                      "notes", "startedAt", "completedAt"):
            self.assertIn(field, prior, field)

    def test_a_snapshot_never_holds_a_token(self):
        text = json.dumps(self.snapshot())
        self.assertNotIn("token", text.lower())

    def test_a_create_has_no_prior_state_only_a_recorded_id(self):
        snapshot = self.snapshot()
        created = snapshot["rows"][1]
        self.assertTrue(created["was_create"])
        self.assertIsNone(created["created_entry_id"])
        gen.record_created_entry(snapshot, 34632, 604576747)
        self.assertEqual(snapshot["rows"][1]["created_entry_id"], 604576747)

    def test_a_snapshot_round_trips_through_disk(self):
        snapshot = self.snapshot()
        name = gen.save_undo_snapshot(snapshot)
        self.assertTrue(name.startswith(".aniexport_undo_"))
        self.assertEqual(gen.load_undo_snapshot(name)["viewer_id"], 12345)
        listed = gen.list_undo_snapshots()
        self.assertEqual(listed[0]["name"], name)
        self.assertEqual(listed[0]["creates"], 1)

    def test_only_a_bare_snapshot_name_is_accepted(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "evil.json", "", None,
                    ".aniexport_undo_x.json", ".aniexport_undo_1.json.tmp"):
            with self.subTest(name=bad):
                self.assertIsNone(gen._undo_snapshot_path(bad))
        self.assertIsNotNone(gen._undo_snapshot_path(".aniexport_undo_17.json"))

    def test_pruning_keeps_the_newest_few(self):
        for stamp in range(1000, 1008):
            gen.save_undo_snapshot(dict(self.snapshot(), written_at=stamp))
        gen.prune_undo_snapshots(keep=5)
        left = sorted(q.name for q in self.dir.glob(".aniexport_undo_*.json"))
        self.assertEqual(len(left), 5)
        self.assertEqual(left[0], ".aniexport_undo_1003.json")

    def test_a_restore_sends_the_empty_values_the_write_refuses_to(self):
        snapshot = self.snapshot()
        snapshot["rows"][0]["prior"]["notes"] = None
        restores, deletes, unrecoverable = gen.iter_undo_payloads(snapshot)
        self.assertEqual(len(restores), 1)
        variables = restores[0]["variables"]
        self.assertEqual(variables["id"], 998)
        self.assertEqual(variables["notes"], "")
        self.assertEqual(variables["completedAt"], {"year": None, "month": None, "day": None})
        self.assertEqual(variables["progressVolumes"], 18)
        self.assertEqual(variables["repeat"], 2)
        self.assertEqual(variables["scoreRaw"], 80)
        self.assertEqual(deletes, [])
        self.assertEqual(len(unrecoverable), 1, "the create had no recorded id")

    def test_an_anime_restore_omits_volumes(self):
        snapshot = dict(self.snapshot(), media_type="anime")
        restores, _, _ = gen.iter_undo_payloads(snapshot)
        self.assertNotIn("progressVolumes", restores[0]["variables"])

    def test_a_created_entry_is_deleted_only_when_asked(self):
        snapshot = self.snapshot()
        gen.record_created_entry(snapshot, 34632, 777)
        _, deletes, unrecoverable = gen.iter_undo_payloads(snapshot, delete_created=False)
        self.assertEqual(deletes, [])
        self.assertEqual(unrecoverable, [])
        _, deletes, _ = gen.iter_undo_payloads(snapshot, delete_created=True)
        self.assertEqual(deletes[0]["entry_id"], 777)

    def test_a_create_with_no_recorded_id_is_reported_not_guessed(self):
        snapshot = self.snapshot()
        _, deletes, unrecoverable = gen.iter_undo_payloads(snapshot, delete_created=True)
        self.assertEqual(deletes, [])
        self.assertEqual(unrecoverable[0]["title"], "Punpun")

    def test_a_damaged_or_foreign_snapshot_is_refused(self):
        path = self.dir / ".aniexport_undo_9999.json"
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(gen.load_undo_snapshot(path.name))
        path.write_text(json.dumps({"version": 99, "rows": []}), encoding="utf-8")
        self.assertIsNone(gen.load_undo_snapshot(path.name))

    def test_the_delete_mutation_matches_the_schema(self):
        self.assertIn("DeleteMediaListEntry (id: $id)", gen.ANILIST_DELETE_ENTRY_MUTATION)
        with patch.object(gen, "_execute_anilist_authenticated",
                          return_value={"data": {"DeleteMediaListEntry": {"deleted": True}}}):
            self.assertTrue(gen.anilist_delete_entry("t", 5)["ok"])
        with patch.object(gen, "_execute_anilist_authenticated",
                          return_value={"data": {"DeleteMediaListEntry": {"deleted": False}}}):
            self.assertFalse(gen.anilist_delete_entry("t", 5)["ok"])


class WriteEndpointTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        out = Path(self._dir.name)
        env = patch.dict(os.environ, {"ANIEXPORT_CACHE": str(out / ".anilist_cache.json")})
        env.start()
        self.addCleanup(env.stop)
        self.out = out
        for name, value in (("EXPORT_XML_PATH", out / "a.xml"), ("EXPORT_GZ_PATH", out / "a.gz"),
                            ("EXPORT_MANGA_XML_PATH", out / "m.xml"),
                            ("EXPORT_MANGA_GZ_PATH", out / "m.gz")):
            patcher = patch.object(gui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
        thread = threading.Thread(target=serve(self.server), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=20).read().decode(), 200
        except urllib.error.HTTPError as exc:
            return exc.read().decode(), exc.code

    def test_token_is_required_everywhere(self):
        for path in ("/api/anilist-test", "/api/anilist-preview", "/api/anilist-write"):
            body, code = self.post(path, {"entries": []})
            self.assertEqual(code, 400, path)

    def test_write_requires_explicit_confirmation(self):
        body, code = self.post("/api/anilist-write",
                               {"token": "t", "entries": [{"anilist_id": 1}]})
        self.assertEqual(code, 400)
        self.assertIn("confirmation", body)

    def test_preview_counts_adds_and_updates(self):
        entries = [{"anilist_id": 30642, "title": "A", "status": "Reading"},
                   {"anilist_id": 148167, "title": "B", "status": "Reading"}]
        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "kevin"}), \
             patch.object(gui, "anilist_existing_entries",
                          return_value={30642: {"id": 55, "mediaId": 30642}}):
            body, code = self.post("/api/anilist-preview",
                                   {"token": "t", "media_type": "manga", "entries": entries})
        self.assertEqual(code, 200)
        plan = json.loads(body)
        self.assertEqual((plan["updates"], plan["creates"]), (1, 1))
        self.assertEqual(plan["viewer"], "kevin")

    def test_a_bad_token_is_rejected_before_anything_is_written(self):
        with patch.object(gui, "anilist_viewer",
                          return_value={"ok": False, "error": "AniList rejected the token."}):
            body, code = self.post("/api/anilist-write", {
                "token": "bad", "confirm": True, "media_type": "manga",
                "entries": [{"anilist_id": 1, "title": "x", "status": "Reading"}]})
        self.assertEqual(code, 401)

    def test_write_streams_progress_and_never_echoes_the_token(self):
        entries = [{"anilist_id": 1, "title": "A", "status": "Reading"},
                   {"anilist_id": 2, "title": "B", "status": "Reading"}]
        secret = "super-secret-token-value"
        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "kevin"}), \
             patch.object(gui, "anilist_existing_entries", return_value={1: {"id": 11, "mediaId": 1}}), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=123)):
            body, code = self.post("/api/anilist-write", {
                "token": secret, "confirm": True, "media_type": "manga", "entries": entries})
        events = [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                  if c.strip().startswith("data: ")]
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "started")
        self.assertIn("plan", kinds)
        self.assertEqual(sum(1 for k in kinds if k == "write_progress"), 2)
        done = [e for e in events if e["type"] == "write_complete"][0]
        self.assertEqual((done["updated"], done["created"]), (1, 1))
        self.assertNotIn(secret, body, "the token must never appear in a response")

    def test_a_failed_entry_is_reported_not_swallowed(self):
        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "k"}), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=batch_of(ok=False, error="The score may not be greater than 100.")):
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "entries": [{"anilist_id": 1, "title": "A", "status": "Reading"}]})
        done = [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")][-1]
        self.assertEqual(done["type"], "write_complete")
        self.assertEqual(len(done["failed"]), 1)
        self.assertIn("score", done["failed"][0]["error"])


class SafeWriteEndpointTests(WriteEndpointTests):
    """The endpoints around the conflict policy, the snapshot, undo and retry."""

    VIEWER = {"ok": True, "id": 7, "name": "kevin"}
    ROW = {"anilist_id": 30642, "title": "Vinland Saga", "chapters": 224, "volumes": 29,
           "status": "Reading", "my_read_chapters": 142}
    ACCOUNT = {30642: {"id": 999, "mediaId": 30642, "status": "CURRENT", "progress": 224,
                       "progressVolumes": 29, "score": 80, "repeat": 0, "notes": None,
                       "startedAt": None, "completedAt": None}}

    def events(self, body):
        return [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")]

    def test_the_preview_shows_the_conflict_before_anything_is_sent(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT):
            body, code = self.post("/api/anilist-preview", {
                "token": "t", "media_type": "manga", "entries": [dict(self.ROW)]})
        self.assertEqual(code, 200)
        plan = json.loads(body)
        self.assertEqual(plan["conflicts"], 1)
        row = plan["rows"][0]
        progress = [d for d in row["diff"] if d["field"] == "progress"][0]
        self.assertEqual((progress["from"], progress["to"], progress["applied"]),
                         (224, 142, 224))
        self.assertTrue(progress["blocked"])

    def test_the_preview_counts_rows_that_change_nothing(self):
        row = {"anilist_id": 30642, "title": "V", "chapters": 224, "volumes": 29,
               "status": "Reading", "my_read_chapters": 224, "my_read_volumes": 29, "score": 8}
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT):
            body, _ = self.post("/api/anilist-preview", {
                "token": "t", "media_type": "manga", "entries": [row]})
        plan = json.loads(body)
        self.assertEqual((plan["unchanged"], plan["conflicts"]), (1, 0))
        self.assertEqual(plan["rows"][0]["diff"], [])

    def test_the_preview_refuses_when_the_account_cannot_be_read(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries",
                          side_effect=gui.AniListReadError("AniList returned HTTP 500.")):
            body, code = self.post("/api/anilist-preview", {
                "token": "t", "media_type": "manga", "entries": [dict(self.ROW)]})
        self.assertEqual(code, 502)
        self.assertIn("second copies", json.loads(body)["error"])

    def test_the_write_refuses_the_same_way_mid_stream(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries",
                          side_effect=gui.AniListReadError("AniList returned HTTP 500.")), \
             patch.object(gui, "anilist_write_batch") as writer:
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "entries": [dict(self.ROW)]})
        kinds = [e["type"] for e in self.events(body)]
        self.assertIn("error", kinds)
        self.assertNotIn("write_progress", kinds)
        writer.assert_not_called()

    def test_an_override_reaches_the_payload_through_the_endpoint(self):
        sent = []
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: sent.extend(batch)
                          or [{"ok": True, "id": 1} for _ in batch]):
            self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "overrides": {"30642": {"progress": "mine"}},
                "entries": [dict(self.ROW)]})
        self.assertEqual(sent[0]["variables"]["progress"], 142)

    def test_a_junk_override_is_dropped_not_trusted(self):
        sent = []
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: sent.extend(batch)
                          or [{"ok": True, "id": 1} for _ in batch]):
            self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "overrides": {"30642": {"progress": "theirs", "id": "mine"},
                              "oops": {"progress": "mine"}},
                "entries": [dict(self.ROW)]})
        self.assertEqual(sent[0]["variables"]["progress"], 224)

    def test_the_write_saves_a_snapshot_the_undo_can_use(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=999)):
            body, _ = self.post("/api/anilist-write", {
                "token": "super-secret-token-value", "confirm": True, "media_type": "manga",
                "entries": [dict(self.ROW)]})
        done = self.events(body)[-1]
        self.assertEqual(done["type"], "write_complete")
        name = done["snapshot"]
        self.assertTrue((self.out / name).is_file())
        snapshot = gen.load_undo_snapshot(name)
        self.assertEqual(snapshot["rows"][0]["prior"]["progress"], 224)
        self.assertNotIn("super-secret-token-value",
                         (self.out / name).read_text(encoding="utf-8"),
                         "a snapshot must not carry the token")

    def test_a_created_entry_id_lands_in_the_snapshot_as_the_write_runs(self):
        row = {"anilist_id": 34632, "title": "Punpun", "chapters": 147, "status": "Reading",
               "my_read_chapters": 40}
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=604576747)):
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga", "entries": [row]})
        snapshot = gen.load_undo_snapshot(self.events(body)[-1]["snapshot"])
        self.assertEqual(snapshot["rows"][0]["created_entry_id"], 604576747)

    def test_undo_restores_prior_values_and_leaves_creates_alone(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=999)):
            written, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "entries": [dict(self.ROW)]})
        name = self.events(written)[-1]["snapshot"]
        sent = []
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: sent.extend(batch)
                          or [{"ok": True, "id": 999} for _ in batch]), \
             patch.object(gui, "anilist_delete_entry") as deleter:
            body, code = self.post("/api/anilist-undo", {
                "token": "t", "confirm": True, "snapshot": name})
        self.assertEqual(code, 200)
        done = self.events(body)[-1]
        self.assertEqual(done["type"], "undo_complete")
        self.assertEqual(done["restored"], 1)
        self.assertEqual(sent[0]["variables"]["progress"], 224)
        deleter.assert_not_called()

    def test_undo_deletes_a_created_entry_only_when_ticked(self):
        row = {"anilist_id": 34632, "title": "Punpun", "chapters": 147, "status": "Reading",
               "my_read_chapters": 40}
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=555)):
            written, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga", "entries": [row]})
        name = self.events(written)[-1]["snapshot"]

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_delete_entry") as quiet:
            body, code = self.post("/api/anilist-undo", {
                "token": "t", "confirm": True, "snapshot": name})
        self.assertEqual(code, 400, "nothing to do without the delete box ticked")
        quiet.assert_not_called()

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_delete_entry", return_value={"ok": True}) as deleter:
            body, code = self.post("/api/anilist-undo", {
                "token": "t", "confirm": True, "snapshot": name, "delete_created": True})
        self.assertEqual(code, 200)
        self.assertEqual(deleter.call_args[0][1], 555)
        self.assertEqual(self.events(body)[-1]["deleted"], 1)

    def test_undo_needs_a_token_and_a_confirmation_and_a_real_snapshot(self):
        self.assertEqual(self.post("/api/anilist-undo", {})[1], 400)
        self.assertEqual(self.post("/api/anilist-undo", {"token": "t"})[1], 400)
        body, code = self.post("/api/anilist-undo",
                               {"token": "t", "confirm": True, "snapshot": "../etc/passwd"})
        self.assertEqual(code, 404)

    def test_a_stale_row_fails_alone_and_the_rest_of_the_undo_runs(self):
        rows = [dict(self.ROW), {"anilist_id": 1, "title": "B", "chapters": 10,
                                 "status": "Reading", "my_read_chapters": 5}]
        account = dict(self.ACCOUNT)
        account[1] = {"id": 1001, "mediaId": 1, "status": "CURRENT", "progress": 9,
                      "progressVolumes": 0, "score": 0, "repeat": 0}
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=account), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=9)):
            written, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga", "entries": rows})
        name = self.events(written)[-1]["snapshot"]
        outcomes = [{"ok": False, "error": "Entry does not exist."}, {"ok": True, "id": 1001}]
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: outcomes[:len(batch)]):
            body, _ = self.post("/api/anilist-undo", {
                "token": "t", "confirm": True, "snapshot": name})
        done = self.events(body)[-1]
        self.assertEqual((done["restored"], done["failed_count"]), (1, 1))

    def test_the_failed_rows_can_be_retried_without_rebuilding_them(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=batch_of(ok=False, error="nope")):
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "entries": [dict(self.ROW)]})
        done = self.events(body)[-1]
        self.assertEqual(done["failed_count"], 1)
        run_id = done["run_id"]

        sent = []
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries") as reader, \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: sent.extend(batch)
                          or [{"ok": True, "id": 999} for _ in batch]):
            retried, code = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "retry": True, "run_id": run_id,
                "media_type": "manga"})
        self.assertEqual(code, 200)
        reader.assert_not_called()
        self.assertEqual(sent[0]["variables"]["progress"], 224)
        self.assertEqual(self.events(retried)[-1]["updated"], 1)

    def test_retrying_an_unknown_run_is_refused_not_guessed(self):
        body, code = self.post("/api/anilist-write", {
            "token": "t", "confirm": True, "retry": True, "run_id": "nope"})
        self.assertEqual(code, 404)

    def test_the_snapshot_listing_is_served_without_a_token(self):
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value=self.ACCOUNT), \
             patch.object(gui, "anilist_write_batch", side_effect=batch_of(ok=True, id=999)):
            self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga",
                "entries": [dict(self.ROW)]})
        with urllib.request.urlopen(self.base + "/api/anilist-snapshots", timeout=20) as res:
            listed = json.loads(res.read().decode())
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["snapshots"]), 1)


class BatchWriteEndpointTests(WriteEndpointTests):
    def test_the_endpoint_sends_one_request_per_batch_not_per_row(self):
        rows = [{"anilist_id": i, "title": "T%d" % i, "chapters": 10, "status": "Reading",
                 "my_read_chapters": 5} for i in range(1, 20)]
        calls = []

        def record(token, batch):
            calls.append(len(batch))
            return [{"ok": True, "id": 500 + i} for i in range(len(batch))]

        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "k"}), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch", side_effect=record):
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga", "entries": rows})
        self.assertEqual(calls, [8, 8, 3], "19 rows must go out as three requests")
        events = [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                  if c.strip().startswith("data: ")]
        progress = [e for e in events if e["type"] == "write_progress"]
        self.assertEqual(len(progress), 19,
                         "one event per row, so the bar does not jump a batch at a time")
        self.assertEqual([e["current"] for e in progress], list(range(1, 20)))
        self.assertTrue(all("eta_seconds" in e for e in progress),
                        "the write phase needs an ETA; only the resolve phase had one")
        self.assertEqual(progress[-1]["eta_seconds"], 0)

    def test_a_partial_batch_failure_is_reported_per_row(self):
        rows = [{"anilist_id": i, "title": "T%d" % i, "chapters": 10, "status": "Reading",
                 "my_read_chapters": 5} for i in range(1, 4)]

        def half(token, batch):
            return [{"ok": i != 1, "id": 7, "error": "nope"} for i in range(len(batch))]

        with patch.object(gui, "anilist_viewer", return_value={"ok": True, "id": 7, "name": "k"}), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "anilist_write_batch", side_effect=half):
            body, _ = self.post("/api/anilist-write", {
                "token": "t", "confirm": True, "media_type": "manga", "entries": rows})
        done = [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")][-1]
        self.assertEqual(done["failed_count"], 1)
        self.assertEqual(done["failed"][0]["title"], "T2")
        self.assertEqual(done["created"], 2)


class UndoBatchingTests(WriteEndpointTests):
    """Undo restores are SaveMediaListEntry too, so they batch like the write.

    Deletes are a different mutation and cannot share a document, so they stay
    one at a time. Order has to survive batching: a repeat repair is a second
    write for a row and must land after the restore it repairs.
    """

    VIEWER = {"ok": True, "id": 7, "name": "kevin"}

    def events(self, body):
        return [json.loads(c[6:]) for c in body.split(chr(10) * 2)
                if c.strip().startswith("data: ")]

    def snapshot_with(self, rows):
        snapshot = {"version": 1, "written_at": 1, "media_type": "manga",
                    "viewer_id": 7, "rows": rows}
        name = gen.save_undo_snapshot(snapshot)
        self.assertIsNotNone(name)
        return name

    def prior_row(self, media_id, repeat=0):
        return {"anilist_id": media_id, "title": "T%d" % media_id, "was_create": False,
                "prior": {"id": 500 + media_id, "status": "CURRENT", "progress": 10,
                          "progressVolumes": 1, "score": 50, "repeat": repeat,
                          "notes": "", "startedAt": None, "completedAt": None}}

    def test_restores_go_out_in_batches(self):
        name = self.snapshot_with([self.prior_row(i) for i in range(1, 20)])
        sizes = []

        def record(token, batch):
            sizes.append(len(batch))
            return [{"ok": True, "id": 1} for _ in batch]

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_write_batch", side_effect=record):
            body, code = self.post("/api/anilist-undo",
                                   {"token": "t", "confirm": True, "snapshot": name})
        self.assertEqual(code, 200)
        self.assertEqual(sizes, [8, 8, 3], "19 restores must be three requests, not nineteen")
        done = self.events(body)[-1]
        self.assertEqual(done["restored"], 19)
        progress = [e for e in self.events(body) if e["type"] == "write_progress"]
        self.assertEqual(len(progress), 19, "one event per row, as the write does")
        self.assertTrue(all("eta_seconds" in e for e in progress))

    def test_a_repair_follows_the_restore_it_repairs(self):
        # A completed row with a reread count emits a second payload; batching
        # must not reorder them, or the repair is overwritten by the restore.
        rows = [dict(self.prior_row(1, repeat=4), prior=dict(self.prior_row(1, repeat=4)["prior"],
                                                             status="COMPLETED", repeat=4))]
        name = self.snapshot_with(rows)
        seen = []

        def record(token, batch):
            seen.extend((p["anilist_id"], p.get("repair") or "restore") for p in batch)
            return [{"ok": True, "id": 1} for _ in batch]

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_write_batch", side_effect=record):
            body, _ = self.post("/api/anilist-undo",
                                {"token": "t", "confirm": True, "snapshot": name})
        self.assertEqual(seen, [(1, "restore"), (1, "repeat")])
        done = self.events(body)[-1]
        self.assertEqual(done["restored"], 1, "the repair is not a second restored row")

    def test_deletes_are_not_batched_with_restores(self):
        rows = [self.prior_row(1),
                {"anilist_id": 2, "title": "C", "was_create": True, "created_entry_id": 77}]
        name = self.snapshot_with(rows)
        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: [{"ok": True, "id": 1} for _ in batch]) as writer, \
             patch.object(gui, "anilist_delete_entry", return_value={"ok": True}) as deleter:
            body, _ = self.post("/api/anilist-undo", {"token": "t", "confirm": True,
                                                      "snapshot": name, "delete_created": True})
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(deleter.call_count, 1)
        done = self.events(body)[-1]
        self.assertEqual((done["restored"], done["deleted"]), (1, 1))

    def test_a_cancel_lands_between_batches(self):
        name = self.snapshot_with([self.prior_row(i) for i in range(1, 20)])

        def cancel_after_first(token, batch):
            for control in list(gui._active_runs.values()):
                control["cancel"].set()
            return [{"ok": True, "id": 1} for _ in batch]

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_write_batch", side_effect=cancel_after_first):
            body, _ = self.post("/api/anilist-undo",
                                {"token": "t", "confirm": True, "snapshot": name})
        kinds = [e["type"] for e in self.events(body)]
        self.assertIn("cancelled", kinds)
        self.assertNotIn("undo_complete", kinds)


class SnapshotDurabilityTests(WriteEndpointTests):
    VIEWER = {"ok": True, "id": 7, "name": "kevin"}

    def test_every_created_id_is_on_disk_before_the_next_row(self):
        # Saved per create rather than per batch: a crash between a batch
        # returning and the file being written would otherwise strand up to
        # eight created entries with no id, and undo cannot delete what it
        # cannot name.
        rows = [{"anilist_id": i, "title": "T%d" % i, "chapters": 10, "status": "Reading",
                 "my_read_chapters": 5} for i in range(1, 6)]
        saves = []
        real_save = gen.save_undo_snapshot

        def counting_save(snapshot, name=None):
            saves.append(sum(1 for r in snapshot["rows"] if r.get("created_entry_id")))
            return real_save(snapshot, name)

        with patch.object(gui, "anilist_viewer", return_value=self.VIEWER), \
             patch.object(gui, "anilist_existing_entries", return_value={}), \
             patch.object(gui, "save_undo_snapshot", side_effect=counting_save), \
             patch.object(gui, "anilist_write_batch",
                          side_effect=lambda token, batch: [{"ok": True, "id": 900 + i}
                                                            for i in range(len(batch))]):
            self.post("/api/anilist-write", {"token": "t", "confirm": True,
                                             "media_type": "manga", "entries": rows})
        # One save before the write, then one per create with a growing count.
        self.assertEqual(saves, [0, 1, 2, 3, 4, 5],
                         "each created id must reach disk before the next row is written")


class ResolveOneTests(unittest.TestCase):
    """One entry point for fixing an unmatched row: title, id, or pasted link."""

    def test_links_are_parsed_exactly(self):
        cases = {
            "https://anilist.co/manga/30002/Berserk/": ("anilist", 30002),
            "anilist.co/anime/1": ("anilist", 1),
            "https://myanimelist.net/manga/642/Vinland_Saga": ("mal", 642),
        }
        for text, (kind, value) in cases.items():
            with self.subTest(text=text):
                ref = gen.parse_media_reference(text)
                self.assertEqual((ref["kind"], ref["value"]), (kind, value))

    def test_bare_number_is_reported_as_ambiguous_not_guessed(self):
        self.assertEqual(gen.parse_media_reference("642"),
                         {"kind": "digits", "value": 642})

    def test_a_link_resolves_through_the_matching_lookup(self):
        entry = {"id": 0, "anilist_id": 148167, "title": "AniList only"}
        with patch.object(gen, "lookup_by_anilist_id", return_value=entry) as by_al,              patch.object(gen, "lookup_by_mal_id") as by_mal:
            out = gen.resolve_one("https://anilist.co/manga/148167/", "manga")
        self.assertTrue(out["ok"])
        self.assertEqual(out["entries"][0]["anilist_id"], 148167)
        self.assertEqual(out["entries"][0]["match_source"], "anilist")
        by_al.assert_called_once()
        by_mal.assert_not_called()

    def test_a_bare_number_is_tried_as_both_ids(self):
        with patch.object(gen, "lookup_by_anilist_id",
                          return_value={"id": 1, "anilist_id": 642, "title": "By AniList id"}),              patch.object(gen, "lookup_by_mal_id",
                          return_value={"id": 642, "anilist_id": 30642, "title": "By MAL id"}):
            out = gen.resolve_one("642", "manga")
        self.assertEqual([e["match_source"] for e in out["entries"]], ["anilist", "mal"])

    def test_an_explicit_id_kind_narrows_the_lookup(self):
        with patch.object(gen, "lookup_by_anilist_id", return_value=None) as by_al,              patch.object(gen, "lookup_by_mal_id",
                          return_value={"id": 642, "anilist_id": 30642, "title": "x"}) as by_mal:
            out = gen.resolve_one("642", "manga", id_kind="mal")
        by_al.assert_not_called()
        by_mal.assert_called_once()
        self.assertEqual(out["entries"][0]["id"], 642)

    def test_free_text_returns_ranked_candidates(self):
        with patch.object(gen, "search_candidates",
                          return_value=[{"id": 1, "anilist_id": 2, "title": "A"},
                                        {"id": 3, "anilist_id": 4, "title": "B"}]):
            out = gen.resolve_one("something", "manga")
        self.assertEqual(len(out["entries"]), 2)
        self.assertTrue(all(e["match_source"] == "search" for e in out["entries"]))

    def test_nothing_found_is_said_plainly(self):
        with patch.object(gen, "search_candidates", return_value=[]):
            out = gen.resolve_one("Raphsody", "manga")
        self.assertFalse(out["ok"])
        self.assertIn("no manga matching", out["error"])

    def test_endpoint_rejects_an_empty_query(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            patcher = patch.object(gui, "EXPORT_MANGA_XML_PATH", out / "m.xml")
            patcher.start(); self.addCleanup(patcher.stop)
            server = ThreadingHTTPServer(("127.0.0.1", 0), gui.MalGuiHandler)
            thread = threading.Thread(target=serve(server), daemon=True)
            thread.start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/resolve-one",
                    data=json.dumps({"query": "  ", "media_type": "manga"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                try:
                    urllib.request.urlopen(req, timeout=10)
                    code = 200
                except urllib.error.HTTPError as exc:
                    code = exc.code
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(code, 400)


class ImportTests(unittest.TestCase):
    def _roundtrip(self, entries, media_type="manga"):
        builder = gen.build_mal_manga_xml if media_type == "manga" else gen.build_mal_xml
        return gui.parse_mal_xml(builder(entries))

    def test_manga_export_round_trips(self):
        original = [{"id": 642, "title": "Vinland Saga", "chapters": 224, "volumes": 29,
                     "status": "Reading", "score": 9, "my_read_chapters": 142,
                     "my_read_volumes": 18, "start_date": "2024-03-01",
                     "comments": "great", "tags": "seinen"}]
        parsed = self._roundtrip(original)
        self.assertEqual(parsed["media_type"], "manga")
        entry = parsed["entries"][0]
        self.assertEqual(entry["id"], 642)
        self.assertEqual(entry["title"], "Vinland Saga")
        self.assertEqual(entry["status"], "Reading")
        self.assertEqual(entry["score"], 9)
        self.assertEqual(entry["my_read_chapters"], 142)
        self.assertEqual(entry["my_read_volumes"], 18)
        self.assertEqual(entry["start_date"], "2024-03-01")
        self.assertEqual(entry["comments"], "great")
        self.assertEqual(entry["tags"], "seinen")
        self.assertTrue(entry["can_export_xml"])
        self.assertFalse(entry["can_write_anilist"], "a MAL file carries no AniList id")

    def test_anime_export_round_trips(self):
        parsed = self._roundtrip(
            [{"id": 1, "title": "Cowboy Bebop", "type": "TV", "episodes": 26,
              "status": "Completed"}], "anime")
        self.assertEqual(parsed["media_type"], "anime")
        self.assertEqual(parsed["entries"][0]["my_watched_episodes"], 26)

    def test_rubbish_input_is_rejected_clearly(self):
        for bad, needle in (("not xml at all", "valid XML"),
                            ("<other><x/></other>", "myanimelist")):
            with self.assertRaises(ValueError) as caught:
                gui.parse_mal_xml(bad)
            self.assertIn(needle, str(caught.exception))

    def test_doctype_is_refused(self):
        with self.assertRaises(ValueError):
            gui.parse_mal_xml('<!DOCTYPE foo [<!ENTITY a "b">]><myanimelist></myanimelist>')

    def test_entries_without_an_id_are_skipped(self):
        xml = ("<myanimelist><myinfo><user_export_type>2</user_export_type></myinfo>"
               "<manga><manga_mangadb_id>0</manga_mangadb_id><my_status>Reading</my_status></manga>"
               "<manga><manga_mangadb_id>5</manga_mangadb_id><my_status>Reading</my_status></manga>"
               "</myanimelist>")
        parsed = gui.parse_mal_xml(xml)
        self.assertEqual(len(parsed["entries"]), 1)
        self.assertEqual(parsed["skipped"], 1)


class ScorePrecisionTests(unittest.TestCase):
    """AniList stores scores out of 100 whatever the account displays. Rounding
    one to 0-10 and multiplying it back by ten turned an 87 into a 90 - visible
    in a write's diff, and silent in a copy, which shows counts only."""

    ROW = {"anilist_id": 1, "title": "x", "status": "Completed",
           "chapters": 10, "score": 9, "score_raw": 87}

    def _sent(self, row):
        payloads, _ = gen.iter_write_payloads([row], "manga", {})
        return payloads[0]["variables"]["scoreRaw"]

    def test_an_untouched_score_is_sent_back_exactly(self):
        self.assertEqual(self._sent(self.ROW), 87)

    def test_an_edited_score_beats_the_value_it_was_read_at(self):
        """The two disagreeing is the only evidence that a human changed it."""
        self.assertEqual(self._sent(dict(self.ROW, score=7)), 70)

    def test_a_row_that_never_came_from_an_account_is_unaffected(self):
        row = {"anilist_id": 1, "title": "x", "status": "Completed",
               "chapters": 10, "score": 8}
        self.assertEqual(self._sent(row), 80)

    def test_a_corrupt_raw_value_falls_back_rather_than_raising(self):
        for junk in ("nonsense", None, [1], {}):
            with self.subTest(junk=junk):
                self.assertEqual(self._sent(dict(self.ROW, score_raw=junk)), 90)

    def test_a_raw_value_out_of_range_is_clamped(self):
        self.assertEqual(self._sent(dict(self.ROW, score=10, score_raw=5000)), 100)

    def test_a_list_read_carries_the_exact_score(self):
        entry = {"id": 5, "status": "COMPLETED", "score": 87, "progress": 3,
                 "media": {"id": 9, "idMal": 8, "title": {"english": "T", "romaji": "T"},
                           "format": "MANGA", "chapters": 10, "volumes": 1,
                           "startDate": {"year": 2001}, "popularity": 1}}
        row = gen._row_from_list_entry(entry, "manga")
        self.assertEqual(row["score_raw"], 87, "the exact value, for writing back")
        self.assertEqual(row["score"], 9, "and the rounded one, for the interface")

    def test_a_round_trip_through_a_copy_changes_nothing(self):
        """Read an account, write it straight back: the diff must be empty."""
        entry = {"id": 5, "status": "COMPLETED", "score": 87, "progress": 10,
                 "progressVolumes": 1, "repeat": 0, "notes": "",
                 "media": {"id": 9, "idMal": 8, "title": {"english": "T", "romaji": "T"},
                           "format": "MANGA", "chapters": 10, "volumes": 1,
                           "startDate": {"year": 2001}, "popularity": 1}}
        row = gen._row_from_list_entry(entry, "manga")
        known = {9: {"id": 5, "mediaId": 9, "status": "COMPLETED", "score": 87,
                     "progress": 10, "progressVolumes": 1, "repeat": 0, "notes": ""}}
        payloads, _ = gen.iter_write_payloads([row], "manga", known)
        self.assertEqual([d["field"] for d in payloads[0]["diff"]], [],
                         "a copy of an unchanged row must not rewrite its score")

    def test_the_page_invalidates_the_raw_score_whenever_it_writes_one(self):
        """Without this a score edit on an AniList-read row is a no-op: the
        stale raw value still agrees with nothing and wins the comparison."""
        self.assertEqual(gui.HTML_TEMPLATE.count("delete item.score_raw;"), 2,
                         "the per-row editor and the bulk control")


class RequestSizeTests(unittest.TestCase):
    """The size checks ran on an already-parsed body, so the whole thing was
    pulled into memory first."""

    def test_the_cap_clears_the_largest_legitimate_body(self):
        self.assertGreater(gui.MAX_BODY_BYTES, gui.MAX_IMPORT_BYTES,
                           "a MyAnimeList export has to fit through it")

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        with io.open("gui.py", encoding="utf-8") as handle:
            source = handle.read()
        head = source.split("raw_body = self.rfile.read", 1)[0]
        self.assertIn("MAX_BODY_BYTES", head.rsplit("def do_POST", 1)[-1],
                      "the check has to come before the read, not after it")

    def test_a_malformed_length_does_not_become_a_500(self):
        with io.open("gui.py", encoding="utf-8") as handle:
            source = handle.read()
        block = source.rsplit("def do_POST", 1)[-1].split("raw_body =", 1)[0]
        self.assertIn("except (TypeError, ValueError)", block)


class DoctypeScanTests(unittest.TestCase):
    """An entity cannot be declared without a DOCTYPE, so the DOCTYPE scan is
    the whole guard - and it used to cover the first 4KB only."""

    def test_a_doctype_behind_a_long_prolog_is_still_caught(self):
        prolog = "<!-- " + ("x" * 5000) + " -->"
        with self.assertRaises(ValueError):
            gui.parse_mal_xml(prolog + "<!DOCTYPE a><myanimelist/>")

    def test_the_check_is_case_insensitive_and_tolerates_spacing(self):
        for form in ("<!doctype a>", "<!DocType a>", "<! DOCTYPE a>"):
            with self.subTest(form=form):
                with self.assertRaises(ValueError):
                    gui.parse_mal_xml(form + "<myanimelist/>")

    def test_an_ordinary_export_still_parses(self):
        xml = ('<?xml version="1.0" encoding="UTF-8" ?><myanimelist>'
               "<myinfo><user_export_type>2</user_export_type></myinfo>"
               "<manga><manga_mangadb_id>2</manga_mangadb_id>"
               "<manga_title>T</manga_title><my_status>Reading</my_status>"
               "</manga></myanimelist>")
        self.assertEqual(gui.parse_mal_xml(xml)["entries"][0]["id"], 2)

if __name__ == "__main__":
    unittest.main()
