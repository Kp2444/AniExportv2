import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import generate_mal_xml
import gui

SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
BACKSLASH = chr(92)
MARKER = 'HTML_TEMPLATE = ' + '"""'


class GuiTemplateTests(unittest.TestCase):
    """HTML_TEMPLATE is a plain (non-raw) Python string, so a backslash escape
    written for JavaScript is consumed by Python and can emit a literal newline
    inside a JS string literal. That is a syntax error which kills the whole
    <script>, leaving the page rendered but completely inert."""

    def _template_source(self) -> str:
        source = Path(gui.__file__).read_text(encoding="utf-8")
        start = source.index(MARKER) + len(MARKER)
        return source[start:source.index('"""', start)]

    def test_template_source_has_no_python_consumed_escapes(self):
        offenders = [
            line.strip()
            for line in self._template_source().splitlines()
            if BACKSLASH in line
        ]
        self.assertEqual(
            offenders, [],
            "backslash escapes in HTML_TEMPLATE are consumed by Python before the "
            "browser sees them; use String.fromCharCode(10) for newlines instead",
        )

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_emitted_javascript_parses(self):
        script = SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "page.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class AccessibilityTests(unittest.TestCase):
    """Static guards for the parts of the markup a screen reader or a keyboard
    depends on. The jsdom suites exercise the behaviour; these keep the
    attributes from being dropped by a later edit to the template."""

    def _markup(self) -> str:
        return gui.HTML_TEMPLATE.split("</style>", 1)[1].split("<script>", 1)[0]

    def test_every_column_header_is_scoped(self):
        headers = re.findall(r"<th\b([^>]*)>", self._markup())
        self.assertGreater(len(headers), 4)
        unscoped = [h.strip() for h in headers if 'scope="col"' not in h]
        self.assertEqual(unscoped, [], "a <th> without scope is ambiguous to a screen reader")

    def test_sortable_headers_are_reachable_and_announce_themselves(self):
        markup = self._markup()
        headers = re.findall(r'<th\b[^>]*class="sortable"[^>]*>', markup, re.S)
        self.assertGreater(len(headers), 1, "the sortable headers moved or were removed")
        for header in headers:
            self.assertIn('tabindex="0"', header, "a sortable header must be focusable")
            self.assertIn('aria-sort="none"', header, "a sortable header must start unsorted")

    def test_sorting_is_bound_to_the_keyboard_as_well_as_the_mouse(self):
        script = SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]
        # the binding block, not resetSession's loop over the same selector
        binding = re.search(
            r"querySelectorAll\('th\.sortable'\)\.forEach\(th => \{\s*th\.addEventListener.*?\}\);",
            script, re.S)
        self.assertIsNotNone(binding, "the sortable-header binding moved or was removed")
        self.assertIn("'click'", binding.group(0))
        self.assertIn("'keydown'", binding.group(0),
                      "sorting must be reachable without a mouse")
        self.assertIn("aria-sort", script)

    def test_interactive_fields_have_accessible_names(self):
        markup = self._markup()
        for element in re.findall(r"<(?:input|select|textarea)\b[^>]*>", markup, re.S):
            ident = re.search(r'id="([^"]+)"', element)
            if not ident or 'type="hidden"' in element or "display:none" in element:
                continue
            labelled = ("aria-label" in element
                        or 'for="' + ident.group(1) + '"' in markup)
            self.assertTrue(
                labelled,
                "#" + ident.group(1) + " has no accessible name (a placeholder is not one)")

    def test_dialogs_are_announced_as_dialogs(self):
        markup = self._markup()
        backdrops = re.findall(r'<div\s+id="(\w+Modal)"[^>]*>', markup)
        self.assertEqual(sorted(backdrops),
                         ["copyModal", "fixModal", "listImportModal", "previewModal",
                          "undoModal", "writeModal"])
        for ident in backdrops:
            tag = re.search(r'<div\s+id="' + ident + r'"[^>]*>', markup, re.S).group(0)
            self.assertIn('role="dialog"', tag, ident + " is not announced as a dialog")
            self.assertIn('aria-modal="true"', tag, ident + " does not trap assistive focus")
            label = re.search(r'aria-labelledby="([^"]+)"', tag)
            self.assertIsNotNone(label, ident + " has no accessible title")
            self.assertIn('id="' + label.group(1) + '"', markup,
                          ident + " points aria-labelledby at an element that does not exist")

    def test_modals_open_and_close_through_the_focus_helpers(self):
        """A raw classList toggle skips the focus move, which is the whole point
        of the helpers: a dialog opened without it leaves the keyboard outside."""
        script = SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]
        for ident in ("previewModal", "writeModal", "fixModal"):
            self.assertNotIn(
                ident + ".classList.", script,
                ident + " is toggled directly; use showModal()/hideModal() instead")
        self.assertIn("function showModal(", script)
        self.assertIn("function hideModal(", script)

    def test_motion_and_browser_chrome_follow_the_theme(self):
        css = gui.HTML_TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn("prefers-reduced-motion: reduce", css,
                      "14 transitions and no way to turn them off")
        self.assertRegex(css, r":root \{\s*color-scheme: dark")
        self.assertRegex(css, r"body\.light \{\s*color-scheme: light")

