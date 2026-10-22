"""The pasted line, before anything else sees it.

`sanitize_title_line` is the first thing every title meets, and its output is
what gets searched, ranked, reviewed and written. A title corrupted here cannot
be recovered by any later stage - it does not fail, it resolves confidently to
the wrong work.

The bug these cases exist for: the numbering rule stripped `\\d+-` with no
requirement for whitespace, so a Japanese counter prefix looked like a list
marker. "3-gatsu no Lion" became "gatsu no Lion", "5-toubun no Hanayome"
became "toubun no Hanayome", and "07-Ghost" became "Ghost".
"""
import unittest

import generate_mal_xml as gen


class CounterPrefixTests(unittest.TestCase):
    """N-counter titles are a real and sizeable class, not an edge case."""

    KEPT = [
        "3-gatsu no Lion",
        "5-toubun no Hanayome",
        "07-Ghost",
        "4-nin wa Sorezore Uso wo Tsuku",
        "3-nen Z-gumi Ginpachi-sensei",
        "6-gatsu wa Ai no Kisetsu",
    ]

    def test_a_counter_prefix_survives(self):
        for title in self.KEPT:
            with self.subTest(title=title):
                self.assertEqual(gen.sanitize_title_line(title), title)

    def test_titles_that_merely_start_with_digits_survive(self):
        # These were never broken; they are here so a future rewrite of the
        # expression cannot quietly take them with it.
        for title in ("86 - Eighty Six", "11eyes", "18if", "20th Century Boys",
                      "7 Seeds", "91 Days", "3x3 Eyes"):
            with self.subTest(title=title):
                self.assertEqual(gen.sanitize_title_line(title), title)


