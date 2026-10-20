"""Match confidence: the ratio that used to be computed and thrown away.

The thresholds here are calibrated against a real 339-title list, not guessed.
The plan's proposed floor of 0.6 would have flagged 51.5% of resolved rows,
because translated titles share almost no characters with the English name a
user pastes; the signal that survived calibration is an inversion - the chosen
entry matching the searched title worse than one the ranker passed over.
"""
import re
import unittest

import generate_mal_xml as gen
import gui


def node(english, romaji=None, **over):
    base = {"id": 1, "idMal": 2, "format": "MANGA", "status": "FINISHED", "popularity": 100,
            "title": {"english": english, "romaji": romaji or english},
            "chapters": 10, "volumes": 1, "episodes": 10,
            "startDate": {"year": 2020}, "coverImage": {"medium": "u"}}
    base.update(over)
    return base


class TitleRatioTests(unittest.TestCase):
    def test_an_exact_title_scores_one(self):
        self.assertEqual(gen._title_ratio(node("Vinland Saga"), "Vinland Saga"), 1.0)

    def test_case_accents_and_punctuation_do_not_count_against_a_match(self):
        self.assertEqual(gen._title_ratio(node("One-Punch Man"), "one punch man"), 1.0)

    def test_the_better_of_the_two_title_forms_wins(self):
        pair = node("Completely Different", romaji="Vinland Saga")
        self.assertEqual(gen._title_ratio(pair, "Vinland Saga"), 1.0)

    def test_nothing_searched_means_no_ratio_rather_than_a_perfect_one(self):
        self.assertEqual(gen._title_ratio(node("Vinland Saga"), ""), 0.0)

    def test_the_ranker_and_the_badge_use_one_implementation(self):
        # _media_rank used to inline the SequenceMatcher calls; two copies of
        # the same arithmetic would drift the moment either was tuned.
        import inspect
        rank = inspect.getsource(gen._media_rank)
        self.assertIn("_title_ratio(", rank)
        self.assertNotIn("SequenceMatcher", rank)


class ParsedEntryTests(unittest.TestCase):
    def test_a_searched_entry_carries_its_ratio(self):
        entry = gen._parse_manga_node(node("Vinland Saga"), fallback_title="Vinland Saga")
        self.assertEqual(entry["match_ratio"], 1.0)
        self.assertFalse(entry["needs_review"])

    def test_a_hopeless_match_is_flagged(self):
        entry = gen._parse_manga_node(node("Something Else Entirely"),
                                      fallback_title="qqqq zzzz")
        self.assertLess(entry["match_ratio"], gen.REVIEW_RATIO_FLOOR)
        self.assertTrue(entry["needs_review"])

    def test_a_row_that_was_never_a_guess_carries_no_ratio(self):
        # Rows read back from an AniList list were not searched for.
        entry = gen._parse_manga_node(node("Vinland Saga"))
        self.assertNotIn("match_ratio", entry)
        self.assertNotIn("needs_review", entry)

    def test_an_anime_entry_gets_the_same_treatment(self):
        entry = gen._parse_media_node(node("Lain", format="TV"), fallback_title="Lain")
        self.assertEqual(entry["match_ratio"], 1.0)


class RunnerUpGapTests(unittest.TestCase):
    def test_the_gap_is_measured_against_the_runner_up(self):
        out = gen._candidates([node("Vinland Saga"), node("Vinland Saga: Slave", id=3, idMal=4)],
                              "manga", "Vinland Saga")
        self.assertEqual(out[0]["match_ratio"], 1.0)
        self.assertGreater(out[0]["runner_up_gap"], 0)
        self.assertFalse(out[0]["needs_review"])

    def test_one_candidate_has_no_gap_which_is_not_a_tie(self):
        out = gen._candidates([node("Vinland Saga")], "manga", "Vinland Saga")
        self.assertIsNone(out[0]["runner_up_gap"])
        self.assertFalse(out[0]["needs_review"],
                         "None must not be read as a gap of zero")

    def test_an_inversion_is_flagged(self):
        # The chosen entry has a MAL id and so outranks a better title match.
        chosen = node("Totally Unrelated Title", id=1, idMal=7)
        passed_over = node("Vinland Saga", id=2, idMal=None)
        out = gen._candidates([chosen, passed_over], "manga", "Vinland Saga")
        self.assertEqual(out[0]["title"], "Totally Unrelated Title",
                         "the MAL-id tier is what puts the worse title first")
        self.assertLess(out[0]["runner_up_gap"], 0)
        self.assertTrue(out[0]["needs_review"])

    def test_a_plain_tie_at_a_good_ratio_is_not_suspicious(self):
        # "One Punch Man" ties with "One-Punch Man" and is right; the
        # calibration is what ruled ties out as a signal.
        out = gen._candidates([node("One-Punch Man", id=1, idMal=2),
                               node("One Punch Man", id=3, idMal=4)],
                              "manga", "One Punch Man")
        self.assertEqual(out[0]["runner_up_gap"], 0.0)
        self.assertFalse(out[0]["needs_review"])


