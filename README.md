# pygitzen

A Python-native LazyGit-like TUI for navigating Git repositories.

- UI: Textual + Rich
- Git backend: dulwich (no external `git` required for core features)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

From any Git repository directory:

```bash
pygitzen
```

Key bindings:
- q: quit
- r: refresh
- Up/Down or j/k: navigate lists
- Enter: select

## Notes
- Uses dulwich to read `.git` directly. Some edge cases (rebase/merge in-progress) may not yet be handled.
- Initial version shows branches, latest commits, and diffs between selected commit and its parent.

