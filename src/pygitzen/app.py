from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container, ScrollableContainer
from textual.widgets import Footer, Header, ListItem, ListView, Static, DataTable
from textual.reactive import reactive
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual import work
from rich.text import Text
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel

from .git_service import GitService, BranchInfo, CommitInfo, FileStatus

# Try to import Cython version for better performance
try:
    from git_service_cython import GitServiceCython
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False
    GitServiceCython = None

class StatusPane(Static):
    """Status pane showing current branch and repo info."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Status"
    
    def update_status(self, branch: str, repo_path: str) -> None:
        from rich.text import Text
        repo_name = repo_path.split('/')[-1]
        status_text = Text()
        status_text.append("✓ ", style="green")
        status_text.append(f"{repo_name} → {branch}", style="white")
        self.update(status_text)


class StagedPane(ListView):
    """Staged Changes pane showing files with staged changes."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Staged Changes"
        self.show_cursor = False
    
    def update_files(self, files: list[FileStatus]) -> None:
        """Update the staged files list."""
        self.clear()
        
        # Filter only staged files
        staged_files = [
            f for f in files
            if f.staged and f.status in ["modified", "staged", "deleted", "renamed", "copied", "submodule"]
        ]
        
        if not staged_files:
            from rich.text import Text
            text = Text()
            text.append("No staged files", style="dim white")
            self.append(ListItem(Static(text)))
            return
        
        for file_status in staged_files:
            from rich.text import Text
            text = Text()
            
            # Add status indicator based on Git standard status letters
            if file_status.status == "modified":
                text.append("M ", style="green")  # Modified and staged
            elif file_status.status == "staged":
                text.append("A ", style="green")  # Added/staged
            elif file_status.status == "deleted":
                text.append("D ", style="red")  # Deleted and staged
            elif file_status.status == "renamed":
                text.append("R ", style="blue")  # Renamed and staged
            elif file_status.status == "copied":
                text.append("C ", style="blue")  # Copied and staged
            elif file_status.status == "submodule":
                text.append("S ", style="cyan")  # Submodule change and staged
            else:
                text.append("  ", style="white")
            
            # Add file path
            text.append(file_status.path, style="white")
            self.append(ListItem(Static(text)))


class ChangesPane(ListView):
    """Changes pane showing files with unstaged changes."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Changes"
        self.show_cursor = False
    
    def update_files(self, files: list[FileStatus]) -> None:
        """Update the unstaged files list."""
        self.clear()
        
        # Filter only unstaged files
        unstaged_files = []
        for f in files:
            # Include files with unstaged changes
            if f.unstaged:
                unstaged_files.append(f)
            # Include files that are not staged but have changes
            elif not f.staged and f.status in ["modified", "untracked", "deleted"]:
                unstaged_files.append(f)
        
        if not unstaged_files:
            from rich.text import Text
            text = Text()
            text.append("No changed files", style="dim white")
            self.append(ListItem(Static(text)))
            return
        
        for file_status in unstaged_files:
            from rich.text import Text
            text = Text()
            
            # Add status indicator based on Git standard status letters
            if file_status.status == "modified":
                text.append("M ", style="yellow")  # Modified but not staged
            elif file_status.status == "untracked":
                text.append("U ", style="cyan")  # Untracked
            elif file_status.status == "deleted":
                text.append("D ", style="red")  # Deleted but not staged
            elif file_status.status == "ignored":
                text.append("! ", style="magenta")  # Ignored
            else:
                text.append("  ", style="white")
            
            # Add file path
            text.append(file_status.path, style="white")
            self.append(ListItem(Static(text)))


class BranchesPane(ListView):
    """Branches pane showing local branches."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Local branches"
    
    def set_branches(self, branches: list[BranchInfo], current_branch: str) -> None:
        self.clear()
        for branch in branches:
            from rich.text import Text
            text = Text()
            if branch.name == current_branch:
                text.append("* ", style="green")
                text.append(branch.name, style="white")
            else:
                text.append("  ", style="white")
                text.append(branch.name, style="white")
            
            item = ListItem(Static(text))
            if branch.name == current_branch:
                item.add_class("current-branch")
            self.append(item)


