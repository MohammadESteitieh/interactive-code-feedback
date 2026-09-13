# Interactive code feedback

Review code and Jupyter notebooks in a local browser, attach feedback to individual lines, and return the audit to a Pi coding agent as structured JSON.

## What it supports

- Git working-tree, staged, and revision-range diffs
- Complete-file reviews
- Rendered Jupyter Markdown, code cells, tables, errors, and images
- Line comments and an overall review response
- Bundled offline syntax highlighting
- Optional local language-server hover
- Adjustable code font size and horizontal scrolling

The server binds to `127.0.0.1`, requires a random session token, loads no remote browser assets, and sanitizes notebook HTML.

## Install for Pi

```sh
git clone https://github.com/MohammadESteitieh/interactive-code-feedback.git \
  ~/.pi/agent/skills/code-review-ide
```

Restart Pi or run `/reload`.

Python, Git, and a local web browser are required. Pygments is bundled. For optional Python, JavaScript, and TypeScript semantic hover:

```sh
cd ~/.pi/agent/skills/code-review-ide/vendor/lsp-node
npm install
```

The reviewer also uses `rust-analyzer`, `gopls`, and `clangd` when they are already available on `PATH`.

## Use

Ask Pi to review a diff, selected files, or a notebook. You can also run the reviewer directly:

```sh
cd /path/to/project
python3 ~/.pi/agent/skills/code-review-ide/scripts/review.py
python3 ~/.pi/agent/skills/code-review-ide/scripts/review.py --staged
python3 ~/.pi/agent/skills/code-review-ide/scripts/review.py --range main...HEAD
python3 ~/.pi/agent/skills/code-review-ide/scripts/review.py --compact-diff
python3 ~/.pi/agent/skills/code-review-ide/scripts/review.py --files src/app.py analysis.ipynb
```

Diff reviews show complete changed text files by default, including unchanged lines before, between, and after edits. This applies to working-tree, staged, and revision-range diffs. Unchanged files are not included. Full context uses Git's signed 32-bit maximum of 2,147,483,647 context lines.

Use `--compact-diff` only when you want the previous three context lines around each change. It also works with `--staged` and `--range` and has no effect on `--files` or rendered notebooks.

The command stays open until the browser review is submitted or cancelled, then prints one JSON result to standard output.

## Test

```sh
python3 -m unittest scripts/test_review.py
node --check web/review.js
```

## License

MIT. The bundled Pygments distribution retains its own license under `vendor/pygments-2.21.0.dist-info/licenses/`.
