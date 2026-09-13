import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
from textwrap import dedent
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("review.py")
SPEC = importlib.util.spec_from_file_location("code_review_ide", SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


@contextlib.contextmanager
def git_repository():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        review.run_git(root, ["init", "-q"])
        review.run_git(root, ["config", "user.email", "test@example.com"])
        review.run_git(root, ["config", "user.name", "Test"])
        yield root


class ReviewTests(unittest.TestCase):
    def test_parse_unified_diff_tracks_old_and_new_lines(self):
        text = dedent("""\
            diff --git a/demo.py b/demo.py
            index 1111111..2222222 100644
            --- a/demo.py
            +++ b/demo.py
            @@ -1,3 +1,4 @@
             first
            -old
            +new
            +extra
             last
            """)
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
        with git_repository() as root:
            path = root / "demo.ipynb"
            notebook = {
                "cells": [{"cell_type": "code", "metadata": {}, "execution_count": None,
                           "source": ["before = 1"], "outputs": []}],
                "metadata": {"language_info": {"name": "python"}},
                "nbformat": 4, "nbformat_minor": 5,
            }
            path.write_text(json.dumps(notebook, indent=1), encoding="utf-8")
            review.run_git(root, ["add", "demo.ipynb"])
            review.run_git(root, ["commit", "-qm", "initial"])
            notebook["cells"][0]["source"] = ["after = 2"]
            path.write_text(json.dumps(notebook, indent=1), encoding="utf-8")
            for compact in (False, True):
                with self.subTest(compact=compact):
                    files, _scope = review.collect_diff(root, False, None, compact_diff=compact)
                    self.assertTrue(files[0]["notebook"])
                    self.assertEqual(files[0]["cells"][0]["lines"][0]["text"], "after = 2")
                    self.assertIn("raw JSON diff hidden", files[0]["diff_note"])

    def test_collect_diff_context_and_snapshot_selection(self):
        original = [f"line {number}" for number in range(1, 81)]
        expected = (
            [("context", n, n, f"line {n}") for n in range(1, 20)]
            + [("added", None, 20, "inserted")]
            + [("context", n, n + 1, f"line {n}") for n in range(20, 60)]
            + [("deleted", 60, None, "line 60")]
            + [("context", n, n, f"line {n}") for n in range(61, 81)]
        )
        compact_expected = [
            row for row in expected
            if row[0] != "context" or 17 <= row[1] <= 22 or 57 <= row[1] <= 63
        ]
        modes = [
            (False, None, "working tree against HEAD"),
            (True, None, "staged changes"),
            (False, "HEAD~1..HEAD", "HEAD~1..HEAD"),
        ]
        for staged, git_range, scope in modes:
            with self.subTest(scope=scope), git_repository() as root:
                path = root / "demo.txt"
                # No final newline, including on the unchanged suffix.
                path.write_text("\n".join(original), encoding="utf-8")
                (root / "unchanged.txt").write_text("not changed\n", encoding="utf-8")
                review.run_git(root, ["add", "."])
                review.run_git(root, ["commit", "-qm", "initial"])
                inserted = original[:19] + ["inserted"] + original[19:]
                path.write_text("\n".join(inserted), encoding="utf-8")
                review.run_git(root, ["add", "demo.txt"])
                changed = original[:19] + ["inserted"] + original[19:59] + original[60:]
                path.write_text("\n".join(changed), encoding="utf-8")
                if staged or git_range:
                    review.run_git(root, ["add", "demo.txt"])
                    if git_range:
                        review.run_git(root, ["commit", "-qm", "changed"])
                        path.write_text("unrelated staged content\n", encoding="utf-8")
                        review.run_git(root, ["add", "demo.txt"])
                    path.write_text("unrelated working-tree content\n", encoding="utf-8")
                for compact in (False, True):
                    with self.subTest(compact=compact):
                        options = {"compact_diff": True} if compact else {}
                        files, actual_scope = review.collect_diff(root, staged, git_range, **options)
                        self.assertEqual(actual_scope, scope)
                        self.assertEqual([file["path"] for file in files], ["demo.txt"])
                        lines = files[0]["lines"]
                        self.assertEqual(len(lines), len({line["id"] for line in lines}))
                        self.assertEqual(sum(line["kind"] == "hunk" for line in lines), 2 if compact else 1)
                        actual_rows = [
                            (line["kind"], line["old_line"], line["new_line"], line["text"])
                            for line in lines if line["kind"] != "hunk"
                        ]
                        expected_rows = compact_expected if compact else expected
                        self.assertEqual(actual_rows, expected_rows)

    def test_main_passes_compact_diff_to_collect_diff(self):
        mode_flags = [[], ["--staged"], ["--range", "main...HEAD"]]
        staged_values = [False, True, False]
        git_ranges = [None, None, "main...HEAD"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = {"status": "submitted", "mode": "diff", "root": str(root),
                      "comments": [], "overall": "keep unchanged"}
            for flags, staged, git_range in zip(mode_flags, staged_values, git_ranges):
                for compact in (False, True):
                    with self.subTest(flags=flags, compact=compact), \
                            mock.patch.object(review, "collect_diff", return_value=([], "test scope")) as collector, \
                            mock.patch.object(review, "serve", return_value=result) as server, \
                            contextlib.redirect_stdout(io.StringIO()) as stdout:
                        options = ["--compact-diff"] if compact else []
                        self.assertEqual(review.main(["--root", str(root), *flags, *options]), 0)
                        collector.assert_called_once_with(root, staged, git_range, compact_diff=compact)
                        self.assertEqual(server.call_args.args[0]["mode"], "diff")
                        self.assertEqual(server.call_args.args[0]["scope"], "test scope")
                        self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_main_files_mode_ignores_compact_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "demo.txt").write_text("one\ntwo\n", encoding="utf-8")
            for compact in (False, True):
                with self.subTest(compact=compact), \
                        mock.patch.object(review, "collect_diff") as collector, \
                        mock.patch.object(review, "serve", return_value={}) as server, \
                        contextlib.redirect_stdout(io.StringIO()):
                    options = ["--compact-diff"] if compact else []
                    self.assertEqual(review.main(["--root", str(root), "--files", "demo.txt", *options]), 0)
                    collector.assert_not_called()
                    payload = server.call_args.args[0]
                    self.assertEqual(payload["mode"], "files")
                    self.assertEqual(payload["scope"], "selected files")
                    self.assertEqual(
                        [(line["kind"], line["old_line"], line["new_line"], line["text"])
                         for line in payload["files"][0]["lines"]],
                        [("file", 1, 1, "one"), ("file", 2, 2, "two")],
                    )

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
