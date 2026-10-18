import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(sorted(backdrops), ["fixModal", "previewModal", "writeModal"])
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


if __name__ == "__main__":
    unittest.main()