class CalibratedConstantTests(unittest.TestCase):
    def test_the_floor_is_the_calibrated_one_not_the_proposed_one(self):
        self.assertEqual(gen.REVIEW_RATIO_FLOOR, 0.15)
        self.assertNotEqual(gen.REVIEW_RATIO_FLOOR, 0.6,
                            "0.6 flagged 51.5% of a real list; see the calibration")
        self.assertEqual(gen.REVIEW_TIE_GAP, 0.0)

    def test_the_cache_version_was_bumped_for_the_new_fields(self):
        # load_cache discards the whole file on a mismatch, so stale rows
        # cannot leak in without match_ratio.
        self.assertGreaterEqual(gen.CACHE_VERSION, 2)

    def test_the_page_is_served_the_floor_rather_than_a_copy_of_it(self):
        page = gui._page_html()
        self.assertNotIn("__REVIEW_RATIO_FLOOR__", page, "the placeholder was left unsubstituted")
        found = re.search(r"REVIEW_RATIO_FLOOR = ([0-9.]+);", page)
        self.assertIsNotNone(found)
        self.assertEqual(float(found.group(1)), gen.REVIEW_RATIO_FLOOR)


class ReviewSurfaceTests(unittest.TestCase):
    def _script(self) -> str:
        return re.findall(r"<script>(.*?)</script>", gui.HTML_TEMPLATE, re.S)[-1]

    def test_a_needs_review_chip_exists_and_filters_on_the_flag(self):
        script = self._script()
        self.assertIn("{ id: 'review', label: 'Needs review' }", script)
        self.assertIn("if (statusFilter === 'review')", script,
                      "a row can be both Completed and doubtful, so it is not a bucket")
        self.assertIn("counts.review = (counts.review || 0) + 1", script,
                      "a chip with no count of its own is never rendered")

    def test_choosing_a_match_by_hand_clears_the_flag(self):
        script = self._script()
        body = script.split("function applyFix(entry)", 1)[1].split("function ", 1)[0]
        self.assertIn("needs_review = false", body)
        # The placeholder row carries `unresolved: true`, and the replacement
        # entry has no such key - so Object.assign leaves it behind saying the
        # opposite of the truth.
        self.assertIn("delete item.unresolved", body)

    def test_the_inline_find_clears_it_too(self):
        script = self._script()
        body = script.split("async function repairRow", 1)[1].split("async function ", 1)[0]
        self.assertIn("delete item.unresolved", body)

    def test_the_inline_find_button_no_longer_takes_the_first_hit(self):
        script = self._script()
        body = script.split("async function repairRow", 1)[1].split("async function ", 1)[0]
        self.assertIn("openFixerWith(", body,
                      "more than one hit must go to the picker, not be guessed at")
        self.assertIn("REVIEW_RATIO_FLOOR", body)

    def test_the_bulk_controls_are_scoped_and_labelled(self):
        script = self._script()
        self.assertIn("function bulkTargets()", script)
        self.assertNotIn("currentAnimes.forEach(item => { item.status = newStatus; });", script)
        self.assertNotIn("currentAnimes.forEach(item => { item.score = newScore; });", script)
        self.assertIn("inputScore.addEventListener('change'", script,
                      "bound to input it re-stamped every row on every keystroke")
        self.assertNotIn("inputScore.addEventListener('input'", script)
        self.assertIn('class="bulk-scope"', gui.HTML_TEMPLATE)
        # The label has to follow the filter, which changes the table without
        # going through updateStatsAndExport.
        render = script.split("function renderTable(items)", 1)[1].split("function ", 1)[0]
        self.assertIn("updateBulkLabels()", render)

    def test_the_fixer_can_go_back_and_searches_as_you_type(self):
        self.assertIn('id="btnFixBack"', gui.HTML_TEMPLATE, "fixPos only ever incremented")
        script = self._script()
        self.assertIn("fixSearchTimer", script, "the fixer needed the button pressed")

    def test_rows_can_be_ranged_and_driven_from_the_keyboard(self):
        script = self._script()
        self.assertIn("e.shiftKey", script)
        self.assertIn("ArrowDown", script)
        self.assertIn("function visibleIndexes()", script,
                      "a range must span what is on screen, not raw indexes")

    def test_navigation_cannot_walk_past_the_row_cap(self):
        # renderTable caps at ROW_CAP, so the raw filter result would let the
        # arrows focus rows that were never rendered.
        body = self._script().split("function visibleIndexes()", 1)[1].split("function ", 1)[0]
        self.assertIn("ROW_CAP", body)
        self.assertIn("showAllRows", body)

    def test_the_keyboard_does_not_steal_keys_it_has_no_claim_on(self):
        body = self._script().split("if (e.key !== 'ArrowDown'", 1)[1].split("tableBody.add", 1)[0]
        self.assertIn("'button'", body, "Space activates a focused button")
        self.assertIn("isContentEditable", body)
        self.assertIn("inTable", body,
                      "claiming the arrow keys globally broke scrolling the page")

    def test_escape_closes_every_dialog_not_a_hand_kept_list(self):
        script = self._script()
        self.assertIn("querySelectorAll('.modal-backdrop.show').forEach(hideModal)", script,
                      "the last two dialogs added were both missed by the old list")


if __name__ == "__main__":
    unittest.main()