class InterfaceCopyTests(unittest.TestCase):
    """The page described features that had since shipped, and named the wrong
    destination on its own primary button. Both are invisible to a test that
    only checks behaviour, so they are asserted here."""

    def _script(self) -> str:
        return SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]

    def test_the_anilist_write_is_not_described_as_unbuilt(self):
        self.assertNotIn("is not built yet", gui.HTML_TEMPLATE,
                         "the direct AniList write shipped; the notice still disclaims it")

    def test_the_primary_action_is_named_per_destination(self):
        script = self._script()
        self.assertIn("'Resolve titles'", script,
                      "an AniList run must not promise an XML file")
        self.assertIn("btnProcessLabel", gui.HTML_TEMPLATE)
        self.assertIn("emptyStateAction", gui.HTML_TEMPLATE)

    def test_one_destination_control_is_offered_at_a_time(self):
        script = self._script()
        self.assertIn("destinationField.style.display", script,
                      "the picker card and the select both asked the same question")

    def test_the_status_chip_names_one_media_type(self):
        script = self._script()
        self.assertNotIn("Reading / Watching", gui.HTML_TEMPLATE)
        self.assertIn("function chipLabel(", script)

    def test_the_narrow_layout_has_a_breakpoint(self):
        css = gui.HTML_TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn("max-width: 760px", css,
                      "below ~760px the title column collapsed and the buttons overflowed")
        self.assertIn("min-width: 780px", css,
                      "the table must scroll rather than crush the title column")


class SkipListTests(unittest.TestCase):
    """Unchecking a row is per-session; the skip list is the persistent version of
    it. The two must not be confused, so the wiring is asserted here: the
    behaviour itself is covered by the node checks described in MANGA_SUPPORT_NOTES."""

    def _script(self) -> str:
        return SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]

    def test_both_controls_exist(self):
        self.assertIn('id="btnRemember"', gui.HTML_TEMPLATE)
        self.assertIn('id="btnForgetIgnored"', gui.HTML_TEMPLATE,
                      "a persistent skip list the user cannot clear is a trap")

    def test_new_rows_consult_the_skip_list(self):
        script = self._script()
        self.assertEqual(script.count("enabled: enabledFor("), 4,
                         "every path that builds a fresh row must honour the skip list")
        self.assertNotIn("enabled: isActionable(e)", script,
                         "a row rebuilt after a run ignored the skip list")

    def test_a_manually_picked_match_overrides_the_skip_list(self):
        script = self._script()
        self.assertEqual(script.count("{ enabled: isActionable("), 3,
                         "choosing a match by hand is an explicit act and must re-enable the row")

    def test_the_skip_list_is_per_media_type(self):
        script = self._script()
        self.assertIn("all[mediaType()] = Array.from(ignoredKeys)", script,
                      "a MAL id means a different work in each media type")
        self.assertIn("ignoredKeys = loadIgnored()", script,
                      "switching media type must load that type's own list")

    def test_a_blocked_write_does_not_throw(self):
        script = self._script()
        body = script.split("function saveIgnored()", 1)[1].split("function enabledFor", 1)[0]
        self.assertIn("catch (e)", body,
                      "localStorage throws in private windows; the page must survive it")


