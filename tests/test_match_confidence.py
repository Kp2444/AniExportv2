"""Match confidence: the ratio that used to be computed and thrown away.

The thresholds here are calibrated against a real 339-title list, not guessed.
The plan's proposed floor of 0.6 would have flagged 51.5% of resolved rows,
because translated titles share almost no characters with the English name a
user pastes; the signal that survived calibration is an inversion - the chosen
entry matching the searched title worse than one the ranker passed over.
"""
import io
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

    def test_escape_finds_the_dialogs_rather_than_keeping_a_list(self):
        """Queried, so a newly added dialog is covered without being added
        anywhere - but only the topmost closes, or one Escape could take a
        write plan out with the sheet sitting over it."""
        script = self._script()
        self.assertIn("const stack = document.querySelectorAll('.modal-backdrop.show');", script)
        self.assertIn("if (stack.length) hideModal(stack[stack.length - 1]);", script)
        self.assertNotIn(".forEach(hideModal)", script)


class WonByDefaultTests(unittest.TestCase):
    """A ranking tier demotes an entry relative to its siblings, so a demotion
    needs something to lose to.

    Measured against the live API: "Gotoubun No Hanayome" returns exactly one
    AniList hit - a 1-chapter ONE_SHOT with no MAL id, not the 122-chapter
    series. The one-shot tier has nothing to rank it below, the ratio floor
    passes it at 0.45, and the runner-up gap is None because there is no
    runner-up. It then renders as "AniList only", which reads as a legitimate
    state rather than a miss.
    """

    GOTOUBUN = {"id": 123, "idMal": None, "format": "ONE_SHOT", "status": "FINISHED",
                "popularity": 2944, "chapters": 1, "volumes": 0,
                "title": {"english": None,
                          "romaji": "Go-toubun no Hanayome: 122 + 1 - Itsutsugo-chan"}}

    def test_the_case_that_defeated_every_other_guard(self):
        entry = gen._parse_manga_node(self.GOTOUBUN, fallback_title="Gotoubun No Hanayome")
        self.assertGreater(entry["match_ratio"], gen.REVIEW_RATIO_FLOOR,
                           "the ratio floor cannot see this one")
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["review_reason"], "one-shot")

    def test_a_single_candidate_has_no_runner_up_to_lose_to(self):
        candidates = gen._candidates([self.GOTOUBUN], "manga", "Gotoubun No Hanayome")
        self.assertIsNone(candidates[0]["runner_up_gap"], "no runner-up, so no gap")
        self.assertTrue(candidates[0]["needs_review"])

    def test_an_unreleased_winner_is_flagged(self):
        entry = gen._parse_manga_node(node("Soon", status="NOT_YET_RELEASED"),
                                      fallback_title="Soon")
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["review_reason"], "not yet released")

    def test_a_lone_one_chapter_manga_is_flagged(self):
        """Manga only: an anime with one episode is an ordinary movie."""
        lone = node("Fragment", format="MANGA", chapters=1)
        self.assertTrue(gen._candidates([lone], "manga", "Fragment")[0]["needs_review"])
        self.assertNotIn("needs_review",
                         {k: v for k, v in gen._candidates(
                             [node("Movie", format="MOVIE", episodes=1)], "anime", "Movie")[0].items()
                          if k == "needs_review" and v})

    def test_an_ordinary_match_is_left_alone(self):
        entry = gen._parse_manga_node(node("The Hero Returns", popularity=9000,
                                           status="RELEASING", chapters=120),
                                      fallback_title="The Hero Returns")
        self.assertFalse(entry["needs_review"])
        self.assertNotIn("review_reason", entry)

    def test_a_row_read_back_from_an_account_is_never_flagged(self):
        """It was not a guess, so there is nothing to review."""
        entry = gen._parse_manga_node(self.GOTOUBUN)
        self.assertNotIn("needs_review", entry)
        self.assertNotIn("review_reason", entry)

    def test_the_chosen_entry_inherits_the_candidate_verdict(self):
        """The entry is parsed from its own node, so it knows nothing about
        ties or sole matches unless they are carried across."""
        entry = gen._parse_manga_node(node("Pick"), fallback_title="Pick")
        gen._carry_candidate_review(entry, [{"runner_up_gap": -0.2, "needs_review": True,
                                             "review_reason": "one-shot"}])
        self.assertEqual(entry["runner_up_gap"], -0.2)
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["review_reason"], "one-shot")

    def test_both_resolver_paths_carry_it(self):
        """The batch path did this inline and the single-title path did not,
        so the two disagreed about the same search."""
        with io.open("generate_mal_xml.py", encoding="utf-8") as handle:
            resolvers = re.sub(r"\s+", " ", handle.read())
        self.assertEqual(resolvers.count("_carry_candidate_review("), 3,
                         "three callers: the batch path and both single-title "
                         "fetchers")
        with io.open("matching.py", encoding="utf-8") as handle:
            self.assertIn("def _carry_candidate_review(", handle.read(),
                          "defined once, beside the ranking it carries")


class ReviewReasonCopyTests(unittest.TestCase):
    def test_every_server_reason_has_a_sentence_in_the_page(self):
        script = gui.HTML_TEMPLATE
        for token in ("one-shot", "not yet released", "only match, one chapter"):
            with self.subTest(token=token):
                self.assertIn("'" + token + "':", script,
                              "a token with no sentence renders as no explanation")

    def test_the_reason_wins_over_the_ratio_sentence(self):
        """|| binds tighter than ?:, so without the parentheses a flagged row
        would always take the inversion branch."""
        self.assertIn("REVIEW_REASONS[item.review_reason]" + chr(10), gui.HTML_TEMPLATE)
        self.assertIn("|| ((gap !== undefined", gui.HTML_TEMPLATE)

    def test_picking_a_match_by_hand_clears_the_reason_too(self):
        """A stale reason outlives the flag otherwise - the same shape as the
        `unresolved: true` flag that survived a fix."""
        self.assertEqual(gui.HTML_TEMPLATE.count("delete item.review_reason;"), 2,
                         "both fix paths: the inline find and the fixer modal")