class CommitsPane(ListView):
    """Commits pane showing commit history."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Commits"
        self._parent_app = None  # Will be set by parent
        self._last_index = None  # Track index changes
        self._last_highlighted = None  # Track highlighted changes

    def set_branch(self, branch: str) -> None:
        """Update title to show which branch commits are displayed."""
        self.border_title = f"Commits ({branch})"
    
    def watch_index(self, index: int | None) -> None:
        """Watch for index changes and auto-update patch panel."""
        self._update_patch_for_index(index)
        self._update_highlighting(index)
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) and auto-update patch panel."""
        # Arrow keys update highlighted, update patch
        if highlighted is not None:
            self._update_patch_for_index(highlighted)
            self._update_highlighting(highlighted)
    
    def _update_highlighting(self, index: int | None) -> None:
        """Update visual highlighting by adding/removing classes."""
        # Remove highlight from previous item
        if self._last_highlighted is not None and self._last_highlighted < len(self.children):
            try:
                item = self.children[self._last_highlighted]
                if isinstance(item, ListItem):
                    item.remove_class("highlighted-commit")
            except:
                pass
        
        # Add highlight to current item
        if index is not None and index < len(self.children):
            try:
                item = self.children[index]
                if isinstance(item, ListItem):
                    item.add_class("highlighted-commit")
                    self._last_highlighted = index
            except:
                pass
    
    def _update_patch_for_index(self, index: int | None) -> None:
        """Update patch panel for the given index."""
        if index is not None and index != self._last_index and self._parent_app:
            self._last_index = index
            self._parent_app.selected_commit_index = index
            self._parent_app.show_commit_diff(index)
    
    def set_commits(self, commits: list[CommitInfo]) -> None:
        self.clear()
        self._last_highlighted = None  # Reset highlighting tracker
        for commit in commits:
            from rich.text import Text
            short_sha = commit.sha[:8]
            author_short = commit.author.split('<')[0].strip()
            
            text = Text()
            text.append(short_sha, style="cyan")
            text.append(" ", style="white")
            
            # Show push status
            if commit.pushed:
                text.append("✓ ", style="green")  # Pushed to remote
            else:
                text.append("↑ ", style="yellow")  # Not pushed (local only)
            
            # Wrap long commit messages
            summary = commit.summary
            if len(summary) > 50:  # Adjust this threshold as needed
                # Split long messages into multiple lines
                words = summary.split()
                lines = []
                current_line = ""
                for word in words:
                    if len(current_line + " " + word) <= 50:
                        current_line += (" " + word) if current_line else word
                    else:
                        if current_line:
                            lines.append(current_line)
                        current_line = word
                if current_line:
                    lines.append(current_line)
                
                # Add the wrapped text
                for i, line in enumerate(lines):
                    if i > 0:
                        text.append("\n     ", style="white")  # Indent continuation lines
                    text.append(line, style="white")
            else:
                text.append(summary, style="white")
            
            self.append(ListItem(Static(text)))

    def append_commits(self, commits: list[CommitInfo]) -> None:
        for commit in commits:
            from rich.text import Text
            short_sha = commit.sha[:8]
            author_short = commit.author.split('<')[0].strip()
            
            text = Text()
            text.append(short_sha, style="cyan")
            text.append(" ", style="white")
            
            if commit.pushed:
                text.append("✓ ", style="green")
            else:
                text.append("↑ ", style="yellow")
            
            summary = commit.summary
            if len(summary) > 50:
                words = summary.split()
                lines = []
                current_line = ""
                for word in words:
                    if len(current_line + " " + word) <= 50:
                        current_line += (" " + word) if current_line else word
                    else:
                        if current_line:
                            lines.append(current_line)
                        current_line = word
                if current_line:
                    lines.append(current_line)
                for i, line in enumerate(lines):
                    if i > 0:
                        text.append("\n     ", style="white")
                    text.append(line, style="white")
            else:
                text.append(summary, style="white")
            
            self.append(ListItem(Static(text)))


