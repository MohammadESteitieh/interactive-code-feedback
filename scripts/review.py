#!/usr/bin/env python3
"""Local read-only code reviewer with line comments, highlighting, and LSP hover."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

SKILL_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = SKILL_DIR / "web"
VENDOR_DIR = SKILL_DIR / "vendor"
NODE_LSP_BIN = VENDOR_DIR / "lsp-node" / "node_modules" / ".bin"
if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

try:
    from pygments import lex
    from pygments.lexers import get_lexer_for_filename
    from pygments.util import ClassNotFound
    PYGMENTS_AVAILABLE = True
except ImportError:
    PYGMENTS_AVAILABLE = False

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
# Unified diff hunk header with the old and new starting line numbers.
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Identifier tokens that can be queried through language-server hover.
IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*\Z")
# Identifier, whitespace, or single-character tokens for plain-text highlighting.
FALLBACK_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*|\s+|.", re.DOTALL)
# ANSI control-sequence introducer escapes found in notebook tracebacks.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SAFE_NOTEBOOK_HTML_TAGS = frozenset({
    "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "span", "strong",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})
SAFE_NOTEBOOK_HTML_ATTRIBUTES = frozenset({"colspan", "rowspan", "title"})
NOTEBOOK_IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
NOTEBOOK_LANGUAGE_SUFFIXES = {
    "python": ".py", "javascript": ".js", "typescript": ".ts", "r": ".r",
    "julia": ".jl", "rust": ".rs", "go": ".go", "c": ".c", "cpp": ".cpp",
    "java": ".java", "bash": ".sh", "shell": ".sh", "ruby": ".rb",
}

LANGUAGE_SUFFIXES = {
    ".py": ("python", "python"), ".pyi": ("python", "python"),
    ".js": ("typescript", "javascript"), ".mjs": ("typescript", "javascript"),
    ".cjs": ("typescript", "javascript"), ".jsx": ("typescript", "javascriptreact"),
    ".ts": ("typescript", "typescript"), ".tsx": ("typescript", "typescriptreact"),
    ".rs": ("rust", "rust"), ".go": ("go", "go"),
    ".c": ("cpp", "c"), ".h": ("cpp", "cpp"), ".cc": ("cpp", "cpp"),
    ".cpp": ("cpp", "cpp"), ".cxx": ("cpp", "cpp"),
    ".hh": ("cpp", "cpp"), ".hpp": ("cpp", "cpp"), ".hxx": ("cpp", "cpp"),
}
SERVER_COMMANDS = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "rust": ["rust-analyzer"],
    "go": ["gopls"],
    "cpp": ["clangd"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_git(root: Path, arguments: list[str]) -> str:
    command = ["git", "-C", str(root), *arguments]
    completed = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or "Git command failed"
        raise RuntimeError(f"{message}\nCommand: {' '.join(command)}")
    return completed.stdout


def display_path(value: str) -> str:
    value = value.strip()
    if value == "/dev/null":
        return value
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def parse_unified_diff(text: str) -> list[dict]:
    files: list[dict] = []
    current: dict | None = None
    old_line = new_line = None
    line_id = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current = {"path": "", "old_path": "", "lines": []}
            files.append(current)
            old_line = new_line = None
            continue
        if current is None:
            continue
        if raw.startswith("--- "):
            current["old_path"] = display_path(raw[4:])
            continue
        if raw.startswith("+++ "):
            current["path"] = display_path(raw[4:])
            continue
        match = HUNK_RE.match(raw)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            current["lines"].append({
                "id": line_id, "kind": "hunk", "old_line": None,
                "new_line": None, "text": raw,
            })
            line_id += 1
            continue
        if old_line is None or new_line is None or raw.startswith("\\ No newline"):
            continue
        prefix = raw[:1]
        content = raw[1:] if raw else ""
        if prefix == "+":
            entry_old, entry_new, kind = None, new_line, "added"
            new_line += 1
        elif prefix == "-":
            entry_old, entry_new, kind = old_line, None, "deleted"
            old_line += 1
        elif prefix == " ":
            entry_old, entry_new, kind = old_line, new_line, "context"
            old_line += 1
            new_line += 1
        else:
            continue
        current["lines"].append({
            "id": line_id, "kind": kind, "old_line": entry_old,
            "new_line": entry_new, "text": content,
        })
        line_id += 1
    for item in files:
        if not item["path"] or item["path"] == "/dev/null":
            item["path"] = item["old_path"]
        if not item["old_path"]:
            item["old_path"] = item["path"]
    return [item for item in files if item["lines"]]


class NotebookHTMLSanitizer(HTMLParser):
    """Keep basic rich output markup while dropping scripts, styles, and unsafe attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "iframe", "object", "embed"}:
            self.suppressed_depth += 1
            return
        if self.suppressed_depth or tag not in SAFE_NOTEBOOK_HTML_TAGS:
            return
        kept = []
        for name, value in attrs:
            name = name.lower()
            if name in SAFE_NOTEBOOK_HTML_ATTRIBUTES and value is not None:
                kept.append(f' {name}="{html.escape(str(value), quote=True)}"')
        self.parts.append(f"<{tag}{''.join(kept)}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in SAFE_NOTEBOOK_HTML_TAGS and tag.lower() not in {"br", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "iframe", "object", "embed"}:
            self.suppressed_depth = max(0, self.suppressed_depth - 1)
            return
        if not self.suppressed_depth and tag in SAFE_NOTEBOOK_HTML_TAGS and tag not in {"br", "hr"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.suppressed_depth:
            self.parts.append(html.escape(data))


def sanitize_notebook_html(value: str) -> str:
    parser = NotebookHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def markdown_inline(value: str) -> str:
    """Render a deliberately small, escaped subset of Markdown inline syntax."""
    escaped = html.escape(value)
    # Inline code enclosed by one backtick on each side.
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    # Strong emphasis enclosed by paired asterisks.
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    # Strong emphasis enclosed by paired underscores.
    escaped = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", escaped)
    # Single-asterisk emphasis without consuming strong-emphasis markers.
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def render_notebook_markdown(value: str) -> str:
    """Render headings, lists, quotes, paragraphs, and fenced code without external JS."""
    output: list[str] = []
    paragraph: list[str] = []
    list_tag: str | None = None
    in_fence = False
    fence_lines: list[str] = []

    def close_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{markdown_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            output.append(f"</{list_tag}>")
            list_tag = None

    for raw in value.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            close_paragraph()
            close_list()
            if in_fence:
                output.append(f"<pre><code>{html.escape(chr(10).join(fence_lines))}</code></pre>")
                fence_lines.clear()
                in_fence = False
            else:
                in_fence = True
            continue
        if in_fence:
            fence_lines.append(raw)
            continue
        if not stripped:
            close_paragraph()
            close_list()
            continue
        # Markdown ATX heading with one through six leading hash marks.
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_paragraph()
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{markdown_inline(heading.group(2))}</h{level}>")
            continue
        # Unordered list item introduced by a dash, asterisk, or plus.
        unordered = re.match(r"^[-*+]\s+(.*)$", stripped)
        # Ordered list item introduced by a number followed by a dot or parenthesis.
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if unordered or ordered:
            close_paragraph()
            wanted = "ul" if unordered else "ol"
            if list_tag != wanted:
                close_list()
                output.append(f"<{wanted}>")
                list_tag = wanted
            output.append(f"<li>{markdown_inline((unordered or ordered).group(1))}</li>")
            continue
        if stripped.startswith(">"):
            close_paragraph()
            close_list()
            output.append(f"<blockquote>{markdown_inline(stripped[1:].strip())}</blockquote>")
            continue
        paragraph.append(stripped)
    if in_fence:
        output.append(f"<pre><code>{html.escape(chr(10).join(fence_lines))}</code></pre>")
    close_paragraph()
    close_list()
    return "".join(output)


def notebook_text(value) -> str:
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def normalize_notebook_output(output: dict) -> dict:
    output_type = str(output.get("output_type", "output"))
    if output_type == "stream":
        return {
            "kind": "text", "label": str(output.get("name", "stream")),
            "text": notebook_text(output.get("text")),
        }
    if output_type == "error":
        traceback = "\n".join(str(item) for item in output.get("traceback", []))
        traceback = ANSI_ESCAPE_RE.sub("", traceback)
        if not traceback:
            traceback = f"{output.get('ename', 'Error')}: {output.get('evalue', '')}".rstrip()
        return {"kind": "error", "label": "error", "text": traceback}

    data = output.get("data", {})
    if not isinstance(data, dict):
        return {"kind": "text", "label": output_type, "text": notebook_text(data)}
    for mime in NOTEBOOK_IMAGE_MIMES:
        if mime in data:
            return {
                "kind": "image", "label": mime, "mime": mime,
                "data": notebook_text(data[mime]).replace("\n", ""),
            }
    if "text/html" in data:
        return {
            "kind": "html", "label": "text/html",
            "html": sanitize_notebook_html(notebook_text(data["text/html"])),
        }
    if "text/markdown" in data:
        return {
            "kind": "markdown", "label": "text/markdown",
            "html": render_notebook_markdown(notebook_text(data["text/markdown"])),
        }
    if "application/json" in data:
        value = data["application/json"]
        text = json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value
        return {"kind": "text", "label": "application/json", "text": text}
    if "text/plain" in data:
        return {"kind": "text", "label": "text/plain", "text": notebook_text(data["text/plain"])}
    return {"kind": "text", "label": output_type, "text": "Output type is not renderable."}


def notebook_highlight_filename(notebook: dict) -> str:
    language_info = notebook.get("metadata", {}).get("language_info", {})
    extension = language_info.get("file_extension")
    if isinstance(extension, str) and extension.startswith("."):
        return f"notebook-cell{extension}"
    language = str(language_info.get("name", "python")).lower()
    return f"notebook-cell{NOTEBOOK_LANGUAGE_SUFFIXES.get(language, '.txt')}"


def collect_notebook(path: Path, label: str, line_id: int) -> tuple[dict, int]:
    try:
        notebook = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Jupyter notebook {label}: {error}") from error
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells", []), list):
        raise ValueError(f"invalid Jupyter notebook structure: {label}")
    filename = notebook_highlight_filename(notebook)
    cells = []
    all_lines = []
    for cell_index, raw_cell in enumerate(notebook.get("cells", [])):
        if not isinstance(raw_cell, dict):
            continue
        cell_type = str(raw_cell.get("cell_type", "raw"))
        source = notebook_text(raw_cell.get("source"))
        source_text_lines = source.splitlines()
        token_lines = highlighted_lines(source, filename) if cell_type == "code" else []
        lines = []
        for cell_line, text in enumerate(source_text_lines, 1):
            tokens = (
                token_lines[cell_line - 1]
                if cell_type == "code" and cell_line - 1 < len(token_lines)
                else fallback_tokens(text)
            )
            line = {
                "id": line_id, "kind": "notebook", "old_line": None, "new_line": None,
                "text": text, "tokens": tokens, "cell_index": cell_index,
                "cell_line": cell_line, "cell_type": cell_type,
            }
            lines.append(line)
            all_lines.append(line)
            line_id += 1
        cells.append({
            "index": cell_index,
            "type": cell_type,
            "execution_count": raw_cell.get("execution_count"),
            "lines": lines,
            "rendered_html": render_notebook_markdown(source) if cell_type == "markdown" else "",
            "outputs": [
                normalize_notebook_output(item)
                for item in raw_cell.get("outputs", []) if isinstance(item, dict)
            ] if cell_type == "code" else [],
        })
    kernelspec = notebook.get("metadata", {}).get("kernelspec", {})
    return ({
        "path": label, "old_path": label, "lines": all_lines, "notebook": True,
        "cells": cells,
        "kernel": kernelspec.get("display_name") or kernelspec.get("name") or "Jupyter",
    }, line_id)


def replace_notebook_diffs(root: Path, files: list[dict]) -> list[dict]:
    """Replace JSON hunks with a rendered current notebook when that file still exists."""
    output = []
    line_id = max((line["id"] for item in files for line in item["lines"]), default=-1) + 1
    for item in files:
        if Path(item["path"]).suffix.lower() != ".ipynb":
            output.append(item)
            continue
        path = payload_path(root, item["path"])
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            output.append(item)
            continue
        notebook, line_id = collect_notebook(path, item["path"], line_id)
        notebook["diff_note"] = "Rendered current notebook; raw JSON diff hidden."
        output.append(notebook)
    return output


def collect_diff(root: Path, staged: bool, git_range: str | None, compact_diff: bool = False) -> tuple[list[dict], str]:
    # Git accepts at most a signed 32-bit context count; use it to include the whole file.
    context = 3 if compact_diff else 2**31 - 1
    arguments = [
        "diff", "--no-ext-diff", "--no-color", "--find-renames",
        f"--unified={context}", "--src-prefix=a/", "--dst-prefix=b/",
    ]
    if staged:
        arguments.append("--cached")
    if git_range:
        arguments.append(git_range)
    elif not staged:
        arguments.append("HEAD")
    arguments.append("--")
    output = run_git(root, arguments)
    scope = git_range or ("staged changes" if staged else "working tree against HEAD")
    return replace_notebook_diffs(root, parse_unified_diff(output)), scope


def resolve_review_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"not a file: {value}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes: {value}")
    return path


def collect_files(root: Path, values: list[str]) -> tuple[list[dict], str]:
    root = root.resolve()
    files = []
    line_id = 0
    for value in values:
        path = resolve_review_path(root, value)
        try:
            label = str(path.relative_to(root))
        except ValueError:
            label = str(path)
        if path.suffix.lower() == ".ipynb":
            notebook, line_id = collect_notebook(path, label, line_id)
            files.append(notebook)
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = []
        for number, text in enumerate(content.splitlines(), 1):
            lines.append({
                "id": line_id, "kind": "file", "old_line": number,
                "new_line": number, "text": text,
            })
            line_id += 1
        files.append({"path": label, "old_path": label, "lines": lines})
    return files, "selected files"


def token_class(token_type) -> str:
    name = str(token_type)
    rules = (
        ("Token.Comment", "comment"), ("Token.Keyword.Type", "type"),
        ("Token.Keyword", "keyword"), ("Token.Literal.String", "string"),
        ("Token.Literal.Number", "number"), ("Token.Name.Function", "function"),
        ("Token.Name.Class", "class"), ("Token.Name.Builtin", "builtin"),
        ("Token.Name.Constant", "constant"), ("Token.Name.Decorator", "decorator"),
        ("Token.Name.Namespace", "namespace"), ("Token.Name.Property", "property"),
        ("Token.Name.Variable", "variable"), ("Token.Operator", "operator"),
        ("Token.Punctuation", "punctuation"), ("Token.Error", "error"),
    )
    for prefix, css_class in rules:
        if name.startswith(prefix):
            return css_class
    return ""


def fallback_tokens(text: str) -> list[dict]:
    tokens = []
    for match in FALLBACK_TOKEN_RE.finditer(text):
        value = match.group(0)
        tokens.append({
            "text": value, "class": "", "column": match.start(),
            "hoverable": bool(IDENTIFIER_RE.fullmatch(value)),
        })
    return tokens


def highlighted_lines(text: str, filename: str) -> list[list[dict]]:
    if not PYGMENTS_AVAILABLE:
        return [fallback_tokens(line) for line in text.splitlines()]
    try:
        lexer = get_lexer_for_filename(filename, text)
    except ClassNotFound:
        return [fallback_tokens(line) for line in text.splitlines()]
    output: list[list[dict]] = [[]]
    column = 0
    for token_type, value in lex(text, lexer):
        parts = value.split("\n")
        for index, part in enumerate(parts):
            if part:
                output[-1].append({
                    "text": part, "class": token_class(token_type),
                    "column": column,
                    "hoverable": bool(IDENTIFIER_RE.fullmatch(part)),
                })
                column += len(part)
            if index < len(parts) - 1:
                output.append([])
                column = 0
    return output


def payload_path(root: Path, label: str) -> Path:
    path = Path(label).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def attach_highlighting(root: Path, files: list[dict]) -> str:
    for file in files:
        if file.get("notebook"):
            continue
        path = payload_path(root, file["path"])
        source_lines: list[str] = []
        source_tokens: list[list[dict]] = []
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            source = path.read_text(encoding="utf-8", errors="replace")
            source_lines = source.splitlines()
            source_tokens = highlighted_lines(source, file["path"])
        for line in file["lines"]:
            if line["kind"] == "hunk":
                line["tokens"] = []
                continue
            index = line["new_line"] - 1 if line["new_line"] is not None else -1
            if (
                index >= 0 and index < len(source_lines)
                and index < len(source_tokens) and source_lines[index] == line["text"]
            ):
                line["tokens"] = source_tokens[index]
            else:
                fragments = highlighted_lines(line["text"], file["path"])
                line["tokens"] = fragments[0] if fragments else fallback_tokens(line["text"])
    return "Pygments" if PYGMENTS_AVAILABLE else "plain text"


def language_for_path(path: Path) -> tuple[str, str] | None:
    return LANGUAGE_SUFFIXES.get(path.suffix.lower())


def server_executable(name: str) -> str | None:
    system_path = shutil.which(name)
    if system_path:
        return system_path
    bundled_path = NODE_LSP_BIN / name
    if bundled_path.is_file() and os.access(bundled_path, os.X_OK):
        return str(bundled_path)
    return None


def hover_content_text(contents) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, list):
        return "\n\n".join(filter(None, (hover_content_text(item) for item in contents)))
    if isinstance(contents, dict):
        if "value" in contents:
            return str(contents["value"])
        return json.dumps(contents, ensure_ascii=False)
    return str(contents)


class LspClient:
    def __init__(self, command: list[str], root: Path, timeout: float):
        self.command = command
        self.root = root
        self.timeout = timeout
        self.process = subprocess.Popen(
            command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.write_lock = threading.Lock()
        self.condition = threading.Condition()
        self.responses: dict[int, dict] = {}
        self.next_id = 1
        self.opened: set[Path] = set()
        self.first_hover_pending = True
        self.reader_error: str | None = None
        self.position_encoding = "utf-16"
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader.start()
        try:
            result = self.request("initialize", {
                "processId": os.getpid(),
                "clientInfo": {"name": "code-review-ide", "version": "1"},
                "rootUri": root.as_uri(),
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                "capabilities": {
                    "general": {"positionEncodings": ["utf-8", "utf-16"]},
                    "textDocument": {"hover": {"dynamicRegistration": False}},
                    "workspace": {"configuration": False},
                },
            })
            capabilities = (result or {}).get("capabilities", {})
            self.position_encoding = capabilities.get("positionEncoding", "utf-16")
            self.notify("initialized", {})
        except Exception:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
            raise

    def _send(self, message: dict) -> None:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        packet = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self.write_lock:
            if self.process.stdin is None:
                raise RuntimeError("language server input is closed")
            self.process.stdin.write(packet)
            self.process.stdin.flush()

    def _read_message(self) -> dict:
        if self.process.stdout is None:
            raise EOFError("language server output is closed")
        content_length = None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise EOFError("language server stopped")
            if line in {b"\r\n", b"\n"}:
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            if name.lower() == "content-length":
                content_length = int(value.strip())
        if content_length is None:
            raise RuntimeError("language server response omitted Content-Length")
        body = self.process.stdout.read(content_length)
        if len(body) != content_length:
            raise EOFError("incomplete language server response")
        return json.loads(body)

    def _reader_loop(self) -> None:
        try:
            while True:
                message = self._read_message()
                if "method" in message and "id" in message:
                    method = message["method"]
                    if method == "workspace/configuration":
                        count = len(message.get("params", {}).get("items", []))
                        result = [None] * count
                    else:
                        result = None
                    self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
                elif "id" in message:
                    with self.condition:
                        self.responses[message["id"]] = message
                        self.condition.notify_all()
        except Exception as error:
            with self.condition:
                self.reader_error = str(error)
                self.condition.notify_all()

    def request(self, method: str, params, timeout: float | None = None):
        with self.condition:
            request_id = self.next_id
            self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self.timeout)
        with self.condition:
            while request_id not in self.responses:
                if self.reader_error:
                    raise RuntimeError(self.reader_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"language server timed out during {method}")
                self.condition.wait(remaining)
            response = self.responses.pop(request_id)
        if "error" in response:
            message = response["error"].get("message", "language server request failed")
            raise RuntimeError(message)
        return response.get("result")

    def notify(self, method: str, params) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open_document(self, path: Path, language_id: str) -> str:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path not in self.opened:
            self.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": path.as_uri(), "languageId": language_id,
                    "version": 1, "text": text,
                }
            })
            self.opened.add(path)
        return text

    def protocol_column(self, line: str, column: int) -> int:
        prefix = line[:column]
        if self.position_encoding == "utf-8":
            return len(prefix.encode("utf-8"))
        if self.position_encoding == "utf-32":
            return len(prefix)
        return len(prefix.encode("utf-16-le")) // 2

    def hover(self, path: Path, language_id: str, line_number: int, column: int):
        text = self.open_document(path, language_id)
        lines = text.splitlines()
        if line_number < 1 or line_number > len(lines):
            return None
        column = max(0, min(column, len(lines[line_number - 1])))
        position = {
            "line": line_number - 1,
            "character": self.protocol_column(lines[line_number - 1], column),
        }
        params = {"textDocument": {"uri": path.as_uri()}, "position": position}
        delays = (0.0, 0.15, 0.35, 0.75, 1.5, 2.5) if self.first_hover_pending else (0.0,)
        last_error: RuntimeError | None = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                result = self.request("textDocument/hover", params)
                if result is not None:
                    self.first_hover_pending = False
                    return result
            except RuntimeError as error:
                transient = str(error).lower()
                if "content modified" not in transient and "no views" not in transient:
                    raise
                last_error = error
        self.first_hover_pending = False
        if last_error is not None:
            raise last_error
        return None

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.request("shutdown", None, timeout=1.0)
            self.notify("exit", None)
            self.process.wait(timeout=1.0)
        except Exception:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