class AliasHitTests(unittest.TestCase):
    """AniList listing the pasted title as another name for an entry is a fact,
    not a similarity score, and it settles what the ratio was guessing at.

    Measured live: "First God Of War" scores **0.129** against the entry it
    correctly resolves to, "Di Yi Zhan Shen", which is below the review floor -
    and AniList carries "First God of War" verbatim in that entry's synonyms.
    On a 16-title slice of the real list, 8 of 12 resolved rows were exact
    alias hits and one of them was being flagged purely on its ratio.
    """

    ALIASED = {"id": 1, "idMal": 2, "format": "MANHUA", "status": "RELEASING",
               "popularity": 500, "chapters": 100, "volumes": 0,
               "title": {"english": "Di Yi Zhan Shen", "romaji": "Di Yi Zhan Shen"},
               "synonyms": ["First God of War"]}

    def test_an_exact_alias_clears_a_low_ratio_flag(self):
        entry = gen._parse_manga_node(self.ALIASED, fallback_title="First God Of War")
        self.assertLess(entry["match_ratio"], gen.REVIEW_RATIO_FLOOR)
        self.assertTrue(entry["alias_hit"])
        self.assertFalse(entry["needs_review"],
                         "the ratio was guessing at something the synonym settles")

    def test_the_same_row_without_the_synonym_is_still_flagged(self):
        entry = gen._parse_manga_node(dict(self.ALIASED, synonyms=[]),
                                      fallback_title="First God Of War")
        self.assertTrue(entry["needs_review"])
        self.assertNotIn("alias_hit", entry)

    def test_an_alias_does_not_rescue_a_won_by_default_match(self):
        """Different failure: the title is right but the entry is the wrong
        kind of thing - a one-shot where the series should be."""
        entry = gen._parse_manga_node(dict(self.ALIASED, format="ONE_SHOT", chapters=1),
                                      fallback_title="First God Of War")
        self.assertTrue(entry["alias_hit"])
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["review_reason"], "one-shot")

    def test_an_alias_beats_an_inversion(self):
        """An inversion compares two fuzzy scores; an alias is not fuzzy.

        Built as the real shape of the problem: the has-a-MAL-id tier puts the
        aliased entry first even though a passed-over candidate matches the
        *typed* name perfectly. That is exactly the inversion the review flag
        exists for - and exactly the case where it is wrong.
        """
        rival = node("First God Of War", id=77, idMal=None, popularity=1)
        ranked = gen._candidates([self.ALIASED, rival], "manga", "First God Of War")
        self.assertEqual(len(ranked), 2, "distinct ids, so neither is deduped away")
        self.assertTrue(ranked[0]["alias_hit"], "the MAL-id tier ranks the alias first")
        self.assertLess(ranked[0]["runner_up_gap"], 0, "which is an inversion")
        self.assertFalse(ranked[0]["needs_review"],
                         "an inversion against an exact alias is a false alarm")

    def test_an_inversion_without_an_alias_is_still_flagged(self):
        rival = node("Totally Different", id=77, idMal=None, popularity=1)
        ranked = gen._candidates([dict(self.ALIASED, synonyms=[]), rival],
                                 "manga", "Totally Different")
        self.assertLess(ranked[0]["runner_up_gap"], 0)
        self.assertTrue(ranked[0]["needs_review"])

    def test_matching_is_case_and_punctuation_insensitive(self):
        for typed in ("first god of war", "FIRST GOD OF WAR", "First God of War!"):
            with self.subTest(typed=typed):
                entry = gen._parse_manga_node(self.ALIASED, fallback_title=typed)
                self.assertTrue(entry.get("alias_hit"), typed)

    def test_a_partial_overlap_is_not_an_alias(self):
        entry = gen._parse_manga_node(self.ALIASED, fallback_title="God of War")
        self.assertNotIn("alias_hit", entry, "exact only - this is the whole point")

    def test_a_row_read_back_from_an_account_gets_no_alias_either(self):
        self.assertNotIn("alias_hit", gen._parse_manga_node(self.ALIASED))

    def test_the_batch_query_carries_synonyms(self):
        for media_type in ("manga", "anime"):
            with self.subTest(media_type=media_type):
                self.assertIn("synonyms", gen._search_batch_query(3, media_type))

    def test_synonyms_stay_out_of_the_similarity_ratio(self):
        """Measured and rejected (REVIEW.md R10-R12): folding synonyms into
        _title_ratio changed 0 of 16 picks, tripled the number of candidates
        sitting above 0.95, and halved the runner-up gap - which is the signal
        that actually works. The boolean is the part worth having."""
        ratio = gen._title_ratio(self.ALIASED, "First God Of War")
        self.assertLess(ratio, 0.2,
                        "the ratio must still describe the titles only; if this "
                        "starts passing at 1.0 the synonyms have been folded in")

    def test_the_chosen_entry_inherits_the_alias(self):
        entry = gen._parse_manga_node(node("Pick"), fallback_title="Pick")
        gen._carry_candidate_review(entry, [{"alias_hit": True, "runner_up_gap": None}])
        self.assertTrue(entry["alias_hit"])

    def test_the_page_says_so_rather_than_staying_silent(self):
        script = gui._script_source()
        self.assertIn("aliasMarkHtml", script)
        self.assertIn("known alias", script)
        self.assertIn("alias-mark", (gui.STATIC_DIR / "app.css").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