class NumberingTests(unittest.TestCase):
    """The rule still has to do the job it was written for."""

    def test_the_usual_list_markers_are_stripped(self):
        cases = {
            "1. Berserk": "Berserk",
            "01. Berserk": "Berserk",
            "1) Berserk": "Berserk",
            "1- Berserk": "Berserk",
            "1.Berserk": "Berserk",
            "- Berserk": "Berserk",
            "* Berserk": "Berserk",
            "• Berserk": "Berserk",
            "  2)   One Piece  ": "One Piece",
            '"Berserk"': "Berserk",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(gen.sanitize_title_line(line), expected)

    def test_numbering_is_stripped_once_not_repeatedly(self):
        """A numbered line whose title is itself a counter title keeps the
        title. Stripping twice would turn this into "gatsu no Lion"."""
        self.assertEqual(gen.sanitize_title_line("1. 3-gatsu no Lion"),
                         "3-gatsu no Lion")
        self.assertEqual(gen.sanitize_title_line("1. 86"), "86")

    def test_comments_and_blanks_are_dropped(self):
        for line in ("", "   ", "# a comment", "\t"):
            with self.subTest(line=repr(line)):
                self.assertIsNone(gen.sanitize_title_line(line))


class SearchVariantTests(unittest.TestCase):
    """Deterministic rewrites tried when a title resolves to nothing."""

    def test_a_typographic_apostrophe_produces_a_straight_one(self):
        """The replacement target used to be built by round-tripping the curly
        quote through unicode_escape, which produced mojibake that is never
        present in a real title, so this rule had never once fired."""
        variants = gen._search_variants("Kaguya-sama’s War", "anime")
        self.assertIn("Kaguya-sama's War", variants)

    def test_the_other_three_quote_forms_fold_too(self):
        self.assertIn("Don't", gen._search_variants("Don‘t", "anime"))
        self.assertIn('"Hello"',
                      gen._search_variants("“Hello”", "anime"))

    def test_an_ordinary_title_produces_no_variants(self):
        self.assertEqual(gen._search_variants("Berserk", "manga"), [])


class LineParsingTests(unittest.TestCase):
    """A human list carries more than a title. All of it used to be thrown
    away, and the danger in picking it up is the same one R1 was about: a
    number read wrongly corrupts the search beyond recovery."""

    def parse(self, line, media_type="anime"):
        return gen.parse_title_line(line, media_type)

    def test_the_shape_this_feature_exists_for(self):
        self.assertEqual(
            self.parse("Frieren 12/28 9/10 watching"),
            {"title": "Frieren", "progress": 12, "score": 9, "status": "Watching"})

    def test_separators_units_and_order_do_not_matter(self):
        cases = {
            ("Berserk - 374 ch - dropped", "manga"):
                {"title": "Berserk", "progress": 374, "status": "Dropped"},
            ("Bleach 366 eps completed 10/10", "anime"):
                {"title": "Bleach", "progress": 366, "score": 10, "status": "Completed"},
            ("One Piece 1100 ch", "manga"): {"title": "One Piece", "progress": 1100},
            ("One Piece ch 1100", "manga"): {"title": "One Piece", "progress": 1100},
            ("Berserk, 3 vols, on hold", "manga"):
                {"title": "Berserk", "volumes": 3, "status": "Paused"},
            ("Solo Leveling [ptw]", "anime"):
                {"title": "Solo Leveling", "status": "Plan to Watch"},
            ("1. Vinland Saga (completed) 8", "manga"):
                {"title": "Vinland Saga", "score": 8, "status": "Completed"},
        }
        for (line, media_type), expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.parse(line, media_type), expected)

    def test_a_bare_number_is_never_believed_on_its_own(self):
        """The whole reason this parser is conservative. Each of these is a
        title, and reading the number would send the search somewhere else."""
        for line in ("86 - Eighty Six", "91 Days", "7 Seeds", "20th Century Boys",
                     "Attack on Titan 139", "3-gatsu no Lion", "07-Ghost",
                     "5-toubun no Hanayome", "11eyes", "18if"):
            with self.subTest(line=line):
                self.assertEqual(self.parse(line, "manga"), {"title": line})

    def test_a_number_is_believed_once_the_line_has_proved_its_shape(self):
        """"(completed) 8" is a score; "139" on its own is part of a title."""
        self.assertEqual(self.parse("Vinland Saga (completed) 8", "manga"),
                         {"title": "Vinland Saga", "score": 8, "status": "Completed"})
        self.assertNotIn("score", self.parse("Attack on Titan 139"))

    def test_a_half_read_line_is_abandoned_whole(self):
        """Searching for "Berserk 374" is worse than searching the line as
        typed, so a leftover bare number throws the whole parse away."""
        self.assertEqual(self.parse("Berserk - dropped 374", "manga"),
                         {"title": "Berserk - dropped 374"})

    def test_a_line_that_is_only_a_status_word_stays_a_title(self):
        for line in ("Dropped", "Plan to Watch", "Completed"):
            with self.subTest(line=line):
                self.assertEqual(self.parse(line, "manga"), {"title": line})

    def test_volumes_are_manga_only(self):
        """On an anime list the word is noise, and reading it as episodes
        would invent progress out of nothing."""
        self.assertEqual(self.parse("Berserk 3 vols", "anime"),
                         {"title": "Berserk 3 vols"})
        self.assertEqual(self.parse("Berserk 3 vols", "manga"),
                         {"title": "Berserk", "volumes": 3})

    def test_the_status_is_spelled_the_way_the_list_spells_it(self):
        self.assertEqual(self.parse("x watching", "manga")["status"], "Reading")
        self.assertEqual(self.parse("x reading", "manga")["status"], "Reading")
        self.assertEqual(self.parse("x rereading", "anime")["status"], "Rewatching")
        self.assertEqual(self.parse("x ptw", "manga")["status"], "Plan to Read")
        self.assertEqual(self.parse("x on hold", "anime")["status"], "Paused")

    def test_a_denominator_of_ten_is_a_score_and_anything_else_is_progress(self):
        self.assertEqual(self.parse("Berserk 9/10", "manga"),
                         {"title": "Berserk", "score": 9})
        self.assertEqual(self.parse("Berserk 12/28", "manga"),
                         {"title": "Berserk", "progress": 12})

    def test_progress_beyond_the_total_is_not_read(self):
        self.assertEqual(self.parse("Berserk 40/28", "manga"), {"title": "Berserk 40/28"})

    def test_words_that_end_real_titles_need_a_separator(self):
        """"complete", "hold", "plan" and friends appear in titles; "watching"
        and "dropped" effectively never do."""
        for line in ("Monster (Complete Edition)", "Berserk hold", "Pure Complete"):
            with self.subTest(line=line):
                self.assertEqual(self.parse(line, "manga"), {"title": line})
        self.assertEqual(self.parse("Berserk - complete", "manga")["status"], "Completed")

    def test_the_known_cost_of_that_rule(self):
        """"reading" is *not* in the weak set: "Berserk reading" is far more
        common in a manga list than a title ending in the word. The trade is
        recorded here rather than left to be discovered - the row shows the
        line it came from, so this misparse is visible."""
        self.assertEqual(self.parse("Speed Reading", "manga"),
                         {"title": "Speed", "status": "Reading"})

    def test_blank_and_comment_lines_behave_like_the_sanitiser(self):
        for line in ("", "   ", "# a comment"):
            with self.subTest(line=repr(line)):
                self.assertIsNone(self.parse(line))

    def test_numbering_is_still_stripped_first(self):
        self.assertEqual(self.parse("1. Berserk 9/10", "manga"),
                         {"title": "Berserk", "score": 9})

if __name__ == "__main__":
    unittest.main()
