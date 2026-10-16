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


if __name__ == "__main__":
    unittest.main()
