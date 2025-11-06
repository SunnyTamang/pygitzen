# pygitzen

**A Python-native Terminal-Based Git Client** - Navigate and manage your Git repositories with a beautiful TUI interface inspired by LazyGit.

## What is pygitzen?

pygitzen is a terminal-based user interface (TUI) for Git repositories. Think of it as a Python-native alternative to LazyGit. It provides a visual, intuitive way to navigate your Git repositories directly in the terminal, without needing external Git CLI tools.

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

## Installation

```bash
pip install pygitzen
```

### Requirements

- Python 3.9 or higher
- A Git repository

**Note for source installations**: If you're installing from source (when no pre-built wheel is available for your platform), pip will automatically install Cython during the build process. No manual steps required!

## Quick Start

1. **Navigate to any Git repository**:
   ```bash
   cd /path/to/your/git/repo
   ```

2. **Launch pygitzen**:
   ```bash
   pygitzen
   ```

That's it! pygitzen will automatically detect the Git repository and display your repository status.

## Where to Use pygitzen

pygitzen is perfect for:
- **Local Development**: Quickly see what files you've changed, what's staged, and review commits
- **Remote Servers**: Works great over SSH - no GUI needed
- **Code Reviews**: Browse commit history and view diffs in the terminal
- **Branch Management**: See branch-specific commits and push status
- **File Tracking**: Monitor staged and unstaged changes side-by-side (VSCode-style)

## Interface Overview

pygitzen displays your Git repository in a multi-panel interface:

### Left Column

1. **Status Panel**: Current branch name and repository information
2. **Staged Changes**: Files with staged changes (green indicators - M, A, D)
3. **Changes**: Files with unstaged modifications (yellow indicators - M, U)
4. **Branches**: List of all local branches - select to switch branches
5. **Commits**: Commit history for the selected branch
6. **Stash**: Placeholder for stashed changes (coming soon)

### Right Column

7. **Patch Panel**: Shows commit diff when a commit is selected
8. **Command Log**: Tips and helpful messages

## Keyboard Shortcuts

### Navigation
- **j** / **↓**: Move down
- **k** / **↑**: Move up
- **h** / **←**: Move left
- **l** / **→**: Move right
- **Enter**: Select item
- **Tab**: Cycle through panels

### Actions
- **r**: Refresh repository data
- **q**: Quit application
- **@**: Toggle Command Log panel

### Focus Navigation
- Click on a panel to focus it
- Focused panels have green borders

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
| **✓** | Pushed | Green | Commit exists on remote |
| **↑** | Unpushed | Yellow | Commit is local only |

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

## Key Features Explained

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

### Auto-Updating Patch Panel

The Patch panel automatically updates when you navigate commits:
- Use arrow keys or j/k to navigate
- Patch panel shows diff immediately (no need to press Enter)
- Visual highlighting shows which commit is selected

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

## Support

* **Issues**: [GitHub Issues](https://github.com/SunnyTamang/pygitzen/issues)
* **Repository**: [GitHub](https://github.com/SunnyTamang/pygitzen)

## License

This project is licensed under the MIT License.

---

**Made with ❤️ for developers who love terminal UIs**

