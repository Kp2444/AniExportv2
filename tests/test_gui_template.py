import io
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_mal_xml
import gui

SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


class GuiTemplateTests(unittest.TestCase):
    """The page used to be a 5,127-line string literal inside gui.py. A Python
    string consumes backslashes, so the JavaScript in it could not contain one,
    and the test that used to live here policed exactly that. The constraint is
    gone with the literal; what needs guarding now is the assembly."""

    def test_the_page_is_assembled_from_the_static_files(self):
        for name in ("index.html", "app.css"):
            with self.subTest(name=name):
                self.assertTrue((gui.STATIC_DIR / name).is_file())
        css = (gui.STATIC_DIR / "app.css").read_text(encoding="utf-8")
        self.assertIn(css, gui.HTML_TEMPLATE)
        self.assertIn(gui._script_source(), gui.HTML_TEMPLATE)

    def test_the_script_is_kept_in_readable_sections(self):
        """Three thousand lines in one file is not something anyone reads."""
        sections = sorted(p.name for p in gui.SCRIPT_DIR.glob("*.js"))
        self.assertGreater(len(sections), 3)
        self.assertEqual(sections, sorted(sections),
                         "the join is by name, so the names have to sort")
        for name in sections:
            with self.subTest(name=name):
                self.assertRegex(name, r"^\d\d-", "zero-padded, or 10 sorts before 2")

    def test_the_sections_join_in_a_fixed_order(self):
        """Adding a section must not be able to reorder the others."""
        body = io.open("gui.py", encoding="utf-8").read()
        block = body.split("def _script_source", 1)[1].split("def _build_page", 1)[0]
        self.assertIn("sorted(", block)
        self.assertIn("key=lambda p: p.name", block)

    def test_every_section_parses_on_its_own(self):
        """The script has no wrapper function, so a cut at a top-level
        statement leaves each piece valid JavaScript. If that stops being true
        the split has been made at the wrong place."""
        if not shutil.which("node"):
            self.skipTest("node not installed")
        for part in sorted(gui.SCRIPT_DIR.glob("*.js")):
            with self.subTest(section=part.name):
                result = subprocess.run(["node", "--check", str(part)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_page_is_one_response_not_three(self):
        """index.html references the two files so it opens standalone, but what
        is served inlines them: one request, one no-store header, and no window
        where the markup has arrived and the script has not."""
        page = gui._page_html()
        self.assertNotIn(gui._CSS_LINK, page)
        self.assertNotIn(gui._JS_TAG, page)
        self.assertEqual(page.count("<style>"), 1)
        index = (gui.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn(gui._CSS_LINK, index, "but the file on disk keeps them")
        self.assertIn(gui._JS_TAG, index)

    def test_an_edit_shows_without_a_restart(self):
        """_page_html re-reads; HTML_TEMPLATE is only the copy built at import."""
        path = gui.STATIC_DIR / "app.css"
        before = path.read_text(encoding="utf-8")
        try:
            path.write_text(before + chr(10) + ".probe-only {}" + chr(10), encoding="utf-8")
            self.assertIn(".probe-only {}", gui._page_html())
        finally:
            path.write_text(before, encoding="utf-8")
        self.assertNotIn(".probe-only {}", gui._page_html())

    def test_a_missing_static_directory_falls_back_rather_than_500s(self):
        with patch.object(gui, "STATIC_DIR", Path(tempfile.gettempdir()) / "no-such-dir"):
            self.assertIn("<style>", gui._page_html())

    def test_the_calibrated_floor_is_still_substituted(self):
        self.assertNotIn("__REVIEW_RATIO_FLOOR__", gui._page_html())
        self.assertIn(repr(float(generate_mal_xml.REVIEW_RATIO_FLOOR)), gui._page_html())

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
        for field in ("genres", "description", "siteUrl"):
            self.assertNotIn(field, query,
                             "a 339-title run would carry this for 10 candidates each and show none of it")

    def test_synonyms_are_the_one_field_that_earns_its_weight(self):
        """The exception to the rule above, and it was measured rather than
        assumed: 8 aliases x perPage 10 grew from 15,572 to 21,831 bytes, and
        the rate-limit cost was 1 either way. Unlike genres or description it
        is *used* - `_alias_hit` reads it, which is what keeps a correct match
        like "First God Of War" -> "Di Yi Zhan Shen" (ratio 0.129) off the
        review list."""
        query = generate_mal_xml._search_batch_query(8, "manga")
        self.assertIn("synonyms", query)

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


class KeepTheWorkTests(unittest.TestCase):
    """Three things the page produced and then threw away."""

    def script(self):
        return gui._script_source()

    def markup(self):
        return (gui.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # ---- R25 ----------------------------------------------------------
    def test_the_write_plan_can_be_kept(self):
        """The diff is the most carefully built thing in the tool and it lived
        only as long as the dialog was open."""
        self.assertIn('id="btnWritePlanSave"', self.markup())
        body = self.script().split("btnWritePlanSave.addEventListener", 1)[1]
        body = body.split("btnWriteCancel", 1)[0]
        self.assertIn("overrides: writeOverrides", body,
                      "without the overrides the plan does not describe what "
                      "would actually have been sent")
        self.assertIn("rows: lastWritePlan.rows", body)
        self.assertIn("saved_at", body)

    # ---- R26 ----------------------------------------------------------
    def test_every_snapshot_is_reachable_not_just_the_newest(self):
        script = self.script()
        self.assertIn('id="undoSnapshot"', self.markup())
        self.assertIn("function refreshSnapshots", script)
        self.assertIn("function renderSnapshotPicker", script)
        self.assertNotIn("(payload.snapshots || [])[0]", script,
                         "the page used to read index 0 and leave four "
                         "recoverable snapshots unreachable")

    def test_a_snapshot_is_labelled_with_its_media_type(self):
        """A copy writes two snapshots, one per media type, so a bare list of
        timestamps shows one operation as two unrelated rows."""
        body = self.script().split("function snapshotLabel", 1)[1].split("async function", 1)[0]
        self.assertIn("media_type", body)
        self.assertIn("entry.rows", body)
        self.assertIn("creates", body)

    def test_picking_an_older_snapshot_undoes_only_that_one(self):
        """The session's own pair belongs together; an older one does not."""
        body = self.script().split("btnUndoConfirm.addEventListener", 1)[1][:1200]
        self.assertIn("picked === lastWriteSnapshot && lastWriteSnapshots.length", body)

    def test_the_delete_box_follows_the_chosen_snapshot(self):
        """Each snapshot created a different number of entries, and offering
        to delete the wrong count would be the worst kind of wrong."""
        self.assertIn("undoSnapshot.addEventListener('change'", self.script())
        body = self.script().split("undoSnapshot.addEventListener('change'", 1)[1][:400]
        self.assertIn("lastWriteCreated", body)
        self.assertIn("describeUndo()", body)

    # ---- R27 ----------------------------------------------------------
    def test_the_rows_can_leave_as_csv(self):
        self.assertIn('id="btnDownloadCsv"', self.markup())
        script = self.script()
        self.assertIn("CSV_COLUMNS", script)
        for column in ("title", "pasted", "mal_id", "anilist_id", "status",
                       "score", "progress", "needs_review", "known_alias"):
            with self.subTest(column=column):
                self.assertIn("'" + column + "'", script)

    def test_a_csv_cell_cannot_become_a_spreadsheet_formula(self):
        """Excel runs a cell beginning =, +, - or @. A title is not a formula."""
        body = self.script().split("function csvCell", 1)[1].split("btnDownloadCsv", 1)[0]
        self.assertIn("^[=+", body)
        self.assertIn('replace(/"/g', body, "and RFC 4180 quoting for the rest")

    def test_the_csv_carries_a_byte_order_mark(self):
        """Without one Excel reads a UTF-8 CSV as the system codepage and every
        non-ASCII title arrives mangled."""
        self.assertIn("String.fromCharCode(65279)", self.script())

    def test_the_csv_is_built_in_the_browser(self):
        """No new endpoint: the server deliberately refuses to be told where to
        write, and this needs nothing it does not already have."""
        self.assertNotIn("/api/download/csv", self.script())
        self.assertIn("function offerDownload", self.script())

if __name__ == "__main__":
    unittest.main()