class LspManager:
    def __init__(self, root: Path, files: list[dict], timeout: float):
        self.root = root
        self.timeout = timeout
        self.allowed = {file["path"] for file in files}
        self.clients: dict[str, LspClient | str] = {}
        self.cache: dict[tuple[str, int, int], str] = {}
        self.lock = threading.Lock()

    def _client(self, language: str) -> LspClient:
        existing = self.clients.get(language)
        if isinstance(existing, LspClient):
            return existing
        if isinstance(existing, str):
            raise RuntimeError(existing)
        command = SERVER_COMMANDS[language]
        executable = server_executable(command[0])
        if not executable:
            message = f"{command[0]} is not installed"
            self.clients[language] = message
            raise RuntimeError(message)
        try:
            client = LspClient([executable, *command[1:]], self.root, self.timeout)
        except Exception as error:
            message = f"could not start {command[0]}: {error}"
            self.clients[language] = message
            raise RuntimeError(message) from error
        self.clients[language] = client
        return client

    def hover(self, file_label: str, line: int, column: int) -> dict:
        key = (file_label, line, column)
        with self.lock:
            if key in self.cache:
                return {"text": self.cache[key]}
            if file_label not in self.allowed:
                return {"text": "Hover unavailable: file is outside this review."}
            path = payload_path(self.root, file_label)
            if not path.is_file():
                return {"text": "Hover unavailable for deleted or missing files."}
            language = language_for_path(path)
            if not language:
                return {"text": "No language server is configured for this file type."}
            server_name, language_id = language
            try:
                result = self._client(server_name).hover(path, language_id, line, column)
                text = hover_content_text((result or {}).get("contents"))
                if not text:
                    text = "No documentation available."
            except Exception as error:
                text = f"Hover unavailable: {error}"
            self.cache[key] = text
            return {"text": text}

    def close(self) -> None:
        with self.lock:
            for client in self.clients.values():
                if isinstance(client, LspClient):
                    client.close()