class WriteDiffTests(unittest.TestCase):
    """The confirm step is the only thing standing between a pasted list and a
    live account, so the wiring is asserted here. The rendering behaviour is
    covered by the node checks described in MANGA_SUPPORT_NOTES."""

    def _script(self) -> str:
        return SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]

    def test_the_confirm_step_renders_the_diff_rather_than_counts(self):
        script = self._script()
        self.assertIn("renderWritePlan(plan)", script,
                      "the confirm modal must show what changes, not just how many rows")
        self.assertIn("function diffLineHtml(row, d)", script)
        self.assertIn('class="diff-list"', script)

    def test_every_value_shown_in_the_diff_is_escaped(self):
        # Notes and titles come back from AniList, so they are untrusted text.
        script = self._script()
        body = script.split("function diffLineHtml", 1)[1].split("function renderWritePlan", 1)[0]
        self.assertNotIn("+ d.from", body)
        self.assertNotIn("+ row.title", body)
        self.assertEqual(body.count("escapeHtml("), 7,
                         "every interpolated value in a diff line must be escaped")

    def test_the_override_is_a_checkbox_the_server_validates(self):
        script = self._script()
        self.assertIn("""data-write-override="' + action""", script)
        self.assertIn("pickBoxHtml(row.anilist_id, d.field, 'mine'", script)
        self.assertIn("pickBoxHtml(row.anilist_id, 'completedAt', 'clear'", script,
                      "a stale completion date needs its own explicit action")
        self.assertIn("overrides: writeOverrides", script,
                      "the ticked boxes have to reach the write")

    def test_the_score_scale_is_stated_and_not_guessed(self):
        script = self._script()
        self.assertIn("Number(value) + '/100'", script,
                      "the server compares in POINT_100, so 80 must not look like a 1-10 score")

    def test_undo_and_retry_are_reachable_from_the_page(self):
        self.assertIn('id="btnUndo"', gui.HTML_TEMPLATE)
        self.assertIn('id="btnRetry"', gui.HTML_TEMPLATE)
        self.assertIn('id="undoDeleteCreated"', gui.HTML_TEMPLATE,
                      "deleting created entries must be a separate, explicit tick")
        script = self._script()
        self.assertIn("'/api/anilist-undo'", script)
        self.assertIn("retry: true", script)

    def test_deleting_created_entries_is_never_the_default(self):
        self.assertNotIn('id="undoDeleteCreated" checked', gui.HTML_TEMPLATE)
        script = self._script()
        self.assertIn("delete_created: !!(deleteBox && deleteBox.checked)", script,
                      "an absent checkbox must mean false, not a crash or a delete")

    def test_both_streams_share_one_reader(self):
        script = self._script()
        self.assertEqual(script.count("async function streamSse("), 1)
        self.assertEqual(script.count("await streamSse(res,"), 3,
                         "the write, the undo and the copy must not drift apart")

    def test_the_diff_list_is_usable_on_a_narrow_screen(self):
        # A scrolling list inside a dialog is the shape that broke last time.
        narrow = gui.HTML_TEMPLATE.split("@media (max-width: 760px)", 1)[1]
        for rule in (".write-card", ".diff-list", ".diff-field"):
            self.assertIn(rule, narrow, rule)


class CascadeOrderTests(unittest.TestCase):
    """Every rule in the narrow block is a plain class, the same specificity as
    the component rule it overrides, so only source order decides. The block
    used to sit above the components and 13 of its declarations were silently
    dead - including the phone sizing for the match picker and the write diff.
    Chrome found that; a test that only checked the rules were present did not,
    so this one checks they can actually win."""

    BREAKPOINT = "@media (max-width: 760px)"

    def _sheet(self) -> str:
        head = gui.HTML_TEMPLATE.index("<style>")
        return gui.HTML_TEMPLATE[head:gui.HTML_TEMPLATE.index("</style>", head)]

    def _block(self, sheet: str):
        start = sheet.index(self.BREAKPOINT)
        depth, opened = 0, sheet.index("{", start)
        for i in range(opened, len(sheet)):
            if sheet[i] == "{":
                depth += 1
            elif sheet[i] == "}":
                depth -= 1
                if depth == 0:
                    return start, i, sheet[opened + 1:i]
        raise AssertionError("unterminated media block")

    def test_the_narrow_block_comes_after_what_it_overrides(self):
        sheet = self._sheet()
        begin, finish, body = self._block(sheet)
        outside = sheet[:begin] + sheet[finish:]
        late = []
        for selector in re.findall(r"([^{}]+)\{", body):
            selector = " ".join(selector.split())
            if "*/" in selector:
                selector = selector.split("*/", 1)[-1].strip()
            if not selector.startswith("."):
                continue
            # The component rule must be declared before the override, or the
            # two tie on specificity and source order hands it to the component.
            where = outside.rfind(selector + " {")
            if where != -1 and where >= begin:
                late.append(selector)
        self.assertEqual(late, [], "these components are declared after the narrow "
                                   "block, so their phone overrides never apply")

    def test_the_narrow_block_is_the_last_breakpoint(self):
        sheet = self._sheet()
        start, _, _ = self._block(sheet)
        for other in re.finditer(r"@media \(max-width: (\d+)px\)", sheet):
            if int(other.group(1)) > 760:
                self.assertLess(other.start(), start,
                                "a wider breakpoint must come first, or it wins at phone width")


