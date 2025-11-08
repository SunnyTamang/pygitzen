from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text
from rich.table import Table
from rich import box

from .app import run_textual


def print_help() -> None:
    """Print a beautiful help message using Rich."""
    console = Console()
    
    # Create title text
    title = Text()
    title.append("pygitzen", style="bold cyan")
    title.append(" - ", style="dim white")
    title.append("Python-native LazyGit-like TUI", style="white")
    
    # Create description
    description = Text()
    description.append("A beautiful terminal-based Git client built with Python.\n", style="white")
    description.append("Navigate and manage your Git repositories with an intuitive TUI interface.\n\n", style="dim white")
    
    # Create usage section
    usage_text = Text()
    usage_text.append("Usage:\n", style="bold yellow")
    usage_text.append("  pygitzen", style="cyan")
    usage_text.append(" [", style="dim white")
    usage_text.append("OPTIONS", style="yellow")
    usage_text.append("] [", style="dim white")
    usage_text.append("PATH", style="yellow")
    usage_text.append("]\n\n", style="dim white")
    
    # Create arguments table
    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED, padding=(0, 2))
    table.add_column("Argument", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")
    
    table.add_row(
        "[cyan]PATH[/cyan]",
        "[white]Path to a Git repository[/white]\n[dim]Defaults to current directory (.)[/dim]"
    )
    table.add_row(
        "[cyan]--no-cython[/cyan]",
        "[white]Force Python-only mode[/white]\n[dim]Disables Cython extension (useful for testing)[/dim]"
    )
    table.add_row(
        "[cyan]-h, --help[/cyan]",
        "[white]Show this help message and exit[/white]"
    )
    
    # Create examples section
    examples_text = Text()
    examples_text.append("Examples:\n", style="bold yellow")
    examples_text.append("  ", style="dim white")
    examples_text.append("pygitzen", style="cyan")
    examples_text.append("                    ", style="dim white")
    examples_text.append("# Launch in current directory\n", style="dim white")
    examples_text.append("  ", style="dim white")
    examples_text.append("pygitzen", style="cyan")
    examples_text.append(" /path/to/repo      ", style="dim white")
    examples_text.append("# Launch in specific repository\n", style="dim white")
    examples_text.append("  ", style="dim white")
    examples_text.append("pygitzen", style="cyan")
    examples_text.append(" --no-cython        ", style="dim white")
    examples_text.append("# Force Python-only mode\n", style="dim white")
    
    # Create footer
    footer_text = Text()
    footer_text.append("Tip: ", style="bold white")
    footer_text.append("Navigate with ", style="white")
    footer_text.append("j/k", style="cyan")
    footer_text.append(" or arrow keys. Press ", style="white")
    footer_text.append("q", style="cyan")
    footer_text.append(" to quit.\n", style="white")
    footer_text.append("\n", style="dim white")
    footer_text.append("Documentation: ", style="bold white")
    footer_text.append("https://github.com/SunnyTamang/pygitzen", style="cyan underline")
    
    # Print all sections with proper formatting
    console.print(title, justify="center")
    console.print()
    console.print(description, justify="center")
    console.print()
    console.print(usage_text)
    console.print(table)
    console.print()
    console.print(examples_text)
    console.print()
    console.print(footer_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pygitzen",
        description="Python-native LazyGit-like TUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False  # We'll handle help ourselves
    )
    parser.add_argument("path", nargs="?", default=".", help="Path to a Git repository (defaults to current directory)")
    parser.add_argument(
        "--no-cython",
        action="store_true",
        help="Force Python-only mode (disable Cython extension, useful for testing)"
    )
    parser.add_argument(
        "-h", "--help",
        action="store_true",
        help="Show this help message and exit"
    )
    
    args = parser.parse_args()
    
    # Handle help
    if args.help:
        print_help()
        sys.exit(0)

    repo_path = Path(args.path).resolve()
    run_textual(str(repo_path), use_cython=not args.no_cython)


if __name__ == "__main__":
    main()