class StashPane(Static):
    """Stash pane showing stashed changes."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Stash"
    
    def update_stash(self, stash_count: int) -> None:
        from rich.text import Text
        text = Text()
        text.append(f"-{stash_count} of {stash_count}-", style="white")
        self.update(text)


class PatchPane(Static):
    """Patch pane showing commit details and diff."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Patch"
    
    def show_commit_info(self, commit: CommitInfo, diff_text: str) -> None:
        from rich.text import Text
        from rich.console import Console
        from rich.syntax import Syntax
        from rich.console import Group
        from datetime import datetime
        
        # Format timestamp as human-readable date (matching Git format)
        commit_datetime = datetime.fromtimestamp(commit.timestamp)
        from time import timezone
        # Calculate timezone offset in hours
        offset_seconds = -timezone if timezone else 0
        offset_hours = offset_seconds // 3600
        offset_sign = '+' if offset_hours >= 0 else '-'
        offset_abs = abs(offset_hours)
        offset_str = f"{offset_sign}{offset_abs:02d}00"
        commit_date = commit_datetime.strftime(f"%a %b %d %H:%M:%S %Y {offset_str}")
        
        # Create commit header
        header_text = f"""commit {commit.sha}
Author: {commit.author}
Date: {commit_date}

{commit.summary}

"""
        
        # Create diff content with proper colors
        if diff_text:
            try:
                # Use Rich syntax highlighting for diff
                syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
                # Use Group to combine Text and Syntax objects
                full_content = Group(
                    Text(header_text, style="white"),
                    syntax
                )
            except:
                # Fallback to manual color formatting with Text only
                lines = diff_text.split('\n')
                diff_text_obj = Text()
                for line in lines:
                    if line.startswith('+'):
                        diff_text_obj.append(line + '\n', style="green")
                    elif line.startswith('-'):
                        diff_text_obj.append(line + '\n', style="red")
                    elif line.startswith('@@'):
                        diff_text_obj.append(line + '\n', style="blue")
                    else:
                        diff_text_obj.append(line + '\n', style="white")
                
                # Now we can concatenate Text objects
                full_content = Text(header_text, style="white") + diff_text_obj
        else:
            # Both are Text objects, so concatenation works
            full_content = Text(header_text, style="white") + Text(diff_text or "No diff available", style="white")
        
        self.update(full_content)


