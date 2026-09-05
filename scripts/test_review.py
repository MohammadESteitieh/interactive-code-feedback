import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("review.py")
SPEC = importlib.util.spec_from_file_location("code_review_ide", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def test_parse_unified_diff_tracks_old_and_new_lines(self):
        text = """diff --git a/demo.py b/demo.py
index 1111111..2222222 100644
--- a/demo.py
+++ b/demo.py
@@ -1,3 +1,4 @@
 first
-old
+new
+extra
 last
"""
        files = review.parse_unified_diff(text)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "demo.py")
        code = [line for line in files[0]["lines"] if line["kind"] != "hunk"]
        self.assertEqual(
            [(line["kind"], line["old_line"], line["new_line"], line["text"]) for line in code],
            [
                ("context", 1, 1, "first"),
                ("deleted", 2, None, "old"),
                ("added", None, 2, "new"),
                ("added", None, 3, "extra"),
                ("context", 3, 4, "last"),
            ],
        )

    def test_collect_files_returns_complete_numbered_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo.py").write_text("one\ntwo\n", encoding="utf-8")
            files, scope = review.collect_files(root, ["demo.py"])
            self.assertEqual(scope, "selected files")
            self.assertEqual(files[0]["path"], "demo.py")
            self.assertEqual([line["new_line"] for line in files[0]["lines"]], [1, 2])

    def test_collect_files_renders_jupyter_cells_instead_of_raw_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notebook = {
                "cells": [
                    {
                        "cell_type": "markdown", "metadata": {},
                        "source": ["# Heading\n", "**bold** <script>bad()</script>"],
                    },
                    {
                        "cell_type": "code", "metadata": {}, "execution_count": 3,
                        "source": ["value = 42\n", "value"],
                        "outputs": [{
                            "output_type": "display_data", "metadata": {},
                            "data": {"text/html": "<table><tr><td>42</td></tr></table><script>bad()</script>"},
                        }],
                    },
                ],
                "metadata": {
                    "kernelspec": {"display_name": "Python 3", "name": "python3"},
                    "language_info": {"name": "python", "file_extension": ".py"},
                },
                "nbformat": 4, "nbformat_minor": 5,
            }
            (root / "demo.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
            files, scope = review.collect_files(root, ["demo.ipynb"])
            self.assertEqual(scope, "selected files")
            file = files[0]
            self.assertTrue(file["notebook"])
            self.assertEqual(file["kernel"], "Python 3")
            self.assertEqual([cell["type"] for cell in file["cells"]], ["markdown", "code"])
            self.assertIn("<h1>Heading</h1>", file["cells"][0]["rendered_html"])
            self.assertNotIn("<script>", file["cells"][0]["rendered_html"])
            self.assertEqual(file["cells"][1]["lines"][0]["text"], "value = 42")
            self.assertEqual(file["cells"][1]["lines"][0]["cell_index"], 1)
            self.assertEqual(file["cells"][1]["lines"][0]["cell_line"], 1)
            rendered_output = file["cells"][1]["outputs"][0]["html"]
            self.assertIn("<table>", rendered_output)
            self.assertNotIn("bad()", rendered_output)
            self.assertFalse(any(line["text"].startswith("{\"cells\"") for line in file["lines"]))

    def test_notebook_outputs_normalize_images_streams_and_errors(self):
        image = review.normalize_notebook_output({
            "output_type": "display_data", "data": {"image/png": ["YW", "Jj"]},
        })
        self.assertEqual(image, {
            "kind": "image", "label": "image/png", "mime": "image/png", "data": "YWJj",
        })
        stream = review.normalize_notebook_output({
            "output_type": "stream", "name": "stdout", "text": ["one", "\ntwo"],
        })
        self.assertEqual(stream["text"], "one\ntwo")
        error = review.normalize_notebook_output({
            "output_type": "error", "traceback": ["\u001b[31mValueError\u001b[0m"],
        })
        self.assertEqual(error["text"], "ValueError")

    def test_collect_diff_replaces_notebook_json_hunks_with_rendered_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            path = root / "demo.ipynb"
            notebook = {
                "cells": [{"cell_type": "code", "metadata": {}, "execution_count": None,
                           "source": ["before = 1"], "outputs": []}],
                "metadata": {"language_info": {"name": "python"}},
                "nbformat": 4, "nbformat_minor": 5,
            }
            path.write_text(json.dumps(notebook, indent=1), encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "demo.ipynb"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
            notebook["cells"][0]["source"] = ["after = 2"]
            path.write_text(json.dumps(notebook, indent=1), encoding="utf-8")
            files, _scope = review.collect_diff(root, False, None)
            self.assertTrue(files[0]["notebook"])
            self.assertEqual(files[0]["cells"][0]["lines"][0]["text"], "after = 2")
            self.assertIn("raw JSON diff hidden", files[0]["diff_note"])

    def test_collect_diff_includes_staged_and_unstaged_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            path = root / "demo.py"
            path.write_text("one\ntwo\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "demo.py"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
            path.write_text("one\nchanged\n", encoding="utf-8")
            files, _scope = review.collect_diff(root, False, None)
            kinds = [line["kind"] for line in files[0]["lines"]]
            self.assertIn("deleted", kinds)
            self.assertIn("added", kinds)

    def test_bundled_highlighter_marks_python_tokens_and_columns(self):
        self.assertTrue(review.PYGMENTS_AVAILABLE)
        lines = review.highlighted_lines("def greet(name):\n    return name\n", "demo.py")
        first = {token["text"]: token for token in lines[0] if token["text"].strip()}
        self.assertEqual(first["def"]["class"], "keyword")
        self.assertEqual(first["greet"]["class"], "function")
        self.assertEqual(first["greet"]["column"], 4)
        self.assertTrue(first["greet"]["hoverable"])

    def test_lsp_configuration_covers_requested_languages(self):
        expected_servers = {
            "python": "pyright-langserver",
            "typescript": "typescript-language-server",
            "rust": "rust-analyzer",
            "go": "gopls",
            "cpp": "clangd",
        }
        self.assertEqual(
            {name: command[0] for name, command in review.SERVER_COMMANDS.items()},
            expected_servers,
        )
        self.assertEqual(review.language_for_path(Path("demo.py")), ("python", "python"))
        self.assertEqual(review.language_for_path(Path("demo.js")), ("typescript", "javascript"))
        self.assertEqual(review.language_for_path(Path("demo.tsx")), ("typescript", "typescriptreact"))
        self.assertEqual(review.language_for_path(Path("demo.rs")), ("rust", "rust"))
        self.assertEqual(review.language_for_path(Path("demo.go")), ("go", "go"))
        self.assertEqual(review.language_for_path(Path("demo.c")), ("cpp", "c"))
        self.assertEqual(review.language_for_path(Path("demo.cpp")), ("cpp", "cpp"))

    def test_hover_content_normalizes_lsp_shapes(self):
        contents = [
            {"language": "python", "value": "def greet() -> str"},
            {"kind": "markdown", "value": "Returns a greeting."},
        ]
        self.assertEqual(
            review.hover_content_text(contents),
            "def greet() -> str\n\nReturns a greeting.",
        )

    def test_timeout_returns_machine_readable_result(self):
        payload = {"title": "Test", "mode": "files", "root": "/tmp",
                   "scope": "test", "files": []}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = review.serve(payload, 0, False, 0.05)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["comments"], [])
        self.assertIn("http://127.0.0.1:", stderr.getvalue())

    def test_page_has_no_external_dependencies(self):
        payload = {"title": "Test", "mode": "files", "root": "/tmp",
                   "scope": "test", "files": []}
        page = review.page_html(payload, "token").decode("utf-8")
        self.assertNotIn("https://", page)
        self.assertNotIn("<script src=", page)
        self.assertIn("img-src 'self' data:", page)
        self.assertIn("Send review", page)
        self.assertIn("grid-template-columns:56px 30px", page)
        self.assertIn("min-height:0; overflow:auto", page)
        self.assertIn("close.textContent='Close'", page)
        self.assertIn("requestHover", page)
        self.assertIn("tok-keyword", page)
        self.assertIn("font-minus", page)
        self.assertIn("hscroll-content", page)
        self.assertIn("width:max-content; min-width:100%", page)
        self.assertIn("code.scrollLeft=hscroll.scrollLeft", page)
        self.assertIn("toggle.textContent='✎'", page)
        self.assertIn("function renderNotebook(file)", page)
        self.assertIn("Notebooks: rendered", page)
        self.assertIn(".notebook-cell", page)
        self.assertIn("cell_index:line.cell_index??null", page)
        self.assertNotIn("row.append(old,neu", page)

    def test_reviewed_source_cannot_replace_page_template_markers(self):
        line = {"id": 1, "kind": "file", "old_line": 1, "new_line": 1,
                "text": "__TOKEN__ __DATA__ __SCRIPT__", "tokens": []}
        payload = {"title": "__TITLE__", "mode": "files", "root": "/tmp",
                   "scope": "test", "files": [{"path": "demo.py", "lines": [line]}]}
        page = review.page_html(payload, "secret-token").decode("utf-8")
        match = re.search(
            r'<script id="review-data" type="application/json">(.*?)</script>',
            page, re.DOTALL,
        )
        decoded = json.loads(match.group(1))
        self.assertEqual(decoded["files"][0]["lines"][0]["text"], line["text"])
        self.assertIn('const sessionToken="secret-token"', page)


if __name__ == "__main__":
    unittest.main()
