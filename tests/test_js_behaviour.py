"""Run the browser-behaviour suites in tests/js.

Three things in the page have no Python to assert against: the write-diff
renderer, the fixer's candidate card and the persistent skip list. They are
pure functions over data, so node can exercise them directly -- and
`tests/js/extract.js` pulls them out of `static/app/*.js` at run time, so these
really do test the shipped source rather than a copy of it.

Skipped, not failed, when node is absent: the Python suite must still run with
nothing installed, which is the project's whole premise.
"""

import pathlib
import shutil
import subprocess
import unittest

JS_DIR = pathlib.Path(__file__).resolve().parent / "js"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BrowserBehaviourTests(unittest.TestCase):

    def test_the_js_suites_pass(self):
        result = subprocess.run(
            ["node", str(JS_DIR / "run.js")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0,
                         "node suites failed:\n" + result.stdout + result.stderr)
        # Guards against a runner that silently runs nothing.
        for suite in ("write diff", "candidate card", "skip list", "account copy"):
            self.assertIn(suite, result.stdout, "a suite did not report")
        self.assertIn("all 4 suites passed", result.stdout)

    def test_a_renamed_function_breaks_the_extract_loudly(self):
        """The point of extracting from the live source is that a rename is a
        failure here, not a suite that quietly tests nothing."""
        probe = (
            "const {extract} = require(" + repr(str(JS_DIR / "extract.js")) + ");"
            "try { extract(['noSuchFunctionAnywhere']); console.log('SILENT'); }"
            "catch (e) { console.log('THREW'); }"
        )
        result = subprocess.run(["node", "-e", probe],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("THREW", result.stdout)


if __name__ == "__main__":
    unittest.main()
