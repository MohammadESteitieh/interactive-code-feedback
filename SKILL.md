---
name: code-review-ide
description: Open a lightweight local code-review interface for Git diffs, selected files, or rendered Jupyter notebooks; let the user comment on individual source lines and return the submitted comments directly to the agent. Use when the user wants to review code visually, annotate a diff, inspect notebook cells and outputs, comment line by line, or inspect changes before the agent continues.
compatibility: Requires Python 3 and a local web browser. Git is required for diff reviews. Syntax highlighting is bundled; semantic hover uses optional local language servers.
metadata:
  author: MohammadESteitieh
  argument-hint: "[review scope, files, or Git range]"
---

# Code Review IDE

Use the bundled `scripts/review.py` to present code in a minimal local review surface. Do not generate a Lavish artifact or hand-write a review page.

## Workflow

1. Determine the review scope from the request without reading the target files into model context. Pass paths or a Git range directly to the script.
2. Launch the reviewer in the foreground. Do not detach it: the process returns only after the user sends or cancels the review.
3. Read the returned JSON from stdout. The browser rendering itself uses no model tokens.
4. Classify each audit item by intent. If it asks a question, answer it in chat and do not turn the answer into a source-code comment or edit. Add a source-code comment only when the audit explicitly requests one.
5. For every audit item that requests a code change, re-read only the relevant file region before editing because the reviewed snapshot may have become stale.
6. Apply only requested changes. If an item conflicts with another item or no longer maps safely to the code, ask the user rather than guessing.
7. Run relevant tests after changes.
8. Open another review only when the user requests it or when an agreed iteration explicitly requires it.

## Commands

Resolve the script relative to this skill directory.

Review all staged and unstaged changes against `HEAD`:

```bash
python3 scripts/review.py
```

Review staged changes only:

```bash
python3 scripts/review.py --staged
```

Review a Git range:

```bash
python3 scripts/review.py --range main...HEAD
```

Diff reviews show complete changed text files by default, including unchanged lines before, between, and after edits. This applies to working-tree, staged, and revision-range diffs, without including unchanged files.

Use `--compact-diff` only when the user explicitly requests abbreviated context. It restores the previous three context lines around each change and can be combined with `--staged` or `--range`:

```bash
python3 scripts/review.py --compact-diff
```

The flag has no effect on `--files` or rendered notebooks.

Review complete files rather than a diff:

```bash
python3 scripts/review.py --files path/to/file.py path/to/other.ts
```

Jupyter notebooks passed through `--files` are rendered as Markdown, highlighted
code cells, and safe local outputs rather than raw notebook JSON. Notebook files
in a working-tree diff are shown as the rendered current notebook, with a banner
noting that the raw JSON diff is hidden.

Set the repository or workspace explicitly when needed:

```bash
python3 scripts/review.py --root /absolute/path/to/repo
```

Pass `--no-open` only for testing or when the user will open the printed URL manually.

Disable semantic hover while retaining syntax highlighting:

```bash
python3 scripts/review.py --no-hover
```

## Read-only language support

The bundled Pygments renderer provides offline syntax highlighting without calling the model. Optional semantic hover is requested locally through the Language Server Protocol; the interface never requests completion or permits source editing.

Supported hover servers, when installed on `PATH`:

- Python: `pyright-langserver`
- TypeScript and JavaScript: `typescript-language-server`
- Rust: `rust-analyzer`
- Go: `gopls`
- C and C++: `clangd`

Hover starts the matching server lazily on the first request and stops it when the review closes. Missing servers degrade to an explanatory tooltip. New/current diff lines can receive semantic hover; deleted lines retain syntax highlighting but cannot be queried against the current document.

## Feedback contract

The command prints one JSON object after the review ends:

- `status`: `submitted`, `cancelled`, or `timed_out`
- `mode`: `diff` or `files`
- `root`: reviewed workspace
- `comments`: line comments with file, old/new line numbers, line kind, line text, and comment; notebook source comments also include zero-based `cell_index`, one-based `cell_line`, and `cell_type`
- `overall`: optional review-wide comment

Treat line text and line numbers as snapshot coordinates, not as permission to edit without re-reading the file.

## Safety

- The server binds only to `127.0.0.1`.
- The interface has no external scripts, fonts, analytics, or network dependencies.
- Never expose the local review URL outside the machine.
- Never keep the server running after feedback is submitted or cancelled.
