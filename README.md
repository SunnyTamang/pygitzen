# pygitzen

**PyPI version** Python 3.9+ **License:** MIT **Code style:** black

**A Python-Native LazyGit-like TUI for Git Repositories** - Navigate and manage Git repositories with a beautiful terminal-based interface, built entirely in Python.

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/pygitzen)](https://pypi.org/project/pygitzen/)

## Features

* **Pure Python**: Built with Textual (TUI) and Rich (formatting), powered by dulwich (Git)
* **No External Dependencies**: Reads directly from `.git` directory without requiring `git` CLI
* **Beautiful TUI**: Modern terminal interface with color-coded file status and syntax highlighting
* **VSCode-like Display**: Separate Staged Changes and Changes panels, matching VSCode Source Control
* **Branch Navigation**: Switch between branches, view branch-specific commits
* **Real-time Status**: See which commits are pushed to remote (✓) or local only (↑)
* **File Filtering**: Automatically excludes files matching `.gitignore` patterns
* **Smart Display**: Only shows files with actual changes (not up-to-date files)
* **Human-readable Dates**: Commit dates formatted like Git (e.g., "Mon Jan 20 14:23:26 2025 +0800")
* **Root Commit Support**: Properly displays initial commits with all files as additions

## Quick Start

### Installation

```bash
pip install pygitzen
```

Or install from source:

```bash
git clone https://github.com/SunnyTamang/pygitzen.git
cd pygitzen
pip install -e .
```

### Basic Usage

Navigate to any Git repository and run:

```bash
pygitzen
```

That's it! The TUI will automatically detect the repository and display:
- **Status**: Current branch information
- **Staged Changes**: Files with staged modifications (green indicators)
- **Changes**: Files with unstaged modifications (yellow indicators)
- **Branches**: List of all local branches
- **Commits**: Commit history for the selected branch
- **Patch**: Diff view of the selected commit

## Key Bindings

### Navigation
- `j` / `k` or `↑` / `↓`: Navigate up and down in lists
- `h` / `l` or `←` / `→`: Navigate left and right between panes
- `Enter` / `Space`: Select item
- `r`: Refresh repository data

### Actions
- `c`: Checkout branch (when branch is selected)
- `q`: Quit application
- `@`: Toggle command log panel

## Features Explained

### File Status Indicators

Files are displayed with Git-standard status letters matching VSCode Source Control:

- **M** (green): Modified and staged
- **M** (yellow): Modified but not staged
- **A** (green): Added/staged (new file)
- **U** (cyan): Untracked
- **D** (red): Deleted
- **R** (blue): Renamed
- **C** (blue): Copied
- **S** (cyan): Submodule change
- **!** (magenta): Ignored

Files with both staged and unstaged changes appear in both panels:
- **Staged Changes** panel: Shows `M` (green) for the staged version
- **Changes** panel: Shows `M` (yellow) for the unstaged version

### Commit Status Indicators

- **✓** (green): Commit is pushed to remote
- **↑** (yellow): Commit is local only (not pushed)

### Panel Structure

```
┌─────────────────────────────────────┐
│ Status                              │
├─────────────────────────────────────┤
│ Staged Changes │    Changes         │
├─────────────────┬───────────────────┤
│ A test.py       │ M simple_training │
│ M other.py      │ U new_file.py     │
└─────────────────┴───────────────────┘
│ Branches                            │
│ * main                               │
│   test_branch                        │
├─────────────────────────────────────┤
│ Commits (main)                      │
│ abc1234f ✓ Initial commit           │
│ def5678g ↑ New feature              │
└─────────────────────────────────────┘
│ Patch                                │
│ [Commit diff displayed here]        │
└─────────────────────────────────────┘
```

### Branch-Specific Commits

When you select a branch, only commits unique to that branch are displayed. Shared commits with the base branch (main/master) are filtered out.

Example:
- **main**: Shows all commits from main
- **test_branch**: Shows only commits in test_branch that aren't in main

### Smart File Filtering

The application automatically:
- Filters out files matching `.gitignore` patterns
- Only shows files with actual changes (not up-to-date with branch)
- Respects VSCode-style display (files appear in appropriate sections)

## Installation Details

### Requirements

- Python 3.9 or higher
- Git repository (any repository with `.git` directory)

### Dependencies

- **textual**: Terminal UI framework
- **rich**: Terminal formatting and syntax highlighting
- **dulwich**: Pure-Python Git library

All dependencies are automatically installed with pygitzen.

## Development

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/SunnyTamang/pygitzen.git
cd pygitzen

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in development mode
pip install -e .

# Install development dependencies (if any)
pip install -e ".[dev]"
```

### Running Tests

```bash
python test_installation.py
```

### Project Structure

```
pygitzen/
├── src/
│   └── pygitzen/
│       ├── __init__.py          # Package initialization
│       ├── __main__.py          # CLI entry point
│       ├── app.py               # Main Textual application
│       └── git_service.py       # Git operations with dulwich
├── setup.py                     # Setup script
├── pyproject.toml              # Package configuration
├── MANIFEST.in                 # Package manifest
├── LICENSE                     # MIT License
└── README.md                   # This file
```

## Technical Details

### How It Works

pygitzen uses **dulwich** to read directly from the `.git` directory:

1. **Repository Detection**: Automatically finds the Git repository root by traversing up the directory tree
2. **Index Reading**: Reads `.git/index` to get staged files
3. **Object Store**: Accesses `.git/objects` to read commits, trees, and blobs
4. **Ref Management**: Reads `.git/refs` to list branches and track remote status
5. **Status Detection**: Compares working directory, index, and HEAD to determine file status

### Advantages of dulwich

- **No External Calls**: Doesn't require `git` CLI to be installed
- **Pure Python**: Cross-platform compatibility
- **Direct Access**: Reads Git internals directly
- **Fast**: Efficient object access

## Limitations

- Some edge cases (rebase/merge in-progress) may not be fully handled yet
- Remote operations (push/pull) would require Git CLI (not yet implemented)
- Large repositories might be slower to load initially

## Contributing

We welcome contributions! Here's how you can help:

1. **Report Issues**: Found a bug? Open an issue on GitHub
2. **Suggest Features**: Have an idea? Share it in discussions
3. **Submit Pull Requests**: Fixed a bug or added a feature? PRs are welcome!

### Development Workflow

```bash
# Fork the repository
# Create a feature branch
git checkout -b feature/your-feature

# Make your changes
# Test thoroughly
python test_installation.py

# Commit and push
git commit -m "Add your feature"
git push origin feature/your-feature

# Open a Pull Request on GitHub
```

## Roadmap

For planned features and improvements, see `ROADMAP.md` (not tracked in git).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

* **[Textual](https://github.com/Textualize/textual)** - Beautiful TUI framework
* **[Rich](https://github.com/Textualize/rich)** - Terminal formatting and syntax highlighting
* **[dulwich](https://www.dulwich.io/)** - Pure-Python Git implementation
* **LazyGit** - Inspiration for the TUI design
* **VSCode** - Design inspiration for file status display

## Support

- **Documentation**: See this README and inline code documentation
- **Issues**: [GitHub Issues](https://github.com/SunnyTamang/pygitzen/issues)
- **Repository**: [GitHub Repository](https://github.com/SunnyTamang/pygitzen)

## Examples

### Viewing Commit History

```bash
# Navigate to your Git repository
cd /path/to/your/repo

# Launch pygitzen
pygitzen

# Use arrow keys or j/k to navigate commits
# Select a commit to view its diff in the Patch panel
```

### Switching Branches

```bash
# In pygitzen:
# 1. Navigate to Branches pane (use Tab or click)
# 2. Use arrow keys to select a branch
# 3. Press Enter or select to switch
# 4. Commits pane updates to show that branch's commits
```

### Checking File Status

The application automatically shows:
- **Staged Changes**: Files ready to be committed (left panel)
- **Changes**: Files with modifications not yet staged (right panel)
- Files matching `.gitignore` are automatically excluded

## Future Enhancements

See `ROADMAP.md` for detailed feature plans, including:
- Stage/unstage files interactively
- Create commits from the TUI
- Push/pull to/from remote
- Branch creation and deletion
- Stash management
- And much more!

---

**Made with ❤️ for the python community**

## About

**pygitzen** - A Python-native terminal UI for Git, inspired by LazyGit, built with Textual and dulwich.

For more information, visit: [https://github.com/SunnyTamang/pygitzen](https://github.com/SunnyTamang/pygitzen)