class MatchPickerTests(unittest.TestCase):
    """The fixer is the one dialog where a person has to read carefully and
    decide. It was the narrowest card on the page, at the smallest type, showing
    four grey items. The rendering itself is covered by node checks; these pin
    the parts a refactor would quietly undo."""

    def _script(self) -> str:
        return SCRIPT_RE.findall(gui.HTML_TEMPLATE)[-1]

    def test_the_card_is_not_capped_at_the_old_width(self):
        self.assertNotIn('style="max-width:720px;"', gui.HTML_TEMPLATE,
                         "the match picker was the narrowest card on the page")
        self.assertIn("class=\"modal-card fix-card\"", gui.HTML_TEMPLATE)

    def test_the_result_shows_more_than_an_id_and_a_count(self):
        script = self._script()
        for bit in ("details || {}", "fix-genre", "Also known as", "otherTitles(", "runYears("):
            self.assertIn(bit, script, f"the match picker lost {bit}")

    def test_a_missing_mal_id_is_a_flag_not_a_grey_note(self):
        script = self._script()
        self.assertIn("No MAL ID - AniList only", script,
                      "whether a row can travel as MAL XML is the one thing that decides a pick")

    def test_the_dialog_survives_a_phone_width(self):
        css = gui.HTML_TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
        narrow = css.split("@media (max-width: 760px)", 1)[1]
        self.assertIn(".modal-backdrop { padding: 0.75rem; }", narrow,
                      "2rem of backdrop padding each side is a quarter of a phone screen")
        self.assertIn(".fix-result .cover-thumb", narrow)


class MatchPickerQueryTests(unittest.TestCase):
    """The picker's extra fields must reach it, and must not reach the batch."""

    def test_the_detail_fields_are_on_every_single_lookup_query(self):
        for name in ("ANILIST_BY_ID_QUERY", "ANILIST_SEARCH_PAGE_QUERY",
                     "ANILIST_MANGA_SEARCH_PAGE_QUERY", "ANILIST_MANGA_BY_ID_QUERY",
                     "ANILIST_BY_MAL_ID_QUERY"):
            query = getattr(generate_mal_xml, name)
            for field in ("synonyms", "genres", "averageScore", "siteUrl",
                          "description(asHtml: false)", "coverImage { medium }"):
                self.assertIn(field, query, f"{name} is missing {field}")

    def test_the_batch_query_stays_lean(self):
        query = generate_mal_xml._search_batch_query(8, "manga")
        for field in ("synonyms", "genres", "description", "siteUrl"):
            self.assertNotIn(field, query,
                             "a 339-title run would carry this for 10 candidates each and show none of it")

    def test_a_batch_node_carries_no_details_key(self):
        node = {"id": 1, "idMal": 2, "title": {"english": "X"}, "format": "MANGA",
                "chapters": 3, "volumes": 1, "status": "FINISHED",
                "startDate": {"year": 2020}, "popularity": 5, "coverImage": {"medium": "u"}}
        self.assertNotIn("details", generate_mal_xml._parse_manga_node(node))
        self.assertNotIn("details", generate_mal_xml._parse_media_node(node))

    def test_details_are_nested_so_they_cannot_overwrite_a_row(self):
        node = {"id": 1, "idMal": 2, "title": {"english": "X", "native": "Y"},
                "format": "MANGA", "chapters": 3, "volumes": 1, "status": "RELEASING",
                "startDate": {"year": 2020}, "endDate": {"year": None}, "popularity": 5,
                "coverImage": {"medium": "u"}, "siteUrl": "https://anilist.co/manga/1",
                "synonyms": ["Z"], "genres": ["Action"], "averageScore": 80,
                "description": "<i>Hi</i>  there"}
        entry = generate_mal_xml._parse_manga_node(node)
        self.assertIn("details", entry)
        # A flat "status" would land on the row and replace the user's list status.
        self.assertNotIn("status", {k: v for k, v in entry.items() if k != "details"})
        self.assertEqual(entry["details"]["status"], "Releasing")
        self.assertEqual(entry["details"]["description"], "Hi there",
                         "AniList ships HTML in the description")


if __name__ == "__main__":
    unittest.main()