class CommandLogPane(Static):
    """Command log pane showing tips and messages."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Command log"
    
    def update_log(self, message: str) -> None:
        from rich.text import Text
        text = Text()
        text.append("You can hide/focus this panel by pressing '@'\n", style="white")
        text.append("Random tip: ", style="white")
        text.append("`git commit`", style="cyan")
        text.append(" is really just the programmer equivalent of saving your game.\n", style="white")
        text.append("Always do it before embarking on an ambitious change!\n", style="white")
        text.append(message, style="white")
        self.update(text)


class PygitzenApp(App):
    CSS = """
    Screen {
        layout: vertical;
        background: #1e1e1e;
    }
    
    Header {
        dock: top;
        height: 3;
        background: #2d2d2d;
        color: white;
    }
    
    Footer {
        dock: bottom;
        height: 3;
        background: #2d2d2d;
        color: white;
    }
    
    #main-container {
        height: 1fr;
        layout: horizontal;
    }
    
    #left-column {
        width: 50%;
        height: 1fr;
        layout: vertical;
    }
    
    #right-column {
        width: 1fr;
        height: 1fr;
        layout: vertical;
    }
    
    #status-pane {
        height: 3;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #status-pane:focus {
        border: solid green;
    }
    
    #files-container {
        height: 5;
        layout: horizontal;
    }
    
    #staged-pane {
        height: 5;
        width: 1fr;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
        scrollbar-size: 1 1;
    }
    
    #staged-pane:focus {
        border: solid green;
    }
    
    #changes-pane {
        height: 5;
        width: 1fr;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
        scrollbar-size: 1 1;
    }
    
    #changes-pane:focus {
        border: solid green;
    }
    
    #branches-pane {
        height: 4;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #branches-pane:focus {
        border: solid green;
    }
    
    #commits-pane {
        height: 1fr;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #commits-pane:focus {
        border: solid green;
    }
    
    #stash-pane {
        height: 3;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #stash-pane:focus {
        border: solid green;
    }
    
    #patch-scroll-container {
        height: 1fr;
        border: solid white;
        overflow: auto;
        scrollbar-size: 1 1;
    }
    
    #patch-scroll-container:focus {
        border: solid green;
    }
    
    #patch-pane {
        background: #1e1e1e;
        min-height: 100%;
    }
    
    #command-log-pane {
        height: 6;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #command-log-pane:focus {
        border: solid green;
    }
    
    ListItem.current-branch {
        background: #404040;
        color: white;
    }
    
    ListItem.--highlight {
        background: #404040;
        color: white;
    }
    
    ListItem:focus {
        background: #404040;
        color: white;
    }
    
    ListItem.--highlight:focus {
        background: #505050;
        color: white;
    }
    
    ListItem {
        background: #1e1e1e;
        color: #cccccc;
        height: auto;
        min-height: 1;
    }
    
    /* Selected/highlighted item styling for commits pane */
    #commits-pane ListItem.--highlight {
        background: #357ABD; /* blue for strong contrast */
        color: #ffffff;
        text-style: bold;
    }
    
    #commits-pane ListItem.--highlight:focus {
        background: #2f6aa3; /* slightly darker when focused */
        color: #ffffff;
        text-style: bold;
    }
    
    #commits-pane ListItem.highlighted-commit {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }
    
    #commits-pane ListItem.highlighted-commit:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
    }

    /* Selected/highlighted item styling for branches pane */
    #branches-pane ListItem.--highlight {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }
    
    #branches-pane ListItem.--highlight:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
    }
    
    #files-pane ListItem {
        height: 1;
        min-height: 1;
    }
    
    #files-pane ListItem.--highlight {
        background: #505050;
        color: white;
    }
    
    #files-pane ListItem.--highlight:focus {
        background: #606060;
        color: white;
    }
    
    Panel {
        padding: 1;
        background: #1e1e1e;
    }
    
    Static {
        background: #1e1e1e;
        color: #cccccc;
        text-align: left;
    }

    /* Ensure highlighted list items show blue background and readable text */
    #commits-pane ListItem.--highlight > Static {
        background: transparent;
        color: #ffffff;
    }
    #commits-pane ListItem.highlighted-commit > Static {
        background: transparent;
        color: #ffffff;
    }
    #branches-pane ListItem.--highlight > Static {
        background: transparent;
        color: #ffffff;
    }
    
    ListView {
        background: #1e1e1e;
        scrollbar-color: #404040 #1e1e1e;
        scrollbar-size: 1 1;
    }
    
    /* Custom scrollbar styling for LazyGit-like appearance */
    ScrollBar {
        background: #1e1e1e;
        color: #404040;
        width: 1;
    }
    
    ScrollBar:hover {
        background: #404040;
    }
    
    ScrollBarCorner {
        background: #1e1e1e;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("j", "down", "Down"),
        Binding("k", "up", "Up"),
        Binding("h", "left", "Left"),
        Binding("l", "right", "Right"),
        Binding("@", "toggle_command_log", "Toggle Command Log"),
        Binding("space", "select", "Select"),
        Binding("enter", "select", "Select"),
        Binding("c", "checkout", "Checkout"),
        Binding("b", "branch", "Branch"),
        Binding("s", "stash", "Stash"),
        Binding("+", "load_more", "More"),
    ]

    active_branch: reactive[str | None] = reactive(None)
    selected_commit_index: reactive[int] = reactive(0)

    def __init__(self, repo_dir: str = ".", use_cython: bool = True) -> None:
        super().__init__()
        from dulwich.errors import NotGitRepository
        try:
            # self.git = GitService(repo_dir)
            # Use Cython version if available and requested, otherwise use Python version
            if use_cython and CYTHON_AVAILABLE:
                self.git = GitServiceCython(repo_dir)
                self.git_python = self.git  # Use Cython for file operations too (now optimized!)
                self._using_cython = True
            else:
                self.git = GitService(repo_dir)
                self.git_python = self.git  # Same instance
                self._using_cython = False
            self.branches: list[BranchInfo] = []
            self.commits: list[CommitInfo] = []
            self.repo_path = repo_dir
            self.page_size = 200
            self.total_commits = 0
            self.loaded_commits = 0
            self._loading_commits = False
            self._loading_file_status = False
        except NotGitRepository:
            # Re-raise to be handled by run_textual()
            raise

    def compose(self) -> ComposeResult:
        yield Header()
        
        with Container(id="main-container"):
            with Container(id="left-column"):
                self.status_pane = StatusPane(id="status-pane")
                self.staged_pane = StagedPane(id="staged-pane")
                self.changes_pane = ChangesPane(id="changes-pane")
                self.branches_pane = BranchesPane(id="branches-pane")
                self.commits_pane = CommitsPane(id="commits-pane")
                self.stash_pane = StashPane(id="stash-pane")
                
                yield self.status_pane
                
                # Side-by-side containers for Staged and Changes panes
                with Horizontal(id="files-container"):
                    yield self.staged_pane
                    yield self.changes_pane
                
                yield self.branches_pane
                yield self.commits_pane
                yield self.stash_pane
            
            with Container(id="right-column"):
                with ScrollableContainer(id="patch-scroll-container"):
                    self.patch_pane = PatchPane(id="patch-pane")
                    yield self.patch_pane
                self.command_log_pane = CommandLogPane(id="command-log-pane")
                yield self.command_log_pane
        
        yield Footer()

    def on_mount(self) -> None:
        # Set parent app reference for commits pane
        self.commits_pane._parent_app = self
        # self.refresh_data()
        self.refresh_data_fast()

    def action_refresh(self) -> None:
        # self.refresh_data()
        self.refresh_data_fast()

    def action_down(self) -> None:
        if self.commits_pane.has_focus:
            # CommitsPane watches index changes and auto-updates patch
            # Update both index and highlighted for visual consistency
            current_index = self.commits_pane.index
            if current_index is not None and current_index < len(self.commits) - 1:
                new_index = current_index + 1
                self.commits_pane.index = new_index
                self.commits_pane.highlighted = new_index
                # Auto-load more when near the end of loaded commits
                if new_index >= len(self.commits) - 5:
                    self.load_more_commits()
        elif self.branches_pane.has_focus:
            # Get current selection and move down
            current_index = self.branches_pane.index
            if current_index is not None and current_index < len(self.branches) - 1:
                self.branches_pane.index = current_index + 1
                self.branches_pane.highlighted = current_index + 1
                # Auto-update commits for the new branch
                if current_index + 1 < len(self.branches):
                    self.active_branch = self.branches[current_index + 1].name
                    # self.load_commits(self.active_branch)
                    # self.update_status_info()
                    self.load_commits_fast(self.active_branch)
                    # Update status pane immediately
                    if self.active_branch:
                        self.status_pane.update_status(self.active_branch, self.repo_path)
                    # Load heavy operations in background
                    self.load_commits_count_background(self.active_branch)
                    self.load_file_status_background()

    def action_up(self) -> None:
        if self.commits_pane.has_focus:
            # CommitsPane watches index changes and auto-updates patch
            # Update both index and highlighted for visual consistency
            current_index = self.commits_pane.index
            if current_index is not None and current_index > 0:
                new_index = current_index - 1
                self.commits_pane.index = new_index
                self.commits_pane.highlighted = new_index
        elif self.branches_pane.has_focus:
            # Get current selection and move up
            current_index = self.branches_pane.index
            if current_index is not None and current_index > 0:
                self.branches_pane.index = current_index - 1
                self.branches_pane.highlighted = current_index - 1
                # Auto-update commits for the new branch
                if current_index - 1 >= 0:
                    self.active_branch = self.branches[current_index - 1].name
                    # self.load_commits(self.active_branch)
                    # self.update_status_info()
                    self.load_commits_fast(self.active_branch)
                    # Update status pane immediately
                    if self.active_branch:
                        self.status_pane.update_status(self.active_branch, self.repo_path)
                    # Load heavy operations in background
                    self.load_commits_count_background(self.active_branch)
                    self.load_file_status_background()

    def action_toggle_command_log(self) -> None:
        """Toggle command log pane visibility."""
        if self.command_log_pane.styles.display == "none":
            self.command_log_pane.styles.display = "block"
        else:
            self.command_log_pane.styles.display = "none"

    def refresh_data_fast(self) -> None:
        """Load UI immediately with minimal data (fast, non-blocking)."""
        # Preserve current branch selection before refreshing
        previous_branch = self.active_branch
        
        # Load branches immediately (fast, ~0.1s)
        self.branches = self.git.list_branches()
        if self.branches:
            # Try to restore the previous branch selection if it still exists
            if previous_branch:
                # Check if previous branch still exists in the list
                branch_names = [b.name for b in self.branches]
                if previous_branch in branch_names:
                    # Restore the previous branch
                    self.active_branch = previous_branch
                    # Update BranchesPane selection to match
                    branch_index = branch_names.index(previous_branch)
                    self.branches_pane.set_branches(self.branches, self.active_branch)
                    # Ensure BranchesPane ListView selection matches (set after list is populated)
                    self.branches_pane.index = branch_index
                    self.branches_pane.highlighted = branch_index
                else:
                    # Branch was deleted, fall back to first branch
                    self.active_branch = self.branches[0].name
                    self.branches_pane.set_branches(self.branches, self.active_branch)
                    self.branches_pane.index = 0
                    self.branches_pane.highlighted = 0
            else:
                # No previous branch, use first branch
                self.active_branch = self.branches[0].name
                self.branches_pane.set_branches(self.branches, self.active_branch)
                self.branches_pane.index = 0
                self.branches_pane.highlighted = 0

            # Load first page of commits immediately (fast, ~0.02s)
            # Don't block on count_commits - load it in background
            self.load_commits_fast(self.active_branch)
            
            # Update status pane immediately (fast)
            if self.active_branch:
                self.status_pane.update_status(self.active_branch, self.repo_path)
            
            # Show loading placeholders for file status
            self.staged_pane.update_files([])
            self.changes_pane.update_files([])
            from rich.text import Text
            loading_text = Text("Loading file status...", style="dim white")
            self.staged_pane.append(ListItem(Static(loading_text)))
            self.changes_pane.append(ListItem(Static(loading_text)))
            
            # Load heavy operations in background (non-blocking)
            # Store branch for background workers
            self._pending_branch = self.active_branch
            self.load_commits_count_background(self.active_branch)
            self.load_file_status_background()

    def refresh_data(self) -> None:
        # Preserve current branch selection before refreshing
        previous_branch = self.active_branch
        self.branches = self.git.list_branches()
        if self.branches:
            # Try to restore the previous branch selection if it still exists
            if previous_branch:
                # Check if previous branch still exists in the list
                branch_names = [b.name for b in self.branches]
                if previous_branch in branch_names:
                    # Restore the previous branch
                    self.active_branch = previous_branch
                    # Update BranchesPane selection to match
                    branch_index = branch_names.index(previous_branch)
                    self.branches_pane.set_branches(self.branches, self.active_branch)
                    # Ensure BranchesPane ListView selection matches (set after list is populated)
                    self.branches_pane.index = branch_index
                    self.branches_pane.highlighted = branch_index
                else:
                    # Branch was deleted, fall back to first branch
                    self.active_branch = self.branches[0].name
                    self.branches_pane.set_branches(self.branches, self.active_branch)
                    self.branches_pane.index = 0
                    self.branches_pane.highlighted = 0
            else:
                # No previous branch, use first branch
                self.active_branch = self.branches[0].name
                self.branches_pane.set_branches(self.branches, self.active_branch)
                self.branches_pane.index = 0
                self.branches_pane.highlighted = 0

            
            self.load_commits(self.active_branch)
            self.update_status_info()

    def update_status_info(self) -> None:
        """Update status pane with current branch info."""
        if self.active_branch:
            self.status_pane.update_status(self.active_branch, self.repo_path)
        
        # Update staged and changes panes with actual file status
        try:
            # files = self.git.get_file_status()
            files = self.git_python.get_file_status()

            # Filter out files that are up to date with the branch (no changes)
            files_with_changes = [
                f for f in files
                if f.staged or f.unstaged or f.status in ["modified", "staged", "untracked", "deleted", "renamed", "copied"]
            ]
            self.staged_pane.update_files(files_with_changes)
            self.changes_pane.update_files(files_with_changes)
        except Exception as e:
            # If file status detection fails, show empty
            self.staged_pane.update_files([])
            self.changes_pane.update_files([])
        
        # Update branches pane
        if self.branches:
            self.branches_pane.set_branches(self.branches, self.active_branch)
        
        # Update stash pane (simplified - just show placeholder)
        self.stash_pane.update_stash(0)
        
        # Update command log
        # self.command_log_pane.update_log("Repository refreshed successfully!")
        # Update command log
        version_info = " (Cython)" if self._using_cython else " (Python)"
        self.command_log_pane.update_log(f"Repository refreshed successfully!{version_info}")

    def load_commits_fast(self, branch: str) -> None:
        """Load first page of commits immediately (fast, non-blocking)."""
        # Update Commits pane title to show which branch
        self.commits_pane.set_branch(branch)
        
        # Load first page immediately (fast, ~0.02s)
        # Don't block on count_commits - load it in background
        self.commits = self.git.list_commits(branch, max_count=self.page_size, skip=0)
        self.loaded_commits = len(self.commits)
        
        # Show placeholder count (will be updated when count loads)
        self.total_commits = 0  # Will be updated in background
        self.commits_pane.set_commits(self.commits)
        self._update_commits_title()  # Use helper to show "..." when count is 0
        
        if self.commits:
            self.selected_commit_index = 0
            # Reset the last index tracker so the first commit shows
            self.commits_pane._last_index = None
            # Ensure the ListView selection and highlighting match our index
            self.commits_pane.index = 0
            self.commits_pane.highlighted = 0
            # Apply highlighting to first item
            self.commits_pane._update_highlighting(0)
            
            self.show_commit_diff(0)
    
    def load_commits_count_background(self, branch: str) -> None:
        """Load commit count in background (non-blocking)."""
        if self._loading_commits:
            return
        self._loading_commits = True
        
        # Start the async handler (Textual will handle async execution)
        # Use Textual's async support - schedule the async function
        import asyncio
        async def run_worker():
            await self._handle_commit_count_worker(branch)
        
        # Get the event loop from Textual
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(run_worker())
        except RuntimeError:
            # If no event loop, schedule it for next event loop iteration
            self.set_timer(0.0, lambda: asyncio.create_task(run_worker()))
    
    async def _handle_commit_count_worker(self, branch: str) -> None:
        """Handle commit count worker completion."""
        @work(exclusive=True, thread=True)
        def count_commits() -> int:
            try:
                count = self.git.count_commits(branch)
                return count if count > 0 else 0
            except Exception as e:
                # Log error but don't crash
                print(f"Error counting commits: {e}")
                return 0
        
        try:
            worker = count_commits()
            count = await worker
            # Only update if we got a valid count
            if count > 0:
                self.total_commits = count
                self._update_commits_title()
            else:
                # If count is 0, try to get it synchronously as fallback
                try:
                    count = self.git.count_commits(branch)
                    if count > 0:
                        self.total_commits = count
                        self._update_commits_title()
                except Exception:
                    pass  # Keep showing "..." if we can't get count
            self._loading_commits = False
        except Exception as e:
            # If worker fails, try synchronous fallback
            print(f"Worker error: {e}")
            try:
                count = self.git.count_commits(branch)
                if count > 0:
                    self.total_commits = count
                    self._update_commits_title()
            except Exception:
                pass  # Keep showing "..." if we can't get count
            self._loading_commits = False
    
    def load_file_status_background(self) -> None:
        """Load file status in background (non-blocking)."""
        if self._loading_file_status:
            return
        
        self._loading_file_status = True
        
        # Use a thread to load files asynchronously without blocking the UI
        # This ensures commits can display immediately while files load in background
        import threading
        
        def load_files_in_thread():
            """Load files in background thread (non-blocking)."""
            try:
                files = self.git_python.get_file_status()
                # Filter out files that are up to date with the branch (no changes)
                files_with_changes = [
                    f for f in files
                    if f.staged or f.unstaged or f.status in ["modified", "staged", "untracked", "deleted", "renamed", "copied"]
                ]
                
                # Update UI from main thread (Textual requires UI updates on main thread)
                # Use call_from_thread to safely update UI from background thread
                try:
                    self.call_from_thread(self._update_file_status_ui, files_with_changes)
                except (AttributeError, RuntimeError):
                    # Fallback: schedule update on next event loop iteration
                    self.set_timer(0.0, lambda: self._update_file_status_ui(files_with_changes))
            except Exception as e:
                # Log error to file
                try:
                    with open("debug_file_status.log", "a") as f:
                        f.write(f"Error loading file status: {e}\n")
                        import traceback
                        f.write(traceback.format_exc())
                except:
                    pass
                
                # Update UI from main thread on error
                try:
                    self.call_from_thread(self._update_file_status_ui, [])
                except (AttributeError, RuntimeError):
                    self.set_timer(0.0, lambda: self._update_file_status_ui([]))
        
        # Start thread immediately - doesn't block UI
        thread = threading.Thread(target=load_files_in_thread, daemon=True)
        thread.start()
    
    def _update_file_status_ui(self, files_with_changes: list) -> None:
        """Update file status UI (called from main thread)."""
        try:
            # Clear loading placeholder
            self.staged_pane.clear()
            self.changes_pane.clear()
            
            # Update with actual files
            self.staged_pane.update_files(files_with_changes)
            self.changes_pane.update_files(files_with_changes)
            
            self._loading_file_status = False
            
            # Update command log
            version_info = " (Cython)" if self._using_cython else " (Python)"
            file_count = len(files_with_changes)
            self.command_log_pane.update_log(f"Repository refreshed successfully!{version_info} ({file_count} files)")
        except Exception as e:
            # Log error to file
            try:
                with open("debug_file_status.log", "a") as f:
                    f.write(f"Error updating file status UI: {e}\n")
                    import traceback
                    f.write(traceback.format_exc())
            except:
                pass
            
            # Show empty on error
            self.staged_pane.clear()
            self.changes_pane.clear()
            self.staged_pane.update_files([])
            self.changes_pane.update_files([])
            self._loading_file_status = False

    def load_commits(self, branch: str) -> None:
         # Update Commits pane title to show which branch
        self.commits_pane.set_branch(branch)
        # Reset paging and load first page
        self.total_commits = self.git.count_commits(branch)
        self.commits = self.git.list_commits(branch, max_count=self.page_size, skip=0)
        self.loaded_commits = len(self.commits)
        self.commits_pane.set_commits(self.commits)
        self._update_commits_title()
        if self.commits:
            self.selected_commit_index = 0
            # Reset the last index tracker so the first commit shows
            self.commits_pane._last_index = None
            # Ensure the ListView selection and highlighting match our index
            self.commits_pane.index = 0
            self.commits_pane.highlighted = 0
            # Apply highlighting to first item
            self.commits_pane._update_highlighting(0)
            self.show_commit_diff(0)

    def _update_commits_title(self) -> None:
        if self.active_branch:
            self.commits_pane.border_title = f"Commits ({self.active_branch}) {len(self.commits)} of {self.total_commits}"

    def load_more_commits(self) -> None:
        if not self.active_branch:
            return
        if self.loaded_commits >= self.total_commits:
            return
        next_batch = self.git.list_commits(self.active_branch, max_count=self.page_size, skip=self.loaded_commits)
        if not next_batch:
            return
        self.commits.extend(next_batch)
        self.loaded_commits = len(self.commits)
        self.commits_pane.append_commits(next_batch)
        self._update_commits_title()

    def show_commit_diff(self, index: int) -> None:
        if 0 <= index < len(self.commits):
            ci = self.commits[index]
            diff = self.git.get_commit_diff(ci.sha)
            self.patch_pane.show_commit_info(ci, diff)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is self.branches_pane:
            index = event.index
            if 0 <= index < len(self.branches):
                self.active_branch = self.branches[index].name
                self.load_commits(self.active_branch)
                self.update_status_info()
        elif event.list_view is self.commits_pane:
            self.selected_commit_index = event.index
            self.show_commit_diff(event.index)

    def action_load_more(self) -> None:
        self.load_more_commits()


def run_textual(repo_dir: str = ".") -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from dulwich.errors import NotGitRepository
    
    try:
        app = PygitzenApp(repo_dir)
        app.run()
    except NotGitRepository:
        console = Console()
        message = Text()
        message.append("The directory you specified is not a Git repository.\n", style="yellow")
        message.append(f"\nPath: ", style="dim")
        message.append(f"{repo_dir}", style="cyan")
        message.append("\n\nPlease navigate to a directory that contains a ", style="dim")
        message.append(".git", style="cyan")
        message.append(" folder, or initialize a new Git repository:\n", style="dim")
        message.append("\n  git init", style="green")
        
        panel = Panel(
            message,
            title="[bold red]❌ Git Repository Not Found[/bold red]",
            border_style="red",
            padding=(1, 2),
        )
        console.print(panel)
        raise SystemExit(1)


