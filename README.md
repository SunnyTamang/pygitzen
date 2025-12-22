# pygitzen

[![PyPI version](https://img.shields.io/pypi/v/pygitzen.svg?color=blue)](https://pypi.org/project/pygitzen/)
[![Python version](https://img.shields.io/pypi/pyversions/pygitzen.svg)](https://pypi.org/project/pygitzen/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Downloads](https://img.shields.io/pypi/dm/pygitzen.svg?color=brightgreen)](https://pypi.org/project/pygitzen/)
[![Build](https://img.shields.io/github/actions/workflow/status/SunnyTamang/pygitzen/publish-pypi.yml?label=build&logo=github)](https://github.com/SunnyTamang/pygitzen/actions)
[![Status](https://img.shields.io/pypi/status/pygitzen.svg)](https://pypi.org/project/pygitzen/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/pygitzen?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/pygitzen)


**A Python-native Terminal-Based Git Client** - Navigate and manage your Git repositories with a beautiful TUI interface inspired by LazyGit.

<img width="1706" height="1225" alt="Screenshot 2025-11-02 at 11 59 31 AM" src="https://github.com/user-attachments/assets/45a54289-46db-4cc3-91a7-e9847c70cf02" />

## Features

* **Terminal-Based UI**: Beautiful TUI interface built with Textual and Rich
* **Pure Python**: Uses dulwich library - no external `git` CLI required for core operations
* **Real-Time Updates**: Live view of your Git repository status
* **Multi-Panel Interface**: Status, Staged Changes, Changes, Branches, Commits, Patch, Stash, and Command Log
* **Branch-Aware**: Shows commits specific to the selected branch
* **Push Status**: Visual indicators for commits pushed/unpushed to remote
* **File Status Detection**: Automatically detects modified, staged, untracked, and deleted files
* **Gitignore Support**: Respects `.gitignore` rules automatically
* **Dark Theme**: Easy-on-the-eyes color scheme with focus highlighting
* **Keyboard Navigation**: Efficient vim-style navigation (j/k, h/l)
* **Auto-Refresh**: Patch panel updates automatically when navigating commits
* **Root Commit Support**: Shows all files in initial commits

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

The TUI will automatically detect the repository and display your Git status.

## Keyboard Shortcuts

### Navigation
- **j** / **↓**: Move down
- **k** / **↑**: Move up
- **h** / **←**: Move left
- **l** / **→**: Move right
- **Enter**: Select item

### Actions
- **r**: Refresh repository data
- **q**: Quit application
- **@**: Toggle Command Log panel

### Focus Navigation
- **Tab**: Cycle through panels
- Click on a panel to focus it
- Focused panels have green borders

## Customizing Keybindings

pygitzen supports custom keybindings via a configuration file. You can override default keybindings or add new ones.

### Configuration File Location

- **macOS/Linux**: `~/.config/pygitzen/keybindings.toml`
- **Windows**: `%APPDATA%\pygitzen\keybindings.toml`

### Configuration Format

Create a TOML file with the following structure:

```toml
[app]
# App-level keybindings
s = "refresh"        # Change refresh key from 'r' to 's'
"@" = "quit"         # Change '@' key to quit (quoted for special chars)
t = "quit"           # Add 't' as another quit key

[panes.branches]
# Branches pane keybindings
x = "delete_branch"  # Change delete key from 'd' to 'x'
c = "checkout"       # Override checkout key (same as default)

[panes.commits]
# Commits pane keybindings
c = "checkout"

[panes.stash]
# Stash pane keybindings
# (add bindings here)

[panes.tags]
# Tags pane keybindings
# (add bindings here)

[panes.remotes]
# Remotes pane keybindings
# (add bindings here)
```

### How Keybinding Merge Works

pygitzen uses a **hybrid merge approach**:

1. **If your key exists in defaults** (key-based override):
   - The key's action is changed to your specified action
   - ⚠️ **Important**: The old action becomes unbound (no key triggers it)
   - Example: If `'s'` defaults to `'stash'` and you set `'s' = "refresh"`, then `'stash'` becomes unbound

2. **If your key is new** (action-based replace):
   - The default binding with that action is replaced with your new key
   - The old key is removed
   - Example: If `'r'` defaults to `'refresh'` and you set `'t' = "refresh"`, then `'r'` is removed and `'t'` does refresh

3. **If your key and action are both new**:
   - A new binding is added

### Important Notes

- **Unbound Actions**: When you override an existing key, the original action may become unbound. Make sure to rebind it if you still want to use that action.
- **Multiple Keys per Action**: You can have multiple keys trigger the same action (e.g., both `'q'` and `'t'` can do `'quit'`).
- **Special Characters**: Use quotes for special characters like `"@"`, `"+"`, `"space"`, `"enter"`.
- **Restart Required**: Changes take effect after restarting pygitzen.
- **No Auto-Creation**: The config file is not created automatically. You must create it manually if you want custom keybindings.

### Example: Replacing Refresh Key

To change the refresh key from `'r'` to `'s'`:

```toml
[app]
s = "refresh"
```

This will:
- Remove `'r'` → `'refresh'` binding
- Add `'s'` → `'refresh'` binding
- Keep all other defaults unchanged

### Example: Overriding Existing Key

To change what `'s'` does (from `'stash'` to `'refresh'`):

```toml
[app]
s = "refresh"
```

This will:
- Change `'s'` from `'stash'` to `'refresh'`
- ⚠️ **Warning**: `'stash'` action becomes unbound (no key triggers it)
- You'll need to bind `'stash'` to another key if you want to use it

### Default Keybindings

#### App-Level
- `q` → `quit`
- `r` → `refresh`
- `j` → `down` (hidden from footer)
- `k` → `up` (hidden from footer)
- `h` → `left` (hidden from footer)
- `l` → `right` (hidden from footer)
- `@` → `toggle_command_log`
- `space` → `select`
- `enter` → `select`
- `c` → `checkout`
- `b` → `branch`
- `s` → `stash`
- `+` → `load_more`
- `g` → `toggle_graph_style`

#### Branches Pane
- `c` → `checkout`
- `space` → `select`
- `enter` → `select`
- `n` → `new_branch`
- `d` → `delete_branch`
- `r` → `rename_branch`
- `m` → `merge_branch`
- `p` → `push_branch`
- `u` → `set_upstream`

#### Commits Pane
- `c` → `checkout`
- `space` → `select`
- `enter` → `select`

#### Other Panes
- `space` → `select`
- `enter` → `select`

### Troubleshooting

- **Config not loading**: Make sure the file path is correct and TOML syntax is valid
- **Changes not applying**: Restart pygitzen after editing the config file
- **Action not working**: Check if the action became unbound (see "Unbound Actions" above)
- **Invalid key format**: Use quotes for special characters (`"@"`, `"+"`, `"space"`, `"enter"`)

For more details on implementing a UI for keybinding configuration, see `KEYBINDING_UI_DESIGN.md`.

## Interface Overview

pygitzen displays your Git repository in a multi-panel interface:

### Left Column

1. **Status Panel**
   - Current branch name
   - Repository name
   - Visual status indicator

2. **Staged Changes Panel**
   - Files with staged changes (green M, A, D)
   - Shows files ready to be committed
   - Status indicators: M (modified), A (added), D (deleted), R (renamed), C (copied)

3. **Changes Panel**
   - Files with unstaged changes (yellow M, U)
   - Shows working directory changes
   - Status indicators: M (modified), U (untracked), D (deleted)

4. **Branches Panel**
   - List of all local branches
   - Current branch highlighted with `*`
   - Select to view branch-specific commits

5. **Commits Panel**
   - Commit history for the selected branch
   - Shows only commits unique to the branch (excludes shared history)
   - Push status: ✓ (green) = pushed, ↑ (yellow) = local only
   - Auto-updates Patch panel when navigating

6. **Stash Panel**
   - Placeholder for stashed changes
   - (Feature coming soon)

### Right Column

7. **Patch Panel**
   - Shows commit diff when a commit is selected
   - Syntax-highlighted diff view
   - Commit header with author, date, and message
   - Scrollable for long diffs

8. **Command Log Panel**
   - Tips and helpful messages
   - Toggle with `@` key

## File Status Indicators

pygitzen uses Git-standard status letters:

| Letter | Meaning | Color | Description |
|--------|---------|-------|-------------|
| **M** | Modified | Green (staged) / Yellow (unstaged) | File changed since last commit |
| **A** | Added | Green | File added to staging area |
| **U** | Untracked | Cyan | New file not yet added to Git |
| **D** | Deleted | Red | File deleted but change not yet committed |
| **R** | Renamed | Blue | File was renamed or moved |
| **C** | Copied | Blue | File was copied from another tracked file |
| **!** | Ignored | Magenta | File is ignored by .gitignore |
| **S** | Submodule | Cyan | Submodule change |
| **✓** | Pushed | Green | Commit exists on remote |
| **↑** | Unpushed | Yellow | Commit is local only |

## Features Explained

### Branch-Specific Commits

When you select a branch, pygitzen shows **only commits unique to that branch**. This means:
- On `main`: Shows all commits from main
- On `feature-branch`: Shows only commits that don't exist in main (unique to the branch)

This makes it easy to see what's new in your feature branch without scrolling through shared history.

### Push Status

Each commit displays its push status:
- **✓** (green): Commit has been pushed to remote
- **↑** (yellow): Commit exists only locally

This helps you track which commits need to be pushed.

### Gitignore Support

pygitzen automatically respects `.gitignore` rules:
- Untracked files matching gitignore patterns are not shown
- Files already tracked are shown even if in gitignore (matching Git behavior)

### Auto-Updating Patch Panel

The Patch panel automatically updates when you navigate commits:
- Use arrow keys or j/k to navigate
- Patch panel shows diff immediately (no need to press Enter)
- Visual highlighting shows which commit is selected

### Focus Indicators

Panels with focus have green borders, making it clear which panel you're interacting with:
- White borders: Not focused
- Green borders: Currently focused

## Examples

### Viewing Commit History

1. Launch pygitzen in a Git repository
2. Navigate to **Commits** panel (use Tab or click)
3. Use **j/k** or arrow keys to navigate commits
4. **Patch** panel automatically shows the diff for selected commit

### Switching Branches

1. Navigate to **Branches** panel
2. Use **j/k** or arrow keys to select a branch
3. Press **Enter** or click to switch
4. **Commits** panel updates to show branch-specific commits

### Monitoring File Changes

- **Staged Changes** panel shows files ready to commit (green indicators)
- **Changes** panel shows files with unstaged modifications (yellow indicators)
- Files with both staged and unstaged changes appear in **both** panels (VSCode-style)

## Project Structure

```
pygitzen/
├── src/
│   └── pygitzen/
│       ├── __init__.py       # Package initialization
│       ├── __main__.py        # CLI entry point
│       ├── app.py             # Main Textual application
│       └── git_service.py     # Git operations using dulwich
├── examples/                  # Example repositories (if any)
├── tests/                     # Test suite
├── pyproject.toml            # Package configuration
├── setup.py                   # Setup script
├── MANIFEST.in                # Package manifest
├── LICENSE                    # MIT License
└── README.md                  # This file
```

## Technical Details

### Dependencies

- **Textual**: Modern TUI framework for Python
- **Rich**: Rich text and beautiful formatting
- **dulwich**: Pure-Python Git implementation

### How It Works

pygitzen reads directly from the `.git` directory using dulwich:
- No external `git` CLI calls required
- Direct access to Git objects, refs, and index
- Fast and efficient for most operations

### Limitations

* Some advanced Git operations (rebase, merge in-progress) may not be fully handled yet
* Large repositories with thousands of commits may be slower
* Some edge cases in complex Git workflows may need refinement

## Development

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/SunnyTamang/pygitzen.git
cd pygitzen

# Install in development mode
pip install -e .

# Run the application
pygitzen
```

### Running Tests

```bash
python test_installation.py
```

### Project Configuration

Configuration is managed through `pyproject.toml`:
- Package metadata
- Dependencies
- Entry points
- Build settings

## Contributing

We welcome contributions! Here's how you can help:

1. **Report Issues**: Found a bug? Open an issue on GitHub
2. **Suggest Features**: Have an idea? Share it in Discussions
3. **Submit Pull Requests**: Improvements and bug fixes are appreciated!

### Development Guidelines

- Follow Python style guidelines (black formatter)
- Add tests for new features
- Update documentation for significant changes
- Keep commits focused and well-described

## Roadmap

See `ROADMAP.md` for planned features and improvements (local file, not tracked in Git).

Upcoming features include:
- Stage/unstage files interactively
- Commit changes with message input
- Push/pull operations
- Stash management
- Branch creation and deletion
- And more!

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

* **[Textual](https://github.com/Textualize/textual)** - Beautiful TUI framework
* **[Rich](https://github.com/Textualize/rich)** - Rich text and terminal formatting
* **[dulwich](https://github.com/jelmer/dulwich)** - Pure-Python Git implementation
* **[LazyGit](https://github.com/jesseduffield/lazygit)** - Inspiration for the UI design

## Support

* **Documentation**: This README and inline code comments
* **Issues**: [GitHub Issues](https://github.com/SunnyTamang/pygitzen/issues)
* **Repository**: [GitHub](https://github.com/SunnyTamang/pygitzen)

## Status

**Current Version**: 0.2.0 (Beta)

This is a beta version of pygitzen. Core features are working and stable. Additional functionality is being added based on user feedback. See the roadmap for upcoming features.

---

**Made with ❤️ for developers who love terminal UIs**
