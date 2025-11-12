from __future__ import annotations

import time
import queue
from functools import wraps

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container, ScrollableContainer
from textual.widgets import Footer, Header, ListItem, ListView, Static, DataTable, Input
from textual.reactive import reactive
from textual import events
from textual.binding import Binding
from textual.message import Message
from rich.text import Text
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel

from .git_service import GitService, BranchInfo, CommitInfo, FileStatus

# Performance timing utilities
_TIMING_LOG_FILE = None
_TIMING_LOG_PATH = "timing.log"

def _get_timing_log_file():
    """Get or create timing log file handle."""
    global _TIMING_LOG_FILE
    if _TIMING_LOG_FILE is None:
        try:
            _TIMING_LOG_FILE = open(_TIMING_LOG_PATH, "a", encoding="utf-8")
        except Exception:
            # If we can't open the file, return None and timing will be skipped
            pass
    return _TIMING_LOG_FILE

def _log_timing_message(message: str):
    """Log timing message to file (non-blocking, won't interfere with TUI)."""
    try:
        log_file = _get_timing_log_file()
        if log_file:
            log_file.write(f"{message}\n")
            log_file.flush()  # Ensure it's written immediately
        else:
            # Fallback: try to write directly if file handle creation failed
            try:
                with open(_TIMING_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"{message}\n")
            except Exception:
                pass  # Silently fail if logging doesn't work
    except Exception as e:
        # Log error to stderr for debugging (only if file logging fails)
        try:
            import sys
            print(f"[TIMING LOG ERROR] {e}", file=sys.stderr)
        except Exception:
            pass

def log_timing(operation_name: str):
    """Decorator to log timing for operations."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                elapsed = time.perf_counter() - start_time
                _log_timing_message(f"[TIMING] {operation_name}: {elapsed:.4f}s")
                return result
            except Exception as e:
                elapsed = time.perf_counter() - start_time
                _log_timing_message(f"[TIMING] {operation_name} (ERROR): {elapsed:.4f}s - {type(e).__name__}: {e}")
                raise
        return wrapper
    return decorator

def log_timing_sync(operation_name: str, *args, **kwargs):
    """Context manager for timing operations."""
    start_time = time.perf_counter()
    return start_time

def log_timing_end(operation_name: str, start_time: float):
    """End timing and log result."""
    elapsed = time.perf_counter() - start_time
    _log_timing_message(f"[TIMING] {operation_name}: {elapsed:.4f}s")

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
        
        # Debug: Log what we received and filtered
        try:
            with open("debug_changes_pane.log", "a", encoding="utf-8") as f:
                f.write(f"[DEBUG] ChangesPane.update_files: received {len(files)} files, filtered to {len(unstaged_files)} unstaged\n")
                for file_status in files[:5]:  # Log first 5
                    f.write(f"  {file_status.path}: status={file_status.status}, staged={file_status.staged}, unstaged={file_status.unstaged}\n")
        except:
            pass
        
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
        
        # Virtual scrolling: limit initial commits to 200 for performance
        # ListView has built-in virtual scrolling, but we still need to limit initial DOM elements
        initial_limit = 200
        commits_to_render = commits[:initial_limit] if len(commits) > initial_limit else commits
        
        for commit in commits_to_render:
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


class CommitSearchInput(Input):
    """Search input for filtering commits by message."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.placeholder = "Search commits... (fuzzy search)"
        self.border_title = "Search"


