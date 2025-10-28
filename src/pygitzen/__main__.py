from __future__ import annotations

import argparse
from pathlib import Path

from .app import run_textual


def main() -> None:
    parser = argparse.ArgumentParser(prog="pygitzen", description="Python-native LazyGit-like TUI")
    parser.add_argument("path", nargs="?", default=".", help="Path to a Git repository (defaults to current directory)")
    args = parser.parse_args()

    repo_path = Path(args.path).resolve()
    run_textual(str(repo_path))


if __name__ == "__main__":
    main()