def page_html(payload: dict, token: str) -> bytes:
    template = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    style = (WEB_DIR / "review.css").read_text(encoding="utf-8")
    script = (WEB_DIR / "review.js").read_text(encoding="utf-8")
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    replacements = {
        "TITLE": html.escape(payload["title"]), "STYLE": style,
        "DATA": encoded, "TOKEN": json.dumps(token), "SCRIPT": script,
    }
    document = re.sub(
        # Replace only the five placeholders defined by the bundled page template.
        r"__(TITLE|STYLE|DATA|TOKEN|SCRIPT)__",
        lambda match: replacements[match.group(1)],
        template,
    )
    return document.encode("utf-8")


def serve(
    payload: dict, port: int, open_browser: bool, timeout: float,
    hover_enabled: bool = True, lsp_timeout: float = 5.0,
) -> dict:
    token = secrets.token_urlsafe(24)
    page = page_html(payload, token)
    state: dict = {"result": None}
    manager = LspManager(Path(payload["root"]), payload["files"], lsp_timeout) if hover_enabled else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            value = json.loads(self.rfile.read(length))
            if value.pop("token", None) != token:
                raise ValueError("invalid session token")
            return value

        def _send_json(self, value: dict, status: int = 200) -> None:
            response = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/" or parse_qs(parsed.query).get("token") != [token]:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            try:
                path = urlparse(self.path).path
                submitted = self._json_body()
                if path == "/hover":
                    if manager is None:
                        self._send_json({"text": "LSP hover is disabled."})
                        return
                    file_label = str(submitted.get("file", ""))
                    line = int(submitted.get("line", 0))
                    column = int(submitted.get("column", 0))
                    self._send_json(manager.hover(file_label, line, column))
                    return
                if path != "/submit":
                    self.send_error(404)
                    return
                if submitted.get("status") not in {"submitted", "cancelled"}:
                    raise ValueError("invalid status")
                submitted["submitted_at"] = utc_now()
                state["result"] = submitted
                self._send_json({"ok": True})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(400, str(error))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/?token={token}"
    print(f"Code review IDE: {url}", file=sys.stderr, flush=True)
    if open_browser:
        threading.Timer(0.15, lambda: webbrowser.open(url)).start()
    started = time.monotonic()
    server.timeout = 0.5
    try:
        while state["result"] is None:
            server.handle_request()
            if timeout > 0 and time.monotonic() - started >= timeout:
                state["result"] = {
                    "status": "timed_out", "mode": payload["mode"],
                    "root": payload["root"], "scope": payload["scope"],
                    "comments": [], "overall": "", "submitted_at": utc_now(),
                }
                break
    except KeyboardInterrupt:
        state["result"] = {
            "status": "cancelled", "mode": payload["mode"],
            "root": payload["root"], "scope": payload["scope"],
            "comments": [], "overall": "", "submitted_at": utc_now(),
        }
    finally:
        server.server_close()
        if manager is not None:
            manager.close()
    return state["result"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository/workspace root")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="review staged Git changes")
    group.add_argument("--range", dest="git_range", help="review a Git revision range")
    group.add_argument("--files", nargs="+", help="review complete files")
    parser.add_argument("--compact-diff", action="store_true", help="show three context lines per diff hunk instead of complete files; ignored with --files")
    parser.add_argument("--title", default="Code review", help="browser title")
    parser.add_argument("--port", type=int, default=0, help="local port; zero chooses a free port")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    parser.add_argument("--no-hover", action="store_true", help="disable language-server hover")
    parser.add_argument("--lsp-timeout", type=float, default=5.0, help="language-server request timeout")
    parser.add_argument("--timeout", type=float, default=0.0, help="seconds before returning timed_out; zero disables")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"root is not a directory: {root}")
    try:
        if args.files:
            files, scope = collect_files(root, args.files)
            mode = "files"
        else:
            files, scope = collect_diff(root, args.staged, args.git_range, compact_diff=args.compact_diff)
            mode = "diff"
        syntax_engine = attach_highlighting(root, files)
    except (RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    payload = {
        "title": args.title, "mode": mode, "root": str(root),
        "scope": scope, "files": files, "syntax_engine": syntax_engine,
        "hover_enabled": not args.no_hover,
    }
    result = serve(
        payload, args.port, not args.no_open, args.timeout,
        hover_enabled=not args.no_hover, lsp_timeout=args.lsp_timeout,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