class LogPane(Static):
    """Log pane showing commit graph/log for a branch."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Log"
        # Cache for incremental updates
        self._cached_commits: list[CommitInfo] = []
        self._cached_branch: str = ""
        self._cached_branch_info: dict = {}
        self._cached_commit_refs_map: dict = {}
        self._last_render_time = 0.0
        self._pending_update = False
        self._pending_branch_info: dict = {}
        self._pending_git_service = None
        # Track loaded commits for pagination
        self._loaded_commits_count = 0
        self._total_commits_count = 0
        # Virtual scrolling: track how many commits to render
        # DISABLED FOR TESTING: Set to very large number to render all commits
        self._max_rendered_commits = 999999  # Render all commits (no limit for testing)
        import time
        self._time = time
    
    def show_branch_log(self, branch: str, commits: list[CommitInfo], branch_info: dict, git_service, append: bool = False, total_commits_count_override: int = None) -> None:
        """
        Display commit log/graph for a branch (optimized with incremental updates and debouncing).
        Only rebuilds if commits or branch changed, otherwise updates incrementally.
        
        Args:
            append: If True, append commits to existing log instead of replacing.
        """
        from rich.text import Text
        from rich.console import Group
        from datetime import datetime
        from time import timezone
        
        # Store pending updates for debouncing
        if branch_info:
            self._pending_branch_info = branch_info.copy()
        if git_service is not None:
            self._pending_git_service = git_service
        
        # Debounce: Wait 100ms before rendering to batch rapid updates
        # This allows branch_info and commit_refs to arrive before we render
        current_time = self._time.perf_counter()
        time_since_last_render = current_time - self._last_render_time
        
        # If updates are coming in rapid succession, defer rendering
        if time_since_last_render < 0.1:  # 100ms debounce window
            self._pending_update = True
            # Schedule a delayed render
            if hasattr(self, '_debounce_timer'):
                # Cancel previous timer if exists
                pass
            return
        
        # Throttle: Don't re-render more than once every 50ms (20fps max)
        if time_since_last_render < 0.05:  # 50ms throttle
            self._pending_update = True
            return
        
        # Use pending data if available
        if self._pending_branch_info:
            branch_info = self._pending_branch_info
        if self._pending_git_service is not None:
            git_service = self._pending_git_service
        
        self._pending_update = False
        self._last_render_time = current_time
        
        # If appending, merge with cached commits
        if append and self._cached_commits and self._cached_branch == branch:
            # Merge commits (avoid duplicates)
            existing_shas = {c.sha for c in self._cached_commits}
            new_commits = [c for c in commits if c.sha not in existing_shas]
            if new_commits:
                commits = self._cached_commits + new_commits
            else:
                commits = self._cached_commits  # No new commits, keep existing
        
        # CRITICAL: Store total count BEFORE limiting (needed for "more commits" message)
        # Use override if provided (for cases where commits is already limited), otherwise use len(commits)
        total_commits_count = total_commits_count_override if total_commits_count_override is not None else len(commits)
        
        # DISABLED FOR TESTING: Skip virtual scrolling limit - render all commits
        # max_rendered = self._max_rendered_commits
        # if len(commits) > max_rendered:
        #     _log_timing_message(f"[TIMING]   show_branch_log: Limiting {len(commits)} commits to {max_rendered} (virtual scroll)")
        #     commits = commits[:max_rendered]
        
        if not commits:
            empty_text = Text()
            empty_text.append(f"No commits found for branch '{branch}'", style="dim white")
            self.update(empty_text)
            self._cached_commits = []
            self._cached_branch = ""
            return
        
        # Check if we can do incremental update
        commits_changed = (
            self._cached_branch != branch or
            len(self._cached_commits) != len(commits) or
            any(cached.sha != new.sha for cached, new in zip(self._cached_commits, commits))
        )
        
        branch_info_changed = (
            self._cached_branch_info.get("remote_tracking") != branch_info.get("remote_tracking") or
            self._cached_branch_info.get("is_current") != branch_info.get("is_current")
        )
        
        # If only branch_info changed and commits are the same, we can update just the header
        if not commits_changed and branch_info_changed and git_service is None:
            # Just update header, keep commit lines
            header = self._build_header(branch, branch_info)
            # Rebuild with new header but cached commit lines
            # Pass total_commits_count so we can show "more commits" message
            log_lines = self._build_log_lines_cached(commits, git_service, branch, total_commits_count)
            log_lines[0] = header  # Replace header
            full_content = Group(*log_lines)
            self.update(full_content)
            self._cached_branch_info = branch_info.copy()
            return
        
        # Always rebuild (Rich doesn't support incremental DOM updates easily)
        # But we optimize by batching updates and throttling
        # DISABLED FOR TESTING: Don't reset virtual scrolling limit
        # if not append or self._cached_branch != branch:
        #     self._max_rendered_commits = 50  # Start with 50 rendered commits
        
        # Build log lines with virtual scrolling (only first N commits)
        # Pass total_commits_count so we can show "more commits" message
        log_lines = self._build_log_lines(commits, branch_info, git_service, branch, total_commits_count)
        
        # Update UI (this is also expensive with Rich)
        update_start = self._time.perf_counter()
        full_content = Group(*log_lines)
        self.update(full_content)
        update_elapsed = self._time.perf_counter() - update_start
        _log_timing_message(f"[TIMING]   show_branch_log update(): {update_elapsed:.4f}s")
        
        # Update cache
        self._cached_commits = commits.copy()
        self._cached_branch = branch
        self._cached_branch_info = branch_info.copy()
        if git_service and hasattr(git_service, 'refs_map'):
            self._cached_commit_refs_map = git_service.refs_map.copy()
    
    def _build_header(self, branch: str, branch_info: dict) -> Text:
        """Build branch header."""
        from rich.text import Text
        header = Text()
        header.append(f"Branch: ", style="dim white")
        header.append(f"{branch}", style="cyan bold")
        
        if branch_info.get("remote_tracking"):
            header.append(f" → ", style="dim white")
            header.append(f"{branch_info['remote_tracking']}", style="yellow")
        
        if branch_info.get("is_current"):
            header.append(f" (HEAD)", style="green bold")
        
        return header
    
    def _build_log_lines(self, commits: list[CommitInfo], branch_info: dict, git_service, branch: str, total_commits_count: int = None) -> list:
        """Build log lines with virtual scrolling - only render visible commits."""
        from rich.text import Text
        import time
        
        build_start = time.perf_counter()
        log_lines = []
        
        # Branch header
        header = self._build_header(branch, branch_info)
        log_lines.append(header)
        log_lines.append(Text())  # Empty line
        
        # DISABLED FOR TESTING: Render all commits (no virtual scrolling limit)
        # max_commits_to_render = min(self._max_rendered_commits, len(commits))
        # commits_to_render = commits[:max_commits_to_render]
        commits_to_render = commits  # Render all commits
        max_commits_to_render = len(commits)  # Use full length
        
        # Use total_commits_count if provided, otherwise fall back to len(commits)
        # This allows us to show "more commits" message even when commits list is already limited
        actual_total = total_commits_count if total_commits_count is not None else len(commits)
        
        # Build commit lines (this is the expensive part)
        commit_lines_start = time.perf_counter()
        for i, commit in enumerate(commits_to_render):
            commit_line = self._build_commit_line(commit, i, actual_total, git_service, branch)
            log_lines.append(commit_line)
            log_lines.append(Text())  # Empty line between commits
        commit_lines_elapsed = time.perf_counter() - commit_lines_start
        _log_timing_message(f"[TIMING]   _build_log_lines: {commit_lines_elapsed:.4f}s ({len(commits_to_render)} commits rendered, {actual_total} total)")
        
        # Add indicator for remaining commits if there are more
        # Check against actual_total (original count) not len(commits) (which may be limited)
        if actual_total > max_commits_to_render:
            remaining = actual_total - max_commits_to_render
            placeholder = Text()
            placeholder.append(f"... ({remaining} more commits - scroll to load) ...", style="dim white")
            log_lines.append(placeholder)
        
        build_elapsed = time.perf_counter() - build_start
        _log_timing_message(f"[TIMING]   _build_log_lines TOTAL: {build_elapsed:.4f}s")
        
        return log_lines
    
    def _build_log_lines_cached(self, commits: list[CommitInfo], git_service, branch: str, total_commits_count: int = None) -> list:
        """Build log lines using cached structure (for incremental updates) - WITH virtual scrolling limit."""
        from rich.text import Text
        log_lines = []
        header = self._build_header(branch, self._cached_branch_info)
        log_lines.append(header)
        log_lines.append(Text())
        
        # DISABLED FOR TESTING: Render all commits (no virtual scrolling limit)
        # max_commits_to_render = min(self._max_rendered_commits, len(commits))
        # commits_to_render = commits[:max_commits_to_render]
        commits_to_render = commits  # Render all commits
        max_commits_to_render = len(commits)  # Use full length
        
        # Use total_commits_count if provided, otherwise fall back to len(commits)
        # This allows us to show "more commits" message even when commits list is already limited
        actual_total = total_commits_count if total_commits_count is not None else len(commits)
        
        for i, commit in enumerate(commits_to_render):
            commit_line = self._build_commit_line(commit, i, actual_total, git_service, branch)
            log_lines.append(commit_line)
            log_lines.append(Text())
        
        # Add indicator for remaining commits if there are more
        # Check against actual_total (original count) not len(commits) (which may be limited)
        if actual_total > max_commits_to_render:
            remaining = actual_total - max_commits_to_render
            placeholder = Text()
            placeholder.append(f"... ({remaining} more commits - scroll to load) ...", style="dim white")
            log_lines.append(placeholder)
        
        return log_lines
    
    def _build_commit_line(self, commit: CommitInfo, index: int, total: int, git_service, branch: str) -> Text:
        """Build a single commit line (optimized for speed - simplified styling)."""
        from rich.text import Text
        from datetime import datetime
        from time import timezone
        
        # OPTIMIZATION: Use simpler Text construction to reduce Rich overhead
        # Build as plain string first, then convert to Text with minimal styling
        short_sha = commit.sha[:8]
        
        # Graph indicator
        graph_indicator = "│ " if index < total - 1 else "  "
        
        # Build refs string (simplified)
        refs_str = ""
        if git_service is not None:
            try:
                commit_refs = git_service.get_commit_refs(commit.sha)
                refs_parts = []
                if commit_refs.get("is_head"):
                    refs_parts.append("HEAD")
                local_branches = [b for b in commit_refs.get("branches", []) if b != branch]
                if local_branches:
                    refs_parts.append(", ".join(local_branches[:2]))  # Limit to 2 branches
                remote_branches = [rb for rb in commit_refs.get("remote_branches", []) if rb.startswith("origin/")]
                if remote_branches:
                    refs_parts.append(", ".join(remote_branches[:1]))  # Limit to 1 remote
                tags = commit_refs.get("tags", [])
                if tags:
                    refs_parts.append(f"tag: {tags[0]}")  # Limit to 1 tag
                if commit_refs.get("is_merge"):
                    refs_parts.append("Merge")
                
                if refs_parts:
                    refs_str = f"({', '.join(refs_parts[:3])}) "  # Limit total refs
            except Exception:
                pass
        
        # Author and date (simplified format)
        commit_datetime = datetime.fromtimestamp(commit.timestamp)
        offset_seconds = -timezone if timezone else 0
        offset_hours = offset_seconds // 3600
        offset_sign = '+' if offset_hours >= 0 else '-'
        offset_abs = abs(offset_hours)
        offset_str = f"{offset_sign}{offset_abs:02d}00"
        commit_date = commit_datetime.strftime(f"%a %b %d %H:%M:%S %Y {offset_str}")
        
        # Build as single Text object with minimal segments (faster than many append calls)
        line1 = f"{graph_indicator}{short_sha} {refs_str}{commit.summary}"
        line2 = f"{graph_indicator}Author: {commit.author} | Date: {commit_date}"
        
        commit_line = Text()
        commit_line.append(line1, style="white")
        commit_line.append("\n", style="white")
        commit_line.append(line2, style="dim white")
        
        return commit_line


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
    
    #commit-search-input {
        height: 3;
        border: solid white;
        background: #1e1e1e;
        min-height: 3;
    }
    
    #commit-search-input:focus {
        border: solid green;
    }
    
    Input {
        color: white;
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
    
    #log-pane {
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
        import sys
        init_start = time.perf_counter()
        _log_timing_message(f"[TIMING] ===== PygitzenApp.__init__ START =====")
        
        super().__init__()
        from dulwich.errors import NotGitRepository
        try:
            # self.git = GitService(repo_dir)
            # Use Cython version if available and requested, otherwise use Python version
            if use_cython and CYTHON_AVAILABLE:
                cython_init_start = time.perf_counter()
                try:
                    self.git = GitServiceCython(repo_dir)
                    self.git_python = self.git  # Use Cython for file operations too (now optimized!)
                    self._using_cython = True
                    # Log successful Cython initialization
                    import sys
                    cython_init_elapsed = time.perf_counter() - cython_init_start
                    _log_timing_message(f"[TIMING] GitServiceCython.__init__: {cython_init_elapsed:.4f}s")
                    print(f"[DEBUG] Cython extension initialized successfully")
                except Exception as e:
                    # If Cython initialization fails, fall back to Python
                    import sys
                    import traceback
                    cython_init_elapsed = time.perf_counter() - cython_init_start
                    error_msg = f"Error initializing Cython extension, falling back to Python: {type(e).__name__}: {e}\n"
                    error_msg += f"Traceback:\n{traceback.format_exc()}\n"
                    _log_timing_message(f"[TIMING] GitServiceCython.__init__ (FAILED): {cython_init_elapsed:.4f}s")
                    _log_timing_message(error_msg)
                    try:
                        with open("debug_cython_init.log", "a", encoding="utf-8") as f:
                            f.write(error_msg)
                    except Exception:
                        pass
                    python_init_start = time.perf_counter()
                    self.git = GitService(repo_dir)
                    python_init_elapsed = time.perf_counter() - python_init_start
                    _log_timing_message(f"[TIMING] GitService.__init__ (fallback): {python_init_elapsed:.4f}s")
                    self.git_python = self.git
                    self._using_cython = False
            else:
                python_init_start = time.perf_counter()
                self.git = GitService(repo_dir)
                python_init_elapsed = time.perf_counter() - python_init_start
                _log_timing_message(f"[TIMING] GitService.__init__: {python_init_elapsed:.4f}s")
                self.git_python = self.git  # Same instance
                self._using_cython = False
            self.branches: list[BranchInfo] = []
            self.commits: list[CommitInfo] = []  # Commits for commits pane (left side)
            self.all_commits: list[CommitInfo] = []  # Store all commits for search (commits pane)
            self.log_commits: list[CommitInfo] = []  # Commits for log pane (right side) - separate from commits pane
            self.repo_path = repo_dir
            self.page_size = 200  # For commits pane
            # Reasonable limit to prevent blocking (dulwich iteration is slow for 78k+ commits)
            self.log_initial_size = 200  # Load 200 commits initially (can load more via pagination)
            self.total_commits = 0
            self.loaded_commits = 0
            self._loading_commits = False
            self._loading_file_status = False
            self._search_query: str = ""
            self._view_mode: str = "patch"  # "patch" or "log"
            
            # Thread-safe queue for UI updates from background threads
            self._ui_update_queue = queue.Queue()
            
            init_elapsed = time.perf_counter() - init_start
            _log_timing_message(f"[TIMING] ===== PygitzenApp.__init__ TOTAL: {init_elapsed:.4f}s =====")
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
                self.search_input = CommitSearchInput(id="commit-search-input")
                self.stash_pane = StashPane(id="stash-pane")
                
                yield self.status_pane
                
                # Side-by-side containers for Staged and Changes panes
                with Horizontal(id="files-container"):
                    yield self.staged_pane
                    yield self.changes_pane
                
                yield self.branches_pane
                yield self.commits_pane
                yield self.search_input
                yield self.stash_pane
            
            with Container(id="right-column"):
                with ScrollableContainer(id="patch-scroll-container"):
                    self.patch_pane = PatchPane(id="patch-pane")
                    self.log_pane = LogPane(id="log-pane")
                    # Make log_pane focusable so it can receive scroll events
                    self.log_pane.can_focus = False  # Don't need focus, just need scroll events
                    yield self.patch_pane
                    yield self.log_pane
                self.command_log_pane = CommandLogPane(id="command-log-pane")
                yield self.command_log_pane
        
        yield Footer()

    def on_mount(self) -> None:
        import sys
        mount_start = time.perf_counter()
        _log_timing_message(f"[TIMING] ===== on_mount START =====")
        
        # Set parent app reference for commits pane
        self.commits_pane._parent_app = self
        # Initialize view mode - will be set by refresh_data_fast
        self._view_mode = "log"  # Default to log view (branch view)
        # Show startup message with version info
        version_info = " (Cython)" if self._using_cython else " (Python)"
        self.command_log_pane.update_log(f"pygitzen started{version_info}")
        # self.refresh_data()
        self.refresh_data_fast()
        
        # Set up periodic check for virtual scrolling expansion (fallback if scroll events don't fire)
        # This ensures virtual scrolling works even if scroll events aren't being captured
        # Check more frequently (0.2s) for more responsive virtual scrolling
        self.set_interval(0.2, self._check_virtual_scroll_expansion)
        self.set_interval(0.2, self._check_commits_pane_scroll)  # Check commits pane scrolling
        
        # Set up periodic processing of UI update queue from background threads
        self.set_interval(0.05, self._process_ui_update_queue)  # Check every 50ms
        
        mount_elapsed = time.perf_counter() - mount_start
        _log_timing_message(f"[TIMING] ===== on_mount TOTAL: {mount_elapsed:.4f}s =====")
    
    def _process_ui_update_queue(self) -> None:
        """Process UI updates from background threads (called periodically from main thread)."""
        try:
            # Process all pending updates (non-blocking)
            while True:
                try:
                    update_func = self._ui_update_queue.get_nowait()
                    update_func()
                except queue.Empty:
                    break
        except Exception:
            pass  # Silently fail if processing errors occur
    
    def _check_commits_pane_scroll(self) -> None:
        """Periodically check if we need to load more commits in commits pane (fallback if scroll events don't fire)."""
        if not self.active_branch or self._search_query:
            return  # Don't auto-load if searching (filtering existing commits)
        
        try:
            # Get commits pane
            commits_pane = self.query_one("#commits-pane", None)
            if not commits_pane:
                return
            
            # Try to get scroll position
            scroll_y = 0
            max_scroll_y = 0
            
            if hasattr(commits_pane, 'scroll_y'):
                scroll_y = commits_pane.scroll_y
            if hasattr(commits_pane, 'max_scroll_y'):
                max_scroll_y = commits_pane.max_scroll_y
            elif hasattr(commits_pane, 'virtual_size'):
                max_scroll_y = commits_pane.virtual_size.height if hasattr(commits_pane.virtual_size, 'height') else 0
            
            # Check if we need to load more commits
            if max_scroll_y > 0 and self.total_commits > 0 and self.loaded_commits < self.total_commits:
                scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                
                # If scrolled near bottom (85%), auto-load more commits
                if scroll_percent >= 0.85:
                    _log_timing_message(f"[TIMING] [PERIODIC CHECK] Commits pane: Loading more commits (scroll_percent={scroll_percent:.2f}, loaded={self.loaded_commits}, total={self.total_commits})")
                    self.load_more_commits()
        except Exception:
            pass  # Silently fail if check fails
    
    def _check_virtual_scroll_expansion(self) -> None:
        """Periodically check if we need to expand virtual scrolling (fallback if scroll events don't fire)."""
        if self._view_mode != "log" or not self.active_branch:
            return
        
        try:
            # Get scroll container
            container = self.query_one("#patch-scroll-container", None)
            if not container:
                return
            
            # Try multiple ways to get scroll position
            scroll_y = 0
            max_scroll_y = 0
            
            # Method 1: Direct attributes
            if hasattr(container, 'scroll_y'):
                scroll_y = container.scroll_y
            if hasattr(container, 'max_scroll_y'):
                max_scroll_y = container.max_scroll_y
            
            # Method 2: Try scroll_offset and scroll_size
            if max_scroll_y <= 0 and hasattr(container, 'scroll_offset'):
                scroll_y = container.scroll_offset.y if hasattr(container.scroll_offset, 'y') else 0
            if max_scroll_y <= 0 and hasattr(container, 'scroll_size'):
                max_scroll_y = container.scroll_size.height if hasattr(container.scroll_size, 'height') else 0
            
            # Method 3: Try virtual_size and scroll_offset
            if max_scroll_y <= 0 and hasattr(container, 'virtual_size'):
                max_scroll_y = container.virtual_size.height if hasattr(container.virtual_size, 'height') else 0
                if hasattr(container, 'scroll_offset'):
                    scroll_y = container.scroll_offset.y if hasattr(container.scroll_offset, 'y') else 0
            
            # If we can't determine scroll position, skip expansion but still check if we need to load more commits
            # (max_scroll_y <= 0 means we can't calculate scroll_percent, so skip virtual scroll expansion)
            
            # CRITICAL: Use total_commits_count (from background load) if available, otherwise use len(self.log_commits)
            # This ensures we expand correctly even when only 50 commits are loaded initially
            total_commits = self.log_pane._total_commits_count if self.log_pane._total_commits_count > 0 else (len(self.log_commits) if self.log_commits else len(self.log_pane._cached_commits) if self.log_pane._cached_commits else 0)
            
            # Check if we need to load more commits (if we have more total commits than loaded)
            # OR if we've loaded more commits than we're rendering (user scrolled past rendered commits)
            needs_more_commits = (
                (self.log_pane._total_commits_count > 0 and self.log_pane._loaded_commits_count < self.log_pane._total_commits_count) or
                (self.log_pane._total_commits_count == 0 and len(self.log_commits) < 200)  # If count not loaded yet, check if we have less than initial batch
            )
            
            # If we've rendered all available commits AND we don't need more, skip
            if total_commits <= self.log_pane._max_rendered_commits and not needs_more_commits:
                return
            
            # Calculate scroll percent - if max_scroll_y is 0, assume we're at the bottom if we have more commits to load
            if max_scroll_y > 0:
                scroll_percent = scroll_y / max_scroll_y
            else:
                # If we can't determine scroll position, but we have more commits loaded than rendered,
                # assume we should load more (user might have scrolled)
                scroll_percent = 0.9 if self.log_pane._loaded_commits_count > self.log_pane._max_rendered_commits else 0
            
            # If scrolled past 60% (lower threshold for faster expansion), expand rendered range
            # This makes virtual scrolling more responsive
            if scroll_percent >= 0.6:
                new_max = min(
                    total_commits,
                    self.log_pane._max_rendered_commits + 50
                )
                if new_max > self.log_pane._max_rendered_commits:
                    _log_timing_message(f"[TIMING] [PERIODIC CHECK] Expanding virtual scroll: {self.log_pane._max_rendered_commits} -> {new_max} commits (total: {total_commits}, scroll_percent={scroll_percent:.2f})")
                    self.log_pane._max_rendered_commits = new_max
                    # Re-render with expanded range - use log_commits (for log pane)
                    commits_to_render = self.log_commits if self.log_commits else self.log_pane._cached_commits
                    if commits_to_render and self.active_branch:
                        branch_info = self.log_pane._cached_branch_info.copy() if hasattr(self.log_pane, '_cached_branch_info') and self.log_pane._cached_branch_info else {"name": self.active_branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                        git_service = None
                        if hasattr(self.log_pane, '_cached_commit_refs_map') and self.log_pane._cached_commit_refs_map:
                            class CachedGitService:
                                def __init__(self, git_service, refs_map):
                                    self.git_service = git_service
                                    self.refs_map = refs_map
                                def get_commit_refs(self, commit_sha: str):
                                    return self.refs_map.get(commit_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                            git_service = CachedGitService(self.git, self.log_pane._cached_commit_refs_map)
                        
                        # Force re-render by bypassing debounce
                        # Use total_commits_count if available for correct "more commits" message
                        total_count = self.log_pane._total_commits_count if self.log_pane._total_commits_count > 0 else len(commits_to_render)
                        self.log_pane._last_render_time = 0
                        self.log_pane.show_branch_log(
                            self.active_branch,
                            commits_to_render,
                            branch_info,
                            git_service,
                            append=False,
                            total_commits_count_override=total_count
                        )
            
            # Only load more commits if actually scrolled near bottom (85% - lower threshold for faster loading)
            # Don't load just because we have more commits - only load when user actually scrolls
            if scroll_percent >= 0.85:
                if (self.log_pane._total_commits_count == 0 or 
                    self.log_pane._loaded_commits_count < self.log_pane._total_commits_count):
                    _log_timing_message(f"[TIMING] [PERIODIC CHECK] Loading more commits (scroll_percent={scroll_percent:.2f}, loaded={self.log_pane._loaded_commits_count}, rendered={self.log_pane._max_rendered_commits}, total={self.log_pane._total_commits_count})")
                    self.load_more_commits_for_log(self.active_branch)
        except Exception as e:
            # Log exception for debugging
            import traceback
            _log_timing_message(f"[TIMING] [PERIODIC CHECK] Exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")

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
                    # Switch to log view when branch is selected
                    self._view_mode = "log"
                    self.patch_pane.styles.display = "none"
                    self.log_pane.styles.display = "block"
                    # Load commits with full history for feature branches
                    self.load_commits_for_log(self.active_branch)
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
                    # Switch to log view when branch is selected
                    self._view_mode = "log"
                    self.patch_pane.styles.display = "none"
                    self.log_pane.styles.display = "block"
                    # Load commits with full history for feature branches
                    self.load_commits_for_log(self.active_branch)
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
        total_start = time.perf_counter()
        _log_timing_message("===== refresh_data_fast START =====")
        
        # Preserve current branch selection before refreshing
        previous_branch = self.active_branch
        
        # Load branches immediately (fast, ~0.1s)
        branch_start = time.perf_counter()
        self.branches = self.git.list_branches()
        branch_elapsed = time.perf_counter() - branch_start
        _log_timing_message(f"list_branches: {branch_elapsed:.4f}s")
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
            # On initial load, show log view for the selected branch
            self._view_mode = "log"
            self.patch_pane.styles.display = "none"
            self.log_pane.styles.display = "block"
            
            log_load_start = time.perf_counter()
            self.load_commits_for_log(self.active_branch)
            log_load_elapsed = time.perf_counter() - log_load_start
            _log_timing_message(f"load_commits_for_log: {log_load_elapsed:.4f}s")
            
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
            
            total_elapsed = time.perf_counter() - total_start
            _log_timing_message(f"===== refresh_data_fast TOTAL: {total_elapsed:.4f}s =====")

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

    def _fuzzy_match(self, query: str, text: str) -> float:
        """Simple fuzzy matching algorithm. Returns a score between 0 and 1."""
        if not query:
            return 1.0
        
        query = query.lower()
        text_lower = text.lower()
        
        # Exact match gets highest score
        if query in text_lower:
            # Score based on position - earlier matches are better
            pos = text_lower.find(query)
            position_score = 1.0 - (pos / max(len(text_lower), 1)) * 0.3
            return position_score
        
        # Check if all characters in query appear in order in text
        query_idx = 0
        for char in text_lower:
            if query_idx < len(query) and char == query[query_idx]:
                query_idx += 1
        
        if query_idx == len(query):
            # All characters found in order, but not contiguous
            # Score based on how close together they are
            return 0.5
        
        # Check substring matches (partial)
        max_match = 0
        for i in range(len(text_lower) - len(query) + 1):
            match_count = 0
            for j, q_char in enumerate(query):
                if i + j < len(text_lower) and text_lower[i + j] == q_char:
                    match_count += 1
            max_match = max(max_match, match_count)
        
        if max_match > 0:
            return 0.2 * (max_match / len(query))
        
        return 0.0
    
    def _filter_commits_by_search(self, commits: list[CommitInfo], query: str) -> list[CommitInfo]:
        """Filter commits using fuzzy search on commit messages."""
        if not query or not query.strip():
            return commits
        
        query = query.strip()
        scored_commits = []
        
        for commit in commits:
            # Search in commit summary (message)
            score = self._fuzzy_match(query, commit.summary)
            # Also search in author name
            author_score = self._fuzzy_match(query, commit.author) * 0.5
            # Also search in SHA
            sha_score = self._fuzzy_match(query, commit.sha) * 0.3
            
            total_score = max(score, author_score, sha_score)
            
            if total_score > 0:
                scored_commits.append((total_score, commit))
        
        # Sort by score (highest first)
        scored_commits.sort(key=lambda x: x[0], reverse=True)
        
        # Return just the commits (without scores)
        return [commit for _, commit in scored_commits]
    
    def load_commits_for_log(self, branch: str, reset: bool = True) -> None:
        """Load commits for log view with full history (optimized - fast initial load)."""
        log_start = time.perf_counter()
        _log_timing_message(f"--- load_commits_for_log START (branch: {branch}, reset: {reset}) ---")
        
        # Update Commits pane title to show which branch (only on reset, preserve title when loading more)
        if reset:
            self.commits_pane.set_branch(branch)
        # When reset=False (loading more), don't call set_branch as it clears the count
        # The title will be preserved from the previous update
        
        # Reset pagination if this is a new branch or reset requested
        if reset or self.active_branch != branch:
            self.log_pane._loaded_commits_count = 0
            self.log_pane._total_commits_count = 0
            # DISABLED FOR TESTING: Don't reset virtual scrolling limit
            # self.log_pane._max_rendered_commits = 50
            self.log_pane._cached_commits = []  # Clear old cached commits
        
        # TESTING: Try git-native command first (has timeout), fallback to dulwich if it fails
        list_start = time.perf_counter()
        skip = self.log_pane._loaded_commits_count if not reset else 0
        max_count = self.log_initial_size if reset else self.page_size
        
        # Try git-native version first (has timeout support)
        if hasattr(self.git, 'list_commits_native'):
            try:
                loaded_commits = self.git.list_commits_native(branch, max_count=max_count, skip=skip, show_full_history=False, timeout=30)
                _log_timing_message(f"  list_commits_native (git log): used")
            except Exception as e:
                _log_timing_message(f"  list_commits_native failed, using dulwich: {e}")
                loaded_commits = self.git.list_commits(branch, max_count=max_count, skip=skip, show_full_history=False)
        else:
            # Fallback to dulwich if native method doesn't exist
            loaded_commits = self.git.list_commits(branch, max_count=max_count, skip=skip, show_full_history=False)
        list_elapsed = time.perf_counter() - list_start
        _log_timing_message(f"  list_commits (show_full_history=False, skip={skip}): {list_elapsed:.4f}s ({len(loaded_commits)} commits)")
        
        # DECOUPLED: Keep commits pane and log pane separate
        # log_commits is for the log pane (right side), commits is for commits pane (left side)
        # Both should be populated initially, but can diverge later (e.g., when scrolling loads more in log pane)
        if reset:
            self.log_commits = loaded_commits.copy()  # Store commits for log pane
            # Also populate commits pane initially (they can diverge later)
            self.all_commits = loaded_commits.copy()  # Store all commits for search
            # Apply search filter if there's a search query
            if self._search_query:
                self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
            else:
                self.commits = loaded_commits.copy()
            self.loaded_commits = len(self.commits)
            # Update commits pane
            self.commits_pane.set_commits(self.commits)
            self._update_commits_title()
        else:
            # Append new commits to log pane only (commits pane loads separately via "+" key)
            self.log_commits.extend(loaded_commits)
        
        # Update log pane loaded count
        self.log_pane._loaded_commits_count = len(self.log_commits)
        
        # Show basic log immediately (without commit refs - fast)
        basic_branch_info = {"name": branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
        show_log_start = time.perf_counter()
        try:
            self.log_pane.show_branch_log(branch, self.log_commits, basic_branch_info, None, append=not reset)  # Pass None to skip commit refs
            show_log_elapsed = time.perf_counter() - show_log_start
            _log_timing_message(f"  show_branch_log (basic, no refs, append={not reset}): {show_log_elapsed:.4f}s")
        except Exception as e:
            # Log error if show_branch_log fails
            import sys
            import traceback
            error_msg = f"Error in show_branch_log for branch {branch}: {type(e).__name__}: {e}\n"
            error_msg += f"Traceback:\n{traceback.format_exc()}\n"
            _log_timing_message(error_msg)
            try:
                with open("debug_show_log.log", "a", encoding="utf-8") as f:
                    f.write(error_msg)
            except Exception:
                pass
        
        # Don't auto-select first commit when in log view (only on reset)
        if reset:
            self.commits_pane.index = None
            self.commits_pane.highlighted = None
            self.selected_commit_index = -1
        
        # Load heavy operations in background (non-blocking)
        # For feature branches, load full history in background (only on reset)
        if reset:
            show_full = branch not in ["main", "master"]
            if show_full:
                self.load_commits_full_history_background(branch)
            
            # Load branch info in background
            self.load_branch_info_background(branch)
            
            # Load commit refs in background (for enhanced log display)
            # DISABLED FOR TESTING: Pass all commits (no virtual scrolling limit)
            if self.log_commits:
                # commits_to_fetch = self.log_commits[:max_rendered] if len(self.log_commits) > max_rendered else self.log_commits
                self.load_commit_refs_background(branch, self.log_commits)
        
        # Load total count in background if not already loaded
        if self.log_pane._total_commits_count == 0:
            self.load_commits_count_background(branch)
        
        log_elapsed = time.perf_counter() - log_start
        _log_timing_message(f"--- load_commits_for_log TOTAL: {log_elapsed:.4f}s ---")
    
    def load_more_commits_for_log(self, branch: str) -> None:
        """Load more commits for log view (pagination)."""
        if not branch:
            return
        
        # Check if we've loaded all commits
        if self.log_pane._total_commits_count > 0 and self.log_pane._loaded_commits_count >= self.log_pane._total_commits_count:
            return
        
        # Load next batch
        self.load_commits_for_log(branch, reset=False)
    
    def load_commits_fast(self, branch: str) -> None:
        """Load first page of commits immediately (fast, non-blocking)."""
        # Update Commits pane title to show which branch
        self.commits_pane.set_branch(branch)
        
        # Load first page immediately (fast, ~0.02s)
        # Don't block on count_commits - load it in background
        loaded_commits = self.git.list_commits(branch, max_count=self.page_size, skip=0)
        self.all_commits = loaded_commits.copy()  # Store all commits for search
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
        else:
            self.commits = loaded_commits
        
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
            
            # Only show patch if in patch mode
            if self._view_mode == "patch":
                self.show_commit_diff(0)
    
    def load_commits_count_background(self, branch: str) -> None:
        """Load commit count in background (non-blocking)."""
        if self._loading_commits:
            return
        self._loading_commits = True
        
        # Use a thread to count commits asynchronously without blocking the UI
        import threading
        
        def count_commits_in_thread():
            """Count commits in background thread (non-blocking)."""
            count_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] _handle_commit_count_worker START (branch: {branch})")
            try:
                count_op_start = time.perf_counter()
                count = self.git.count_commits(branch)
                count_op_elapsed = time.perf_counter() - count_op_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   count_commits: {count_op_elapsed:.4f}s (result: {count})")
                
                # Update UI from main thread (use queue instead of set_timer to avoid event loop issues)
                if count > 0:
                    # Use queue which is thread-safe and doesn't require event loop
                    branch_copy = branch
                    count_copy = count
                    self._ui_update_queue.put(lambda: self._update_commit_count_ui(branch_copy, count_copy))
                
                count_elapsed = time.perf_counter() - count_start
                _log_timing_message(f"[TIMING] [BACKGROUND] _handle_commit_count_worker TOTAL: {count_elapsed:.4f}s")
            except Exception as e:
                # Log error but don't crash
                import traceback
                error_msg = f"Error counting commits for branch {branch}: {type(e).__name__}: {e}\n"
                error_msg += f"Traceback:\n{traceback.format_exc()}\n"
                _log_timing_message(error_msg)
                count_elapsed = time.perf_counter() - count_start
                _log_timing_message(f"[TIMING] [BACKGROUND] _handle_commit_count_worker (ERROR): {count_elapsed:.4f}s")
            finally:
                self._loading_commits = False
        
        # Start thread immediately - doesn't block UI
        thread = threading.Thread(target=count_commits_in_thread, daemon=True)
        thread.start()
    
    def _update_commit_count_ui(self, branch: str, count: int) -> None:
        """Update commit count UI (called from main thread)."""
        try:
            # Only update if we're still viewing this branch
            if self.active_branch == branch and count > 0:
                self.total_commits = count
                self.log_pane._total_commits_count = count  # Update log pane count too
                self._update_commits_title()
                
                # DISABLED FOR TESTING: Re-render log view with all commits (no limit)
                if self._view_mode == "log" and self.log_commits:
                    # Re-render with all commits (use log_commits, not commits)
                    commits_to_render = self.log_commits
                    
                    # Get branch info (use cached if available)
                    branch_info = self.log_pane._cached_branch_info if hasattr(self.log_pane, '_cached_branch_info') and self.log_pane._cached_branch_info else {"name": branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                    
                    # Get git service (use cached if available)
                    git_service = None
                    if hasattr(self.log_pane, '_cached_commit_refs_map') and self.log_pane._cached_commit_refs_map:
                        class CachedGitService:
                            def __init__(self, git_service, refs_map):
                                self.git_service = git_service
                                self.refs_map = refs_map
                            def get_commit_refs(self, commit_sha: str):
                                return self.refs_map.get(commit_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                        git_service = CachedGitService(self.git, self.log_pane._cached_commit_refs_map)
                    
                    # Force re-render with correct total count
                    self.log_pane._last_render_time = 0  # Reset debounce to force immediate render
                    self.log_pane.show_branch_log(branch, commits_to_render, branch_info, git_service, total_commits_count_override=count)
        except Exception:
            pass  # Silently fail if branch changed
    
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
            import sys
            file_status_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background START")
            try:
                get_files_start = time.perf_counter()
                files = self.git_python.get_file_status()
                get_files_elapsed = time.perf_counter() - get_files_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   get_file_status: {get_files_elapsed:.4f}s ({len(files)} files)")
                # Filter out files that are up to date with the branch (no changes)
                files_with_changes = [
                    f for f in files
                    if f.staged or f.unstaged or f.status in ["modified", "staged", "untracked", "deleted", "renamed", "copied"]
                ]
                
                # Update UI from main thread (use queue instead of set_timer to avoid event loop issues)
                update_start = time.perf_counter()
                # Use queue which is thread-safe and doesn't require event loop
                files_copy = files_with_changes.copy()
                self._ui_update_queue.put(lambda: self._update_file_status_ui(files_copy))
                update_elapsed = time.perf_counter() - update_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   _update_file_status_ui (queued): {update_elapsed:.4f}s")
                
                file_status_elapsed = time.perf_counter() - file_status_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background TOTAL: {file_status_elapsed:.4f}s")
            except Exception as e:
                # Log error to file
                try:
                    with open("debug_file_status.log", "a") as f:
                        f.write(f"Error loading file status: {e}\n")
                        import traceback
                        f.write(traceback.format_exc())
                except:
                    pass
                
                # Update UI from main thread on error (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self._update_file_status_ui([]))
                file_status_elapsed = time.perf_counter() - file_status_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background (ERROR): {file_status_elapsed:.4f}s")
        
        # Start thread immediately - doesn't block UI
        thread = threading.Thread(target=load_files_in_thread, daemon=True)
        thread.start()
    
    def _update_file_status_ui(self, files_with_changes: list) -> None:
        """Update file status UI (called from main thread) - optimized for large file lists."""
        import time
        update_start = time.perf_counter()
        try:
            # OPTIMIZATION: Limit display to 500 files max (virtual scrolling)
            # Rendering 4,681 ListItems takes 4.6s - this reduces it to <0.1s
            # User can still see all files by scrolling (ListView handles it)
            display_limit = 500
            files_to_display = files_with_changes[:display_limit] if len(files_with_changes) > display_limit else files_with_changes
            
            # Clear loading placeholder
            self.staged_pane.clear()
            self.changes_pane.clear()
            
            # Update with limited files (faster initial render)
            self.staged_pane.update_files(files_to_display)
            self.changes_pane.update_files(files_to_display)
            
            # Store full list for scrolling (ListView will handle virtual scrolling)
            self._all_files_with_changes = files_with_changes
            # Don't render all files - ListView virtual scrolling will handle it
            # Only update if we have more than display_limit (show message)
            
            self._loading_file_status = False
            
            # Update command log
            version_info = " (Cython)" if self._using_cython else " (Python)"
            file_count = len(files_with_changes)
            display_count = len(files_to_display)
            if file_count > display_limit:
                self.command_log_pane.update_log(f"Repository refreshed successfully!{version_info} ({display_count}/{file_count} files shown - ListView virtual scrolling)")
            else:
                self.command_log_pane.update_log(f"Repository refreshed successfully!{version_info} ({file_count} files)")
            
            update_elapsed = time.perf_counter() - update_start
            _log_timing_message(f"[TIMING]   _update_file_status_ui (limited to {display_count}): {update_elapsed:.4f}s")
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
    
    def _update_file_status_full(self, files_with_changes: list) -> None:
        """Update file status UI with full file list - DEPRECATED: Not used anymore (virtual scrolling instead)."""
        # This method is kept for compatibility but no longer used
        # ListView handles virtual scrolling automatically, so we don't need to render all files
        pass
    
    def load_commits_full_history_background(self, branch: str) -> None:
        """Load commits with full history in background (for feature branches)."""
        import threading
        
        def load_full_history_in_thread():
            """Load full history in background thread."""
            import sys
            full_history_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_commits_full_history_background START (branch: {branch})")
            try:
                # Load commits with full history
                list_start = time.perf_counter()
                full_commits = self.git.list_commits(branch, max_count=self.page_size, skip=0, show_full_history=True)
                list_elapsed = time.perf_counter() - list_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   list_commits (show_full_history=True): {list_elapsed:.4f}s ({len(full_commits)} commits)")
                
                # Update UI from main thread (use queue instead of set_timer to avoid event loop issues)
                update_start = time.perf_counter()
                # Use queue which is thread-safe and doesn't require event loop
                branch_copy = branch
                full_commits_copy = full_commits.copy()
                self._ui_update_queue.put(lambda: self._update_commits_full_history_ui(branch_copy, full_commits_copy))
                update_elapsed = time.perf_counter() - update_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   _update_commits_full_history_ui (queued): {update_elapsed:.4f}s")
                
                full_history_elapsed = time.perf_counter() - full_history_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_commits_full_history_background TOTAL: {full_history_elapsed:.4f}s")
            except Exception as e:
                # Log error but don't crash
                import sys
                import traceback
                full_history_elapsed = time.perf_counter() - full_history_start
                error_msg = f"Error loading full history for branch {branch}: {type(e).__name__}: {e}\n"
                error_msg += f"Traceback:\n{traceback.format_exc()}\n"
                _log_timing_message(f"[TIMING] [BACKGROUND] load_commits_full_history_background (ERROR): {full_history_elapsed:.4f}s")
                _log_timing_message(error_msg)
        
        thread = threading.Thread(target=load_full_history_in_thread, daemon=True)
        thread.start()
    
    def _update_commits_full_history_ui(self, branch: str, full_commits: list) -> None:
        """Update commits with full history (called from main thread)."""
        try:
            # Only update if we're still viewing this branch
            if self.active_branch == branch and self._view_mode == "log":
                self.all_commits = full_commits.copy()
                # Apply search filter if active
                if self._search_query:
                    self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
                else:
                    self.commits = full_commits
                
                self.loaded_commits = len(self.commits)
                self.commits_pane.set_commits(self.commits)
                
                # Refresh log view with updated commits
                try:
                    branch_info = self.git.get_branch_info(branch)
                except Exception:
                    branch_info = {"name": branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                
                self.log_pane.show_branch_log(branch, self.commits, branch_info, self.git)
        except Exception:
            pass  # Silently fail if branch changed
    
    def load_branch_info_background(self, branch: str) -> None:
        """Load branch info in background and update log view."""
        import threading
        
        def load_branch_info_in_thread():
            """Load branch info in background thread."""
            import sys
            branch_info_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_branch_info_background START (branch: {branch})")
            try:
                get_info_start = time.perf_counter()
                branch_info = self.git.get_branch_info(branch)
                get_info_elapsed = time.perf_counter() - get_info_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   get_branch_info: {get_info_elapsed:.4f}s")
                
                # Update UI from main thread (use queue instead of set_timer to avoid event loop issues)
                update_start = time.perf_counter()
                # Use queue which is thread-safe and doesn't require event loop
                branch_copy = branch
                branch_info_copy = branch_info.copy()
                self._ui_update_queue.put(lambda: self._update_branch_info_ui(branch_copy, branch_info_copy))
                update_elapsed = time.perf_counter() - update_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   _update_branch_info_ui (queued): {update_elapsed:.4f}s")
                
                branch_info_elapsed = time.perf_counter() - branch_info_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_branch_info_background TOTAL: {branch_info_elapsed:.4f}s")
            except Exception as e:
                # Log error if get_branch_info fails
                import sys
                import traceback
                error_msg = f"Error in get_branch_info for branch {branch}: {type(e).__name__}: {e}\n"
                error_msg += f"Traceback:\n{traceback.format_exc()}\n"
                _log_timing_message(error_msg)
                try:
                    with open("debug_branch_info.log", "a", encoding="utf-8") as f:
                        f.write(error_msg)
                except Exception:
                    pass
                # Use empty branch info as fallback
                branch_info = {"name": branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                # Use queue which is thread-safe
                branch_copy = branch
                branch_info_copy = branch_info.copy()
                self._ui_update_queue.put(lambda: self._update_branch_info_ui(branch_copy, branch_info_copy))
        
        thread = threading.Thread(target=load_branch_info_in_thread, daemon=True)
        thread.start()
    
    def _update_branch_info_ui(self, branch: str, branch_info: dict) -> None:
        """Update log view with branch info (called from main thread) - optimized to batch with commit_refs."""
        import time
        update_start = time.perf_counter()
        try:
            # Only update if we're still viewing this branch in log mode
            if self.active_branch == branch and self._view_mode == "log" and self.log_commits:
                # OPTIMIZATION: Always cache branch info, only re-render if we have cached refs ready
                # This avoids expensive re-renders when refs aren't ready yet
                self.log_pane._cached_branch_info = branch_info.copy()
                
                # Only re-render if we have commit refs cached (batch update)
                # Otherwise, just cache the branch info and wait for refs
                if hasattr(self.log_pane, '_cached_commit_refs_map') and self.log_pane._cached_commit_refs_map:
                    # We have cached refs, create CachedGitService and render with both
                    # DISABLED FOR TESTING: Render all commits (no virtual scrolling limit)
                    commits_to_render = self.log_commits
                    
                    # Log what we're doing
                    _log_timing_message(f"[TIMING]   _update_branch_info_ui START: {len(self.log_commits)} total commits (no limit)")
                    
                    class CachedGitService:
                        def __init__(self, git_service, refs_map):
                            self.git_service = git_service
                            self.refs_map = refs_map
                        
                        def get_commit_refs(self, commit_sha: str):
                            return self.refs_map.get(commit_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                    
                    cached_git = CachedGitService(self.git, self.log_pane._cached_commit_refs_map)
                    # Virtual scrolling will limit rendering to _max_rendered_commits
                    # Force immediate render (bypass debounce) since we've already limited commits
                    # Pass full count: use _total_commits_count if available (from background load), otherwise len(self.log_commits)
                    total_count = self.log_pane._total_commits_count if self.log_pane._total_commits_count > 0 else len(self.log_commits)
                    self.log_pane._last_render_time = 0  # Reset debounce to force immediate render
                    self.log_pane.show_branch_log(branch, commits_to_render, branch_info, cached_git, total_commits_count_override=total_count)
                    
                    update_elapsed = time.perf_counter() - update_start
                    _log_timing_message(f"[TIMING]   _update_branch_info_ui TOTAL: {update_elapsed:.4f}s ({len(commits_to_render)} commits)")
                # else: Don't re-render yet - wait for commit_refs to arrive (batched update)
        except Exception:
            pass  # Silently fail if branch changed
    
    def load_commit_refs_background(self, branch: str, commits: list[CommitInfo]) -> None:
        """Load commit refs in background and update log view incrementally."""
        import threading
        
        def load_commit_refs_in_thread():
            """Load commit refs in background thread (optimized: single git log call)."""
            import sys
            commit_refs_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_commit_refs_background START (branch: {branch}, {len(commits)} commits)")
            try:
                # DISABLED FOR TESTING: Get refs for all commits (no virtual scrolling limit)
                # max_refs_to_fetch = min(len(commits), self.log_pane._max_rendered_commits)
                # commits_to_fetch = commits[:max_refs_to_fetch]
                commits_to_fetch = commits
                
                # OPTIMIZATION: Get refs for rendered commits in a single git log call (LazyGit approach)
                # Instead of calling get_commit_refs() 200 times, use git log with %D format
                commit_shas = [commit.sha for commit in commits_to_fetch]
                
                git_log_start = time.perf_counter()
                commit_refs_map = self.git.get_commit_refs_from_git_log(branch, commit_shas)
                git_log_elapsed = time.perf_counter() - git_log_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   get_commit_refs_from_git_log (single call): {git_log_elapsed:.4f}s ({len(commits_to_fetch)} commits, virtual scroll limit)")
                
                # Fill in any missing commits with empty refs (fallback) - only for rendered commits
                for commit in commits_to_fetch:
                    if commit.sha not in commit_refs_map:
                        commit_refs_map[commit.sha] = {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []}
                
                _log_timing_message(f"[TIMING] [BACKGROUND]   get_commit_refs TOTAL ({len(commits_to_fetch)} rendered commits): {git_log_elapsed:.4f}s (avg: {git_log_elapsed/len(commits_to_fetch):.6f}s per commit)")
                
                # Update UI from main thread (use queue instead of set_timer to avoid event loop issues)
                update_start = time.perf_counter()
                # Use queue which is thread-safe and doesn't require event loop
                branch_copy = branch
                commit_refs_map_copy = commit_refs_map.copy()
                self._ui_update_queue.put(lambda: self._update_commit_refs_ui(branch_copy, commit_refs_map_copy))
                update_elapsed = time.perf_counter() - update_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   _update_commit_refs_ui (queued): {update_elapsed:.4f}s")
                
                commit_refs_elapsed = time.perf_counter() - commit_refs_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_commit_refs_background TOTAL: {commit_refs_elapsed:.4f}s")
            except Exception as e:
                # Log error but don't crash
                import sys
                import traceback
                commit_refs_elapsed = time.perf_counter() - commit_refs_start
                error_msg = f"Error loading commit refs for branch {branch}: {type(e).__name__}: {e}\n"
                error_msg += f"Traceback:\n{traceback.format_exc()}\n"
                _log_timing_message(f"[TIMING] [BACKGROUND] load_commit_refs_background (ERROR): {commit_refs_elapsed:.4f}s")
                _log_timing_message(error_msg)
        
        thread = threading.Thread(target=load_commit_refs_in_thread, daemon=True)
        thread.start()
    
    def _update_commit_refs_ui(self, branch: str, commit_refs_map: dict) -> None:
        """Update log view with commit refs (called from main thread) - optimized to batch with branch_info."""
        import time
        update_start = time.perf_counter()
        try:
            # Only update if we're still viewing this branch in log mode
            if self.active_branch == branch and self._view_mode == "log" and self.log_commits:
                # Always cache the refs map
                self.log_pane._cached_commit_refs_map = commit_refs_map.copy()
                
                # DISABLED FOR TESTING: Render all commits (no virtual scrolling limit)
                # max_rendered = self.log_pane._max_rendered_commits
                # commits_to_render = self.log_commits[:max_rendered] if len(self.log_commits) > max_rendered else self.log_commits
                commits_to_render = self.log_commits
                
                # Log what we're doing
                _log_timing_message(f"[TIMING]   _update_commit_refs_ui START: {len(self.log_commits)} total commits (no limit)")
                
                # Get branch info (use cached if available, otherwise fetch)
                branch_info = self.log_pane._cached_branch_info if hasattr(self.log_pane, '_cached_branch_info') and self.log_pane._cached_branch_info else None
                if not branch_info:
                    try:
                        branch_info = self.git.get_branch_info(branch)
                        self.log_pane._cached_branch_info = branch_info.copy()
                    except Exception:
                        branch_info = {"name": branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                
                # Create a wrapper git service that uses cached commit refs
                class CachedGitService:
                    def __init__(self, git_service, refs_map):
                        self.git_service = git_service
                        self.refs_map = refs_map
                    
                    def get_commit_refs(self, commit_sha: str):
                        return self.refs_map.get(commit_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                
                cached_git = CachedGitService(self.git, commit_refs_map)
                # Pass both branch_info and commit_refs together - single render
                # Virtual scrolling will limit rendering to _max_rendered_commits (only first 50 commits)
                # Force immediate render (bypass debounce) since we've already limited commits
                # Pass full count: use _total_commits_count if available (from background load), otherwise len(self.log_commits)
                total_count = self.log_pane._total_commits_count if self.log_pane._total_commits_count > 0 else len(self.log_commits)
                self.log_pane._last_render_time = 0  # Reset debounce to force immediate render
                self.log_pane.show_branch_log(branch, commits_to_render, branch_info, cached_git, total_commits_count_override=total_count)
                
                update_elapsed = time.perf_counter() - update_start
                _log_timing_message(f"[TIMING]   _update_commit_refs_ui TOTAL: {update_elapsed:.4f}s ({len(commits_to_render)} commits)")
        except Exception:
            pass  # Silently fail if branch changed

    def load_commits(self, branch: str) -> None:
         # Update Commits pane title to show which branch
        self.commits_pane.set_branch(branch)
        # Reset paging and load first page
        self.total_commits = self.git.count_commits(branch)
        loaded_commits = self.git.list_commits(branch, max_count=self.page_size, skip=0)
        self.all_commits = loaded_commits.copy()  # Store all commits for search
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
        else:
            self.commits = loaded_commits
        
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
            # Only show patch if in patch mode
            if self._view_mode == "patch":
                self.show_commit_diff(0)

    def _update_commits_title(self) -> None:
        if self.active_branch:
            # Use log_pane._total_commits_count as fallback if total_commits is 0
            # This ensures the count shows correctly even when count_commits hasn't finished loading
            total_count = self.total_commits if self.total_commits > 0 else (self.log_pane._total_commits_count if hasattr(self.log_pane, '_total_commits_count') and self.log_pane._total_commits_count > 0 else 0)
            self.commits_pane.border_title = f"Commits ({self.active_branch}) {len(self.commits)} of {total_count}"

    def load_more_commits(self) -> None:
        if not self.active_branch:
            return
        # If searching, don't load more - we're filtering existing commits
        if self._search_query:
            return
        if self.loaded_commits >= self.total_commits:
            return
        next_batch = self.git.list_commits(self.active_branch, max_count=self.page_size, skip=self.loaded_commits)
        if not next_batch:
            return
        self.all_commits.extend(next_batch)
        self.commits.extend(next_batch)
        self.loaded_commits = len(self.commits)
        self.commits_pane.append_commits(next_batch)
        self._update_commits_title()

    def show_commit_diff(self, index: int) -> None:
        if 0 <= index < len(self.commits):
            import sys
            diff_start = time.perf_counter()
            ci = self.commits[index]
            get_diff_start = time.perf_counter()
            diff = self.git.get_commit_diff(ci.sha)
            get_diff_elapsed = time.perf_counter() - get_diff_start
            _log_timing_message(f"[TIMING] get_commit_diff: {get_diff_elapsed:.4f}s (commit: {ci.sha[:8]})")
            show_start = time.perf_counter()
            self.patch_pane.show_commit_info(ci, diff)
            show_elapsed = time.perf_counter() - show_start
            _log_timing_message(f"[TIMING] show_commit_info: {show_elapsed:.4f}s")
            diff_total = time.perf_counter() - diff_start
            _log_timing_message(f"[TIMING] show_commit_diff TOTAL: {diff_total:.4f}s")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is self.branches_pane:
            index = event.index
            if 0 <= index < len(self.branches):
                self.active_branch = self.branches[index].name
                # Switch to log view when branch is selected
                self._view_mode = "log"
                self.patch_pane.styles.display = "none"
                self.log_pane.styles.display = "block"
                # Load commits with full history for feature branches
                self.load_commits_for_log(self.active_branch)
                self.update_status_info()
        elif event.list_view is self.commits_pane:
            # Switch to patch view when commit is selected
            self._view_mode = "patch"
            self.log_pane.styles.display = "none"
            self.patch_pane.styles.display = "block"
            self.selected_commit_index = event.index
            self.show_commit_diff(event.index)

    def action_load_more(self) -> None:
        """Load more commits - works for both commits pane and log view."""
        if self._view_mode == "log":
            # Load more for log view
            self.load_more_commits_for_log(self.active_branch)
        else:
            # Load more for commits pane
            self.load_more_commits()
    
    def on_scroll(self, event) -> None:
        """Handle scroll events - update virtual scrolling range and auto-load more commits."""
        widget = event.widget
        widget_id = widget.id if hasattr(widget, 'id') else None
        
        # Handle scroll for commits pane (left side)
        if widget_id == "commits-pane" or (hasattr(widget, 'id') and widget.id == "commits-pane"):
            try:
                # Get scroll position
                scroll_y = 0
                max_scroll_y = 0
                
                if hasattr(widget, 'scroll_y'):
                    scroll_y = widget.scroll_y
                if hasattr(widget, 'max_scroll_y'):
                    max_scroll_y = widget.max_scroll_y
                elif hasattr(widget, 'virtual_size'):
                    max_scroll_y = widget.virtual_size.height if hasattr(widget.virtual_size, 'height') else 0
                
                # Check if we need to load more commits
                if max_scroll_y > 0 and self.total_commits > 0:
                    scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                    
                    # If scrolled near bottom (85%), auto-load more commits
                    if scroll_percent >= 0.85 and self.loaded_commits < self.total_commits:
                        _log_timing_message(f"[TIMING] [SCROLL] Commits pane: Loading more commits (scroll_percent={scroll_percent:.2f}, loaded={self.loaded_commits}, total={self.total_commits})")
                        self.load_more_commits()
            except Exception:
                pass  # Silently fail if scroll detection fails
        
        # Handle scroll for log view (right side)
        # Check if scroll is from the log pane or its container
        if self._view_mode == "log" and (widget_id == "log-pane" or widget_id == "patch-scroll-container"):
            try:
                # Get scroll position - try multiple ways to get scroll info
                scroll_y = 0
                max_scroll_y = 0
                
                # Try to get scroll position from the widget
                if hasattr(widget, 'scroll_y'):
                    scroll_y = widget.scroll_y
                elif hasattr(event, 'y'):
                    scroll_y = event.y
                
                if hasattr(widget, 'max_scroll_y'):
                    max_scroll_y = widget.max_scroll_y
                elif hasattr(widget, 'virtual_size'):
                    # Alternative: use virtual_size if available
                    max_scroll_y = widget.virtual_size.height if hasattr(widget.virtual_size, 'height') else 0
                
                # Also try to get from the scroll container if widget is log-pane
                if widget_id == "log-pane" and hasattr(self, 'log_pane'):
                    # Find the scroll container parent
                    container = self.query_one("#patch-scroll-container", None)
                    if container and hasattr(container, 'scroll_y'):
                        scroll_y = container.scroll_y
                        max_scroll_y = container.max_scroll_y if hasattr(container, 'max_scroll_y') else 0
                
                # VIRTUAL SCROLLING: Expand rendered range when scrolling near bottom
                # This allows smooth scrolling through large commit lists
                # Use self.log_commits (current loaded commits for log pane) instead of _cached_commits (which might be stale)
                total_commits = len(self.log_commits) if self.log_commits else len(self.log_pane._cached_commits) if self.log_pane._cached_commits else 0
                if total_commits > self.log_pane._max_rendered_commits and max_scroll_y > 0:
                    scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                    _log_timing_message(f"[TIMING] [SCROLL] scroll_percent={scroll_percent:.2f}, scroll_y={scroll_y}, max_scroll_y={max_scroll_y}, total_commits={total_commits}, max_rendered={self.log_pane._max_rendered_commits}")
                    
                    # If scrolled past 70%, expand rendered range (lower threshold for faster expansion)
                    if scroll_percent >= 0.7:
                        new_max = min(
                            total_commits,
                            self.log_pane._max_rendered_commits + 50
                        )
                        if new_max > self.log_pane._max_rendered_commits:
                            _log_timing_message(f"[TIMING] [SCROLL] Expanding virtual scroll: {self.log_pane._max_rendered_commits} -> {new_max} commits (total: {total_commits})")
                            self.log_pane._max_rendered_commits = new_max
                            # Re-render with expanded range - use self.log_commits (current) not cached
                            commits_to_render = self.log_commits if self.log_commits else self.log_pane._cached_commits
                            if commits_to_render and self.active_branch:
                                branch_info = self.log_pane._cached_branch_info.copy() if hasattr(self.log_pane, '_cached_branch_info') and self.log_pane._cached_branch_info else {"name": self.active_branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                                git_service = None
                                if hasattr(self.log_pane, '_cached_commit_refs_map') and self.log_pane._cached_commit_refs_map:
                                    class CachedGitService:
                                        def __init__(self, git_service, refs_map):
                                            self.git_service = git_service
                                            self.refs_map = refs_map
                                        def get_commit_refs(self, commit_sha: str):
                                            return self.refs_map.get(commit_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                                    git_service = CachedGitService(self.git, self.log_pane._cached_commit_refs_map)
                                
                                # Force re-render by bypassing debounce (we want immediate expansion)
                                # Pass full count from self.log_commits so "more commits" message shows correctly
                                self.log_pane._last_render_time = 0  # Reset debounce timer
                                total_count = len(self.log_commits) if self.log_commits else len(commits_to_render)
                                self.log_pane.show_branch_log(
                                    self.active_branch,
                                    commits_to_render,
                                    branch_info,
                                    git_service,
                                    append=False,
                                    total_commits_count_override=total_count
                                )
                
                # If scrolled near bottom (within 10% of bottom), load more commits
                if max_scroll_y > 0:
                    scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                    if scroll_percent >= 0.9:  # 90% scrolled
                        # Load more commits if not already loading and not all loaded
                        if (self.log_pane._total_commits_count == 0 or 
                            self.log_pane._loaded_commits_count < self.log_pane._total_commits_count):
                            _log_timing_message(f"[TIMING] [SCROLL] Loading more commits (scroll_percent={scroll_percent:.2f})")
                            self.load_more_commits_for_log(self.active_branch)
            except Exception:
                pass  # Silently fail if scroll detection fails
    
    def on_input_changed(self, event: events.Input.Changed) -> None:
        """Handle search input changes - filter commits in real-time."""
        if event.input == self.search_input:
            self._search_query = event.value
            # Filter commits from all_commits
            if self.all_commits:
                if self._search_query:
                    self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
                else:
                    # No search query, show all commits (but only loaded ones)
                    self.commits = self.all_commits.copy()
                
                # Update the commits pane
                self.commits_pane.set_commits(self.commits)
                self._update_commits_title()
                
                # Reset selection to first commit
                if self.commits:
                    self.commits_pane.index = 0
                    self.commits_pane.highlighted = 0
                    self.commits_pane._last_index = None
                    self.commits_pane._update_highlighting(0)
                    self.selected_commit_index = 0
                    self.show_commit_diff(0)
                else:
                    # No results, clear selection
                    self.commits_pane.index = None
                    self.commits_pane.highlighted = None


def run_textual(repo_dir: str = ".", use_cython: bool = True) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from dulwich.errors import NotGitRepository
    
    try:
        app = PygitzenApp(repo_dir, use_cython=use_cython)
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


