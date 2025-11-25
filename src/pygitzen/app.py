from __future__ import annotations

import time
import queue
import subprocess
import threading
from functools import wraps
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container, ScrollableContainer
from textual.widgets import Footer, Header, ListItem, ListView, Static, DataTable, Input, TabbedContent, TabPane
from textual.reactive import reactive
from textual import events
from textual.binding import Binding
from textual.message import Message
from rich.text import Text
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel

from .git_service import GitService, BranchInfo, CommitInfo, FileStatus, StashInfo, TagInfo

# Helper function to format time recency (e.g., "18h", "1d", "1w")
def format_recency(timestamp: int) -> str:
    """Format timestamp as human-readable recency (e.g., '18h', '1d', '1w').
    
    Args:
        timestamp: Unix timestamp (0 if not available)
    
    Returns:
        Formatted string like "18h", "1d", "1w", or empty string if timestamp is 0
    """
    if timestamp == 0:
        return ""
    
    import time
    now = int(time.time())
    diff_seconds = now - timestamp
    
    if diff_seconds < 60:
        # Less than a minute
        return f"{diff_seconds}s"
    elif diff_seconds < 3600:
        # Less than an hour - show minutes
        minutes = diff_seconds // 60
        return f"{minutes}m"
    elif diff_seconds < 86400:
        # Less than a day - show hours
        hours = diff_seconds // 3600
        return f"{hours}h"
    elif diff_seconds < 604800:
        # Less than a week - show days
        days = diff_seconds // 86400
        return f"{days}d"
    elif diff_seconds < 2592000:
        # Less than a month - show weeks
        weeks = diff_seconds // 604800
        return f"{weeks}w"
    elif diff_seconds < 31536000:
        # Less than a year - show months
        months = diff_seconds // 2592000
        return f"{months}mo"
    else:
        # Years
        years = diff_seconds // 31536000
        return f"{years}y"

# Performance timing utilities
# DISABLED: Timing logs commented out for main branch
# Uncomment to enable timing logs for debugging/performance analysis
# _TIMING_LOG_FILE = None
# _TIMING_LOG_PATH = "timing.log"

# def _get_timing_log_file():
#     """Get or create timing log file handle."""
#     global _TIMING_LOG_FILE
#     if _TIMING_LOG_FILE is None:
#         try:
#             _TIMING_LOG_FILE = open(_TIMING_LOG_PATH, "a", encoding="utf-8")
#         except Exception:
#             # If we can't open the file, return None and timing will be skipped
#             pass
#     return _TIMING_LOG_FILE

def _normalize_commit_sha(sha) -> str:
    """
    Normalize commit SHA to a proper 40-character hex string.
    Handles various formats including hex-encoded ASCII (80 chars).
    """
    if isinstance(sha, bytes):
        return sha.hex()
    elif not isinstance(sha, str):
        sha = str(sha)
    
    sha = sha.strip()
    
    # Special case: If it's 80 characters, it might be hex-encoded ASCII codes
    # Pattern: Each pair of hex digits represents the ASCII code of a hex character
    # Example: '7' (0x37) 'f' (0x66) '2' (0x32) -> "376632" -> "7f2"
    if len(sha) == 80:
        try:
            hex_chars = []
            for i in range(0, len(sha), 2):
                if i + 1 < len(sha):
                    try:
                        ascii_code = int(sha[i:i+2], 16)  # Parse as hex
                        if 48 <= ascii_code <= 102:  # '0'-'9' (48-57) or 'a'-'f' (97-102)
                            hex_chars.append(chr(ascii_code))
                    except ValueError:
                        break
            # If we got 40 characters and they're all hex, this is the fix
            if len(hex_chars) == 40:
                potential_sha = ''.join(hex_chars).lower()
                if all(c in '0123456789abcdef' for c in potential_sha):
                    return potential_sha
        except Exception:
            pass
    
    # Validate it's a proper hex string
    if len(sha) == 40 and all(c in '0123456789abcdefABCDEF' for c in sha):
        return sha.lower()
    
    # Try to extract valid hex from the string
    import re
    hex_match = re.search(r'[0-9a-fA-F]{40}', str(sha))
    if hex_match:
        return hex_match.group(0).lower()
    
    # Last resort: return as-is (will be logged as error)
    return sha

def _log_timing_message(message: str):
    """Log timing message to file (non-blocking, won't interfere with TUI)."""
    # ENABLED for debugging PTY issues
    try:
        # Write directly to file (simple approach for debugging)
        with open("timing.log", "a", encoding="utf-8") as f:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            f.write(f"[{timestamp}] {message}\n")
            f.flush()  # Ensure it's written immediately
    except Exception as e:
        # Log error to stderr for debugging (only if file logging fails)
        try:
            import sys
            print(f"[TIMING LOG ERROR] {e}", file=sys.stderr)
        except Exception:
            pass  # Timing logs disabled

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
    
    def update_status(self, branch: str, repo_path: str, sync_status: dict | None = None) -> None:
        """Update status pane with branch info and optional sync status.
        
        Args:
            branch: Branch name
            repo_path: Repository path
            sync_status: Optional dict with 'behind', 'ahead', 'synced', 'upstream' keys
        """
        from rich.text import Text
        repo_name = repo_path.split('/')[-1]
        status_text = Text()
        
        # Add sync status indicators if available
        if sync_status:
            behind = sync_status.get("behind", 0)
            ahead = sync_status.get("ahead", 0)
            synced = sync_status.get("synced", False)
            
            if synced and behind == 0 and ahead == 0:
                # Fully synced
                status_text.append("✓ ", style="green")
            else:
                # Show behind/ahead counts
                if behind > 0:
                    status_text.append(f"↓{behind} ", style="red")
                if ahead > 0:
                    status_text.append(f"↑{ahead} ", style="yellow")
                if behind == 0 and ahead == 0:
                    status_text.append("✓ ", style="green")
        else:
            # Default checkmark if no sync status
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
    
    def set_branches(self, branches: list[BranchInfo], current_branch: str, sync_status: dict[str, dict] | None = None) -> None:
        """Set branches with optional sync status indicators.
        
        Args:
            branches: List of branch info
            current_branch: Name of current branch
            sync_status: Optional dict mapping branch name to sync status dict with keys:
                'behind', 'ahead', 'synced', 'upstream'
        """
        self.clear()
        if sync_status is None:
            sync_status = {}
        
        for branch in branches:
            from rich.text import Text
            text = Text()
            
            # Current branch indicator
            if branch.name == current_branch:
                text.append("* ", style="green")
            else:
                text.append("  ", style="white")
            
            # Recency (time since last commit) - format: "18h ", "1d ", etc.
            # Pad to fixed width (4 chars) for alignment: "36s ", "2h  ", "2d  ", etc.
            recency = format_recency(branch.timestamp)
            if recency:
                # Pad recency to 4 characters for consistent alignment
                recency_padded = f"{recency:<4}"
                text.append(recency_padded, style="dim white")
            else:
                # If no recency, add 4 spaces to maintain alignment
                text.append("    ", style="dim white")
            
            # Branch name
            text.append(branch.name, style="white")
            
            # Sync status indicators
            branch_sync = sync_status.get(branch.name, {})
            behind = branch_sync.get("behind", 0)
            ahead = branch_sync.get("ahead", 0)
            synced = branch_sync.get("synced", False)
            
            # Add sync status indicators
            if synced and behind == 0 and ahead == 0:
                # Fully synced
                text.append(" ✓", style="green")
            else:
                # Show behind/ahead counts
                if behind > 0:
                    text.append(f" ↓{behind}", style="red")
                if ahead > 0:
                    text.append(f" ↑{ahead}", style="yellow")
            
            item = ListItem(Static(text))
            if branch.name == current_branch:
                item.add_class("current-branch")
            self.append(item)


class RemotesPane(ListView):
    """Remotes pane showing remote branches."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Remotes"
        self._parent_app = None  # Will be set by parent
        self._remotes: list[BranchInfo] = []  # Store remotes for selection access
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)
        self._last_highlighted = None
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) to update visual highlighting."""
        if highlighted is not None:
            # Remove highlight from previous item
            if self._last_highlighted is not None and self._last_highlighted < len(self.children):
                try:
                    item = self.children[self._last_highlighted]
                    if isinstance(item, ListItem):
                        item.remove_class("highlighted-remote")
                except:
                    pass
            
            # Add highlight to current item
            if highlighted < len(self.children):
                try:
                    item = self.children[highlighted]
                    if isinstance(item, ListItem):
                        item.add_class("highlighted-remote")
                        self._last_highlighted = highlighted
                except:
                    pass
    
    def set_on_render_to_main(self, callback: callable) -> None:
        """Set callback for automatic patch updates (lazygit GetOnRenderToMain pattern)."""
        self._on_render_to_main = callback
    
    def set_remotes(self, remotes: list[BranchInfo]) -> None:
        self.clear()
        self._remotes = remotes  # Store remotes for selection access
        
        for remote in remotes:
            from rich.text import Text
            text = Text()
            
            # Always use "  " for alignment (matching branches pane format)
            text.append("  ", style="white")
            
            # Recency (time since last commit) - format: "18h ", "1d ", etc.
            # Pad to fixed width (4 chars) for alignment: "36s ", "2h  ", "2d  ", etc.
            recency = format_recency(remote.timestamp)
            if recency:
                # Pad recency to 4 characters for consistent alignment
                recency_padded = f"{recency:<4}"
                text.append(recency_padded, style="dim white")
            else:
                # If no recency, add 4 spaces to maintain alignment
                text.append("    ", style="dim white")
            
            # Remote branch name (e.g., origin/main)
            text.append(remote.name, style="white")
            
            item = ListItem(Static(text))
            self.append(item)
    
    def on_list_view_selected(self, event) -> None:
        """Handle remote selection - show remote info."""
        if self._on_render_to_main:
            try:
                self._on_render_to_main()
            except Exception:
                pass


class TagsPane(ListView):
    """Tags pane showing tags with virtual scrolling."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Tags"
        self._parent_app = None  # Will be set by parent
        self._tags: list[TagInfo] = []  # Store all loaded tags
        self._loaded_tags_count = 0  # How many tags we've loaded
        self._total_tags_count = 0  # Total number of tags available
        self._page_size = 200  # Load 200 tags at a time
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)
        self._last_highlighted = None
        self._rendered_count = 0  # Track how many tags are actually rendered in UI
        self._scroll_check_timer = None  # Timer for checking scroll position
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) to update visual highlighting."""
        if highlighted is not None:
            # Remove highlight from previous item
            if self._last_highlighted is not None and self._last_highlighted < len(self.children):
                try:
                    item = self.children[self._last_highlighted]
                    if isinstance(item, ListItem):
                        item.remove_class("highlighted-tag")
                except:
                    pass
            
            # Add highlight to current item
            if highlighted < len(self.children):
                try:
                    item = self.children[highlighted]
                    if isinstance(item, ListItem):
                        item.add_class("highlighted-tag")
                        self._last_highlighted = highlighted
                except:
                    pass
    
    def set_on_render_to_main(self, callback: callable) -> None:
        """Set callback for automatic patch updates (lazygit GetOnRenderToMain pattern)."""
        self._on_render_to_main = callback
    
    def set_tags(self, tags: list[TagInfo], total_count: int = 0, append: bool = False) -> None:
        """Set tags in the pane, with support for virtual scrolling.
        
        Args:
            tags: List of tags to display
            total_count: Total number of tags available (for virtual scrolling)
            append: If True, append to existing tags; if False, replace
        """
        if not append:
            self.clear()
            self._tags = []
            self._loaded_tags_count = 0
            self._rendered_count = 0  # Reset rendered count on initial load
            # Store initial tags and set total count
            self._tags = tags.copy()
            self._loaded_tags_count = len(self._tags)
            self._total_tags_count = total_count if total_count > 0 else len(self._tags)
        else:
            # Append mode: add new tags to existing list
            self._tags.extend(tags)
            self._loaded_tags_count = len(self._tags)
            # Keep existing total_count (don't overwrite it)
        
        # CRITICAL: Limit initial rendering to prevent UI blocking on large repos (59k+ tags)
        # Only render first 200 tags initially, rest will be loaded on scroll (virtual scrolling)
        # This matches the approach used for commits - fast initial render, load more on demand
        if append:
            # When appending, render the NEW tags that were just passed in (not all tags)
            # This is for virtual scrolling - we only render the newly loaded batch
            tags_to_render = tags  # Render only the new tags being appended
        else:
            # Initial load: render first 200 tags
            initial_limit = 200
            tags_to_render = self._tags[:initial_limit] if len(self._tags) > initial_limit else self._tags
        
        # Calculate max widths for proper alignment (like Lazygit's column layout)
        # Use all tags for width calculation, but only render subset
        if tags_to_render:
            # Calculate max tag name width for alignment
            max_name_width = max(len(tag.name) for tag in tags_to_render) if tags_to_render else 0
            # Add some padding for better readability
            max_name_width = max(max_name_width, 15)  # Minimum width for alignment
        else:
            max_name_width = 15
        
        # Only render the limited subset (not all 59k tags)
        for tag in tags_to_render:
            from rich.text import Text
            text = Text()
            
            # Add tag version (name) with fixed width (left-aligned, like Lazygit column 1)
            text.append(f"{tag.name:<{max_name_width}} ", style="white")
            
            # Add tag message (like Lazygit column 2) - shown in yellow
            if tag.message:
                text.append(tag.message, style="yellow")
            
            item = ListItem(Static(text))
            self.append(item)
        
        # Update rendered count
        self._rendered_count = len(self.children)
        
        # Start scroll monitoring for virtual scrolling (only on initial load, not append)
        if self._parent_app and not append:
            self._start_scroll_monitoring()
    
    def _start_scroll_monitoring(self) -> None:
        """Start monitoring scroll position for virtual scrolling."""
        if self._parent_app:
            # Cancel existing timer if any
            if hasattr(self, '_scroll_check_timer') and self._scroll_check_timer:
                try:
                    self._scroll_check_timer.stop()
                except:
                    pass
            
            # Check scroll position periodically
            def check_scroll():
                try:
                    if hasattr(self, '_rendered_count') and hasattr(self, '_total_tags_count'):
                        rendered = self._rendered_count
                        total = self._total_tags_count
                        
                        if rendered >= total:
                            return  # All tags rendered, stop monitoring
                        
                        # Check if we're near the bottom
                        if hasattr(self, 'scroll_y') and hasattr(self, 'max_scroll_y'):
                            scroll_y = self.scroll_y
                            max_scroll_y = self.max_scroll_y
                            
                            if max_scroll_y > 0:
                                scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                                
                                # If scrolled near bottom (85%), load more tags
                                if scroll_percent >= 0.85 and rendered < total:
                                    if self._parent_app:
                                        self._parent_app._load_more_tags()
                except Exception:
                    pass
            
            # Check every 0.5 seconds using set_interval
            try:
                self._scroll_check_timer = self.set_interval(0.5, check_scroll)
            except Exception:
                # If set_interval doesn't work, fall back to on_scroll handler
                pass
    
    def append_tags(self, tags: list[TagInfo]) -> None:
        """Append more tags (for virtual scrolling)."""
        self.set_tags(tags, append=True)
    
    def on_list_view_selected(self, event) -> None:
        """Handle tag selection - show tag info and git log graph."""
        if self._on_render_to_main:
            try:
                self._on_render_to_main()
            except Exception:
                pass


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
        """Set commits in the commits pane.
        
        Phase 2: Added timing diagnostics to identify lag sources.
        """
        import time
        set_start = time.perf_counter()
        
        clear_start = time.perf_counter()
        self.clear()
        clear_time = time.perf_counter() - clear_start
        
        self._last_highlighted = None  # Reset highlighting tracker
        
        # Store commit SHAs and commit info for in-place updates
        self._commit_shas = []
        self._commit_info_map = {}  # SHA -> CommitInfo for quick lookup
        
        # Virtual scrolling: limit initial commits to 300 for performance
        # ListView has built-in virtual scrolling, but we still need to limit initial DOM elements
        initial_limit = 300
        commits_to_render = commits[:initial_limit] if len(commits) > initial_limit else commits
        
        text_creation_time = 0.0
        listview_append_time = 0.0
        
        for commit in commits_to_render:
            text_start = time.perf_counter()
            from rich.text import Text
            
            # Normalize SHA format (fix for Cython version hex-encoded ASCII issue)
            commit_sha = _normalize_commit_sha(commit.sha)
            short_sha = commit_sha[:8] if len(commit_sha) >= 8 else commit_sha
            author_short = commit.author.split('<')[0].strip()
            
            # Store SHA and commit info for in-place updates
            self._commit_shas.append(commit_sha)
            self._commit_info_map[commit_sha] = commit
            
            text = Text()
            text.append(short_sha, style="cyan")
            text.append(" ", style="white")
            
            # Show push status if available (will be updated by background thread if needed)
            # Three-tier status display (lazygit-style):
            if commit.merged:
                text.append("✓ ", style="green")  # StatusMerged
            elif hasattr(commit, 'pushed') and commit.pushed:
                text.append("↑ ", style="yellow")  # StatusPushed
            elif hasattr(commit, 'pushed') and not commit.pushed:
                text.append("- ", style="red")  # StatusUnpushed
            # else: don't show anything initially (will be updated by background thread)
            
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
            
            text_creation_time += time.perf_counter() - text_start
            
            # Time the ListView.append operation (this is where Textual might lag)
            append_item_start = time.perf_counter()
            self.append(ListItem(Static(text)))
            listview_append_time += time.perf_counter() - append_item_start
        
        set_total = time.perf_counter() - set_start
        if set_total > 0.01:  # Only log if it takes more than 10ms
            _log_timing_message(f"[TIMING] [RENDER] set_commits({len(commits_to_render)} commits): {set_total*1000:.1f}ms total (clear: {clear_time*1000:.1f}ms, text creation: {text_creation_time*1000:.1f}ms, ListView.append: {listview_append_time*1000:.1f}ms)")

    def append_commits(self, commits: list[CommitInfo]) -> None:
        """Append commits to the commits pane.
        
        Phase 2: Added timing diagnostics and batched appends to reduce Textual lag.
        Textual ListView.append() is expensive, so we batch commits into smaller chunks.
        """
        import time
        append_start = time.perf_counter()
        
        # Initialize _commit_shas and _commit_info_map if not exists
        if not hasattr(self, '_commit_shas'):
            self._commit_shas = []
        if not hasattr(self, '_commit_info_map'):
            self._commit_info_map = {}
        
        text_creation_time = 0.0
        listview_append_time = 0.0
        
        for commit in commits:
            text_start = time.perf_counter()
            from rich.text import Text
            
            # Normalize SHA format (fix for Cython version hex-encoded ASCII issue)
            commit_sha = _normalize_commit_sha(commit.sha)
            short_sha = commit_sha[:8] if len(commit_sha) >= 8 else commit_sha
            author_short = commit.author.split('<')[0].strip()
            
            # Store SHA and commit info for in-place updates
            self._commit_shas.append(commit_sha)
            self._commit_info_map[commit_sha] = commit
            
            text = Text()
            text.append(short_sha, style="cyan")
            text.append(" ", style="white")
            
            # Show push status if available (will be updated by background thread if needed)
            # Three-tier status display (lazygit-style) - same as set_commits
            # CRITICAL: Show initial status so commits don't appear blank, then update when background thread completes
            if commit.merged:
                text.append("✓ ", style="green")  # StatusMerged
            elif hasattr(commit, 'pushed') and commit.pushed:
                text.append("↑ ", style="yellow")  # StatusPushed
            elif hasattr(commit, 'pushed') and not commit.pushed:
                text.append("- ", style="red")  # StatusUnpushed
            # else: don't show anything initially (will be updated by background thread)
            
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
            
            text_creation_time += time.perf_counter() - text_start
            
            # Time the ListView.append operation (this is where Textual might lag)
            append_item_start = time.perf_counter()
            self.append(ListItem(Static(text)))
            listview_append_time += time.perf_counter() - append_item_start
        
        append_total = time.perf_counter() - append_start
        if append_total > 0.01:  # Only log if it takes more than 10ms
            _log_timing_message(f"[TIMING] [RENDER] append_commits({len(commits)} commits): {append_total*1000:.1f}ms total (text creation: {text_creation_time*1000:.1f}ms, ListView.append: {listview_append_time*1000:.1f}ms)")
    
    def update_push_status_in_place(self, commits: list[CommitInfo]) -> None:
        """Update push status for existing commits without clearing the list."""
        if not commits or len(commits) == 0:
            return
        
        # Create maps of normalized SHA to push status and merged status for quick lookup
        push_status_map = {}
        merged_status_map = {}
        for commit in commits:
            commit_sha = _normalize_commit_sha(commit.sha)
            push_status_map[commit_sha] = commit.pushed
            merged_status_map[commit_sha] = commit.merged
        
        # Check if we have stored commit SHAs
        if not hasattr(self, '_commit_shas') or len(self._commit_shas) == 0:
            return
        
        # Check if we have stored commit info map
        if not hasattr(self, '_commit_info_map'):
            self._commit_info_map = {}
        
        # Update items in place using stored SHAs
        from rich.text import Text
        
        updated_ui_count = 0
        skipped_not_in_map = 0
        skipped_no_commit_info = 0
        
        # CRITICAL FIX: Only update commits that are in the provided batch
        # The maps only contain the commits passed to this function, so we should only
        # update UI items whose SHAs are in the maps. This prevents skipping old commits.
        # Build a set of normalized SHAs that we should update
        commits_to_update = set(push_status_map.keys())
        
        for i, item in enumerate(self.children):
            try:
                # Check if we have a stored SHA for this index
                if i >= len(self._commit_shas):
                    continue
                
                stored_sha = self._commit_shas[i]
                normalized_stored_sha = _normalize_commit_sha(stored_sha)
                
                # CRITICAL: Only update if this commit is in the batch we're processing
                # Skip commits that aren't in the current batch (they already have correct status)
                if normalized_stored_sha not in commits_to_update:
                    skipped_not_in_map += 1
                    continue
                
                pushed_status = push_status_map[normalized_stored_sha]
                merged_status = merged_status_map.get(normalized_stored_sha, False)  # Default to False if not in map
                
                # Get commit info from stored map (we have the commit message here)
                commit_info = self._commit_info_map.get(stored_sha)
                if not commit_info:
                    continue
                
                # CRITICAL: Update commit_info with latest merged status (in case _commit_info_map wasn't updated)
                commit_info.merged = merged_status
                commit_info.pushed = pushed_status
                
                # Rebuild the text exactly as we created it originally
                if hasattr(item, 'children') and len(item.children) > 0:
                    static_widget = item.children[0]
                    
                    # Build new text with updated three-tier status (lazygit-style)
                    new_text = Text()
                    short_sha = stored_sha[:8] if len(stored_sha) >= 8 else stored_sha
                    new_text.append(short_sha, style="cyan")
                    new_text.append(" ", style="white")
                    
                    # Three-tier status display:
                    # 1. Merged (green ✓): Commit exists on main/master
                    # 2. Pushed (yellow ↑): Commit is pushed but NOT merged
                    # 3. Unpushed (red -): Commit is not pushed
                    if merged_status:  # Use merged_status from map, not commit_info (which might be stale)
                        new_text.append("✓ ", style="green")  # StatusMerged
                    elif pushed_status:
                        new_text.append("↑ ", style="yellow")  # StatusPushed
                    else:
                        new_text.append("- ", style="red")  # StatusUnpushed
                    
                    # Add commit message (with wrapping if needed)
                    summary = commit_info.summary
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
                        
                        for j, line in enumerate(lines):
                            if j > 0:
                                new_text.append("\n     ", style="white")
                            new_text.append(line, style="white")
                    else:
                        new_text.append(summary, style="white")
                    
                    # Update the static widget
                    static_widget.update(new_text)
                    updated_ui_count += 1
                else:
                    skipped_no_commit_info += 1
            except Exception as e:
                continue


class StashPane(ListView):
    """Stash pane showing stashed changes."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Stash"
        self._parent_app = None  # Will be set by parent
        self._last_index = None  # Track index changes
        self._last_highlighted = None  # Track highlighted changes
        self._stashes = []  # Store stashes for access
    
    def set_stashes(self, stashes: list) -> None:
        """Update the stash list with new stashes."""
        # Clear existing items
        self.clear()
        self._stashes = stashes
        self._last_highlighted = None  # Reset highlighting tracker
        
        if not stashes:
            from rich.text import Text
            from textual.widgets import ListItem, Static
            text = Text()
            text.append("No stashes", style="dim white")
            self.append(ListItem(Static(text)))
            return
        
        # Update title with count
        self.border_title = f"Stash ({len(stashes)})"
        
        # Add each stash entry
        for stash in stashes:
            from rich.text import Text
            from textual.widgets import ListItem, Static
            
            text = Text()
            
            # Recency (time since stash creation) - format: "18h ", "1d ", etc.
            recency = format_recency(stash.timestamp)
            if recency:
                text.append(f"{recency} ", style="dim white")
            
            # Format: stash@{index}: branch: message
            text.append(f"stash@{{{stash.index}}}", style="cyan")
            text.append(": ", style="white")
            text.append(f"{stash.branch}", style="yellow")
            text.append(": ", style="white")
            
            # Show full message, wrap if too long
            message = stash.message
            max_line_length = 50  # Maximum characters per line (adjusted for recency)
            
            if len(message) <= max_line_length:
                # Short message, show on one line
                text.append(message, style="white")
            else:
                # Long message, wrap to multiple lines
                words = message.split()
                current_line = ""
                lines = []
                
                for word in words:
                    if len(current_line + " " + word) <= max_line_length:
                        current_line += (" " + word) if current_line else word
                    else:
                        if current_line:
                            lines.append(current_line)
                        current_line = word
                if current_line:
                    lines.append(current_line)
                
                # Add first line
                text.append(lines[0], style="white")
                # Add continuation lines with indentation
                for i, line in enumerate(lines[1:], 1):
                    text.append("\n     ", style="white")  # Indent continuation lines
                    text.append(line, style="dim white")
            
            self.append(ListItem(Static(text)))
    
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
                    item.remove_class("highlighted-stash")
            except:
                pass
        
        # Add highlight to current item
        if index is not None and index < len(self.children):
            try:
                item = self.children[index]
                if isinstance(item, ListItem):
                    item.add_class("highlighted-stash")
                    self._last_highlighted = index
            except:
                pass
    
    def _update_patch_for_index(self, index: int | None) -> None:
        """Update patch panel for the given index."""
        if index is not None and index != self._last_index and self._parent_app:
            self._last_index = index
            if 0 <= index < len(self._stashes):
                self._parent_app.show_stash_diff(index)


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
        self._cached_graph_prefixes: dict = {}  # sha -> plain graph prefix
        self._cached_graph_prefixes_colored: dict = {}  # sha -> colored graph prefix (with ANSI codes)
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
        # Native git log virtual scrolling
        self._native_git_log_lines: list = []  # Cached lines from git log
        self._native_git_log_count = 50  # Current limit for git log
        self._native_git_log_loading = False  # Prevent concurrent loads
        # Start with blank log - don't update here, let it be empty initially
    
    def show_branch_log(self, branch: str, commits: list[CommitInfo], branch_info: dict, git_service, append: bool = False, total_commits_count_override: int = None) -> None:
        """
        Display native git log --graph --color=always output for a branch.
        Only loads when user clicks on a branch.
        """
        from rich.text import Text
        from pathlib import Path
        from pygitzen.pty_utils import should_use_pty
        
        # Check if PTY streaming should be used
        if should_use_pty():
            try:
                self._show_native_git_log_pty(branch, branch_info, git_service, append=append)
                return
            except Exception as e:
                # Fallback to subprocess if PTY fails
                import traceback
                _log_timing_message(f"[PTY] Fallback to subprocess: {type(e).__name__}: {e}")
                # Continue to subprocess method below
        
        # Only show native git log if we have git_service with repo_path
        if git_service is not None:
            # Check if git_service has repo_path attribute
            repo_path = None
            try:
                # Try to get repo_path to see if it exists
                try:
                    test_repo_path = getattr(git_service, 'repo_path', 'NOT_FOUND')
                except:
                    pass
                
                # Try multiple ways to get repo_path (works for both cython and non-cython)
                # Method 1: Direct attribute access (works for both, including cython cdef attributes and wrappers)
                try:
                    repo_path = git_service.repo_path
                    # Verify it's not None or empty
                    if not repo_path or (isinstance(repo_path, str) and not repo_path.strip()):
                        repo_path = None
                except (AttributeError, TypeError) as e:
                    repo_path = None
                
                # Method 2: Use getattr (works even if hasattr returns False for cython)
                if repo_path is None:
                    try:
                        repo_path = getattr(git_service, 'repo_path', None)
                        # Verify it's not None or empty
                        if not repo_path or (isinstance(repo_path, str) and not repo_path.strip()):
                            repo_path = None
                    except (AttributeError, TypeError):
                        repo_path = None
                
                # Method 3: Try via repo.path (fallback)
                if repo_path is None:
                    try:
                        if hasattr(git_service, 'repo'):
                            repo = getattr(git_service, 'repo', None)
                            if repo and hasattr(repo, 'path'):
                                repo_path = getattr(repo, 'path', None)
                    except (AttributeError, TypeError):
                        pass
                
                # Method 4: Check if git_service itself is a Path
                if repo_path is None and isinstance(git_service, Path):
                    repo_path = git_service
                
                # Convert to Path object if it's a string
                # Check if repo_path is valid (not None, not empty string)
                if repo_path and str(repo_path).strip():
                    if isinstance(repo_path, str):
                        repo_path = Path(repo_path)
                    elif not isinstance(repo_path, Path):
                        # Try to convert other types
                        repo_path = Path(str(repo_path))
                    
                    # Resolve "." to absolute path
                    if str(repo_path) == ".":
                        repo_path = Path(".").resolve()
                    
                    # Check if PTY streaming should be used
                    from pygitzen.pty_utils import should_use_pty
                    if should_use_pty():
                        try:
                            self._show_native_git_log_pty(branch, branch_info, git_service, append=append)
                        except Exception as e:
                            # Fallback to subprocess if PTY fails
                            _log_timing_message(f"[PTY] Fallback to subprocess: {type(e).__name__}: {e}")
                            self._show_native_git_log(branch, branch_info, git_service, append=append)
                    else:
                        # Use subprocess method (default)
                        self._show_native_git_log(branch, branch_info, git_service, append=append)
                else:
                    # No repo_path found or invalid
                    pass
                    self.update(Text())
            except Exception as e:
                # On any error, show empty
                import traceback
                self.update(Text())
        else:
            # Show empty if no git service
            self.update(Text())
    
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
    
    def _show_native_git_log(self, branch: str, branch_info: dict, git_service, append: bool = False) -> None:
        """
        Display native git log --graph --color=always output directly.
        This shows exactly what git outputs, preserving all colors and formatting.
        Supports virtual scrolling - loads more commits as user scrolls.
        
        Phase 2: Moved git command execution to background thread to prevent UI blocking.
        """
        from rich.text import Text
        from rich.console import Group
        from pathlib import Path
        import subprocess
        import threading
        from pygitzen.git_graph import parse_ansi_to_rich_text
        
        # Prevent concurrent loads
        if self._native_git_log_loading:
            return
        self._native_git_log_loading = True
        
        # Phase 2: Run git command in background thread to prevent UI blocking (400ms+ lag fix)
        def load_log_in_background():
            """Load git log in background thread to prevent UI blocking."""
            try:
                import time
                load_start = time.perf_counter()
                
                # Track if this is the first update (for progressive updates)
                is_first_update = not append
                
                # Get repo path from git_service
                # Try multiple methods to get repo_path (works for both cython and non-cython)
                repo_path = None
                
                # Method 1: Direct attribute access
                try:
                    if hasattr(git_service, 'repo_path'):
                        repo_path = git_service.repo_path
                except (AttributeError, TypeError):
                    pass
                
                # Method 2: Use getattr (works even if hasattr returns False for cython)
                if repo_path is None:
                    try:
                        repo_path = getattr(git_service, 'repo_path', None)
                    except (AttributeError, TypeError):
                        pass
                
                # Method 3: Try via repo.path
                if repo_path is None:
                    try:
                        if hasattr(git_service, 'repo'):
                            repo = getattr(git_service, 'repo', None)
                            if repo and hasattr(repo, 'path'):
                                repo_path = getattr(repo, 'path', None)
                    except (AttributeError, TypeError):
                        pass
                
                # Convert to Path object
                if repo_path:
                    if isinstance(repo_path, str):
                        repo_path = Path(repo_path)
                    elif not isinstance(repo_path, Path):
                        repo_path = Path(str(repo_path))
                else:
                    # Fallback to current directory
                    repo_path = Path(".")
                
                # If appending, increase the limit; otherwise reset
                if not append:
                    self._native_git_log_count = 50
                    self._native_git_log_lines = []
                else:
                    # Increase limit by 50 more commits
                    self._native_git_log_count += 50
                
                # Build git command - use native git log --graph --color=always
                # Add --abbrev-commit for short SHAs and --decorate to show refs (branches, tags, HEAD)
                cmd = ['git', 'log', '--graph', '--color=always', '--abbrev-commit', '--decorate', f'-{self._native_git_log_count}']
                
                # Add branch if specified (don't use --all, it's slower)
                # Only add branch if it's not empty
                if branch and branch.strip():
                    # Use refs/heads/ prefix for branches with '/' to ensure they're treated as branches, not paths
                    # This avoids the "ambiguous argument" error for branch names like feature/fuzzy-search-commits
                    if branch.startswith('refs/'):
                        # Already a full ref path, use as is
                        cmd.append(branch)
                    elif '/' in branch:
                        # Branch name contains '/' - use refs/heads/ prefix to avoid ambiguity
                        cmd.append(f'refs/heads/{branch}')
                    else:
                        # Simple branch name without '/' - use as is
                        cmd.append(branch)
                
                # Time git command execution
                git_cmd_start = time.perf_counter()
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=False,  # Get bytes first
                    cwd=str(repo_path),
                    timeout=5  # Short timeout for fast feedback
                )
                git_cmd_time = time.perf_counter() - git_cmd_start
                
                # Decode with error handling for non-UTF-8 characters
                # Use errors='replace' to handle any invalid UTF-8 bytes
                decode_start = time.perf_counter()
                output_text = result.stdout.decode('utf-8', errors='replace')
                error_text = result.stderr.decode('utf-8', errors='replace')
                decode_time = time.perf_counter() - decode_start
                
                # Create a simple result-like object with decoded text
                class DecodedResult:
                    def __init__(self, returncode, stdout, stderr):
                        self.returncode = returncode
                        self.stdout = stdout
                        self.stderr = stderr
                
                result = DecodedResult(result.returncode, output_text, error_text)
                
                if result.returncode != 0:
                    # Show error message via main thread
                    error_text = Text()
                    error_text.append(f"Error running git log: {result.stderr}\n", style="red")
                    if hasattr(self, 'app') and self.app:
                        self.app.call_from_thread(lambda: self.update(error_text))
                    else:
                        self.update(error_text)
                    self._native_git_log_loading = False
                    return
                
                # Parse ANSI-colored output and convert to Rich Text
                # Process the entire output at once for better performance
                if not output_text.strip():
                    # No output, show empty via main thread
                    if hasattr(self, 'app') and self.app:
                        self.app.call_from_thread(lambda: self.update(Text()))
                    else:
                        self.update(Text())
                    self._native_git_log_loading = False
                    return
                
                # Phase 2: Progressive updates - parse and update UI incrementally to prevent "stuck" scroll
                parse_start = time.perf_counter()
                output_lines = output_text.split('\n')
                new_log_lines = []
                
                # Progressive update strategy: Line-based batching only (no time-based to reduce update frequency)
                # Start with smaller batches for immediate feedback, then increase for efficiency
                initial_batch_size = 50  # First few updates: every 50 lines
                large_batch_size = 100  # After initial feedback: every 100 lines
                batch_size_switch = 200  # Switch to large batch after 200 lines
                
                progressive_append = append  # Track append state for progressive updates
                lines_processed = 0  # Track total lines processed (including empty lines)
                last_update_line_count = 0  # Track the last line count at which we updated
                
                # Convert each line from ANSI to Rich Text with progressive updates
                for i, line in enumerate(output_lines):
                    lines_processed += 1
                    
                    if line:  # Only process non-empty lines
                        try:
                            rich_line = parse_ansi_to_rich_text(line)
                            new_log_lines.append(rich_line)
                        except Exception:
                            # If parsing fails, strip ANSI and add as plain text
                            from pygitzen.git_graph import strip_ansi_codes
                            plain_line = strip_ansi_codes(line)
                            new_log_lines.append(Text(plain_line, style="white"))
                    
                    # Adaptive batch size: smaller batches initially, larger as content grows
                    current_batch_size = initial_batch_size if len(new_log_lines) < batch_size_switch else large_batch_size
                    
                    # Progressive update: update UI ONLY when we've accumulated enough NEW lines since last update
                    # This prevents multiple updates from being queued when processing is fast
                    new_lines_since_update = len(new_log_lines) - last_update_line_count
                    should_update = (
                        len(new_log_lines) >= current_batch_size and 
                        new_lines_since_update >= current_batch_size
                    )
                    
                    if should_update and new_log_lines:
                        # CRITICAL: Update the tracker IMMEDIATELY (synchronously) before queuing the async update
                        # This prevents the loop from queuing multiple updates for milestones that were passed quickly
                        last_update_line_count = len(new_log_lines)
                        
                        # Prepare lines for update
                        if progressive_append and self._native_git_log_lines:
                            # Count existing content lines (excluding header and empty line)
                            existing_content_lines = len(self._native_git_log_lines) - 2
                            if existing_content_lines < len(new_log_lines):
                                new_lines_to_add = new_log_lines[existing_content_lines:]
                                self._native_git_log_lines.extend(new_lines_to_add)
                        else:
                            # First load - build full content with header
                            log_lines = []
                            header = self._build_header(branch, branch_info)
                            log_lines.append(header)
                            log_lines.append(Text())  # Empty line
                            log_lines.extend(new_log_lines)
                            self._native_git_log_lines = log_lines
                            progressive_append = True  # After first update, always append
                        
                        # Progressive UI update via main thread
                        def progressive_update_ui():
                            import time
                            update_start = time.perf_counter()
                            
                            # Preserve scroll position
                            scroll_container = None
                            preserved_scroll_y = 0
                            try:
                                if hasattr(self, 'app') and self.app:
                                    scroll_container = self.app.query_one("#patch-scroll-container", None)
                                    if scroll_container and hasattr(scroll_container, 'scroll_y'):
                                        preserved_scroll_y = scroll_container.scroll_y
                            except:
                                pass
                            
                            # Update the pane
                            if self._native_git_log_lines:
                                full_content = Group(*self._native_git_log_lines)
                                self.update(full_content)
                            
                            # Restore scroll position
                            if scroll_container and hasattr(scroll_container, 'scroll_y') and preserved_scroll_y > 0:
                                try:
                                    scroll_container.scroll_y = preserved_scroll_y
                                except:
                                    pass
                            
                            update_total = time.perf_counter() - update_start
                            # Always log progressive updates to track if they're happening
                            _log_timing_message(f"[TIMING] [RENDER] [LOG] Progressive update: {update_total*1000:.1f}ms (lines={len(self._native_git_log_lines)}, new_lines={len(new_log_lines)})")
                        
                        if hasattr(self, 'app') and self.app:
                            self.app.call_from_thread(progressive_update_ui)
                        else:
                            progressive_update_ui()
                
                # Final update with all remaining lines
                if new_log_lines:
                    # If appending, only add new lines (skip already loaded ones)
                    if progressive_append and self._native_git_log_lines:
                        existing_content_lines = len(self._native_git_log_lines) - 2
                        if existing_content_lines < len(new_log_lines):
                            new_lines_to_add = new_log_lines[existing_content_lines:]
                            self._native_git_log_lines.extend(new_lines_to_add)
                    else:
                        # First load - build full content with header
                        log_lines = []
                        header = self._build_header(branch, branch_info)
                        log_lines.append(header)
                        log_lines.append(Text())  # Empty line
                        log_lines.extend(new_log_lines)
                        self._native_git_log_lines = log_lines
                
                parse_time = time.perf_counter() - parse_start
                load_time = time.perf_counter() - load_start
                _log_timing_message(f"[TIMING] [SCROLL] [LOG] Background load: {load_time*1000:.1f}ms total (git_cmd: {git_cmd_time*1000:.1f}ms, decode: {decode_time*1000:.1f}ms, parse: {parse_time*1000:.1f}ms)")
                
                # Final UI update via main thread
                def update_ui():
                    import time
                    update_start = time.perf_counter()
                    
                    # Preserve scroll position before update
                    scroll_container = None
                    preserved_scroll_y = 0
                    preserve_start = time.perf_counter()
                    try:
                        if hasattr(self, 'app') and self.app:
                            scroll_container = self.app.query_one("#patch-scroll-container", None)
                            if scroll_container and hasattr(scroll_container, 'scroll_y'):
                                preserved_scroll_y = scroll_container.scroll_y
                    except:
                        pass
                    preserve_time = time.perf_counter() - preserve_start
                    
                    # Update the pane
                    group_start = time.perf_counter()
                    if self._native_git_log_lines:
                        full_content = Group(*self._native_git_log_lines)
                        group_time = time.perf_counter() - group_start
                        
                        update_ui_start = time.perf_counter()
                        self.update(full_content)
                        update_ui_time = time.perf_counter() - update_ui_start
                    else:
                        group_time = 0
                        update_ui_start = time.perf_counter()
                        self.update(Text())
                        update_ui_time = time.perf_counter() - update_ui_start
                    
                    # Restore scroll position after update
                    restore_start = time.perf_counter()
                    if scroll_container and hasattr(scroll_container, 'scroll_y') and preserved_scroll_y > 0:
                        try:
                            scroll_container.scroll_y = preserved_scroll_y
                        except Exception as e:
                            pass
                    restore_time = time.perf_counter() - restore_start
                    
                    update_total = time.perf_counter() - update_start
                    
                    # Always log timing for UI updates (even if < 50ms) to track scroll "stuck" issue
                    _log_timing_message(f"[TIMING] [RENDER] [LOG] update_ui: {update_total*1000:.1f}ms total (preserve: {preserve_time*1000:.1f}ms, Group: {group_time*1000:.1f}ms, update: {update_ui_time*1000:.1f}ms, restore: {restore_time*1000:.1f}ms, append={progressive_append}, lines={len(self._native_git_log_lines)})")
                    
                    # If update took significant time, it's likely causing the "stuck" scroll
                    if update_total > 0.05:  # More than 50ms
                        _log_timing_message(f"[TIMING] [RENDER] [LOG] [LAG] WARNING: update_ui took {update_total*1000:.1f}ms - likely causing scroll to stick")
                    
                    # Update cache
                    self._cached_branch = branch
                    self._cached_branch_info = branch_info.copy()
                    
                    self._native_git_log_loading = False
                
                # Call UI update from main thread
                if hasattr(self, 'app') and self.app:
                    self.app.call_from_thread(update_ui)
                else:
                    update_ui()
                
                load_time = time.perf_counter() - load_start
                _log_timing_message(f"[TIMING] [SCROLL] [LOG] Background load: {load_time*1000:.1f}ms total")
                
            except Exception as e:
                # On error, show error message via main thread
                error_text = Text()
                error_text.append(f"Error showing native git log: {e}\n", style="red")
                if hasattr(self, 'app') and self.app:
                    self.app.call_from_thread(lambda: self.update(error_text))
                else:
                    self.update(error_text)
                self._native_git_log_loading = False
        
        # Start background thread
        thread = threading.Thread(target=load_log_in_background, daemon=True)
        thread.start()
    
    def _show_native_git_log_pty(self, branch: str, branch_info: dict, git_service, append: bool = False) -> None:
        """
        Display native git log using PTY streaming for real-time output.
        This provides progressive display and better UX, especially for large repos.
        
        Runs in background thread to avoid blocking UI.
        """
        from rich.text import Text
        from rich.console import Group
        from pathlib import Path
        from pygitzen.pty_utils import stream_git_command_pty
        import threading
        
        # Prevent concurrent loads
        if self._native_git_log_loading:
            return
        self._native_git_log_loading = True
        
        # Get app reference for call_from_thread
        # LogPane is a Static widget, need to get app from parent
        app = None
        try:
            if hasattr(self, 'app'):
                app = self.app
            elif hasattr(self, 'parent') and hasattr(self.parent, 'app'):
                app = self.parent.app
        except:
            pass
        
        def stream_in_background():
            """Stream git log output in background thread."""
            try:
                # Get repo path from git_service (same logic as subprocess version)
                repo_path = None
                
                # Method 1: Direct attribute access
                try:
                    if hasattr(git_service, 'repo_path'):
                        repo_path = git_service.repo_path
                except (AttributeError, TypeError):
                    pass
                
                # Method 2: Use getattr (works even if hasattr returns False for cython)
                if repo_path is None:
                    try:
                        repo_path = getattr(git_service, 'repo_path', None)
                    except (AttributeError, TypeError):
                        pass
                
                # Method 3: Try via repo.path
                if repo_path is None:
                    try:
                        if hasattr(git_service, 'repo'):
                            repo = getattr(git_service, 'repo', None)
                            if repo and hasattr(repo, 'path'):
                                repo_path = getattr(repo, 'path', None)
                    except (AttributeError, TypeError):
                        pass
                
                # Convert to Path object
                if repo_path:
                    if isinstance(repo_path, str):
                        repo_path = Path(repo_path)
                    elif not isinstance(repo_path, Path):
                        repo_path = Path(str(repo_path))
                else:
                    repo_path = Path(".")
                
                # If appending, increase the limit; otherwise reset
                if not append:
                    self._native_git_log_count = 50
                    self._native_git_log_lines = []
                else:
                    self._native_git_log_count += 50
                
                # Build git command (same as subprocess version)
                cmd = ['git', 'log', '--graph', '--color=always', '--abbrev-commit', '--decorate', f'-{self._native_git_log_count}']
                
                # Add branch if specified
                if branch and branch.strip():
                    if branch.startswith('refs/'):
                        cmd.append(branch)
                    elif '/' in branch:
                        cmd.append(f'refs/heads/{branch}')
                    else:
                        cmd.append(branch)
                
                # Prepare for progressive display
                new_log_lines = []
                batch_size = 10  # Update UI every 10 lines for performance
                
                # Stream output using PTY and collect lines
                for rich_line in stream_git_command_pty(
                    cmd,
                    repo_path,
                    timeout=30.0,
                    max_lines=None  # No limit, use git's --max-count
                ):
                    new_log_lines.append(rich_line)
                    
                    # Update UI periodically (every batch_size lines) via main thread
                    if len(new_log_lines) % batch_size == 0:
                        if app:
                            app.call_from_thread(
                                lambda: self._update_log_pane_ui(branch, branch_info, new_log_lines.copy(), append)
                            )
                        else:
                            self._update_log_pane_ui(branch, branch_info, new_log_lines, append)
                
                # Final update with all lines via main thread
                if app:
                    app.call_from_thread(
                        lambda: self._update_log_pane_ui(branch, branch_info, new_log_lines, append)
                    )
                    app.call_from_thread(
                        lambda: setattr(self, '_cached_branch', branch)
                    )
                    app.call_from_thread(
                        lambda: setattr(self, '_cached_branch_info', branch_info.copy())
                    )
                else:
                    self._update_log_pane_ui(branch, branch_info, new_log_lines, append)
                    self._cached_branch = branch
                    self._cached_branch_info = branch_info.copy()
                    
            except Exception as e:
                # Show error message and fallback via main thread
                error_text = Text()
                error_text.append(f"Error streaming git log: {e}\n", style="red")
                if app:
                    app.call_from_thread(lambda: self.update(error_text))
                    app.call_from_thread(
                        lambda: self._show_native_git_log(branch, branch_info, git_service, append=append)
                    )
                else:
                    self.update(error_text)
                    self._show_native_git_log(branch, branch_info, git_service, append=append)
            finally:
                if app:
                    app.call_from_thread(lambda: setattr(self, '_native_git_log_loading', False))
                else:
                    self._native_git_log_loading = False
        
        # Start streaming in background thread
        thread = threading.Thread(target=stream_in_background, daemon=True)
        thread.start()
        
        # Return immediately - streaming happens in background
        # Show initial "Loading..." message
        loading_text = Text()
        loading_text.append("Loading git log...", style="dim white")
        self.update(loading_text)
    
    def _update_log_pane_ui(self, branch: str, branch_info: dict, new_log_lines: list, append: bool) -> None:
        """Helper method to update log pane UI with new lines.
        
        Phase 2: Added timing diagnostics and scroll position preservation to prevent "stuck" scroll.
        """
        import time
        from rich.console import Group
        
        update_start = time.perf_counter()
        
        # Phase 2: Preserve scroll position before update to prevent "stuck" scroll
        # Get the scroll container (parent of log pane)
        scroll_container = None
        preserved_scroll_y = 0
        try:
            # Try to get the scroll container from the app
            if hasattr(self, 'app') and self.app:
                scroll_container = self.app.query_one("#patch-scroll-container", None)
                if scroll_container and hasattr(scroll_container, 'scroll_y'):
                    preserved_scroll_y = scroll_container.scroll_y
        except:
            pass
        
        # If appending, only add new lines
        if append and self._native_git_log_lines:
            existing_content_lines = len(self._native_git_log_lines) - 2  # Subtract header and empty line
            if existing_content_lines < len(new_log_lines):
                new_lines_to_add = new_log_lines[existing_content_lines:]
                self._native_git_log_lines.extend(new_lines_to_add)
        else:
            # First load - build full content with header
            log_lines = []
            header = self._build_header(branch, branch_info)
            log_lines.append(header)
            log_lines.append(Text())  # Empty line
            log_lines.extend(new_log_lines)
            self._native_git_log_lines = log_lines
        
        # Time the Group creation and update
        group_start = time.perf_counter()
        if self._native_git_log_lines:
            full_content = Group(*self._native_git_log_lines)
            group_time = time.perf_counter() - group_start
            
            update_ui_start = time.perf_counter()
            self.update(full_content)
            update_ui_time = time.perf_counter() - update_ui_start
        else:
            group_time = 0
            update_ui_start = time.perf_counter()
            self.update(Text())
            update_ui_time = time.perf_counter() - update_ui_start
        
        update_total = time.perf_counter() - update_start
        
        # Log timing if significant
        if update_total > 0.05:  # More than 50ms
            _log_timing_message(f"[TIMING] [RENDER] [LOG] _update_log_pane_ui: {update_total*1000:.1f}ms total (Group: {group_time*1000:.1f}ms, update: {update_ui_time*1000:.1f}ms, append={append}, lines={len(new_log_lines)})")
        
        # Phase 2: Restore scroll position after update to prevent "stuck" scroll
        if scroll_container and hasattr(scroll_container, 'scroll_y') and preserved_scroll_y > 0:
            try:
                scroll_container.scroll_y = preserved_scroll_y
                if update_total > 0.05:
                    _log_timing_message(f"[TIMING] [SCROLL] [LOG] Scroll position restored: {preserved_scroll_y}")
            except Exception as e:
                if update_total > 0.05:
                    _log_timing_message(f"[TIMING] [SCROLL] [LOG] Failed to restore scroll position: {e}")
    
    def _build_graph_structure(self, commits: list[CommitInfo], git_service) -> dict:
        """
        Build graph structure showing branch relationships with proper tracking of divergence and merging.
        Returns dict mapping commit SHA to graph info with column tracking, active columns, and branch state.
        """
        graph_info = {}
        commit_shas = [_normalize_commit_sha(c.sha) for c in commits]
        sha_to_index = {sha: i for i, sha in enumerate(commit_shas)}
        
        # Build parent/child relationships
        for commit in commits:
            normalized_sha = _normalize_commit_sha(commit.sha)
            commit_refs = {}
            if git_service is not None:
                try:
                    commit_refs = git_service.get_commit_refs(normalized_sha)
                except:
                    pass
            
            parents = commit_refs.get("merge_parents", [])
            # For non-merge commits, get first parent
            if not parents:
                try:
                    if git_service is not None:
                        commit_bytes = bytes.fromhex(normalized_sha)
                        commit_obj = git_service.repo[commit_bytes]
                        if commit_obj.parents:
                            parents = [p.hex() for p in commit_obj.parents[:1]]  # First parent only for non-merge
                except:
                    pass
            
            graph_info[commit.sha] = {
                'parents': parents,
                'children': [],
                'is_merge': commit_refs.get("is_merge", False),
                'column': 0,
                'index': sha_to_index.get(normalized_sha, 0),
                'diverges': False,  # True if this commit has multiple children (branch point)
                'merges': False,  # True if this commit merges multiple branches
            }
        
        # Build child relationships
        for sha, info in graph_info.items():
            for parent_sha in info['parents']:
                parent_normalized = _normalize_commit_sha(parent_sha)
                # Find parent in our commits list
                for commit in commits:
                    if _normalize_commit_sha(commit.sha) == parent_normalized:
                        if commit.sha not in graph_info:
                            graph_info[commit.sha] = {'parents': [], 'children': [], 'is_merge': False, 'column': 0, 'index': 0, 'diverges': False, 'merges': False}
                        graph_info[commit.sha]['children'].append(sha)
                        break
        
        # Mark divergence points (commits with multiple children)
        for sha, info in graph_info.items():
            if len(info['children']) > 1:
                info['diverges'] = True
        
        # Mark merge points
        for sha, info in graph_info.items():
            if info['is_merge'] and len(info['parents']) >= 2:
                info['merges'] = True
        
        # Calculate columns using a proper graph algorithm
        # Track active columns and assign commits to columns based on parent relationships
        commit_to_column = {}
        next_column = 0
        # Track which columns are active at each commit index
        columns_at_index = {}  # index -> set of active column numbers
        
        for i, commit in enumerate(commits):
            sha = commit.sha
            info = graph_info.get(sha, {'parents': [], 'children': [], 'is_merge': False, 'column': 0, 'index': i, 'diverges': False, 'merges': False})
            
            if i == 0:
                # First commit is always in column 0
                commit_to_column[sha] = 0
                info['column'] = 0
            else:
                # Find parent in our commits list
                parent_column = 0
                parent_found = False
                parent_columns = []
                
                if info['parents']:
                    # Check all parents to find the ones in our list
                    for parent_sha in info['parents']:
                        parent_normalized = _normalize_commit_sha(parent_sha)
                        for c in commits:
                            if _normalize_commit_sha(c.sha) == parent_normalized:
                                if c.sha in commit_to_column:
                                    col = commit_to_column[c.sha]
                                    parent_columns.append(col)
                                    if not parent_found:
                                        parent_column = col
                                    parent_found = True
                            break
                
                if info['is_merge'] and len(parent_columns) >= 2:
                    # Merge commit: use leftmost parent's column
                    leftmost_parent_col = min(parent_columns)
                    commit_to_column[sha] = leftmost_parent_col
                    info['column'] = leftmost_parent_col
                elif info['is_merge'] and len(info['parents']) >= 2:
                    # Merge commit but parents not in list - assign to new column temporarily
                    # This will be corrected when we see the actual merge
                    commit_to_column[sha] = parent_column if parent_found else 0
                    info['column'] = parent_column if parent_found else 0
                else:
                    # Regular commit: use parent's column (or column 0 if no parent found)
                    commit_to_column[sha] = parent_column
                    info['column'] = parent_column
            
            graph_info[sha] = info
        
        # Calculate active columns at each index (for drawing continuation lines)
        for i in range(len(commits)):
            active_cols = set()
            # Look ahead to see which columns will be active
            for j in range(i, len(commits)):
                future_commit = commits[j]
                future_sha = future_commit.sha
                future_info = graph_info.get(future_sha, {})
                future_col = future_info.get('column', 0)
                active_cols.add(future_col)
                
                # Also check if current commit is a parent of future commits
                future_parents = future_info.get('parents', [])
                current_sha = commits[i].sha
                for parent_sha in future_parents:
                    if _normalize_commit_sha(parent_sha) == _normalize_commit_sha(current_sha):
                        current_info = graph_info.get(current_sha, {})
                        active_cols.add(current_info.get('column', 0))
                        break
            
            columns_at_index[i] = active_cols
        
        # Store active columns in graph_info
        for sha, info in graph_info.items():
            idx = info.get('index', 0)
            info['active_columns'] = columns_at_index.get(idx, set())
        
        return graph_info
    
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
        
        # Build graph structure
        graph_structure = self._build_graph_structure(commits_to_render, git_service)
        
        # Build commit lines (this is the expensive part)
        commit_lines_start = time.perf_counter()
        for i, commit in enumerate(commits_to_render):
            # Get colored graph prefix for this commit if available
            normalized_sha = _normalize_commit_sha(commit.sha)
            git_graph_prefix_colored = self._cached_graph_prefixes_colored.get(normalized_sha)
            commit_line = self._build_commit_line(
                commit, i, actual_total, git_service, branch, 
                graph_structure, commits_to_render, git_graph_prefix_colored
            )
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
    
    def _format_relative_date(self, timestamp: int) -> str:
        """
        Format timestamp as relative date (e.g., "11 days ago", "3 weeks ago").
        
        Args:
            timestamp: Unix timestamp
        
        Returns:
            Relative date string like "11 days ago", "3 weeks ago", "2 months ago", etc.
        """
        from datetime import datetime, timezone
        import time
        
        now = datetime.now(timezone.utc)
        commit_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        delta = now - commit_time
        
        total_seconds = int(delta.total_seconds())
        
        if total_seconds < 60:
            return "just now"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif total_seconds < 604800:  # 7 days
            days = total_seconds // 86400
            return f"{days} day{'s' if days != 1 else ''} ago"
        elif total_seconds < 2592000:  # ~30 days
            weeks = total_seconds // 604800
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        elif total_seconds < 31536000:  # ~365 days
            months = total_seconds // 2592000
            return f"{months} month{'s' if months != 1 else ''} ago"
        else:
            years = total_seconds // 31536000
            return f"{years} year{'s' if years != 1 else ''} ago"
    
    def _calculate_graph_chars(self, commit: CommitInfo, index: int, total: int, graph_structure: dict, commits: list[CommitInfo]) -> str:
        """
        Calculate graph characters for a commit based on its position in the graph.
        Returns string like "*", "|", "\", "/", "|/", "|\", etc.
        
        Style 1 (ASCII): Uses *, |, |/, |\
        Style 2 (dots): Uses dots (●) and lines
        """
        commit_sha = _normalize_commit_sha(commit.sha)
        info = graph_structure.get(commit.sha, {'parents': [], 'children': [], 'is_merge': False, 'column': 0, 'diverges': False, 'merges': False})
        
        is_merge = info.get('is_merge', False)
        merges = info.get('merges', False)
        diverges = info.get('diverges', False)
        column = info.get('column', 0)
        
        if self.graph_style == "dots":
            # Dots style: use dot for commits
            return "●"
        
        # ASCII style
        # Check if this commit merges branches (has multiple parents from different columns)
        if merges or (is_merge and len(info.get('parents', [])) >= 2):
            # Check if any parent is in a different column
            parent_columns = []
            for parent_sha in info.get('parents', []):
                parent_normalized = _normalize_commit_sha(parent_sha)
                for c in commits:
                    if _normalize_commit_sha(c.sha) == parent_normalized:
                        parent_info = graph_structure.get(c.sha, {})
                        parent_columns.append(parent_info.get('column', 0))
                        break
            
            # If we have parents in different columns, this is a merge
            if len(set(parent_columns)) > 1:
                return "*"  # Commit marker, merge line will be shown separately
        
        # Check if this commit diverges (has multiple children in different columns)
        if diverges:
            children_columns = []
            for child_sha in info.get('children', []):
                child_normalized = _normalize_commit_sha(child_sha)
                for c in commits:
                    if _normalize_commit_sha(c.sha) == child_normalized:
                        child_info = graph_structure.get(c.sha, {})
                        children_columns.append(child_info.get('column', 0))
                        break
            
            # If we have children in different columns, this is a divergence
            if len(set(children_columns)) > 1:
                return "*"  # Commit marker, divergence will be shown in prefix
        
        # Regular commit: use *
        return "*"
    
    def _get_active_columns_at_index(self, index: int, commits: list[CommitInfo], graph_structure: dict) -> set:
        """Get set of active column numbers at a given commit index."""
        active_columns = set()
        for i in range(index, len(commits)):
            commit = commits[i]
            sha = commit.sha
            info = graph_structure.get(sha, {})
            column = info.get('column', 0)
            active_columns.add(column)
        return active_columns
    
    def _calculate_graph_prefix(self, commit: CommitInfo, index: int, total: int, graph_structure: dict, commits: list[CommitInfo], line_type: str = "commit", git_graph_prefix_colored: str = None) -> Text:
        """
        Calculate graph prefix for each line of a commit.
        line_type: "commit", "merge", "author", "date", "message", "signed_off"
        Returns Rich Text object with colors if git_graph_prefix_colored is provided, otherwise returns plain string.
        
        Algorithm: Track active columns and show proper graph characters for merges/divergences.
        If git colored graph is available, use it directly for accurate visualization.
        """
        from rich.text import Text
        from pygitzen.git_graph import strip_ansi_codes, convert_graph_prefix_to_rich
        
        commit_sha = _normalize_commit_sha(commit.sha)
        info = graph_structure.get(commit.sha, {'parents': [], 'children': [], 'is_merge': False, 'column': 0, 'diverges': False, 'merges': False, 'active_columns': set()})
        
        # If we have git's colored graph prefix, use it directly (most accurate)
        if git_graph_prefix_colored and line_type == "commit":
            # Handle both string and list formats
            if isinstance(git_graph_prefix_colored, list) and len(git_graph_prefix_colored) > 0:
                main_prefix_colored = git_graph_prefix_colored[0]
            else:
                main_prefix_colored = git_graph_prefix_colored
            # Use git's colored prefix for commit line
            return convert_graph_prefix_to_rich(main_prefix_colored)
        
        # For continuation lines, we need to derive from git's prefix or calculate
        if git_graph_prefix_colored and line_type != "commit":
            # Handle both string and list formats
            if isinstance(git_graph_prefix_colored, list) and len(git_graph_prefix_colored) > 0:
                main_prefix_colored = git_graph_prefix_colored[0]
            else:
                main_prefix_colored = git_graph_prefix_colored
            
            # For continuation lines, replace * with | and remove \ characters
            plain_prefix = strip_ansi_codes(main_prefix_colored)
            continuation_prefix_plain = plain_prefix.replace('*', '|').replace('●', '│')
            continuation_prefix_plain = continuation_prefix_plain.replace('\\', ' ')
            # Normalize whitespace - preserve column structure
            leading_spaces = len(continuation_prefix_plain) - len(continuation_prefix_plain.lstrip())
            continuation_prefix_plain = '|' + (' ' * max(1, leading_spaces))
            # Create Rich Text - try to preserve colors from git prefix
            # For now, use dim white for continuation lines (could enhance to preserve colors)
            result = Text()
            result.append(continuation_prefix_plain, style="dim white")
            return result
        
        is_merge = info.get('is_merge', False)
        merges = info.get('merges', False)
        diverges = info.get('diverges', False)
        column = info.get('column', 0)
        active_columns = info.get('active_columns', set())
        
        # If this is the last commit, no continuation lines
        if index >= total - 1:
            if self.graph_style == "dots":
                return Text("  ", style="dim white")
            # For ASCII style, show empty space for last commit
            if column == 0:
                return Text("  ", style="dim white")
            else:
                # Show spaces for columns before this one
                return Text("  " + "  " * column, style="dim white")
        
        # For merge line, use backslash
        if line_type == "merge" and (is_merge or merges):
            # Check if we have git's merge continuation line
            if git_graph_prefix_colored and isinstance(git_graph_prefix_colored, list) and len(git_graph_prefix_colored) > 1:
                # Use git's merge continuation line (|\)
                for cont_line in git_graph_prefix_colored[1:]:
                    if '\\' in strip_ansi_codes(cont_line):
                        return convert_graph_prefix_to_rich(cont_line)
            
            # Fallback: calculate merge line
            if self.graph_style == "dots":
                # Dots style: use line for merge
                if column == 0:
                    return Text("│\\ ", style="dim white")
                else:
                    return Text("│\\ " + "  " * column, style="dim white")
            else:
                # ASCII style
                if column == 0:
                    return Text("|\\ ", style="dim white")
                else:
                    return Text("|\\ " + "  " * column, style="dim white")
        
        # Check if this commit has a direct future child
        has_direct_future_child = False
        next_commit_column = None
        for i in range(index + 1, min(index + 50, total, len(commits))):
            future_commit = commits[i]
            future_sha = _normalize_commit_sha(future_commit.sha)
            future_info = graph_structure.get(future_commit.sha, {})
            future_parents = future_info.get('parents', [])
            # Check if this commit is a direct parent of a future commit
            for parent_sha in future_parents:
                if _normalize_commit_sha(parent_sha) == commit_sha:
                    has_direct_future_child = True
                    next_commit_column = future_info.get('column', 0)
                    break
            if has_direct_future_child:
                break
        
        # Check if this commit diverges (has children in different columns)
        if diverges and line_type == "commit":
            # Find the next commit that's a child of this one
            child_columns = set()
            for child_sha in info.get('children', []):
                child_normalized = _normalize_commit_sha(child_sha)
                for c in commits:
                    if _normalize_commit_sha(c.sha) == child_normalized:
                        child_info = graph_structure.get(c.sha, {})
                        child_columns.add(child_info.get('column', 0))
                        break
            
            # If we have children in different columns, show divergence
            if len(child_columns) > 1:
                # Find the rightmost child column
                rightmost_child_col = max(child_columns)
                if rightmost_child_col > column:
                    # Branch diverges to the right
                    if self.graph_style == "dots":
                        if column == 0:
                            return Text("│/ ", style="dim white")
                        else:
                            return Text("│/ " + "  " * (column - 1), style="dim white")
                    else:
                        if column == 0:
                            return Text("|/ ", style="dim white")
                        else:
                            return Text("|/ " + "  " * (column - 1), style="dim white")
        
        # Build prefix based on column and active columns
        if self.graph_style == "dots":
            # Dots style: use vertical lines
            if column == 0:
                if has_direct_future_child or column in active_columns:
                    if line_type == "commit":
                        return Text("● ", style="dim white")  # Dot for commit
                    else:
                        return Text("│ ", style="dim white")  # Vertical line for continuation
                else:
                    if line_type == "commit":
                        return Text("● ", style="dim white")
                    else:
                        return Text("  ", style="dim white")
            else:
                # Multiple columns: show lines for each column
                prefix = ""
                for col in range(column):
                    if col in active_columns or col < column:
                        prefix += "│ "
                    else:
                        prefix += "  "
                
                if line_type == "commit":
                    prefix += "● "  # Dot for commit
                elif has_direct_future_child or column in active_columns:
                    prefix += "│ "  # Vertical line
                else:
                    prefix += "  "
                
                return Text(prefix, style="dim white")
        else:
            # ASCII style
            if column == 0:
                if has_direct_future_child or column in active_columns:
                    if line_type == "commit":
                        return Text("* ", style="dim white")  # Star for commit
                    else:
                        return Text("| ", style="dim white")  # Vertical line for continuation
                else:
                    if line_type == "commit":
                        return Text("* ", style="dim white")
                    else:
                        return Text("  ", style="dim white")
            else:
                # Multiple columns: show lines for each column
                prefix = ""
                for col in range(column):
                    if col in active_columns or col < column:
                        prefix += "| "
                    else:
                        prefix += "  "
                
                if line_type == "commit":
                    prefix += "* "  # Star for commit
                elif has_direct_future_child or column in active_columns:
                    prefix += "| "  # Vertical line
                else:
                    prefix += "  "
                
                return Text(prefix, style="dim white")
    
    def _build_commit_line(self, commit: CommitInfo, index: int, total: int, git_service, branch: str, graph_structure: dict = None, commits: list[CommitInfo] = None, git_graph_prefix_colored: str = None) -> Text:
        """
        Build full commit display with graph visualization, 'commit' prefix, Merge: line, full message, and Signed-off-by.
        Format matches git log --graph style.
        
        Args:
            git_graph_prefix_colored: Colored graph prefix from git (with ANSI codes) if available
        """
        from rich.text import Text
        from datetime import datetime
        from time import timezone
        
        # Normalize SHA format (fix for Cython version hex-encoded ASCII issue)
        commit_sha = _normalize_commit_sha(commit.sha)
        short_sha = commit_sha[:8] if len(commit_sha) >= 8 else commit_sha
        
        # Calculate graph prefix using graph structure
        commits_list = commits if commits is not None else []
        if graph_structure is None:
            graph_prefix = Text("│ " if index < total - 1 else "  ", style="dim white")
        else:
            graph_prefix = self._calculate_graph_prefix(commit, index, total, graph_structure, commits_list, "commit", git_graph_prefix_colored)
            # Ensure graph_prefix is a Text object
            if isinstance(graph_prefix, str):
                graph_prefix = Text(graph_prefix, style="dim white")
        
        # Format date as relative (e.g., "11 days ago")
        commit_date = self._format_relative_date(commit.timestamp)
        
        # Get commit refs and merge info
        commit_refs = {}
        is_merge = False
        merge_parents = []
        if git_service is not None:
            try:
                normalized_sha = _normalize_commit_sha(commit.sha)
                commit_refs = git_service.get_commit_refs(normalized_sha)
                is_merge = commit_refs.get("is_merge", False)
                merge_parents = commit_refs.get("merge_parents", [])
            except Exception:
                pass
        
        # Get full commit message and Signed-off-by lines
        full_message_info = {}
        if git_service is not None:
            try:
                normalized_sha = _normalize_commit_sha(commit.sha)
                full_message_info = git_service.get_commit_message_full(normalized_sha)
            except Exception:
                pass
        
        full_message = full_message_info.get("message", commit.summary)
        signed_off_by = full_message_info.get("signed_off_by", [])
        
        # Build refs for display with colors
        refs_parts = []
        refs_styles = []  # Store styles for each ref part
        
        if commit_refs.get("is_head"):
            if branch:
                refs_parts.append(f"HEAD -> {branch}")
                refs_styles.append("green")  # HEAD -> branch in green
            else:
                refs_parts.append("HEAD")
                refs_styles.append("green")
        
        local_branches = [b for b in commit_refs.get("branches", []) if b != branch]
        for b in local_branches[:2]:
            refs_parts.append(b)
            refs_styles.append("cyan")  # Local branches in cyan
        
        remote_branches = [rb for rb in commit_refs.get("remote_branches", []) if rb.startswith("origin/")]
        for rb in remote_branches[:1]:
            refs_parts.append(rb)
            refs_styles.append("dim white")  # Remote branches in dim white
        
        tags = commit_refs.get("tags", [])
        for tag in tags[:1]:
            refs_parts.append(f"tag: {tag}")
            refs_styles.append("yellow")  # Tags in yellow
        
        # Build commit display
        commit_display = Text()
        
        # Line 1: graph prefix (includes * or ●) + commit SHA (refs) [Merge branch 'xxx' if merge]
        # graph_prefix is already a Text object with colors
        commit_display.append(graph_prefix)
        commit_display.append("commit ", style="dim white")
        # Use full SHA (at least 10 chars, show full if available)
        full_sha = commit_sha[:10] if len(commit_sha) >= 10 else commit_sha
        commit_display.append(full_sha, style="yellow")  # SHA in yellow/orange
        if refs_parts:
            commit_display.append(" (", style="dim white")
            for i, (ref_part, ref_style) in enumerate(zip(refs_parts, refs_styles)):
                if i > 0:
                    commit_display.append(", ", style="dim white")
                commit_display.append(ref_part, style=ref_style)
            commit_display.append(")", style="dim white")
        
        # For merge commits only, add "Merge branch 'xxx'" on first line
        # Regular commits: no summary on first line
        if is_merge and commit.summary.startswith("Merge"):
            commit_display.append(" ", style="white")
            commit_display.append(commit.summary, style="white")
        
        commit_display.append("\n", style="white")
        
        # Check for merge continuation line from git (|\)
        normalized_sha = _normalize_commit_sha(commit.sha)
        git_prefix_colored = self._cached_graph_prefixes_colored.get(normalized_sha)
        merge_cont_line = None
        diverge_cont_line = None
        
        if git_prefix_colored and isinstance(git_prefix_colored, list) and len(git_prefix_colored) > 1:
            # Check continuation lines for merge (|\) or divergence (|/)
            from pygitzen.git_graph import strip_ansi_codes, convert_graph_prefix_to_rich
            for cont_line in git_prefix_colored[1:]:
                plain_cont = strip_ansi_codes(cont_line)
                if '\\' in plain_cont:
                    merge_cont_line = cont_line
                elif '/' in plain_cont:
                    diverge_cont_line = cont_line
        
        # Add merge continuation line if present (appears as separate line after commit)
        if merge_cont_line:
            from pygitzen.git_graph import convert_graph_prefix_to_rich
            merge_cont_rich = convert_graph_prefix_to_rich(merge_cont_line)
            commit_display.append(merge_cont_rich)
            commit_display.append("\n", style="white")
        
        # Add divergence continuation line if present (appears after commit line)
        if diverge_cont_line:
            from pygitzen.git_graph import convert_graph_prefix_to_rich
            diverge_cont_rich = convert_graph_prefix_to_rich(diverge_cont_line)
            commit_display.append(diverge_cont_rich)
        commit_display.append("\n", style="white")
        
        # Line 2: Merge: parent1 parent2 ... (only for merge commits)
        if is_merge and len(merge_parents) >= 2:
            # Use regular continuation prefix (|) not merge prefix (|\)
            continuation_prefix = self._calculate_graph_prefix(commit, index, total, graph_structure or {}, commits_list, "author", git_graph_prefix_colored) if graph_structure else (Text("│ ", style="dim white") if index < total - 1 else Text("  ", style="dim white"))
            if isinstance(continuation_prefix, str):
                continuation_prefix = Text(continuation_prefix, style="dim white")
            commit_display.append(continuation_prefix)
            # Convert parent SHAs to 10-char short format
            parent_shas_short = [p[:10] for p in merge_parents]
            commit_display.append(f"Merge: {' '.join(parent_shas_short)}", style="dim white")
            commit_display.append("\n", style="white")
        
        # Calculate continuation prefix (vertical lines, not commit marker) - reuse if already calculated
        if 'continuation_prefix' not in locals():
            continuation_prefix = self._calculate_graph_prefix(commit, index, total, graph_structure or {}, commits_list, "author", git_graph_prefix_colored) if graph_structure else (Text("│ ", style="dim white") if index < total - 1 else Text("  ", style="dim white"))
            # Ensure continuation_prefix is Text
            if isinstance(continuation_prefix, str):
                continuation_prefix = Text(continuation_prefix, style="dim white")
        
        # Line 3: Author
        commit_display.append(continuation_prefix)
        commit_display.append("Author: ", style="dim white")
        commit_display.append(commit.author, style="white")
        commit_display.append("\n", style="white")
        
        # Line 4: Date
        commit_display.append(continuation_prefix)
        commit_display.append("Date: ", style="dim white")
        commit_display.append(commit_date, style="dim white")
        commit_display.append("\n", style="white")
        
        # Line 5: Blank line
        commit_display.append(continuation_prefix)
        commit_display.append("\n", style="white")
        
        # Lines 6+: Full commit message
        message_lines = full_message.split('\n')
        for msg_line in message_lines:
            if msg_line.strip():  # Skip empty lines in message
                commit_display.append(continuation_prefix)
                commit_display.append(msg_line, style="white")
                commit_display.append("\n", style="white")
        
        # Blank line before Signed-off-by
        if signed_off_by:
            commit_display.append(continuation_prefix)
            commit_display.append("\n", style="white")
        
        # Signed-off-by lines
        for signer in signed_off_by:
            commit_display.append(continuation_prefix)
            commit_display.append(f"Signed-off-by: {signer}", style="dim white")
            commit_display.append("\n", style="white")
        
        return commit_display


class PatchPane(Static):
    """Patch pane showing commit details and diff."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Patch"
    
    def show_commit_info(self, commit: CommitInfo, diff_text: str | Text, is_partial: bool = False) -> None:
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
        
        # Normalize SHA format (fix for Cython version hex-encoded ASCII issue)
        commit_sha = _normalize_commit_sha(commit.sha)
        
        
        # Create commit header
        header_text = f"""commit {commit_sha}
Author: {commit.author}
Date: {commit_date}

{commit.summary}

"""
        
        # Create diff content with proper colors
        if diff_text:
            try:
                # Handle both string and Rich Text objects
                if isinstance(diff_text, Text):
                    # Already a Rich Text object (from PTY streaming) - use directly
                    diff_text_obj = diff_text
                else:
                    # String input - parse ANSI or use syntax highlighting
                    if is_partial:
                        # For partial updates, parse ANSI colors from git output
                        try:
                            diff_text_obj = Text.from_ansi(diff_text)
                        except:
                            # Fallback to plain text
                            diff_text_obj = Text(diff_text, style="white")
                    else:
                        # Use Rich syntax highlighting for complete diff
                        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
                        # Convert Syntax to Text for consistency
                        from rich.console import Console
                        console = Console()
                        diff_text_obj = Text()
                        # Render syntax to get colored text
                        with console.capture() as capture:
                            console.print(syntax)
                        diff_text_obj = Text.from_ansi(capture.get())
                
                full_content = Text(header_text, style="white") + diff_text_obj
                if is_partial:
                    # Add "Loading..." indicator for partial updates
                    full_content.append("\n...", style="dim white")
                # Note: For complete diff, we use the diff_text_obj directly (already formatted)
            except Exception as e:
                # Fallback to manual color formatting with Text only
                # Handle both string and Text objects
                if isinstance(diff_text, Text):
                    # Convert Text object to string for processing
                    # Extract plain text from Rich Text object
                    diff_text_str = str(diff_text.plain)
                else:
                    diff_text_str = str(diff_text)
                
                lines = diff_text_str.split('\n')
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
                if is_partial:
                    full_content.append("\n...", style="dim white")
        else:
            # Both are Text objects, so concatenation works
            full_content = Text(header_text, style="white") + Text(diff_text or "No diff available", style="white")
        
        self.update(full_content)
    
    def show_stash_info(self, stash: StashInfo, diff_text: str, stat_text: str = "") -> None:
        """Show stash details and diff in the patch pane."""
        from rich.text import Text
        from rich.console import Console
        from rich.syntax import Syntax
        from rich.console import Group
        
        # Create stash header with newline after message
        header_text = f"""stash@{stash.index}: On {stash.branch}: {stash.message}

"""
        
        # Build content with git's native colors preserved
        full_content = Text(header_text)
        
        # Add stat summary if available (preserve git's native colors)
        # Ensure all lines start at column 0 (strip leading whitespace from each line)
        if stat_text:
            # Split into lines, strip leading whitespace from each, then rejoin
            # This ensures all stat lines align properly
            lines = stat_text.split('\n')
            cleaned_lines = [line.lstrip() for line in lines]
            stat_text_cleaned = '\n'.join(cleaned_lines)
            
            try:
                stat_text_obj = Text.from_ansi(stat_text_cleaned)
            except:
                stat_text_obj = Text(stat_text_cleaned)
            full_content += stat_text_obj
            full_content += Text("\n\n")
        
        # Display diff as-is (with native git colors preserved via ANSI codes)
        if diff_text:
            # Parse ANSI color codes from git output and convert to Rich Text
            # Rich can parse ANSI codes using Text.from_ansi()
            try:
                diff_text_obj = Text.from_ansi(diff_text)
            except:
                # Fallback to plain text if ANSI parsing fails
                diff_text_obj = Text(diff_text)
            
            # Combine header and diff
            full_content += diff_text_obj
        else:
            # No diff available
            full_content += Text("No diff available", style="dim white")
        
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
    
    #branches-tabbed {
        height: 9;
        border: solid white;
        background: #1e1e1e;
    }
    
    #branches-tabbed:focus,
    #branches-tabbed:focus-within {
        border: solid green;
    }
    
    #branches-tabbed > TabbedContent > Tab {
        background: #1e1e1e;
        color: #cccccc;
    }
    
    #branches-tabbed > TabbedContent > Tab.--active {
        background: #2d2d2d;
        color: white;
    }
    
    #branches-pane {
        height: 1fr;
        border: none;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #remotes-pane {
        height: 1fr;
        border: none;
        background: #1e1e1e;
        overflow: auto;
    }
    
    #tags-pane {
        height: 1fr;
        border: none;
        background: #1e1e1e;
        overflow: auto;
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
        height: 8;
        border: solid white;
        background: #1e1e1e;
        overflow: auto;
        scrollbar-size: 1 1;
    }
    
    #stash-pane:focus {
        border: solid green;
    }
    
    #patch-scroll-container {
        height: 1fr;
        border: solid white;
        overflow: auto;
        overflow-x: auto;
        overflow-y: auto;
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
        width: auto;
        min-width: 100%;
        text-wrap: wrap;
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
    
    /* Selected/highlighted item styling for stash pane */
    #stash-pane ListItem.--highlight {
        background: #357ABD; /* blue for strong contrast */
        color: #ffffff;
        text-style: bold;
    }
    
    #stash-pane ListItem.--highlight:focus {
        background: #2f6aa3; /* slightly darker when focused */
        color: #ffffff;
        text-style: bold;
    }
    
    #stash-pane ListItem.highlighted-stash {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }
    
    #stash-pane ListItem.highlighted-stash:focus {
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
    #stash-pane ListItem.--highlight > Static {
        background: transparent;
        color: #ffffff;
    }
    #stash-pane ListItem.highlighted-stash > Static {
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
        Binding("g", "toggle_graph_style", "Toggle Graph Style"),
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
                except Exception as e:
                    # If Cython initialization fails, fall back to Python
                    import sys
                    import traceback
                    cython_init_elapsed = time.perf_counter() - cython_init_start
                    error_msg = f"Error initializing Cython extension, falling back to Python: {type(e).__name__}: {e}\n"
                    error_msg += f"Traceback:\n{traceback.format_exc()}\n"
                    _log_timing_message(f"[TIMING] GitServiceCython.__init__ (FAILED): {cython_init_elapsed:.4f}s")
                    _log_timing_message(error_msg)
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
            self.remotes: list[BranchInfo] = []
            self.tags: list[TagInfo] = []  # Tags for tags pane
            self.commits: list[CommitInfo] = []  # Commits for commits pane (left side)
            self.stashes: list[StashInfo] = []  # Stashes for stash pane
            self.all_commits: list[CommitInfo] = []  # Store all commits for search (commits pane)
            self.log_commits: list[CommitInfo] = []  # Commits for log pane (right side) - separate from commits pane
            self.repo_path = repo_dir
            
            # PTY Task Manager for commit diff streaming (similar to Lazygit's ViewBufferManager)
            # This ensures only one PTY task runs at a time, with automatic cancellation
            from pygitzen.pty_task_manager import PtyTaskManager
            self._pty_diff_task_manager = PtyTaskManager(log_callback=_log_timing_message)
            self.page_size = 300  # For commits pane
            # Reasonable limit to prevent blocking (dulwich iteration is slow for 78k+ commits)
            self.log_initial_size = 200  # Load 200 commits initially (can load more via pagination)
            self.total_commits = 0
            self.loaded_commits = 0
            self._loading_commits = False
            self._loading_file_status = False
            self._loading_stashes = False
            self._loading_tags = False
            self._search_query: str = ""
            # Phase 4: Debouncing for file status updates
            self._last_file_status_update_time = 0.0
            self._file_status_debounce_delay = 0.5  # 500ms debounce
            self._view_mode: str = "patch"  # "patch" or "log"
            
            # Thread-safe queue for UI updates from background threads
            self._ui_update_queue = queue.Queue()
            
            # PHASE 2: Cache with proper invalidation
            # Cache commit counts per branch
            self._commit_count_cache: dict[str, int] = {}
            # Cache remote branch existence per branch
            self._remote_branch_cache: dict[str, bool] = {}
            # Cache remote commits per branch (set of commit SHAs)
            self._remote_commits_cache: dict[str, set[str]] = {}
            
            # Track HEAD SHA for invalidation detection
            # Maps branch -> HEAD SHA (for local branches)
            self._last_head_sha: dict[str, str] = {}
            # Maps branch -> remote HEAD SHA (for remote branches)
            self._last_remote_head_sha: dict[str, str] = {}
            
            # Cache branch sync status (behind/ahead counts)
            self._branch_sync_status_cache: dict[str, dict] = {}
            
            # Phase 2: Pre-buffering for virtual scrolling
            self._prebuffering = False  # Track if pre-buffering is in progress
            self._last_scroll_time = 0.0  # For debouncing scroll events
            self._scroll_debounce_delay = 0.1  # 100ms debounce delay
            self._prebuffer_batch_size = 100  # Load 100 commits ahead
            self._prebuffer_threshold = 0.8  # Start pre-buffering at 80% of loaded commits
            
            # Phase 4: Real-time change detection (GitWatcher)
            self._git_watcher = None  # Will be initialized in on_mount
            
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
                # Create branches/remotes/tags panes
                self.branches_pane = BranchesPane(id="branches-pane")
                self.remotes_pane = RemotesPane(id="remotes-pane")
                self.tags_pane = TagsPane(id="tags-pane")
                self.tags_pane._parent_app = self  # Set parent reference for scroll monitoring
                self.commits_pane = CommitsPane(id="commits-pane")
                self.search_input = CommitSearchInput(id="commit-search-input")
                self.stash_pane = StashPane(id="stash-pane")
                self.stash_pane._parent_app = self  # Set parent reference for stash selection
                
                yield self.status_pane
                
                # Side-by-side containers for Staged and Changes panes
                with Horizontal(id="files-container"):
                    yield self.staged_pane
                    yield self.changes_pane
                
                # TabbedContent for branches/remotes/tags
                with TabbedContent(id="branches-tabbed", initial="branches-tab") as self.branches_tabbed:
                    with TabPane("Local branches", id="branches-tab"):
                        yield self.branches_pane
                    with TabPane("Remotes", id="remotes-tab"):
                        yield self.remotes_pane
                    with TabPane("Tags", id="tags-tab"):
                        yield self.tags_pane
                
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
        
        # Phase 4: Start GitWatcher for real-time change detection
        self._start_git_watcher()
        
        # Set up periodic processing of UI update queue from background threads
        self.set_interval(0.05, self._process_ui_update_queue)  # Check every 50ms
    
    def _start_git_watcher(self) -> None:
        """Start GitWatcher for real-time change detection."""
        try:
            from pygitzen.git_watcher import GitWatcher, ChangeEvent, ChangeType
            
            def handle_change(event: ChangeEvent) -> None:
                """Handle Git repository change events."""
                _log_timing_message(f"[GITWATCHER] Change detected: {event.change_type.value} (branch={event.branch}, tag={event.tag}, file={event.file})")
                
                # Smart refresh: Only refresh affected panes
                if event.change_type in (ChangeType.FILE_STAGED, ChangeType.FILE_UNSTAGED, ChangeType.FILE_CHANGED):
                    # File changes - refresh files pane and status pane
                    # Use call_from_thread to ensure it runs on main thread
                    try:
                        _log_timing_message(f"[GITWATCHER] Calling load_file_status_background from thread")
                        self.call_from_thread(self.load_file_status_background)
                        _log_timing_message(f"[GITWATCHER] load_file_status_background called successfully")
                    except Exception as e:
                        import traceback
                        error_msg = f"Error calling load_file_status_background: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                        _log_timing_message(f"[GITWATCHER] [ERROR] {error_msg}")
                        # Fallback: try calling directly (might work if we're already on main thread)
                        try:
                            self.load_file_status_background()
                        except Exception as e2:
                            _log_timing_message(f"[GITWATCHER] [ERROR] Fallback also failed: {e2}")
                elif event.change_type == ChangeType.NEW_COMMIT:
                    # New commit - refresh commits pane, log pane, and status
                    try:
                        _log_timing_message(f"[GITWATCHER] Calling _refresh_on_new_commit from thread")
                        self.call_from_thread(self._refresh_on_new_commit)
                        _log_timing_message(f"[GITWATCHER] _refresh_on_new_commit called successfully")
                    except Exception as e:
                        import traceback
                        error_msg = f"Error calling _refresh_on_new_commit: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                        _log_timing_message(f"[GITWATCHER] [ERROR] {error_msg}")
                elif event.change_type == ChangeType.NEW_TAG:
                    # New tag - refresh tags pane only
                    try:
                        self.call_from_thread(self._refresh_tags)
                    except Exception as e:
                        _log_timing_message(f"[GITWATCHER] [ERROR] Error calling _refresh_tags: {e}")
                elif event.change_type == ChangeType.TAG_DELETED:
                    # Tag deleted - refresh tags pane only
                    try:
                        self.call_from_thread(self._refresh_tags)
                    except Exception as e:
                        _log_timing_message(f"[GITWATCHER] [ERROR] Error calling _refresh_tags: {e}")
                elif event.change_type in (ChangeType.BRANCH_CREATED, ChangeType.BRANCH_DELETED):
                    # Branch created/deleted - refresh branches pane
                    try:
                        self.call_from_thread(self._refresh_branches)
                    except Exception as e:
                        _log_timing_message(f"[GITWATCHER] [ERROR] Error calling _refresh_branches: {e}")
                elif event.change_type == ChangeType.COMMIT_PUSHED:
                    # Commits pushed - update commit status in commits pane
                    try:
                        _log_timing_message(f"[GITWATCHER] Calling _refresh_commit_status from thread")
                        self.call_from_thread(self._refresh_commit_status)
                        _log_timing_message(f"[GITWATCHER] _refresh_commit_status called successfully")
                    except Exception as e:
                        import traceback
                        error_msg = f"Error calling _refresh_commit_status: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                        _log_timing_message(f"[GITWATCHER] [ERROR] {error_msg}")
            
            self._git_watcher = GitWatcher(
                repo_path=self.repo_path,
                on_change=handle_change,
                head_poll_interval=1.5,
                remote_poll_interval=8.0,
                use_watchdog=True
            )
            self._git_watcher.start()
            _log_timing_message("[GITWATCHER] Started real-time change detection")
        except Exception as e:
            _log_timing_message(f"[GITWATCHER] Failed to start: {e}")
            # Continue without watcher - manual refresh still works
    
    def _refresh_on_new_commit(self) -> None:
        """Refresh UI when new commit is detected."""
        # Refresh commits pane and log pane
        if self.active_branch:
            _log_timing_message(f"[GITWATCHER] Refreshing UI for new commit on branch {self.active_branch}")
            
            # CRITICAL: Clear commit status cache when new commit is detected
            # This ensures fresh status is fetched instead of using stale cache
            cache_keys_to_clear = [
                f"refs/heads/{self.active_branch}_unpushed",
                f"refs/heads/{self.active_branch}_merged",
                f"{self.active_branch}_unpushed",
                f"{self.active_branch}_merged",
            ]
            for key in cache_keys_to_clear:
                if key in self._remote_commits_cache:
                    del self._remote_commits_cache[key]
                    _log_timing_message(f"[GITWATCHER] Cleared cache key: {key}")
            
            # OPTIMIZATION: Call load_commits() for commits pane, and load_commits_for_log() with reset=False
            # to update log pane without triggering load_commits_full_history_background() (which causes duplicate refresh)
            self.load_commits(self.active_branch)
            # Use reset=False to avoid triggering load_commits_full_history_background() which causes duplicate refresh
            self.load_commits_for_log(self.active_branch, reset=False)
            self.update_status_info()
            
            # CRITICAL: Trigger commit status update after reloading commits
            # This ensures the new commit gets the correct status (green ✓, yellow ↑, or red -)
            # Use a small delay to ensure commits are loaded first
            import threading
            import time
            
            def trigger_status_update():
                time.sleep(0.2)  # Brief delay to let commits load
                if hasattr(self, 'commits') and self.commits:
                    _log_timing_message(f"[GITWATCHER] Triggering status update for {len(self.commits)} commits (cache cleared)")
                    # Get ref_spec and repo_path for status update
                    repo_path_str = str(self.repo_path)
                    ref_spec = f"refs/heads/{self.active_branch}"
                    
                    # CRITICAL: Clear cache again right before status update to ensure fresh fetch
                    # (in case load_commits repopulated it)
                    cache_keys_to_clear = [
                        f"refs/heads/{self.active_branch}_unpushed",
                        f"refs/heads/{self.active_branch}_merged",
                        f"{self.active_branch}_unpushed",
                        f"{self.active_branch}_merged",
                    ]
                    for key in cache_keys_to_clear:
                        if key in self._remote_commits_cache:
                            del self._remote_commits_cache[key]
                            _log_timing_message(f"[GITWATCHER] Cleared cache key before status update: {key}")
                    
                    # Force fresh fetch by calling the full background update directly
                    # This bypasses cache and always fetches from Git
                    self._update_commits_status_full_background(self.commits, ref_spec, self.active_branch, repo_path_str)
            
            status_thread = threading.Thread(target=trigger_status_update, daemon=True)
            status_thread.start()
    
    def _refresh_tags(self) -> None:
        """Refresh tags pane when tags change."""
        self.load_tags_background()
    
    def _refresh_branches(self) -> None:
        """Refresh branches pane when branches change."""
        self.branches = self.git.list_branches()
        if self.branches:
            self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
    
    def _refresh_commit_status(self) -> None:
        """Refresh commit status when commits are pushed."""
        # Update commit status (green tick for merged commits)
        if self.active_branch and hasattr(self, 'commits') and self.commits:
            # Clear cache to force refresh
            self._remote_commits_cache.clear()
            # Trigger status update with correct parameters
            repo_path_str = str(self.repo_path)
            ref_spec = f"refs/heads/{self.active_branch}"
            self._start_commits_status_update_background(self.commits, ref_spec, self.active_branch, repo_path_str)
    
    def on_unmount(self) -> None:
        """Stop GitWatcher when app unmounts."""
        if self._git_watcher:
            self._git_watcher.stop()
            _log_timing_message("[GITWATCHER] Stopped")
    
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
                except Exception as e:
                    # Log errors from update functions (e.g., _update_tags_ui)
                    import traceback
                    error_msg = f"Error in UI update function: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                    _log_timing_message(f"[ERROR] {error_msg}")
                    # Continue processing other updates
        except Exception as e:
            # Log errors in the queue processing itself
            import traceback
            error_msg = f"Error in _process_ui_update_queue: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
            _log_timing_message(f"[ERROR] {error_msg}")
    
    def _check_commits_pane_scroll(self) -> None:
        """Periodically check if we need to load more commits in commits pane (fallback if scroll events don't fire)."""
        if self._search_query:
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
        # Check for native git log virtual scrolling first
        if self._view_mode == "log" and self.log_pane._native_git_log_lines:
            try:
                # Get scroll container
                container = self.query_one("#patch-scroll-container", None)
                if container is None:
                    return
                
                # Get scroll position
                scroll_y = 0
                max_scroll_y = 0
                
                if hasattr(container, 'scroll_y'):
                    scroll_y = container.scroll_y
                if hasattr(container, 'max_scroll_y'):
                    max_scroll_y = container.max_scroll_y
                elif hasattr(container, 'virtual_size'):
                    max_scroll_y = container.virtual_size.height if hasattr(container.virtual_size, 'height') else 0
                
                # Check if we need to load more commits for native git log
                if max_scroll_y > 0 and not self.log_pane._native_git_log_loading:
                    scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                    
                    # If scrolled near bottom (85%), load more commits
                    if scroll_percent >= 0.85:
                        _log_timing_message(f"[TIMING] [PERIODIC CHECK] Log pane: Loading more commits (scroll_percent={scroll_percent:.2f}, current_count={self.log_pane._native_git_log_count})")
                        # Load more commits - use same wrapper approach as load_commits_for_log
                        if self.active_branch and self.git:
                            # Get repo_path (same logic as load_commits_for_log)
                            repo_path_to_use = None
                            if hasattr(self, 'repo_path') and self.repo_path:
                                repo_path_to_use = self.repo_path
                            elif hasattr(self.git, 'repo_path'):
                                try:
                                    repo_path_to_use = self.git.repo_path
                                except:
                                    pass
                            elif hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                                try:
                                    repo_path_to_use = self.git.repo.path
                                except:
                                    pass
                            
                            # Create wrapper with repo_path
                            class GitServiceWithPath:
                                def __init__(self, git_service, repo_path):
                                    self.git_service = git_service
                                    self.repo_path = Path(repo_path) if repo_path else None
                                    if hasattr(git_service, 'repo'):
                                        self.repo = git_service.repo
                            
                            git_service_wrapper = GitServiceWithPath(self.git, repo_path_to_use or ".")
                            basic_branch_info = {"name": self.active_branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                            
                            # Phase 2: Time the load operation to identify scroll "stuck" issue
                            import time
                            load_start = time.perf_counter()
                            _log_timing_message(f"[TIMING] [SCROLL] [LOG] _show_native_git_log START (append=True, current_count={self.log_pane._native_git_log_count})")
                            
                            # Phase 2: Preserve scroll position before loading
                            scroll_container = self.query_one("#patch-scroll-container", None)
                            preserved_scroll_y = 0
                            if scroll_container and hasattr(scroll_container, 'scroll_y'):
                                preserved_scroll_y = scroll_container.scroll_y
                            
                            self.log_pane._show_native_git_log(self.active_branch, basic_branch_info, git_service_wrapper, append=True)
                            
                            load_time = time.perf_counter() - load_start
                            _log_timing_message(f"[TIMING] [SCROLL] [LOG] _show_native_git_log TOTAL: {load_time*1000:.1f}ms")
                            
                            # Phase 2: Restore scroll position after loading
                            if scroll_container and hasattr(scroll_container, 'scroll_y') and preserved_scroll_y > 0:
                                try:
                                    scroll_container.scroll_y = preserved_scroll_y
                                    if load_time > 0.05:
                                        _log_timing_message(f"[TIMING] [SCROLL] [LOG] Scroll position restored: {preserved_scroll_y}")
                                except Exception as e:
                                    if load_time > 0.05:
                                        _log_timing_message(f"[TIMING] [SCROLL] [LOG] Failed to restore scroll position: {e}")
                            
                            # If load took significant time, it's likely causing the "stuck" scroll
                            if load_time > 0.1:  # More than 100ms
                                _log_timing_message(f"[TIMING] [SCROLL] [LOG] [LAG] WARNING: _show_native_git_log took {load_time*1000:.1f}ms - likely causing scroll to stick")
                        return
            except Exception:
                pass  # Silently fail if check fails
        
        # Original virtual scrolling check for custom rendering (if still used)
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
                                    # Normalize SHA before lookup (fix for Cython version)
                                    normalized_sha = _normalize_commit_sha(commit_sha)
                                    return self.refs_map.get(normalized_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
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
    
    def action_toggle_graph_style(self) -> None:
        """Toggle graph visualization style between ASCII (*, |, |/, |\\) and dots (●, │)."""
        if self.log_pane.graph_style == "ascii":
            self.log_pane.graph_style = "dots"
        else:
            self.log_pane.graph_style = "ascii"
        
        # Refresh the log view to show the new style
        if self.active_branch and self._view_mode == "log":
            # Re-render the log with the new style
            self.log_pane._last_render_time = 0  # Force immediate render
            self._update_branch_info_ui(self.active_branch, self.log_pane._cached_branch_info)
    

    def refresh_data_fast(self) -> None:
        """Load UI immediately with minimal data (fast, non-blocking)."""
        total_start = time.perf_counter()
        _log_timing_message("===== refresh_data_fast START =====")
        
        # Clear caches on refresh to ensure fresh data
        self._remote_commits_cache.clear()
        self._commit_count_cache.clear()
        self._last_head_sha.clear()
        self._last_remote_head_sha.clear()
        
        # Preserve current branch selection before refreshing
        previous_branch = self.active_branch
        
        # Load branches immediately (fast, ~0.1s)
        branch_start = time.perf_counter()
        self.branches = self.git.list_branches()
        branch_elapsed = time.perf_counter() - branch_start
        _log_timing_message(f"list_branches: {branch_elapsed:.4f}s")
        
        # Load remotes immediately (fast, ~0.1s)
        remotes_start = time.perf_counter()
        # Use Python version if Cython version doesn't have the method
        if hasattr(self.git, 'list_remote_branches'):
            self.remotes = self.git.list_remote_branches()
        else:
            # Fallback to Python version (create if needed)
            if not hasattr(self, 'git_python') or self.git_python is None:
                from pygitzen.git_service import GitService
                self.git_python = GitService(self.repo_path)
            self.remotes = self.git_python.list_remote_branches()
        remotes_elapsed = time.perf_counter() - remotes_start
        _log_timing_message(f"list_remote_branches: {remotes_elapsed:.4f}s")
        
        # Update remotes pane
        if self.remotes:
            self.remotes_pane.set_remotes(self.remotes)
        
        # Calculate sync status for all branches in background
        if self.branches:
            self._calculate_all_branches_sync_status()
        
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
                    self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                    # Ensure BranchesPane ListView selection matches (set after list is populated)
                    self.branches_pane.index = branch_index
                    self.branches_pane.highlighted = branch_index
                else:
                    # Branch was deleted, fall back to first branch
                    self.active_branch = self.branches[0].name
                    self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                    self.branches_pane.index = 0
                    self.branches_pane.highlighted = 0
            else:
                # No previous branch, use first branch
                self.active_branch = self.branches[0].name
                self.branches_pane.set_branches(self.branches, self.active_branch)
                self.branches_pane.index = 0
                self.branches_pane.highlighted = 0

            # Load commits for commits pane (left side) - shows all commits from all branches
            commits_load_start = time.perf_counter()
            self.load_commits(self.active_branch)
            commits_load_elapsed = time.perf_counter() - commits_load_start
            _log_timing_message(f"load_commits: {commits_load_elapsed:.4f}s")

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
            
            # Initialize stashes as empty (will be loaded in background)
            self.stashes = []
            self.stash_pane.set_stashes([])
            
            # Load tags in background (non-blocking, can be 50k+ tags)
            self.load_tags_background()
            
            # Load heavy operations in background (non-blocking)
            # Store branch for background workers
            self._pending_branch = self.active_branch
            self.load_commits_count_background(self.active_branch)
            self.load_file_status_background()
            self.load_stashes_background()
            
            total_elapsed = time.perf_counter() - total_start
            _log_timing_message(f"===== refresh_data_fast TOTAL: {total_elapsed:.4f}s =====")

    def refresh_data(self) -> None:
        # Preserve current branch selection before refreshing
        previous_branch = self.active_branch
        self.branches = self.git.list_branches()
        # Use Python version if Cython version doesn't have the method
        if hasattr(self.git, 'list_remote_branches'):
            self.remotes = self.git.list_remote_branches()
        else:
            # Fallback to Python version (create if needed)
            if not hasattr(self, 'git_python') or self.git_python is None:
                from pygitzen.git_service import GitService
                self.git_python = GitService(self.repo_path)
            self.remotes = self.git_python.list_remote_branches()
        
        # Update remotes pane
        if self.remotes:
            self.remotes_pane.set_remotes(self.remotes)
        
        # Calculate sync status for all branches in background
        if self.branches:
            self._calculate_all_branches_sync_status()
        
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
                    self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                    # Ensure BranchesPane ListView selection matches (set after list is populated)
                    self.branches_pane.index = branch_index
                    self.branches_pane.highlighted = branch_index
                else:
                    # Branch was deleted, fall back to first branch
                    self.active_branch = self.branches[0].name
                    self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                    self.branches_pane.index = 0
                    self.branches_pane.highlighted = 0
            else:
                # No previous branch, use first branch
                self.active_branch = self.branches[0].name
                self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                self.branches_pane.index = 0
                self.branches_pane.highlighted = 0

            
            self.load_commits(self.active_branch)
            self.update_status_info()

    def _calculate_branch_sync_status(self, branch: str) -> dict:
        """Calculate sync status (behind/ahead counts) for a branch.
        
        Returns dict with keys: 'behind', 'ahead', 'synced', 'upstream'
        """
        import subprocess
        
        repo_path_str = str(self.repo_path) if hasattr(self, 'repo_path') else "."
        sync_status = {"behind": 0, "ahead": 0, "synced": False, "upstream": None}
        
        try:
            # Get upstream tracking branch
            upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{u}}"]
            upstream_result = subprocess.run(
                upstream_cmd,
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str
            )
            
            if upstream_result.returncode == 0:
                upstream = upstream_result.stdout.strip()
                sync_status["upstream"] = upstream
                
                # Calculate behind/ahead using git rev-list --left-right --count
                # Format: <behind>	<ahead> (tab-separated)
                rev_list_cmd = ["git", "rev-list", "--left-right", "--count", f"{upstream}...{branch}"]
                rev_list_result = subprocess.run(
                    rev_list_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str
                )
                
                if rev_list_result.returncode == 0:
                    parts = rev_list_result.stdout.strip().split("\t")
                    if len(parts) == 2:
                        behind = int(parts[0].strip()) if parts[0].strip().isdigit() else 0
                        ahead = int(parts[1].strip()) if parts[1].strip().isdigit() else 0
                        sync_status["behind"] = behind
                        sync_status["ahead"] = ahead
                        sync_status["synced"] = (behind == 0 and ahead == 0)
        except Exception:
            # If sync status calculation fails, return default values
            pass
        
        return sync_status
    
    def _refresh_branch_sync_status(self, branch: str) -> None:
        """Refresh sync status for a specific branch (called when branch is selected)."""
        import threading
        
        def calculate_sync_in_thread():
            """Calculate sync status for the branch in background thread."""
            try:
                sync_status = self._calculate_branch_sync_status(branch)
                self._branch_sync_status_cache[branch] = sync_status
                
                # Update UI in main thread
                if self.branches:
                    self.call_from_thread(
                        lambda: self.branches_pane.set_branches(
                            self.branches, 
                            self.active_branch, 
                            self._branch_sync_status_cache
                        )
                    )
                    # Also update status pane
                    if self.active_branch == branch:
                        self.call_from_thread(
                            lambda: self.status_pane.update_status(
                                self.active_branch, 
                                self.repo_path, 
                                sync_status
                            )
                        )
            except Exception:
                pass  # Silently fail if calculation errors
        
        # Start background thread
        thread = threading.Thread(target=calculate_sync_in_thread, daemon=True)
        thread.start()
    
    def _calculate_all_branches_sync_status(self) -> None:
        """Calculate sync status for all branches in background."""
        import threading
        
        def calculate_sync_in_thread():
            """Calculate sync status for all branches in background thread."""
            try:
                updated_count = 0
                for branch in self.branches:
                    branch_name = branch.name
                    # Skip if already cached (unless we want to refresh)
                    if branch_name not in self._branch_sync_status_cache:
                        sync_status = self._calculate_branch_sync_status(branch_name)
                        self._branch_sync_status_cache[branch_name] = sync_status
                        updated_count += 1
                
                # Update UI once after all branches are calculated (more efficient)
                if updated_count > 0 and self.branches:
                    self.call_from_thread(
                        lambda: self.branches_pane.set_branches(
                            self.branches, 
                            self.active_branch, 
                            self._branch_sync_status_cache
                        )
                    )
            except Exception:
                pass  # Silently fail if calculation errors
        
        # Start background thread
        thread = threading.Thread(target=calculate_sync_in_thread, daemon=True)
        thread.start()
    
    def update_status_info(self) -> None:
        """Update status pane with current branch info."""
        if self.active_branch:
            current_sync = self._branch_sync_status_cache.get(self.active_branch) if self.active_branch else None
            self.status_pane.update_status(self.active_branch, self.repo_path, current_sync)
        
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
        
        # Calculate sync status for all branches in background
        if self.branches:
            self._calculate_all_branches_sync_status()
        
        # Update branches pane with sync status (will be updated again when sync status is calculated)
        if self.branches:
            self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
        
        # Stashes are loaded in background (not here to avoid blocking)
        
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
        """Load commits for log view - now uses native git log directly (fast)."""
        log_start = time.perf_counter()
        _log_timing_message(f"--- load_commits_for_log START (branch: {branch}, reset: {reset}) ---")
        
        # NOTE: We don't update the commits pane title here because it should always show "All Branches"
        # The commits pane is managed by load_commits() which shows all commits from all branches
        
        # Reset pagination if this is a new branch or reset requested
        if reset or self.active_branch != branch:
            self.log_pane._loaded_commits_count = 0
            self.log_pane._total_commits_count = 0
            self.log_pane._cached_commits = []  # Clear old cached commits
        
        # NOTE: We no longer update the commits pane here because it should show ALL commits from all branches
        # The commits pane is managed separately by load_commits() which uses git log --all
        # This method only handles the log pane (right side) which shows branch-specific git log --graph
        
        # Show native git log in log pane (right side) - much faster, no dulwich needed
        basic_branch_info = {"name": branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
        show_log_start = time.perf_counter()
        try:
            # Pass git service AND repo_path (for cython compatibility)
            # Use self.repo_path from app if available, otherwise try to get from git_service
            repo_path_to_use = None
            
            # Method 1: Try self.repo_path from app (should always be set during initialization)
            if hasattr(self, 'repo_path'):
                try:
                    repo_path_value = self.repo_path
                    # Convert to string if it's a Path object, then check if it's valid
                    if repo_path_value:
                        if isinstance(repo_path_value, Path):
                            repo_path_to_use = str(repo_path_value)
                        else:
                            repo_path_to_use = str(repo_path_value)
                except Exception as e:
                    pass
            
            # Method 2: Try to get from git_service (for cython, this might not work)
            if not repo_path_to_use:
                try:
                    repo_path_to_use = getattr(self.git, 'repo_path', None)
                except:
                    pass
            
            # Method 3: Try via repo.path
            if not repo_path_to_use:
                try:
                    if hasattr(self.git, 'repo'):
                        repo = getattr(self.git, 'repo', None)
                        if repo and hasattr(repo, 'path'):
                            repo_path_to_use = getattr(repo, 'path', None)
                except:
                    pass
            
            # Fallback: use current directory (shouldn't happen, but just in case)
            if not repo_path_to_use:
                repo_path_to_use = "."
            
            class GitServiceWithPath:
                def __init__(self, git_service, repo_path):
                    self.git_service = git_service
                    # Always set repo_path as Path object - this is critical for cython compatibility
                    if isinstance(repo_path, Path):
                        self.repo_path = repo_path
                    elif isinstance(repo_path, str):
                        self.repo_path = Path(repo_path)
                    else:
                        self.repo_path = Path(str(repo_path))
                    # Also expose repo if available
                    if hasattr(git_service, 'repo'):
                        self.repo = git_service.repo
            
            git_service_wrapper = GitServiceWithPath(self.git, repo_path_to_use)
            
            self.log_pane.show_branch_log(branch, [], basic_branch_info, git_service_wrapper, append=not reset)
            show_log_elapsed = time.perf_counter() - show_log_start
            _log_timing_message(f"  show_branch_log (native git): {show_log_elapsed:.4f}s")
        except Exception as e:
            # Log error if show_branch_log fails
            import sys
            import traceback
            error_msg = f"Error in show_branch_log for branch {branch}: {type(e).__name__}: {e}\n"
            error_msg += f"Traceback:\n{traceback.format_exc()}\n"
            _log_timing_message(error_msg)
        
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
        """Load more commits for log view (pagination).
        
        Phase 2: Added timing diagnostics to identify scroll "stuck" issue.
        """
        import time
        if not branch:
            return
        
        # Check if we've loaded all commits
        if self.log_pane._total_commits_count > 0 and self.log_pane._loaded_commits_count >= self.log_pane._total_commits_count:
            return
        
        # Phase 2: Preserve scroll position before loading to prevent "stuck" scroll
        scroll_container = self.query_one("#patch-scroll-container", None)
        preserved_scroll_y = 0
        if scroll_container and hasattr(scroll_container, 'scroll_y'):
            preserved_scroll_y = scroll_container.scroll_y
        
        # Time the load operation
        load_start = time.perf_counter()
        _log_timing_message(f"[TIMING] [SCROLL] [LOG] load_more_commits_for_log START (preserved_scroll_y={preserved_scroll_y})")
        
        # Load next batch
        self.load_commits_for_log(branch, reset=False)
        
        load_time = time.perf_counter() - load_start
        _log_timing_message(f"[TIMING] [SCROLL] [LOG] load_more_commits_for_log TOTAL: {load_time*1000:.1f}ms")
        
        # Phase 2: Restore scroll position after loading to prevent "stuck" scroll
        if scroll_container and hasattr(scroll_container, 'scroll_y') and preserved_scroll_y > 0:
            restore_start = time.perf_counter()
            try:
                scroll_container.scroll_y = preserved_scroll_y
                restore_time = time.perf_counter() - restore_start
                _log_timing_message(f"[TIMING] [SCROLL] [LOG] Scroll position restored: {preserved_scroll_y} (took {restore_time*1000:.1f}ms)")
            except Exception as e:
                _log_timing_message(f"[TIMING] [SCROLL] [LOG] Failed to restore scroll position: {e}")
        
        # If load took significant time, it's likely causing the "stuck" scroll
        if load_time > 0.1:  # More than 100ms
            _log_timing_message(f"[TIMING] [SCROLL] [LOG] [LAG] WARNING: load_more_commits_for_log took {load_time*1000:.1f}ms - likely causing scroll to stick")
    
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
            # Skip if we're using native git log (it handles its own updates)
            if self.log_pane._native_git_log_lines:
                return
            
            # Update count for the current branch (matching lazygit behavior)
            
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
                                # Normalize SHA before lookup (fix for Cython version)
                                normalized_sha = _normalize_commit_sha(commit_sha)
                                return self.refs_map.get(normalized_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                        git_service = CachedGitService(self.git, self.log_pane._cached_commit_refs_map)
                    
                    # Force re-render with correct total count
                    self.log_pane._last_render_time = 0  # Reset debounce to force immediate render
                    self.log_pane.show_branch_log(branch, commits_to_render, branch_info, git_service, total_commits_count_override=count)
        except Exception:
            pass  # Silently fail if branch changed
    
    def load_stashes_background(self) -> None:
        """Load stashes in background (non-blocking)."""
        if getattr(self, '_loading_stashes', False):
            return
        
        self._loading_stashes = True
        
        # Use a thread to load stashes asynchronously without blocking the UI
        import threading
        
        def load_stashes_in_thread():
            """Load stashes in background thread (non-blocking)."""
            stash_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_stashes_background START")
            try:
                # Get repo_path (cached if available)
                repo_path = getattr(self, '_cached_repo_path', None)
                if repo_path is None:
                    # Method 1: Direct attribute access
                    try:
                        if hasattr(self.git, 'repo_path'):
                            repo_path = self.git.repo_path
                    except (AttributeError, TypeError):
                        pass
                    
                    # Method 2: Use getattr
                    if repo_path is None:
                        try:
                            repo_path = getattr(self.git, 'repo_path', None)
                        except (AttributeError, TypeError):
                            pass
                    
                    # Method 3: Try via repo.path
                    if repo_path is None:
                        try:
                            if hasattr(self.git, 'repo'):
                                repo = getattr(self.git, 'repo', None)
                                if repo and hasattr(repo, 'path'):
                                    repo_path = getattr(repo, 'path', None)
                        except (AttributeError, TypeError):
                            pass
                    
                    # Fallback
                    if repo_path is None:
                        repo_path = self.repo_path if hasattr(self, 'repo_path') else "."
                    
                    # Cache it for future use
                    self._cached_repo_path = repo_path
                
                # Convert to string/Path for consistency
                if isinstance(repo_path, Path):
                    repo_path_str = str(repo_path)
                else:
                    repo_path_str = str(repo_path) if repo_path else "."
                
                get_stashes_start = time.perf_counter()
                # Check if method exists (Cython version might not have it)
                if hasattr(self.git, 'list_stashes'):
                    stashes = self.git.list_stashes()
                else:
                    # Fallback to Python version if Cython doesn't have the method
                    from .git_service import GitService
                    python_git = GitService(repo_path_str)
                    stashes = python_git.list_stashes()
                get_stashes_elapsed = time.perf_counter() - get_stashes_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   list_stashes: {get_stashes_elapsed:.4f}s ({len(stashes)} stashes)")
                
                # Update UI from main thread (use queue which is thread-safe)
                stashes_copy = stashes.copy()
                self._ui_update_queue.put(lambda: self._update_stashes_ui(stashes_copy))
                
                stash_total = time.perf_counter() - stash_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_stashes_background TOTAL: {stash_total:.4f}s")
            except Exception as e:
                # If stash fetching fails, show empty
                import traceback
                
                # Update UI from main thread on error (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self._update_stashes_ui([]))
        
        thread = threading.Thread(target=load_stashes_in_thread, daemon=True)
        thread.start()
    
    def load_tags_background(self) -> None:
        """Load tags in background (non-blocking, can be 50k+ tags)."""
        import threading
        
        def load_tags_in_thread():
            """Load tags in background thread (non-blocking)."""
            tags_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_tags_background START")
            try:
                # Get repo_path
                repo_path = getattr(self, '_cached_repo_path', None)
                if repo_path is None:
                    if hasattr(self.git, 'repo_path'):
                        repo_path = self.git.repo_path
                    elif hasattr(self, 'repo_path'):
                        repo_path = self.repo_path
                    else:
                        repo_path = "."
                    self._cached_repo_path = repo_path
                
                repo_path_str = str(repo_path) if repo_path else "."
                
                get_tags_start = time.perf_counter()
                tags = []
                # Check if method exists (Cython version might not have it)
                try:
                    if hasattr(self.git, 'list_tags'):
                        _log_timing_message(f"[TIMING] [BACKGROUND]   Using Cython list_tags for {repo_path_str}")
                        tags = self.git.list_tags()
                        _log_timing_message(f"[TIMING] [BACKGROUND]   Cython list_tags returned {len(tags)} tags")
                    else:
                        # Fallback to Python version if Cython doesn't have the method
                        _log_timing_message(f"[TIMING] [BACKGROUND]   Cython method not found, using Python list_tags")
                        from .git_service import GitService
                        python_git = GitService(repo_path_str)
                        tags = python_git.list_tags()
                        _log_timing_message(f"[TIMING] [BACKGROUND]   Python list_tags returned {len(tags)} tags")
                except Exception as e:
                    # If Cython fails, try Python fallback
                    import traceback
                    error_details = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                    _log_timing_message(f"[ERROR] [BACKGROUND]   Cython list_tags failed: {error_details}")
                    try:
                        _log_timing_message(f"[TIMING] [BACKGROUND]   Trying Python fallback...")
                        from .git_service import GitService
                        python_git = GitService(repo_path_str)
                        tags = python_git.list_tags()
                        _log_timing_message(f"[TIMING] [BACKGROUND]   Python fallback succeeded: {len(tags)} tags")
                    except Exception as e2:
                        import traceback
                        error_details2 = f"{type(e2).__name__}: {e2}\n{traceback.format_exc()}"
                        _log_timing_message(f"[ERROR] [BACKGROUND]   Python fallback also failed: {error_details2}")
                        raise e2  # Re-raise to be caught by outer exception handler
                
                get_tags_elapsed = time.perf_counter() - get_tags_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   list_tags: {get_tags_elapsed:.4f}s ({len(tags)} tags)")
                
                if not tags:
                    _log_timing_message(f"[WARNING] [BACKGROUND]   list_tags returned empty list! This might indicate an issue.")
                
                # Update UI from main thread (use queue which is thread-safe)
                # Create a copy of the tags list (shallow copy is fine since TagInfo objects are immutable)
                tags_copy = list(tags)  # Use list() instead of .copy() for clarity
                _log_timing_message(f"[TIMING] [BACKGROUND]   Queuing UI update with {len(tags_copy)} tags")
                self._ui_update_queue.put(lambda: self._update_tags_ui(tags_copy))
                
                tags_total = time.perf_counter() - tags_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_tags_background TOTAL: {tags_total:.4f}s")
            except Exception as e:
                # If tag fetching fails, log the error and show empty
                import traceback
                error_msg = f"Error loading tags (background): {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                _log_timing_message(f"[ERROR] {error_msg}")
                
                # Update UI from main thread on error (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self._update_tags_ui([]))
        
        thread = threading.Thread(target=load_tags_in_thread, daemon=True)
        thread.start()
    
    def _update_tags_ui(self, tags: list[TagInfo]) -> None:
        """Update tags pane UI (called from main thread)."""
        try:
            _log_timing_message(f"[TIMING] _update_tags_ui called with {len(tags)} tags")
            # Ensure tags are sorted (in case they weren't sorted in git_service)
            # Sort by recency (most recent first, matching GitHub's behavior), then alphabetically
            # Tags with no timestamp (0) go to the end
            # Note: git_service now uses creatordate:unix which works for both annotated and lightweight tags
            tags.sort(key=lambda t: (t.timestamp == 0, -t.timestamp, t.name.lower()))
            _log_timing_message(f"[TIMING] Tags sorted: first={tags[0].name if tags else 'N/A'}, last={tags[-1].name if tags else 'N/A'}")
            self.tags = tags
            total_count = len(tags)
            # Only render first 200 tags initially (virtual scrolling)
            self.tags_pane.set_tags(tags, total_count=total_count, append=False)
            _log_timing_message(f"[TIMING] _update_tags_ui completed, rendered {self.tags_pane._rendered_count} tags")
            self._loading_tags = False
        except Exception as e:
            import traceback
            error_msg = f"Error in _update_tags_ui: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
            _log_timing_message(f"[ERROR] {error_msg}")
            self._loading_tags = False
    
    def load_file_status_background(self) -> None:
        """Load file status in background (non-blocking)."""
        import time
        
        # Phase 4: Debouncing to prevent rapid-fire updates from GitWatcher
        current_time = time.perf_counter()
        if current_time - self._last_file_status_update_time < self._file_status_debounce_delay:
            _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background: Debounced (last update {current_time - self._last_file_status_update_time:.3f}s ago)")
            return
        
        if self._loading_file_status:
            _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background: Already loading, skipping")
            return
        
        self._loading_file_status = True
        self._last_file_status_update_time = current_time
        
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
                # CRITICAL: Wrap in a function that ensures flag is reset even on error
                def update_ui_safely():
                    try:
                        _log_timing_message(f"[TIMING] [BACKGROUND]   _update_file_status_ui called from queue")
                        self._update_file_status_ui(files_copy)
                    except Exception as e:
                        import traceback
                        error_msg = f"Error in _update_file_status_ui: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                        _log_timing_message(f"[ERROR] [BACKGROUND] {error_msg}")
                        # CRITICAL: Reset flag even on error
                        self._loading_file_status = False
                
                self._ui_update_queue.put(update_ui_safely)
                update_elapsed = time.perf_counter() - update_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   _update_file_status_ui (queued): {update_elapsed:.4f}s")
                
                file_status_elapsed = time.perf_counter() - file_status_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background TOTAL: {file_status_elapsed:.4f}s")
            except Exception as e:
                import traceback
                error_msg = f"Error loading file status: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
                _log_timing_message(f"[ERROR] [BACKGROUND] {error_msg}")
                
                # Update UI from main thread on error (use queue which is thread-safe)
                # CRITICAL: Always reset the flag, even on error
                def update_ui_on_error():
                    try:
                        self._update_file_status_ui([])
                    except Exception:
                        # Even if UI update fails, reset the flag
                        self._loading_file_status = False
                
                self._ui_update_queue.put(update_ui_on_error)
                file_status_elapsed = time.perf_counter() - file_status_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_file_status_background (ERROR): {file_status_elapsed:.4f}s")
        
        # Start thread immediately - doesn't block UI
        thread = threading.Thread(target=load_files_in_thread, daemon=True)
        thread.start()
    
    def _update_stashes_ui(self, stashes: list) -> None:
        """Update stashes UI (called from main thread)."""
        try:
            self.stashes = stashes
            self.stash_pane.set_stashes(stashes)
            self._loading_stashes = False
        except Exception:
            # Silently fail if UI update fails
            self._loading_stashes = False
    
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
            import traceback
            error_msg = f"Error updating file status UI: {type(e).__name__}: {e}\nTraceback:\n{traceback.format_exc()}"
            _log_timing_message(f"[ERROR] {error_msg}")
            
            # Show empty on error
            try:
                self.staged_pane.clear()
                self.changes_pane.clear()
                self.staged_pane.update_files([])
                self.changes_pane.update_files([])
            except:
                pass
            finally:
                # CRITICAL: Always reset the flag, even on error
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
            # Skip if we're using native git log (it handles its own updates)
            if self.log_pane._native_git_log_lines:
                return
            
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
            # Skip if we're using native git log (it handles its own updates)
            if self.log_pane._native_git_log_lines:
                return
            
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
                            # Normalize SHA before lookup (fix for Cython version)
                            normalized_sha = _normalize_commit_sha(commit_sha)
                            return self.refs_map.get(normalized_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                    
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
                # Normalize SHAs to ensure they're in proper hex format
                commit_shas = [_normalize_commit_sha(commit.sha) for commit in commits_to_fetch]
                
                git_log_start = time.perf_counter()
                commit_refs_map = self.git.get_commit_refs_from_git_log(branch, commit_shas)
                git_log_elapsed = time.perf_counter() - git_log_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   get_commit_refs_from_git_log (single call): {git_log_elapsed:.4f}s ({len(commits_to_fetch)} commits, virtual scroll limit)")
                
                # Fill in any missing commits with empty refs (fallback) - only for rendered commits
                # Use normalized SHA for lookup
                for commit in commits_to_fetch:
                    normalized_sha = _normalize_commit_sha(commit.sha)
                    if normalized_sha not in commit_refs_map:
                        commit_refs_map[normalized_sha] = {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []}
                
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
            # Skip if we're using native git log (it handles its own updates)
            if self.log_pane._native_git_log_lines:
                return
            
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
                        # Normalize SHA before lookup (fix for Cython version)
                        normalized_sha = _normalize_commit_sha(commit_sha)
                        return self.refs_map.get(normalized_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
                
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

    def _load_commits_pty(self, cmd: list[str], repo_path_str: str, ref_spec: str, branch: str) -> None:
        """Load commits using PTY streaming for progressive display.
        
        UX Optimization: Load first page instantly, then stream the rest in background.
        This gives instant feedback while still being efficient for large repos.
        """
        from pathlib import Path
        from pygitzen.pty_utils import stream_git_command_pty
        from rich.text import Text
        import threading
        import time
        import subprocess
        
        # Reset flags for new load
        if hasattr(self, '_status_update_started'):
            delattr(self, '_status_update_started')
        if hasattr(self, '_commits_initialized'):
            delattr(self, '_commits_initialized')
        
        # UX OPTIMIZATION: Load first page instantly for immediate feedback
        # This makes the app feel instant instead of waiting for streaming
        def load_first_page_instantly():
            """Load first page of commits instantly using fast git command."""
            try:
                # Use fast git log command to get first page immediately
                fast_cmd = ["git", "log", ref_spec, "--oneline", f"--max-count={self.page_size}",
                           "--pretty=format:+%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s",
                           "--abbrev=40", "--no-show-signature"]
                
                result = subprocess.run(
                    fast_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str
                )
                
                if result.returncode == 0:
                    # Parse commits from output
                    first_page_commits = []
                    seen_shas = set()
                    
                    for line in result.stdout.strip().split('\n'):
                        if not line:
                            continue
                        
                        # Parse commit line (same format as PTY streaming)
                        if line.startswith('+'):
                            line = line[1:]
                        
                        parts = line.split("\x00")
                        if len(parts) >= 8:
                            sha = parts[0].strip()
                            if sha.startswith('+'):
                                sha = sha[1:]
                            
                            if sha in seen_shas:
                                continue
                            seen_shas.add(sha)
                            
                            timestamp_str = parts[1].strip()
                            author_name = parts[2].strip()
                            author_email = parts[3].strip()
                            summary = parts[7].strip()
                            
                            author = f"{author_name} <{author_email}>" if author_email else author_name
                            
                            try:
                                timestamp = int(timestamp_str)
                            except ValueError:
                                timestamp = 0
                            
                            from pygitzen.git_service import CommitInfo
                            first_page_commits.append(
                                CommitInfo(
                                    sha=sha,
                                    summary=summary,
                                    author=author,
                                    timestamp=timestamp,
                                    pushed=False,  # Default to unpushed (red -), will be updated by background thread
                                    merged=False,  # Will be updated by background thread
                                )
                            )
                        elif len(parts) >= 5:
                            # Fallback: try to parse with old format
                            sha = parts[0].strip()
                            if sha.startswith('+'):
                                sha = sha[1:]
                            
                            if sha in seen_shas:
                                continue
                            seen_shas.add(sha)
                            
                            author_name = parts[1].strip()
                            author_email = parts[2].strip()
                            timestamp_str = parts[3].strip()
                            summary = parts[4].strip()
                            
                            author = f"{author_name} <{author_email}>" if author_email else author_name
                            
                            try:
                                timestamp = int(timestamp_str)
                            except ValueError:
                                timestamp = 0
                            
                            from pygitzen.git_service import CommitInfo
                            first_page_commits.append(
                                CommitInfo(
                                    sha=sha,
                                    summary=summary,
                                    author=author,
                                    timestamp=timestamp,
                                    pushed=False,  # Default to unpushed (red -), will be updated by background thread
                                    merged=False,  # Will be updated by background thread
                                )
                            )
                    
                    # Show first page instantly in UI
                    if first_page_commits:
                        self.call_from_thread(
                            lambda: self._show_first_page_instantly(first_page_commits, ref_spec, branch, repo_path_str)
                        )
                        _log_timing_message(f"[PTY] [DEBUG] Loaded first page instantly: {len(first_page_commits)} commits")
            except Exception as e:
                _log_timing_message(f"[ERROR] load_first_page_instantly: {type(e).__name__}: {e}")
        
        # Start loading first page instantly in separate thread
        instant_thread = threading.Thread(target=load_first_page_instantly, daemon=True)
        instant_thread.start()
        
        # Start fetching total commit count IMMEDIATELY in separate thread (independent of streaming)
        # This ensures we show the real total count as soon as possible, not wait for streaming
        def fetch_total_count_background():
            """Fetch total commit count independently."""
            try:
                # Resolve HEAD to branch name if needed
                actual_ref = ref_spec
                if ref_spec == "HEAD":
                    branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                    branch_result = subprocess.run(
                        branch_cmd,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=repo_path_str
                    )
                    if branch_result.returncode == 0:
                        actual_ref = branch_result.stdout.strip()
                
                # Check cache first
                if actual_ref and actual_ref in self._commit_count_cache:
                    count = self._commit_count_cache[actual_ref]
                    self.call_from_thread(self._update_commits_count_ui, count)
                    _log_timing_message(f"[PTY] [DEBUG] Total commit count from cache: {count}")
                    return
                
                # Fetch total count
                count_cmd = ["git", "rev-list", "--count", ref_spec]
                count_result = subprocess.run(
                    count_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=repo_path_str
                )
                if count_result.returncode == 0:
                    count = int(count_result.stdout.strip())
                    # Cache the result
                    if actual_ref:
                        self._commit_count_cache[actual_ref] = count
                    # Update UI immediately
                    self.call_from_thread(self._update_commits_count_ui, count)
                    _log_timing_message(f"[PTY] [DEBUG] Total commit count fetched: {count}")
            except Exception as e:
                _log_timing_message(f"[ERROR] fetch_total_count_background: {type(e).__name__}: {e}")
        
        # Start count fetch in separate thread immediately
        count_thread = threading.Thread(target=fetch_total_count_background, daemon=True)
        count_thread.start()
        
        # Start status update immediately (independent of streaming)
        # This ensures status updates happen continuously, not just at the end
        def start_status_update_immediately():
            """Start status update immediately, not waiting for commits to load."""
            # Small delay to ensure self.commits exists, then start status update
            import time
            time.sleep(0.1)  # Brief delay to let first commit load
            if hasattr(self, 'commits') and self.commits:
                self._start_commits_status_update_background(self.commits, ref_spec, branch, repo_path_str)
        
        status_thread = threading.Thread(target=start_status_update_immediately, daemon=True)
        status_thread.start()
        
        _log_timing_message(f"[PTY] [DEBUG] _load_commits_pty: Starting for branch {branch}")
        
        # Parse commit lines as they stream in
        commits: list[CommitInfo] = []
        seen_shas = set()
        batch_size = 20  # Update UI every 20 commits for better performance
        
        def parse_commit_line(line: str) -> Optional[CommitInfo]:
            """Parse a single commit line from git log output."""
            if not line:
                return None
            
            # Skip the '+' prefix (lazygit format)
            if line.startswith('+'):
                line = line[1:]
            
            parts = line.split("\x00")
            # LazyGit format has 8 fields: SHA, timestamp, author name, author email, parents, merge, refs, subject
            if len(parts) >= 8:
                sha = parts[0].strip()
                # Remove '+' prefix if present (from lazygit format: +%H)
                if sha.startswith('+'):
                    sha = sha[1:]
                
                # Skip if we've already seen this commit SHA (deduplicate)
                if sha in seen_shas:
                    return None
                seen_shas.add(sha)
                
                timestamp_str = parts[1].strip()
                author_name = parts[2].strip()
                author_email = parts[3].strip()
                # parts[4] = parents (not used)
                # parts[5] = merge status (not used)
                # parts[6] = refs (not used)
                summary = parts[7].strip()
                
                # Combine author name and email
                author = f"{author_name} <{author_email}>" if author_email else author_name
                
                # Parse timestamp
                try:
                    timestamp = int(timestamp_str)
                except ValueError:
                    timestamp = 0
                
                return CommitInfo(
                    sha=sha,
                    summary=summary,
                    author=author,
                    timestamp=timestamp,
                    pushed=False,  # Default to unpushed (red -) initially, will be updated in background
                    merged=False,  # Will be updated in background
                )
            elif len(parts) >= 5:
                # Fallback: try to parse with old format if new format fails
                sha = parts[0].strip()
                if sha in seen_shas:
                    return None
                seen_shas.add(sha)
                
                # Try old format: %H%x00%an%x00%ae%x00%at%x00%s
                author_name = parts[1].strip()
                author_email = parts[2].strip()
                timestamp_str = parts[3].strip()
                summary = parts[4].strip()
                
                author = f"{author_name} <{author_email}>" if author_email else author_name
                
                try:
                    timestamp = int(timestamp_str)
                except ValueError:
                    timestamp = 0
                
                return CommitInfo(
                    sha=sha,
                    summary=summary,
                    author=author,
                    timestamp=timestamp,
                    pushed=False,  # Default to unpushed (red -) initially, will be updated in background
                    merged=False,  # Will be updated in background
                )
            return None
        
        def stream_commits_in_background():
            """Stream commits in background thread."""
            try:
                repo_path = Path(repo_path_str)
                line_count = 0
                
                # Stream git log output via PTY
                for rich_line in stream_git_command_pty(
                    cmd,
                    repo_path,
                    timeout=30.0,
                    max_lines=None
                ):
                    # Get plain text from Rich Text object
                    line = rich_line.plain
                    
                    # Parse commit from line
                    commit = parse_commit_line(line)
                    if commit:
                        # Skip commits that are already in first page (loaded instantly)
                        if hasattr(self, 'commits') and self.commits:
                            # Check if this commit is already displayed
                            existing_shas = {_normalize_commit_sha(c.sha) for c in self.commits}
                            if _normalize_commit_sha(commit.sha) in existing_shas:
                                continue  # Skip, already shown in first page
                        
                        commits.append(commit)
                        line_count += 1
                        
                        # Update UI progressively (every batch_size commits)
                        # First page is already shown, so we just append new commits
                        should_update = len(commits) % batch_size == 0
                        if should_update:
                            # Update commits pane from main thread (append new commits)
                            commits_copy = commits.copy()
                            self.call_from_thread(
                                lambda: self._update_commits_ui_progressive(commits_copy, ref_spec, branch)
                            )
                            
                            # Trigger status update periodically as commits load (every batch = 20 commits)
                            if len(commits) % batch_size == 0:
                                # Update status for all currently displayed commits
                                self.call_from_thread(
                                    lambda: self._start_commits_status_update_background(commits_copy, ref_spec, branch, repo_path_str)
                                )
                
                # Final update with all commits
                _log_timing_message(f"[PTY] [DEBUG] _load_commits_pty: Streamed {len(commits)} commits")
                self.call_from_thread(
                    lambda: self._finalize_commits_loading(commits, ref_spec, branch, repo_path_str)
                )
                
            except Exception as e:
                import traceback
                error_msg = f"Error streaming commits with PTY: {e}"
                _log_timing_message(f"[PTY] [ERROR] {error_msg}")
                _log_timing_message(f"[PTY] [ERROR] Traceback:\n{traceback.format_exc()}")
                # Fallback to empty commits list
                self.call_from_thread(
                    lambda: self._finalize_commits_loading([], ref_spec, branch, repo_path_str)
                )
        
        # Start streaming in background thread
        thread = threading.Thread(target=stream_commits_in_background, daemon=True)
        thread.start()
    
    def _prebuffer_commits_ahead(self, start_index: int, count: int) -> None:
        """Pre-buffer commits ahead of current scroll position.
        
        Phase 2: Loads commits in background before user scrolls to them,
        ensuring smooth virtual scrolling without visible lag.
        
        Args:
            start_index: Index to start loading from (current loaded count)
            count: Number of commits to pre-buffer
        """
        if self._prebuffering:
            return  # Already pre-buffering
        
        if not self.active_branch:
            return
        
        if self._search_query:
            return  # Don't pre-buffer when searching
        
        self._prebuffering = True
        
        def prebuffer_in_background():
            """Load commits in background thread."""
            try:
                self._load_commits_batch_background(start_index, count)
            finally:
                self._prebuffering = False
        
        # Start pre-buffering in background thread (non-blocking)
        import threading
        thread = threading.Thread(target=prebuffer_in_background, daemon=True)
        thread.start()
    
    def _load_commits_batch_background(self, start_index: int, count: int) -> None:
        """Load a batch of commits in background thread.
        
        Phase 2: Loads commits and appends them to all_commits without
        triggering full UI re-render. Commits are ready when user scrolls.
        
        Args:
            start_index: Index to start loading from
            count: Number of commits to load
        """
        import subprocess
        from pathlib import Path
        
        if not self.active_branch:
            return
        
        ref_spec = self.active_branch if self.active_branch else "HEAD"
        
        # Get repo path
        repo_path = getattr(self, 'repo_path', None)
        if not repo_path:
            try:
                repo_path = getattr(self.git, 'repo_path', None)
            except:
                pass
        if not repo_path:
            repo_path = "."
        
        repo_path_str = str(repo_path) if repo_path else "."
        
        try:
            # Build git log command
            cmd = [
                "git", "log",
                ref_spec,
                "--oneline",
                f"--max-count={count}",
                f"--skip={start_index}",
                "--pretty=format:+%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s",
                "--abbrev=40",
                "--no-show-signature",
            ]
            
            # Run git log
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str
            )
            
            if result.returncode == 0:
                # Parse commits
                new_commits = []
                seen_shas = set()
                
                # Get existing SHAs to avoid duplicates
                existing_shas = {_normalize_commit_sha(c.sha) for c in self.all_commits} if hasattr(self, 'all_commits') and self.all_commits else set()
                
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    
                    # Skip the '+' prefix (lazygit format)
                    if line.startswith('+'):
                        line = line[1:]
                    
                    parts = line.split("\x00")
                    if len(parts) >= 8:
                        sha = parts[0].strip()
                        if sha.startswith('+'):
                            sha = sha[1:]
                        
                        normalized_sha = _normalize_commit_sha(sha)
                        if normalized_sha in seen_shas or normalized_sha in existing_shas:
                            continue
                        seen_shas.add(normalized_sha)
                        
                        timestamp_str = parts[1].strip()
                        author_name = parts[2].strip()
                        author_email = parts[3].strip()
                        summary = parts[7].strip()
                        
                        author = f"{author_name} <{author_email}>" if author_email else author_name
                        
                        try:
                            timestamp = int(timestamp_str)
                        except ValueError:
                            timestamp = 0
                        
                        from pygitzen.git_service import CommitInfo
                        commit = CommitInfo(
                            sha=sha,
                            summary=summary,
                            author=author,
                            timestamp=timestamp,
                            pushed=True,  # Will be updated by background thread
                            merged=False,  # Will be updated by background thread
                        )
                        new_commits.append(commit)
                
                if new_commits:
                    # Append to all_commits (but don't render yet - they'll be rendered when user scrolls)
                    if not hasattr(self, 'all_commits') or not self.all_commits:
                        self.all_commits = []
                    self.all_commits.extend(new_commits)
                    
                    # Update loaded_commits count
                    self.loaded_commits = len(self.all_commits)
                    
                    _log_timing_message(f"[TIMING] [PREBUFFER] Loaded {len(new_commits)} commits in background (total loaded: {self.loaded_commits})")
                    
                    # Note: We don't call append_commits() here - commits are pre-buffered
                    # and will be rendered when user actually scrolls to them via load_more_commits()
        except Exception as e:
            _log_timing_message(f"[ERROR] [PREBUFFER] Error loading commits batch: {type(e).__name__}: {e}")
    
    def _show_first_page_instantly(self, commits: list[CommitInfo], ref_spec: str, branch: str, repo_path_str: str) -> None:
        """Show first page of commits instantly for immediate UX feedback.
        
        This loads the first page immediately and starts status updates right away.
        """
        # Store commits
        self.commits = commits.copy()
        self.all_commits = commits.copy()
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
        
        # Update UI immediately
        self.commits_pane.set_commits(self.commits)
        self._commits_initialized = True
        
        # Select first commit if available
        if self.commits and self.selected_commit_index is None:
            self.selected_commit_index = 0
            self.commits_pane.index = 0
            self.commits_pane.highlighted = 0
            self.commits_pane._last_index = None
            self.commits_pane._update_highlighting(0)
        
        # Update title
        self._update_commits_title()
        
        # CRITICAL: Start status update IMMEDIATELY for instant commits
        # This ensures status is correct from the start, not waiting for streaming
        self._start_commits_status_update_background(self.commits, ref_spec, branch, repo_path_str)
    
    def _update_commits_ui_progressive(self, commits: list[CommitInfo], ref_spec: str, branch: str) -> None:
        """Update commits pane progressively as commits stream in."""
        if not commits:
            return
        
        # Store commits (limit to page_size to match original behavior)
        all_commits = commits.copy()
        # Limit displayed commits to page_size (matching original load_commits behavior)
        displayed_commits = all_commits[:self.page_size] if len(all_commits) > self.page_size else all_commits
        
        # Track if this is the first update (to use set_commits vs append_commits)
        is_first_update = not hasattr(self, '_commits_initialized') or not self._commits_initialized
        
        if is_first_update:
            # First update: use set_commits to initialize the pane
            self.commits = displayed_commits.copy()
            self.all_commits = all_commits.copy()  # Store all commits for search
            
            # Apply search filter if there's a search query
            if self._search_query:
                self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
            
            # Update UI immediately - show commits as they stream in
            self.commits_pane.set_commits(self.commits)
            self._commits_initialized = True
            
            # Select first commit if available
            if self.commits and self.selected_commit_index is None:
                self.selected_commit_index = 0
                self.commits_pane.index = 0
                self.commits_pane.highlighted = 0
                self.commits_pane._last_index = None
                self.commits_pane._update_highlighting(0)
        else:
            # Subsequent updates: only append new commits (don't clear and re-render)
            current_count = len(self.commits) if hasattr(self, 'commits') else 0
            new_commits = displayed_commits[current_count:]
            
            if new_commits:
                # Update stored commits
                self.commits.extend(new_commits)
                self.all_commits = all_commits.copy()
                
                # Apply search filter if there's a search query
                if self._search_query:
                    self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
                
                # Append only new commits to UI (doesn't clear existing)
                self.commits_pane.append_commits(new_commits)
        
        # Don't update total_commits here - it's fetched independently in background
        # Only update the title to show current displayed count
        # The total will be updated by _update_commits_count_ui when background thread finishes
        self._update_commits_title()
    
    def _start_commits_status_update_background(self, commits: list[CommitInfo], ref_spec: str, branch: str, repo_path_str: str) -> None:
        """Start background thread to update commit status early (not waiting for all commits).
        
        This updates ALL currently displayed commits, not just the batch passed to it.
        """
        import threading
        
        def update_commits_metadata_background():
            """Update commit count and push status in background with cache and invalidation."""
            import subprocess
            import time
            try:
                # Resolve HEAD to branch name if needed (for cache key)
                actual_ref = ref_spec
                if ref_spec == "HEAD":
                    branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                    branch_result = subprocess.run(
                        branch_cmd,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=repo_path_str
                    )
                    if branch_result.returncode == 0:
                        actual_ref = branch_result.stdout.strip()
                
                # Get ALL currently displayed commits (not just the batch parameter)
                # This ensures we update all visible commits, not just the first batch
                all_displayed_commits = self.commits.copy() if hasattr(self, 'commits') and self.commits else commits.copy()
                
                # Update push/merge status from cache if available
                # CRITICAL: Always fetch fresh status when called from GitWatcher (new commit detected)
                # Skip cache to ensure accurate status for new commits
                if actual_ref and actual_ref != "HEAD":
                    cache_key = f"{actual_ref}_unpushed"
                    merged_cache_key = f"{actual_ref}_merged"
                    # Also try refs/heads/ prefix
                    cache_key_refs = f"refs/heads/{actual_ref}_unpushed"
                    merged_cache_key_refs = f"refs/heads/{actual_ref}_merged"
                    
                    # Check both cache key formats
                    has_cache = (cache_key in self._remote_commits_cache or 
                               merged_cache_key in self._remote_commits_cache or
                               cache_key_refs in self._remote_commits_cache or
                               merged_cache_key_refs in self._remote_commits_cache)
                    
                    # CRITICAL: If cache exists but was recently cleared (indicates new commit),
                    # force fresh fetch instead of using stale cache
                    # We detect this by checking if cache was cleared in _refresh_on_new_commit
                    # For now, always fetch fresh when status update is triggered after new commit
                    # (The cache clearing in _refresh_on_new_commit ensures cache is empty)
                    if has_cache:
                        # Update status from cache (only if cache wasn't cleared)
                        unpushed_commits = (self._remote_commits_cache.get(cache_key, set()) or 
                                          self._remote_commits_cache.get(cache_key_refs, set()))
                        merged_commits = (self._remote_commits_cache.get(merged_cache_key, set()) or 
                                        self._remote_commits_cache.get(merged_cache_key_refs, set()))
                        normalized_unpushed = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                        normalized_merged = {_normalize_commit_sha(sha) for sha in merged_commits}
                        
                        # Update ALL displayed commits in place
                        for commit in all_displayed_commits:
                            normalized_sha = _normalize_commit_sha(commit.sha)
                            commit.merged = normalized_sha in normalized_merged
                            commit.pushed = normalized_sha not in normalized_unpushed
                        
                        # Update UI with ALL displayed commits
                        self.call_from_thread(self._update_commits_push_status_ui, all_displayed_commits)
                        _log_timing_message(f"[PTY] [DEBUG] Updated push/merge status early from cache for {len(all_displayed_commits)} displayed commits")
                    else:
                        # Cache miss - need to fetch status (use full background update logic)
                        # This will fetch merged/unpushed status and update all commits
                        _log_timing_message(f"[PTY] [DEBUG] Cache miss for {actual_ref}, will fetch status in background")
                        # Trigger full background update (similar to _finalize_commits_loading)
                        self._update_commits_status_full_background(all_displayed_commits, ref_spec, branch, repo_path_str)
                else:
                    # No valid ref - trigger full background update
                    _log_timing_message(f"[PTY] [DEBUG] No valid ref, will fetch status in background")
                    self._update_commits_status_full_background(all_displayed_commits, ref_spec, branch, repo_path_str)
            except Exception as e:
                _log_timing_message(f"[ERROR] _start_commits_status_update_background: {type(e).__name__}: {e}")
        
        thread = threading.Thread(target=update_commits_metadata_background, daemon=True)
        thread.start()
    
    def _update_commits_status_full_background(self, commits: list[CommitInfo], ref_spec: str, branch: str, repo_path_str: str) -> None:
        """Full background update of commit status (fetches from git if cache miss).
        
        This updates ALL currently displayed commits in self.commits, not just the commits parameter.
        """
        import threading
        import subprocess
        import time
        
        def update_full_background():
            """Fetch and update commit status from git."""
            try:
                # Resolve HEAD to branch name if needed
                actual_ref = ref_spec
                if ref_spec == "HEAD":
                    branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                    branch_result = subprocess.run(
                        branch_cmd,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=repo_path_str
                    )
                    if branch_result.returncode == 0:
                        actual_ref = branch_result.stdout.strip()
                
                if not actual_ref or actual_ref == "HEAD":
                    return
                
                # Fetch merged commits from main/master (same logic as load_commits)
                main_branches = ["main", "master"]
                merged_commits = set()
                merged_cache_key = f"{actual_ref}_merged"
                
                for main_branch in main_branches:
                    try:
                        merged_cmd = ["git", "rev-list", "--max-count=100000", main_branch]
                        merged_result = subprocess.run(
                            merged_cmd,
                            capture_output=True,
                            text=True,
                            timeout=30,
                            cwd=repo_path_str
                        )
                        if merged_result.returncode == 0:
                            merged_shas = set(merged_result.stdout.strip().split('\n'))
                            merged_commits.update(merged_shas)
                            # Cache the result
                            self._remote_commits_cache[merged_cache_key] = merged_commits
                            break
                    except Exception:
                        continue
                
                # Fetch unpushed commits (using same logic as load_commits)
                cache_key = f"{actual_ref}_unpushed"
                unpushed_commits = set()
                try:
                    # Get upstream tracking branch
                    upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{actual_ref}@{{u}}"]
                    upstream_result = subprocess.run(
                        upstream_cmd,
                        capture_output=True,
                        text=True,
                        timeout=2,
                        cwd=repo_path_str
                    )
                    
                    if upstream_result.returncode == 0:
                        upstream_branch = upstream_result.stdout.strip()
                        # Get main branches to exclude
                        main_branches = []
                        for main_branch in ["origin/main", "origin/master"]:
                            check_main = subprocess.run(
                                ["git", "rev-parse", "--verify", main_branch],
                                capture_output=True,
                                text=True,
                                timeout=1,
                                cwd=repo_path_str
                            )
                            if check_main.returncode == 0:
                                main_branches.append(main_branch)
                        
                        # Build command: git rev-list <branch> --not <upstream> --not <main-branches>
                        unpushed_cmd = ["git", "rev-list", actual_ref, "--not", upstream_branch]
                        for main_branch in main_branches:
                            unpushed_cmd.extend(["--not", main_branch])
                        
                        unpushed_result = subprocess.run(
                            unpushed_cmd,
                            capture_output=True,
                            text=True,
                            timeout=10,
                            cwd=repo_path_str
                        )
                        if unpushed_result.returncode == 0:
                            unpushed_shas = set(unpushed_result.stdout.strip().split('\n'))
                            unpushed_commits.update([sha.strip() for sha in unpushed_shas if sha.strip()])
                            # Cache the result
                            self._remote_commits_cache[cache_key] = unpushed_commits
                except Exception:
                    pass
                
                # CRITICAL: Get ALL currently displayed commits (not just the parameter)
                # This ensures we update all visible commits, even if more were loaded since this started
                all_displayed_commits = self.commits.copy() if hasattr(self, 'commits') and self.commits else commits.copy()
                
                # Update commits with fetched status
                normalized_merged = {_normalize_commit_sha(sha) for sha in merged_commits}
                normalized_unpushed = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                
                for commit in all_displayed_commits:
                    normalized_sha = _normalize_commit_sha(commit.sha)
                    commit.merged = normalized_sha in normalized_merged
                    commit.pushed = normalized_sha not in normalized_unpushed
                    # Mark as updated to prevent reset
                    commit._status_updated = True
                
                # Update UI with ALL displayed commits
                self.call_from_thread(self._update_commits_push_status_ui, all_displayed_commits)
                _log_timing_message(f"[PTY] [DEBUG] Updated push/merge status from git for {len(all_displayed_commits)} displayed commits")
            except Exception as e:
                _log_timing_message(f"[ERROR] _update_commits_status_full_background: {type(e).__name__}: {e}")
        
        thread = threading.Thread(target=update_full_background, daemon=True)
        thread.start()
    
    def _finalize_commits_loading(self, commits: list[CommitInfo], ref_spec: str, branch: str, repo_path_str: str) -> None:
        """Finalize commits loading with cache checking and background updates.
        
        CRITICAL: This should NOT reset status that was already set. Only update if status is unknown.
        """
        import subprocess
        # Reset status update flag for next load
        if hasattr(self, '_status_update_started'):
            delattr(self, '_status_update_started')
        
        # CRITICAL: If commits are already displayed with correct status, DON'T reset them
        # Only merge in new commits that weren't in the first page
        if hasattr(self, 'commits') and self.commits and hasattr(self, '_commits_initialized') and self._commits_initialized:
            # Commits are already displayed - just merge in any new commits from streaming
            existing_shas = {_normalize_commit_sha(c.sha) for c in self.commits}
            new_commits = [c for c in commits if _normalize_commit_sha(c.sha) not in existing_shas]
            
            if new_commits:
                # Limit to page_size total
                current_count = len(self.commits)
                remaining_slots = max(0, self.page_size - current_count)
                commits_to_add = new_commits[:remaining_slots] if remaining_slots > 0 else []
                
                if commits_to_add:
                    # Append new commits
                    self.commits.extend(commits_to_add)
                    self.all_commits = (self.all_commits + commits_to_add) if hasattr(self, 'all_commits') else commits_to_add.copy()
                    
                    # Apply search filter if there's a search query
                    if self._search_query:
                        self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
                    
                    # Append to UI without clearing
                    self.commits_pane.append_commits(commits_to_add)
            
            # Update status for ALL displayed commits (including new ones)
            # This ensures status is updated without resetting existing status
            # Only update if status hasn't been set yet (avoid overwriting correct status)
            # Check if status was already updated - if so, don't reset it
            needs_status_update = any(not hasattr(c, '_status_updated') or not c._status_updated for c in self.commits)
            if needs_status_update:
                self._start_commits_status_update_background(self.commits, ref_spec, branch, repo_path_str)
            
            # Update title
            self._update_commits_title()
            return  # Don't proceed with the rest - commits are already displayed
        
        # Fallback: If commits aren't initialized yet, proceed with normal initialization
        # Store final commits
        all_commits = commits.copy()
        # Limit displayed commits to page_size (matching original load_commits behavior)
        displayed_commits = all_commits[:self.page_size] if len(all_commits) > self.page_size else all_commits
        
        # CRITICAL: Preserve status from existing commits if they're already displayed
        # This prevents resetting status back to default values
        existing_status_map = {}
        if hasattr(self, 'commits') and self.commits:
            # Create a map of existing commit status by SHA
            for existing_commit in self.commits:
                normalized_sha = _normalize_commit_sha(existing_commit.sha)
                existing_status_map[normalized_sha] = {
                    'pushed': existing_commit.pushed,
                    'merged': existing_commit.merged
                }
            
            # Apply preserved status to new commits
            for commit in displayed_commits:
                normalized_sha = _normalize_commit_sha(commit.sha)
                if normalized_sha in existing_status_map:
                    # Preserve the status that was already set
                    commit.pushed = existing_status_map[normalized_sha]['pushed']
                    commit.merged = existing_status_map[normalized_sha]['merged']
        
        self.commits = displayed_commits.copy()
        self.all_commits = all_commits.copy()
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
        
        # Only update UI if commits have changed (avoid clearing if already set)
        # Check if we need to update (different count or different commits)
        current_commits_count = len(self.commits_pane._commit_shas) if hasattr(self.commits_pane, '_commit_shas') else 0
        if current_commits_count != len(self.commits) or not hasattr(self, '_commits_initialized') or not self._commits_initialized:
            # Update UI only if needed (final set ensures consistency)
            self.commits_pane.set_commits(self.commits)
            self._commits_initialized = True
        else:
            # Commits are already displayed - just update status in place without clearing
            # This preserves the UI and only updates the status
            self.commits_pane.update_push_status_in_place(self.commits)
        
        # Don't override total_commits here - it's fetched independently in background
        # Only update title (total will be updated by _update_commits_count_ui when background thread finishes)
        self._update_commits_title()
        
        # Select first commit if available
        if self.commits and self.selected_commit_index is None:
            self.selected_commit_index = 0
            self.commits_pane.index = 0
            self.commits_pane.highlighted = 0
        
        # Try to get status from cache immediately (same logic as original load_commits)
        actual_ref = ref_spec
        if ref_spec == "HEAD":
            try:
                branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                branch_result = subprocess.run(
                    branch_cmd,
                    capture_output=True,
                    text=True,
                    timeout=2,
                    cwd=repo_path_str
                )
                if branch_result.returncode == 0:
                    actual_ref = branch_result.stdout.strip()
            except Exception:
                pass
        
        # Check cache for unpushed commits and merged commits (same logic as original)
        if actual_ref and actual_ref != "HEAD":
            cache_key = f"{actual_ref}_unpushed"
            
            merged_commits = set()
            merged_cache_key = f"{actual_ref}_merged"
            if merged_cache_key in self._remote_commits_cache:
                merged_commits = self._remote_commits_cache[merged_cache_key]
                _log_timing_message(f"[CACHE] HIT merged_commits_cache for {actual_ref}: {len(merged_commits)} merged commits")
            else:
                _log_timing_message(f"[CACHE] MISS merged_commits_cache for {actual_ref}: skipping sync fetch, will fetch in background")
            
            normalized_merged = {_normalize_commit_sha(sha) for sha in merged_commits}
            
            # Set status immediately from cache if available
            # CRITICAL: Update status on self.commits (displayed commits), not the commits parameter
            # This ensures status is preserved and applied to the correct objects
            # CRITICAL: Only update status if it hasn't been set yet (avoid overwriting correct status)
            if cache_key in self._remote_commits_cache:
                unpushed_commits = self._remote_commits_cache[cache_key]
                normalized_unpushed = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                
                for commit in self.commits:
                    normalized_sha = _normalize_commit_sha(commit.sha)
                    # Only update if status hasn't been set yet (avoid overwriting correct status)
                    if not hasattr(commit, '_status_updated') or not commit._status_updated:
                        commit.merged = normalized_sha in normalized_merged
                        commit.pushed = normalized_sha not in normalized_unpushed
                        commit._status_updated = True
            else:
                for commit in self.commits:
                    normalized_sha = _normalize_commit_sha(commit.sha)
                    # Only update if status hasn't been set yet (avoid overwriting correct status)
                    if not hasattr(commit, '_status_updated') or not commit._status_updated:
                        commit.merged = normalized_sha in normalized_merged
                        # Keep pushed=False (red -) initially - background thread will update it
                        # Unpushed commits should show red dash (-) until status is determined
                        commit._status_updated = False  # Mark as not fully updated yet
        
        # Start background thread to update commit count and push status
        # Copy the full background update logic from load_commits() below
        def update_commits_metadata_background():
            """Update commit count and push status in background with cache and invalidation."""
            import subprocess
            import time
            try:
                # Resolve HEAD to branch name if needed (for cache key)
                actual_ref = ref_spec
                if ref_spec == "HEAD":
                    head_resolve_start = time.perf_counter()
                    branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                    branch_result = subprocess.run(
                        branch_cmd,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=repo_path_str
                    )
                    head_resolve_elapsed = time.perf_counter() - head_resolve_start
                    if branch_result.returncode == 0:
                        actual_ref = branch_result.stdout.strip()
                        _log_timing_message(f"[TIMING] git rev-parse --abbrev-ref HEAD: {head_resolve_elapsed:.4f}s (result: {actual_ref})")
                    else:
                        _log_timing_message(f"[TIMING] git rev-parse --abbrev-ref HEAD: {head_resolve_elapsed:.4f}s (ERROR: {branch_result.stderr})")
                
                # Check if local HEAD changed (for commit count cache invalidation)
                current_head_sha = None
                if actual_ref and actual_ref != "HEAD":
                    try:
                        head_sha_cmd = ["git", "rev-parse", actual_ref]
                        head_sha_result = subprocess.run(
                            head_sha_cmd,
                            capture_output=True,
                            text=True,
                            timeout=5,
                            cwd=repo_path_str
                        )
                        if head_sha_result.returncode == 0:
                            current_head_sha = head_sha_result.stdout.strip()
                    except Exception:
                        pass  # If we can't get HEAD SHA, proceed without invalidation check
                
                # Invalidate commit count cache if HEAD changed
                cache_invalidated_count = False
                if current_head_sha and actual_ref in self._last_head_sha:
                    if self._last_head_sha[actual_ref] != current_head_sha:
                        # HEAD changed → invalidate commit count cache
                        self._commit_count_cache.pop(actual_ref, None)
                        cache_invalidated_count = True
                        _log_timing_message(f"[CACHE] INVALIDATED commit_count_cache for {actual_ref} (HEAD changed: {self._last_head_sha[actual_ref][:8]} → {current_head_sha[:8]})")
                
                # Update commit count - check cache first
                count_start = time.perf_counter()
                if actual_ref in self._commit_count_cache and not cache_invalidated_count:
                    # Cache HIT
                    count = self._commit_count_cache[actual_ref]
                    count_elapsed = time.perf_counter() - count_start
                    self.call_from_thread(self._update_commits_count_ui, count)
                    _log_timing_message(f"[CACHE] HIT commit_count_cache for {actual_ref}: {count} (saved {count_elapsed:.4f}s)")
                else:
                    # Cache MISS or INVALIDATED - fetch fresh data
                    try:
                        count_cmd = ["git", "rev-list", "--count", ref_spec]
                        count_result = subprocess.run(
                            count_cmd,
                            capture_output=True,
                            text=True,
                            timeout=30,  # Increased timeout for large repos (68k+ commits)
                            cwd=repo_path_str
                        )
                        count_elapsed = time.perf_counter() - count_start
                        if count_result.returncode == 0:
                            count = int(count_result.stdout.strip())
                            # Cache the result
                            self._commit_count_cache[actual_ref] = count
                            # Update tracked HEAD SHA
                            if current_head_sha:
                                self._last_head_sha[actual_ref] = current_head_sha
                            # Update UI in main thread
                            self.call_from_thread(self._update_commits_count_ui, count)
                            cache_reason = "INVALIDATED" if cache_invalidated_count else "MISS"
                            _log_timing_message(f"[CACHE] {cache_reason} commit_count_cache for {actual_ref}: fetched {count} in {count_elapsed:.4f}s")
                        else:
                            _log_timing_message(f"[TIMING] git rev-list --count {ref_spec}: {count_elapsed:.4f}s (ERROR: {count_result.stderr})")
                    except Exception as count_e:
                        count_elapsed = time.perf_counter() - count_start
                        _log_timing_message(f"[TIMING] git rev-list --count {ref_spec}: {count_elapsed:.4f}s (EXCEPTION: {type(count_e).__name__}: {count_e})")
                
                # Update push/merge status (simplified version - full implementation in load_commits)
                # For now, just update the commits we have with cache if available
                if actual_ref and actual_ref != "HEAD":
                    cache_key = f"{actual_ref}_unpushed"
                    merged_cache_key = f"{actual_ref}_merged"
                    
                    if cache_key in self._remote_commits_cache or merged_cache_key in self._remote_commits_cache:
                        # Update status from cache
                        unpushed_commits = self._remote_commits_cache.get(cache_key, set())
                        merged_commits = self._remote_commits_cache.get(merged_cache_key, set())
                        normalized_unpushed = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                        normalized_merged = {_normalize_commit_sha(sha) for sha in merged_commits}
                        
                        # Update commits in place
                        for commit in commits:
                            normalized_sha = _normalize_commit_sha(commit.sha)
                            commit.merged = normalized_sha in normalized_merged
                            commit.pushed = normalized_sha not in normalized_unpushed
                        
                        # Update UI
                        self.call_from_thread(self._update_commits_push_status_ui, commits)
                        _log_timing_message(f"[PTY] [DEBUG] Updated push/merge status from cache for {len(commits)} commits")
            except Exception as e:
                _log_timing_message(f"[ERROR] update_commits_metadata_background (PTY): {type(e).__name__}: {e}")
        
        import threading
        thread = threading.Thread(target=update_commits_metadata_background, daemon=True)
        thread.start()

    def load_commits(self, branch: str) -> None:
        """Load all commits from all branches (not branch-specific)."""
        import subprocess
        from datetime import datetime
        from pygitzen.pty_utils import should_use_pty
        
        # Update Commits pane title to show current branch (matching lazygit)
        self.commits_pane.border_title = f"Commits ({branch})" if branch else "Commits (HEAD)"
        
        # Get commits for the current branch (matching lazygit behavior)
        # LazyGit shows commits for the current branch by default, not all branches
        commits: list[CommitInfo] = []
        repo_path = None
        try:
            # Build git log command for the current branch (matching lazygit)
            # Format matches lazygit's format: +%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s
            # Fields: + prefix, SHA, timestamp, author name, author email, parents, merge status, refs, subject
            # Use branch name or HEAD if branch is not available
            ref_spec = branch if branch else "HEAD"
            cmd = [
                "git", "log",
                ref_spec,  # Current branch (matching lazygit - shows branch-specific commits)
                "--oneline",  # Match lazygit
                f"--max-count={self.page_size}",  # Keep our limit of 300
                "--pretty=format:+%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s",
                "--abbrev=40",  # Match lazygit (40-char abbreviated SHA)
                "--no-show-signature",  # Match lazygit
            ]
            
            # Get repo_path - try multiple methods
            repo_path = getattr(self, 'repo_path', None)
            if not repo_path:
                try:
                    repo_path = getattr(self.git, 'repo_path', None)
                except:
                    pass
            if not repo_path:
                try:
                    if hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                        repo_path = self.git.repo.path
                except:
                    pass
            if not repo_path:
                repo_path = "."
            
            # Convert to string if it's a Path object
            repo_path_str = str(repo_path) if repo_path else "."
            
            # Check if PTY streaming should be used
            if should_use_pty():
                _log_timing_message(f"[PTY] [DEBUG] load_commits: Using PTY streaming for branch {branch}")
                self._load_commits_pty(cmd, repo_path_str, ref_spec, branch)
                return  # PTY version handles everything, including UI updates
            
            # Run git log with timeout (fallback to subprocess)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str
            )
            
            if result.returncode == 0:
                # Parse output and deduplicate by SHA (git log --all shouldn't have duplicates, but be safe)
                # Format: +%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s
                # Fields: + prefix, SHA, timestamp, author name, author email, parents, merge status, refs, subject
                seen_shas = set()
                output_lines = result.stdout.strip().split("\n")
                for line in output_lines:
                    if not line:
                        continue
                    
                    # Skip the '+' prefix (lazygit format)
                    if line.startswith('+'):
                        line = line[1:]
                    
                    parts = line.split("\x00")
                    # LazyGit format has 8 fields: SHA, timestamp, author name, author email, parents, merge, refs, subject
                    if len(parts) >= 8:
                        sha = parts[0].strip()
                        # Remove '+' prefix if present (from lazygit format: +%H)
                        if sha.startswith('+'):
                            sha = sha[1:]
                        
                        # Skip if we've already seen this commit SHA (deduplicate)
                        if sha in seen_shas:
                            continue
                        seen_shas.add(sha)
                        
                        timestamp_str = parts[1].strip()
                        author_name = parts[2].strip()
                        author_email = parts[3].strip()
                        # parts[4] = parents (not used)
                        # parts[5] = merge status (not used)
                        # parts[6] = refs (not used)
                        summary = parts[7].strip()
                        
                        # Combine author name and email
                        author = f"{author_name} <{author_email}>" if author_email else author_name
                        
                        # Parse timestamp
                        try:
                            timestamp = int(timestamp_str)
                        except ValueError:
                            timestamp = 0
                        
                        commits.append(
                            CommitInfo(
                                sha=sha,
                                summary=summary,
                                author=author,
                                timestamp=timestamp,
                                pushed=False,  # Will be updated in background
                                merged=False,  # Will be updated in background
                            )
                        )
                    elif len(parts) >= 5:
                        # Fallback: try to parse with old format if new format fails
                        sha = parts[0].strip()
                        if sha in seen_shas:
                            continue
                        seen_shas.add(sha)
                        
                        # Try old format: %H%x00%an%x00%ae%x00%at%x00%s
                        if len(parts) >= 5:
                            author_name = parts[1].strip()
                            author_email = parts[2].strip()
                            timestamp_str = parts[3].strip()
                            summary = parts[4].strip()
                            
                            author = f"{author_name} <{author_email}>" if author_email else author_name
                            
                            try:
                                timestamp = int(timestamp_str)
                            except ValueError:
                                timestamp = 0
                            
                            commits.append(
                                CommitInfo(
                                    sha=sha,
                                    summary=summary,
                                    author=author,
                                    timestamp=timestamp,
                                    pushed=False,  # Will be updated in background
                                    merged=False,  # Will be updated in background
                                )
                            )
            else:
                # Log error for debugging
                error_msg = f"git log failed: {result.stderr}"
                _log_timing_message(f"[ERROR] load_commits: {error_msg}")
                print(f"[ERROR] load_commits: {error_msg}")
            
            # Use approximate count initially (will be updated in background)
            self.total_commits = len(commits) if commits else 0
            
            # Try to get status from cache immediately (before background thread)
            # This ensures status is shown when branch is clicked again
            actual_ref = ref_spec
            if ref_spec == "HEAD":
                try:
                    branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                    branch_result = subprocess.run(
                        branch_cmd,
                        capture_output=True,
                        text=True,
                        timeout=2,
                        cwd=repo_path_str
                    )
                    if branch_result.returncode == 0:
                        actual_ref = branch_result.stdout.strip()
                except Exception:
                    pass
            
            # Check cache for unpushed commits and merged commits
            if actual_ref and actual_ref != "HEAD":
                cache_key = f"{actual_ref}_unpushed"
                
                # Get merged commits from main branches (quick check)
                # OPTIMIZATION: Only use cache for initial load to avoid blocking
                # If not cached, skip merged check here - background thread will fetch it
                merged_commits = set()
                merged_cache_key = f"{actual_ref}_merged"
                if merged_cache_key in self._remote_commits_cache:
                    merged_commits = self._remote_commits_cache[merged_cache_key]
                    _log_timing_message(f"[CACHE] HIT merged_commits_cache for {actual_ref}: {len(merged_commits)} merged commits")
                else:
                    # OPTIMIZATION: Skip synchronous fetch - let background thread handle it
                    # This prevents blocking initial load for 1+ seconds
                    _log_timing_message(f"[CACHE] MISS merged_commits_cache for {actual_ref}: skipping sync fetch, will fetch in background")
                    # merged_commits will be empty, background thread will update status later
                
                normalized_merged = {_normalize_commit_sha(sha) for sha in merged_commits}
                
                # Set status immediately from cache if available
                if cache_key in self._remote_commits_cache:
                    unpushed_commits = self._remote_commits_cache[cache_key]
                    normalized_unpushed = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                    
                    # Set status immediately from cache
                    for commit in commits:
                        normalized_sha = _normalize_commit_sha(commit.sha)
                        commit.merged = normalized_sha in normalized_merged
                        commit.pushed = normalized_sha not in normalized_unpushed
                else:
                    # No cache yet - set merged status at least
                    # Don't assume push status - wait for background thread to determine it correctly
                    # This prevents showing incorrect yellow (pushed) status on refresh
                    for commit in commits:
                        normalized_sha = _normalize_commit_sha(commit.sha)
                        commit.merged = normalized_sha in normalized_merged
                        # Don't set pushed status yet - let background thread determine it
                        # This prevents incorrect status on refresh
                        commit.pushed = False  # Will be updated by background thread
            
            # Start background thread to update commit count and push status
            def update_commits_metadata_background():
                """Update commit count and push status in background with cache and invalidation."""
                try:
                    # Resolve HEAD to branch name if needed (for cache key)
                    actual_ref = ref_spec
                    if ref_spec == "HEAD":
                        head_resolve_start = time.perf_counter()
                        branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                        branch_result = subprocess.run(
                            branch_cmd,
                            capture_output=True,
                            text=True,
                            timeout=5,
                            cwd=repo_path_str
                        )
                        head_resolve_elapsed = time.perf_counter() - head_resolve_start
                        if branch_result.returncode == 0:
                            actual_ref = branch_result.stdout.strip()
                            _log_timing_message(f"[TIMING] git rev-parse --abbrev-ref HEAD: {head_resolve_elapsed:.4f}s (result: {actual_ref})")
                        else:
                            _log_timing_message(f"[TIMING] git rev-parse --abbrev-ref HEAD: {head_resolve_elapsed:.4f}s (ERROR: {branch_result.stderr})")
                    
                    # Check if local HEAD changed (for commit count cache invalidation)
                    current_head_sha = None
                    if actual_ref and actual_ref != "HEAD":
                        try:
                            head_sha_cmd = ["git", "rev-parse", actual_ref]
                            head_sha_result = subprocess.run(
                                head_sha_cmd,
                                capture_output=True,
                                text=True,
                                timeout=5,
                                cwd=repo_path_str
                            )
                            if head_sha_result.returncode == 0:
                                current_head_sha = head_sha_result.stdout.strip()
                        except Exception:
                            pass  # If we can't get HEAD SHA, proceed without invalidation check
                    
                    # Invalidate commit count cache if HEAD changed
                    cache_invalidated_count = False
                    if current_head_sha and actual_ref in self._last_head_sha:
                        if self._last_head_sha[actual_ref] != current_head_sha:
                            # HEAD changed → invalidate commit count cache
                            self._commit_count_cache.pop(actual_ref, None)
                            cache_invalidated_count = True
                            _log_timing_message(f"[CACHE] INVALIDATED commit_count_cache for {actual_ref} (HEAD changed: {self._last_head_sha[actual_ref][:8]} → {current_head_sha[:8]})")
                    
                    # Update commit count - check cache first
                    count_start = time.perf_counter()
                    if actual_ref in self._commit_count_cache and not cache_invalidated_count:
                        # Cache HIT
                        count = self._commit_count_cache[actual_ref]
                        count_elapsed = time.perf_counter() - count_start
                        self.call_from_thread(self._update_commits_count_ui, count)
                        _log_timing_message(f"[CACHE] HIT commit_count_cache for {actual_ref}: {count} (saved {count_elapsed:.4f}s)")
                    else:
                        # Cache MISS or INVALIDATED - fetch fresh data
                        try:
                            count_cmd = ["git", "rev-list", "--count", ref_spec]
                            count_result = subprocess.run(
                                count_cmd,
                                capture_output=True,
                                text=True,
                                timeout=10,
                                cwd=repo_path_str
                            )
                            count_elapsed = time.perf_counter() - count_start
                            if count_result.returncode == 0:
                                count = int(count_result.stdout.strip())
                                # Cache the result
                                self._commit_count_cache[actual_ref] = count
                                # Update tracked HEAD SHA
                                if current_head_sha:
                                    self._last_head_sha[actual_ref] = current_head_sha
                                # Update UI in main thread
                                self.call_from_thread(self._update_commits_count_ui, count)
                                cache_reason = "INVALIDATED" if cache_invalidated_count else "MISS"
                                _log_timing_message(f"[CACHE] {cache_reason} commit_count_cache for {actual_ref}: fetched {count} in {count_elapsed:.4f}s")
                            else:
                                _log_timing_message(f"[TIMING] git rev-list --count {ref_spec}: {count_elapsed:.4f}s (ERROR: {count_result.stderr})")
                        except Exception as count_e:
                            count_elapsed = time.perf_counter() - count_start
                            _log_timing_message(f"[TIMING] git rev-list --count {ref_spec}: {count_elapsed:.4f}s (EXCEPTION: {type(count_e).__name__}: {count_e})")
                    
                    # Lazygit's approach: Use git rev-list to get unpushed commits (works offline)
                    # This uses local tracking refs instead of network calls
                    unpushed_commits = set()
                    cache_invalidated_remote_branch = False
                    # Initialize main_branches at function scope to avoid UnboundLocalError
                    main_branches = []
                    if actual_ref and actual_ref != "HEAD":
                        # Check cache for unpushed commits
                        cache_key = f"{actual_ref}_unpushed"
                        if cache_key in self._remote_commits_cache and not cache_invalidated_remote_branch:
                            # Cache HIT
                            unpushed_commits = self._remote_commits_cache[cache_key]
                            _log_timing_message(f"[CACHE] HIT unpushed_commits_cache for {actual_ref}: {len(unpushed_commits)} unpushed commits")
                        else:
                            # Cache MISS - use lazygit's approach: git rev-list <branch> --not origin/<branch>@{u} --not <main-branches>
                            # Try to get upstream tracking branch using @{u} syntax
                            rev_list_start = time.perf_counter()
                            try:
                                # Get main branches to exclude (commits on main are considered pushed)
                                main_branches = []
                                for main_branch in ["origin/main", "origin/master"]:
                                    check_main = subprocess.run(
                                        ["git", "rev-parse", "--verify", main_branch],
                                        capture_output=True,
                                        text=True,
                                        timeout=1,
                                        cwd=repo_path_str
                                    )
                                    if check_main.returncode == 0:
                                        main_branches.append(main_branch)
                                
                                # First, try to resolve upstream tracking branch
                                upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{actual_ref}@{{u}}"]
                                upstream_result = subprocess.run(
                                    upstream_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=2,
                                    cwd=repo_path_str
                                )
                                
                                if upstream_result.returncode == 0:
                                    upstream_branch = upstream_result.stdout.strip()
                                    
                                    # Track remote HEAD SHA for change detection
                                    try:
                                        remote_head_cmd = ["git", "rev-parse", upstream_branch]
                                        remote_head_result = subprocess.run(
                                            remote_head_cmd,
                                            capture_output=True,
                                            text=True,
                                            timeout=2,
                                            cwd=repo_path_str
                                        )
                                        if remote_head_result.returncode == 0:
                                            current_remote_head_sha = remote_head_result.stdout.strip()
                                            cache_key_remote = f"{actual_ref}_remote_head"
                                            self._last_remote_head_sha[cache_key_remote] = current_remote_head_sha
                                    except Exception:
                                        pass  # Silently fail if we can't track remote HEAD
                                    
                                    # Use lazygit's approach: get commits in local branch that are NOT in upstream or main
                                    # Build command: git rev-list <branch> --not <upstream> --not <main-branches>
                                    unpushed_cmd = ["git", "rev-list", actual_ref, "--not", upstream_branch]
                                    for main_branch in main_branches:
                                        unpushed_cmd.extend(["--not", main_branch])
                                    unpushed_result = subprocess.run(
                                        unpushed_cmd,
                                        capture_output=True,
                                        text=True,
                                        timeout=10,
                                        cwd=repo_path_str
                                    )
                                    rev_list_elapsed = time.perf_counter() - rev_list_start
                                    
                                    if unpushed_result.returncode == 0:
                                        # Parse unpushed commit SHAs
                                        for sha in unpushed_result.stdout.strip().split("\n"):
                                            if sha.strip():
                                                unpushed_commits.add(sha.strip())
                                        # Cache the result
                                        self._remote_commits_cache[cache_key] = unpushed_commits
                                        cache_reason = "INVALIDATED" if cache_invalidated_remote_branch else "MISS"
                                        _log_timing_message(f"[CACHE] {cache_reason} unpushed_commits_cache for {actual_ref}: fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s (upstream: {upstream_branch})")
                                    else:
                                        _log_timing_message(f"[TIMING] git rev-list {actual_ref} --not {upstream_branch}: {rev_list_elapsed:.4f}s (ERROR: {unpushed_result.stderr})")
                                else:
                                    # No upstream tracking branch configured
                                    # Check if remote tracking ref exists (refs/remotes/origin/<branch>)
                                    upstream_branch = f"origin/{actual_ref}"
                                    check_remote_cmd = ["git", "rev-parse", "--verify", f"refs/remotes/{upstream_branch}"]
                                    check_remote_result = subprocess.run(
                                        check_remote_cmd,
                                        capture_output=True,
                                        text=True,
                                        timeout=2,
                                        cwd=repo_path_str
                                    )
                                    
                                    if check_remote_result.returncode == 0:
                                        # Remote tracking ref exists - use it
                                        # Build command: git rev-list <branch> --not <upstream> --not <main-branches>
                                        unpushed_cmd = ["git", "rev-list", actual_ref, "--not", upstream_branch]
                                        for main_branch in main_branches:
                                            unpushed_cmd.extend(["--not", main_branch])
                                        unpushed_result = subprocess.run(
                                            unpushed_cmd,
                                            capture_output=True,
                                            text=True,
                                            timeout=10,
                                            cwd=repo_path_str
                                        )
                                        rev_list_elapsed = time.perf_counter() - rev_list_start
                                        
                                        if unpushed_result.returncode == 0:
                                            for sha in unpushed_result.stdout.strip().split("\n"):
                                                if sha.strip():
                                                    unpushed_commits.add(sha.strip())
                                            self._remote_commits_cache[cache_key] = unpushed_commits
                                            _log_timing_message(f"[CACHE] MISS unpushed_commits_cache for {actual_ref}: fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s (no @{{u}}, using {upstream_branch})")
                                        else:
                                            _log_timing_message(f"[TIMING] git rev-list {actual_ref} --not {upstream_branch}: {rev_list_elapsed:.4f}s (ERROR: {unpushed_result.stderr})")
                                    else:
                                        # Remote tracking ref doesn't exist
                                        # If main branches exist, commits NOT on main are likely PUSHED (yellow), not UNPUSHED (red)
                                        # Only mark as unpushed if we can't determine push status
                                        # For now, assume commits NOT on main are PUSHED (will show yellow)
                                        # This matches lazygit behavior: if branch might be pushed, show yellow
                                        if main_branches:
                                            # Don't mark commits as unpushed - they're likely pushed but not merged
                                            # Empty unpushed_commits means all commits will show as pushed (yellow if not merged)
                                            unpushed_commits = set()
                                            self._remote_commits_cache[cache_key] = unpushed_commits
                                            _log_timing_message(f"[TIMING] No remote tracking ref for {actual_ref}, assuming commits NOT on main are PUSHED (yellow) - matching lazygit behavior")
                                        else:
                                            # No main branches exist - can't determine status, assume all are unpushed
                                            rev_list_elapsed = time.perf_counter() - rev_list_start
                                            all_local_cmd = ["git", "rev-list", actual_ref]
                                            all_local_result = subprocess.run(
                                                all_local_cmd,
                                                capture_output=True,
                                                text=True,
                                                timeout=10,
                                                cwd=repo_path_str
                                            )
                                            if all_local_result.returncode == 0:
                                                for sha in all_local_result.stdout.strip().split("\n"):
                                                    if sha.strip():
                                                        unpushed_commits.add(sha.strip())
                                                self._remote_commits_cache[cache_key] = unpushed_commits
                                            _log_timing_message(f"[TIMING] No remote tracking ref for {actual_ref} (refs/remotes/{upstream_branch}) and no main branches, treating all {len(unpushed_commits)} commits as unpushed")
                            except Exception as e:
                                rev_list_elapsed = time.perf_counter() - rev_list_start
                                _log_timing_message(f"[TIMING] Error getting unpushed commits for {actual_ref}: {type(e).__name__}: {e} in {rev_list_elapsed:.4f}s")
                    
                    # Get merged commits (those on main/master branches)
                    # OPTIMIZATION: Check cache first, use larger limit, fetch in background if needed
                    merged_commits = set()
                    merged_cache_key = f"{actual_ref}_merged"
                    if merged_cache_key in self._remote_commits_cache:
                        merged_commits = self._remote_commits_cache[merged_cache_key]
                        _log_timing_message(f"[CACHE] HIT merged_commits_cache for {actual_ref}: {len(merged_commits)} merged commits")
                    elif main_branches:
                        # Cache MISS - fetch merged commits (this runs in background thread, so it's non-blocking)
                        merged_fetch_start = time.perf_counter()
                        for main_branch in main_branches:
                            # Use larger limit for large repos (68k+ commits)
                            merged_cmd = ["git", "rev-list", main_branch, "--max-count=100000"]
                            merged_result = subprocess.run(
                                merged_cmd,
                                capture_output=True,
                                text=True,
                                timeout=30,  # Increased timeout for large repos
                                cwd=repo_path_str
                            )
                            if merged_result.returncode == 0:
                                for sha in merged_result.stdout.strip().split("\n"):
                                    if sha.strip():
                                        merged_commits.add(sha.strip())
                        merged_fetch_elapsed = time.perf_counter() - merged_fetch_start
                        # Cache the result for future use
                        if merged_commits:
                            self._remote_commits_cache[merged_cache_key] = merged_commits
                            _log_timing_message(f"[CACHE] MISS merged_commits_cache for {actual_ref}: fetched {len(merged_commits)} merged commits in {merged_fetch_elapsed:.4f}s")
                    
                    # Update status for all commits using three-tier lazygit logic:
                    # 1. StatusMerged (green ✓): Commit exists on main/master
                    # 2. StatusPushed (yellow ↑): Commit is pushed but NOT on main/master
                    # 3. StatusUnpushed (red -): Commit is not pushed
                    normalized_unpushed_commits = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                    normalized_merged_commits = {_normalize_commit_sha(sha) for sha in merged_commits}
                    
                    merged_count = 0
                    pushed_count = 0
                    unpushed_count = 0
                    
                    for commit in commits:
                        normalized_commit_sha = _normalize_commit_sha(commit.sha)
                        
                        # Check if merged (exists on main/master)
                        is_merged = normalized_commit_sha in normalized_merged_commits
                        commit.merged = is_merged
                        
                        # Check if unpushed
                        is_unpushed = normalized_commit_sha in normalized_unpushed_commits
                        commit.pushed = not is_unpushed
                        
                        # Count for logging
                        if is_merged:
                            merged_count += 1
                        elif is_unpushed:
                            unpushed_count += 1
                        else:
                            pushed_count += 1
                    
                    # Always update UI in main thread
                    self.call_from_thread(self._update_commits_push_status_ui, commits)
                    _log_timing_message(f"[TIMING] update_commits_metadata_background TOTAL: Updated push status for {len(commits)} commits")
                except Exception as e:
                    _log_timing_message(f"[ERROR] update_commits_metadata_background: {type(e).__name__}: {e}")
            
            # Always start background thread
            import threading
            metadata_thread = threading.Thread(target=update_commits_metadata_background, daemon=True)
            metadata_thread.start()
                
        except Exception as e:
            # Log error for debugging
            error_msg = f"load_commits exception: {type(e).__name__}: {e}"
            _log_timing_message(f"[ERROR] {error_msg}")
            print(f"[ERROR] {error_msg}")
            
            # Fallback: try to use existing methods if available
            try:
                # Try to get commits from current branch as fallback
                if hasattr(self.git, 'list_commits_native'):
                    commits = self.git.list_commits_native(branch, max_count=self.page_size, skip=0, timeout=10)
                else:
                    commits = self.git.list_commits(branch, max_count=self.page_size, skip=0)
                self.total_commits = len(commits)  # Approximate
            except Exception as fallback_e:
                error_msg = f"load_commits fallback exception: {type(fallback_e).__name__}: {fallback_e}"
                _log_timing_message(f"[ERROR] {error_msg}")
                print(f"[ERROR] {error_msg}")
                commits = []
                self.total_commits = 0
        
        loaded_commits = commits
        self.all_commits = loaded_commits.copy()  # Store all commits for search
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
        else:
            self.commits = loaded_commits
        
        self.loaded_commits = len(self.commits)
        
        # OPTIMIZATION: Show commits to UI immediately (critical path)
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
            # OPTIMIZATION: Defer patch loading (non-critical, can load after UI is shown)
            # Only show patch if in patch mode (but do it after commits are shown)
            if self._view_mode == "patch":
                # Load patch in background to avoid blocking UI
                def load_patch_background():
                    self.call_from_thread(self.show_commit_diff, 0)
                import threading
                patch_thread = threading.Thread(target=load_patch_background, daemon=True)
                patch_thread.start()

    def _update_commits_title(self) -> None:
        # Show current branch (matching lazygit behavior)
        branch_name = self.active_branch if self.active_branch else "HEAD"
        displayed_count = len(self.commits) if hasattr(self, 'commits') and self.commits else 0
        
        # If total_commits is 0, show "loading..." or just the displayed count
        if self.total_commits > 0:
            self.commits_pane.border_title = f"Commits ({branch_name}) {displayed_count} of {self.total_commits}"
        else:
            # Total not known yet - show just displayed count or "loading..."
            self.commits_pane.border_title = f"Commits ({branch_name}) {displayed_count}..."
    
    def _update_commits_count_ui(self, count: int) -> None:
        """Update UI to reflect commit count changes (called from background thread)."""
        self.total_commits = count
        self._update_commits_title()
    
    def _update_commits_push_status_ui(self, commits: list[CommitInfo]) -> None:
        """Update UI to reflect push status changes (called from background thread)."""
        # Update push status in place without clearing (prevents flicker during virtual scrolling)
        if commits and len(commits) > 0:
            # Find matching commits in self.commits and update their push status AND merged status
            commit_shas = {c.sha: c for c in commits}
            updated_count = 0
            pushed_count_in_self = 0
            merged_count_in_self = 0
            for commit in self.commits:
                if commit.sha in commit_shas:
                    updated_commit = commit_shas[commit.sha]
                    commit.pushed = updated_commit.pushed
                    commit.merged = updated_commit.merged  # CRITICAL: Also update merged status
                    updated_count += 1
                    if commit.pushed:
                        pushed_count_in_self += 1
                    if commit.merged:
                        merged_count_in_self += 1
            
            # Update the commits pane display in place (no clearing)
            # CRITICAL: Also update _commit_info_map so update_push_status_in_place can access merged status
            if hasattr(self.commits_pane, '_commit_info_map'):
                for commit in commits:
                    normalized_sha = _normalize_commit_sha(commit.sha)
                    # Update the commit info in the map with both pushed and merged status
                    if normalized_sha in self.commits_pane._commit_info_map:
                        self.commits_pane._commit_info_map[normalized_sha].pushed = commit.pushed
                        self.commits_pane._commit_info_map[normalized_sha].merged = commit.merged
            
            self.commits_pane.update_push_status_in_place(commits)

    def _load_more_tags(self) -> None:
        """Load more tags for virtual scrolling."""
        if not hasattr(self, 'tags') or not self.tags:
            return
        
        if hasattr(self.tags_pane, '_rendered_count') and hasattr(self.tags_pane, '_total_tags_count'):
            rendered = self.tags_pane._rendered_count
            total = self.tags_pane._total_tags_count
            
            if rendered >= total:
                return  # All tags already rendered
            
            # Load next batch (200 tags at a time)
            batch_size = 200
            start_idx = rendered
            end_idx = min(start_idx + batch_size, total)
            
            if start_idx < len(self.tags):
                next_batch = self.tags[start_idx:end_idx]
                if next_batch:
                    self.tags_pane.append_tags(next_batch)
                    _log_timing_message(f"[TIMING] [SCROLL] Tags pane: Loaded batch {start_idx}-{end_idx} of {total}")
    
    def load_more_commits(self) -> None:
        """Load more commits for the current branch (matching lazygit behavior).
        
        Phase 2: Updated to use pre-buffered commits when available for smoother scrolling.
        """
        import subprocess
        
        # If searching, don't load more - we're filtering existing commits
        if self._search_query:
            return
        if not self.active_branch:
            return
        if self.loaded_commits >= self.total_commits:
            return
        
        # Phase 2: Check if we have pre-buffered commits ready
        import time
        load_more_start = time.perf_counter()
        
        current_displayed = len(self.commits) if hasattr(self, 'commits') and self.commits else 0
        if hasattr(self, 'all_commits') and self.all_commits and len(self.all_commits) > current_displayed:
            # We have pre-buffered commits - use them instead of fetching from git
            commits_to_append = self.all_commits[current_displayed:current_displayed + self.page_size]
            if commits_to_append:
                _log_timing_message(f"[TIMING] [SCROLL] Using {len(commits_to_append)} pre-buffered commits (displayed: {current_displayed}, available: {len(self.all_commits)})")
                
                # Time the append operation
                append_start = time.perf_counter()
                self.commits.extend(commits_to_append)
                extend_time = time.perf_counter() - append_start
                
                append_ui_start = time.perf_counter()
                self.commits_pane.append_commits(commits_to_append)
                append_ui_time = time.perf_counter() - append_ui_start
                
                # Note: Textual may batch updates internally, so lag might appear after append_commits returns
                # The actual rendering might happen asynchronously in Textual's event loop
                
                title_start = time.perf_counter()
                self.loaded_commits = len(self.commits)
                self._update_commits_title()
                title_time = time.perf_counter() - title_start
                
                load_more_total = time.perf_counter() - load_more_start
                _log_timing_message(f"[TIMING] [SCROLL] load_more_commits (pre-buffered): {load_more_total*1000:.1f}ms total (extend: {extend_time*1000:.1f}ms, append_commits: {append_ui_time*1000:.1f}ms, title: {title_time*1000:.1f}ms)")
                
                # If append_commits took significant time, it's likely Textual ListView rendering
                if append_ui_time > 0.05:  # More than 50ms
                    _log_timing_message(f"[TIMING] [LAG] WARNING: append_commits took {append_ui_time*1000:.1f}ms - likely Textual ListView rendering bottleneck")
                
                # Update status for newly displayed commits
                if self.active_branch:
                    ref_spec = self.active_branch
                    repo_path_str = str(getattr(self, 'repo_path', '.'))
                    self._start_commits_status_update_background(commits_to_append, ref_spec, self.active_branch, repo_path_str)
                
                return  # Successfully used pre-buffered commits
        
        # Get more commits for the current branch (matching lazygit format)
        next_batch: list[CommitInfo] = []
        try:
            # Build git log command for the current branch (matching lazygit)
            # Format matches lazygit's format: +%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s
            ref_spec = self.active_branch if self.active_branch else "HEAD"
            cmd = [
                "git", "log",
                ref_spec,  # Current branch (matching lazygit - shows branch-specific commits)
                "--oneline",  # Match lazygit
                f"--max-count={self.page_size}",
                f"--skip={self.loaded_commits}",
                "--pretty=format:+%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s",
                "--abbrev=40",  # Match lazygit (40-char abbreviated SHA)
                "--no-show-signature",  # Match lazygit
            ]
            
            # Get repo_path - try multiple methods
            repo_path = getattr(self, 'repo_path', None)
            if not repo_path:
                try:
                    repo_path = getattr(self.git, 'repo_path', None)
                except:
                    pass
            if not repo_path:
                try:
                    if hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                        repo_path = self.git.repo.path
                except:
                    pass
            if not repo_path:
                repo_path = "."
            
            # Convert to string if it's a Path object
            repo_path_str = str(repo_path) if repo_path else "."
            
            # Run git log with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str
            )
            
            if result.returncode == 0:
                # Parse output (lazygit format: +%H%x00%at%x00%aN%x00%ae%x00%P%x00%m%x00%D%x00%s)
                seen_shas = set()
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    
                    # Skip the '+' prefix (lazygit format)
                    if line.startswith('+'):
                        line = line[1:]
                    
                    parts = line.split("\x00")
                    # LazyGit format has 8 fields: SHA, timestamp, author name, author email, parents, merge, refs, subject
                    if len(parts) >= 8:
                        sha = parts[0].strip()
                        # Remove '+' prefix if present (from lazygit format: +%H)
                        if sha.startswith('+'):
                            sha = sha[1:]
                        
                        # Skip if we've already seen this commit SHA (deduplicate)
                        if sha in seen_shas:
                            continue
                        seen_shas.add(sha)
                        
                        timestamp_str = parts[1].strip()
                        author_name = parts[2].strip()
                        author_email = parts[3].strip()
                        # parts[4] = parents (not used)
                        # parts[5] = merge status (not used)
                        # parts[6] = refs (not used)
                        summary = parts[7].strip()
                        
                        # Combine author name and email
                        author = f"{author_name} <{author_email}>" if author_email else author_name
                        
                        # Parse timestamp
                        try:
                            timestamp = int(timestamp_str)
                        except ValueError:
                            timestamp = 0
                        
                        next_batch.append(
                            CommitInfo(
                                sha=sha,
                                summary=summary,
                                author=author,
                                timestamp=timestamp,
                                pushed=False,  # Will be updated below
                            )
                        )
                    elif len(parts) >= 5:
                        # Fallback: try to parse with old format if new format fails
                        sha = parts[0].strip()
                        if sha in seen_shas:
                            continue
                        seen_shas.add(sha)
                        
                        # Try old format: %H%x00%an%x00%ae%x00%at%x00%s
                        author_name = parts[1].strip()
                        author_email = parts[2].strip()
                        timestamp_str = parts[3].strip()
                        summary = parts[4].strip()
                        
                        author = f"{author_name} <{author_email}>" if author_email else author_name
                        
                        try:
                            timestamp = int(timestamp_str)
                        except ValueError:
                            timestamp = 0
                        
                        next_batch.append(
                            CommitInfo(
                                sha=sha,
                                summary=summary,
                                author=author,
                                timestamp=timestamp,
                                pushed=False,  # Will be updated below
                            )
                        )
                
                # OPTIMIZATION: Defer remote checking - show commits immediately, update push status in background
                # Set initial status: merged=False, pushed=True (assume pushed until background thread determines otherwise)
                # This matches lazygit behavior where commits show yellow (pushed) by default if not merged
                for commit in next_batch:
                    commit.merged = False
                    commit.pushed = True  # Assume pushed (yellow) until background thread determines otherwise
                
                # Start background thread to update push status for this batch
                def update_push_status_background_batch():
                    """Update push status for commits in background with cache."""
                    try:
                        # Resolve HEAD to branch name if needed
                        actual_ref = ref_spec
                        if ref_spec == "HEAD":
                            head_resolve_start = time.perf_counter()
                            branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                            branch_result = subprocess.run(
                                branch_cmd,
                                capture_output=True,
                                text=True,
                                timeout=5,
                                cwd=repo_path_str
                            )
                            head_resolve_elapsed = time.perf_counter() - head_resolve_start
                            if branch_result.returncode == 0:
                                actual_ref = branch_result.stdout.strip()
                                _log_timing_message(f"[TIMING] git rev-parse --abbrev-ref HEAD (load_more): {head_resolve_elapsed:.4f}s (result: {actual_ref})")
                            else:
                                _log_timing_message(f"[TIMING] git rev-parse --abbrev-ref HEAD (load_more): {head_resolve_elapsed:.4f}s (ERROR: {branch_result.stderr})")
                        
                        # Use lazygit's approach: get unpushed commits (works offline)
                        # No need to check if remote exists - we use local tracking refs
                        cache_invalidated_remote_branch = False
                        unpushed_commits = set()
                        # Initialize main_branches at function scope to avoid UnboundLocalError
                        main_branches = []
                        cache_key = f"{actual_ref}_unpushed"
                        if cache_key in self._remote_commits_cache and not cache_invalidated_remote_branch:
                            unpushed_commits = self._remote_commits_cache[cache_key]
                            _log_timing_message(f"[CACHE] HIT unpushed_commits_cache for {actual_ref} (load_more): {len(unpushed_commits)} unpushed commits")
                        else:
                            # Cache MISS - use lazygit's approach: git rev-list <branch> --not origin/<branch>@{u} --not <main-branches>
                            rev_list_start = time.perf_counter()
                            try:
                                # Get main branches to exclude (commits on main are considered pushed)
                                main_branches = []
                                for main_branch in ["origin/main", "origin/master"]:
                                    check_main = subprocess.run(
                                        ["git", "rev-parse", "--verify", main_branch],
                                        capture_output=True,
                                        text=True,
                                        timeout=1,
                                        cwd=repo_path_str
                                    )
                                    if check_main.returncode == 0:
                                        main_branches.append(main_branch)
                                
                                # Try to resolve upstream tracking branch
                                upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{actual_ref}@{{u}}"]
                                upstream_result = subprocess.run(
                                    upstream_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=2,
                                    cwd=repo_path_str
                                )
                                
                                if upstream_result.returncode == 0:
                                    upstream_branch = upstream_result.stdout.strip()
                                    # Build command: git rev-list <branch> --not <upstream> --not <main-branches>
                                    unpushed_cmd = ["git", "rev-list", actual_ref, "--not", upstream_branch]
                                    for main_branch in main_branches:
                                        unpushed_cmd.extend(["--not", main_branch])
                                    unpushed_result = subprocess.run(
                                        unpushed_cmd,
                                        capture_output=True,
                                        text=True,
                                        timeout=10,
                                        cwd=repo_path_str
                                    )
                                    rev_list_elapsed = time.perf_counter() - rev_list_start
                                    
                                    if unpushed_result.returncode == 0:
                                        for sha in unpushed_result.stdout.strip().split("\n"):
                                            if sha.strip():
                                                unpushed_commits.add(sha.strip())
                                        self._remote_commits_cache[cache_key] = unpushed_commits
                                        cache_reason = "INVALIDATED" if cache_invalidated_remote_branch else "MISS"
                                        _log_timing_message(f"[CACHE] {cache_reason} unpushed_commits_cache for {actual_ref} (load_more): fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s")
                                    else:
                                        _log_timing_message(f"[TIMING] git rev-list {actual_ref} --not {upstream_branch} (load_more): {rev_list_elapsed:.4f}s (ERROR: {unpushed_result.stderr})")
                                else:
                                    # No upstream tracking branch configured
                                    # Check if remote tracking ref exists (refs/remotes/origin/<branch>)
                                    upstream_branch = f"origin/{actual_ref}"
                                    check_remote_cmd = ["git", "rev-parse", "--verify", f"refs/remotes/{upstream_branch}"]
                                    check_remote_result = subprocess.run(
                                        check_remote_cmd,
                                        capture_output=True,
                                        text=True,
                                        timeout=2,
                                        cwd=repo_path_str
                                    )
                                    
                                    if check_remote_result.returncode == 0:
                                        # Remote tracking ref exists - use it
                                        # Build command: git rev-list <branch> --not <upstream> --not <main-branches>
                                        unpushed_cmd = ["git", "rev-list", actual_ref, "--not", upstream_branch]
                                        for main_branch in main_branches:
                                            unpushed_cmd.extend(["--not", main_branch])
                                        unpushed_result = subprocess.run(
                                            unpushed_cmd,
                                            capture_output=True,
                                            text=True,
                                            timeout=10,
                                            cwd=repo_path_str
                                        )
                                        rev_list_elapsed = time.perf_counter() - rev_list_start
                                        
                                        if unpushed_result.returncode == 0:
                                            for sha in unpushed_result.stdout.strip().split("\n"):
                                                if sha.strip():
                                                    unpushed_commits.add(sha.strip())
                                            self._remote_commits_cache[cache_key] = unpushed_commits
                                            _log_timing_message(f"[CACHE] MISS unpushed_commits_cache for {actual_ref} (load_more): fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s")
                                        else:
                                            _log_timing_message(f"[TIMING] git rev-list {actual_ref} --not {upstream_branch} (load_more): {rev_list_elapsed:.4f}s (ERROR: {unpushed_result.stderr})")
                                    else:
                                        # Remote tracking ref doesn't exist
                                        # If main branches exist, commits NOT on main are likely PUSHED (yellow), not UNPUSHED (red)
                                        # Only mark as unpushed if we can't determine push status
                                        # For now, assume commits NOT on main are PUSHED (will show yellow)
                                        # This matches lazygit behavior: if branch might be pushed, show yellow
                                        if main_branches:
                                            # Don't mark commits as unpushed - they're likely pushed but not merged
                                            # Empty unpushed_commits means all commits will show as pushed (yellow if not merged)
                                            unpushed_commits = set()
                                            self._remote_commits_cache[cache_key] = unpushed_commits
                                            _log_timing_message(f"[TIMING] No remote tracking ref for {actual_ref} (load_more), assuming commits NOT on main are PUSHED (yellow) - matching lazygit behavior")
                                        else:
                                            # No main branches exist - can't determine status, assume all are unpushed
                                            rev_list_elapsed = time.perf_counter() - rev_list_start
                                            all_local_cmd = ["git", "rev-list", actual_ref]
                                            all_local_result = subprocess.run(
                                                all_local_cmd,
                                                capture_output=True,
                                                text=True,
                                                timeout=10,
                                                cwd=repo_path_str
                                            )
                                            if all_local_result.returncode == 0:
                                                for sha in all_local_result.stdout.strip().split("\n"):
                                                    if sha.strip():
                                                        unpushed_commits.add(sha.strip())
                                                self._remote_commits_cache[cache_key] = unpushed_commits
                                            _log_timing_message(f"[TIMING] No remote tracking ref for {actual_ref} (refs/remotes/{upstream_branch}) (load_more) and no main branches, treating all {len(unpushed_commits)} commits as unpushed")
                            except Exception as e:
                                rev_list_elapsed = time.perf_counter() - rev_list_start
                                _log_timing_message(f"[TIMING] Error getting unpushed commits for {actual_ref} (load_more): {type(e).__name__}: {e} in {rev_list_elapsed:.4f}s")
                        
                        # Get merged commits (those on main/master branches)
                        # CRITICAL: Check cache first to avoid re-fetching for every batch
                        merged_commits = set()
                        merged_cache_key = f"{actual_ref}_merged"
                        if merged_cache_key in self._remote_commits_cache:
                            merged_commits = self._remote_commits_cache[merged_cache_key]
                            _log_timing_message(f"[CACHE] HIT merged_commits_cache for {actual_ref}: {len(merged_commits)} merged commits")
                        elif main_branches:
                            # Cache MISS - fetch merged commits from main/master
                            # CRITICAL FIX: Remove --max-count limit or use very large number
                            # For large repos (68k+ commits), we need to check ALL commits on main/master
                            # to properly detect merged status for commits loaded via virtual scrolling
                            merged_fetch_start = time.perf_counter()
                            for main_branch in main_branches:
                                # Use --max-count with a very large number (or remove it entirely)
                                # For haiku repo with 68k commits, we need at least that many
                                merged_cmd = ["git", "rev-list", main_branch, "--max-count=100000"]
                                merged_result = subprocess.run(
                                    merged_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=30,  # Increased timeout for large repos
                                    cwd=repo_path_str
                                )
                                if merged_result.returncode == 0:
                                    for sha in merged_result.stdout.strip().split("\n"):
                                        if sha.strip():
                                            merged_commits.add(sha.strip())
                            merged_fetch_elapsed = time.perf_counter() - merged_fetch_start
                            # Cache the result for future batches
                            self._remote_commits_cache[merged_cache_key] = merged_commits
                            _log_timing_message(f"[CACHE] MISS merged_commits_cache for {actual_ref}: fetched {len(merged_commits)} merged commits in {merged_fetch_elapsed:.4f}s")
                        
                        # Update status using three-tier lazygit logic:
                        # 1. StatusMerged (green ✓): Commit exists on main/master
                        # 2. StatusPushed (yellow ↑): Commit is pushed but NOT on main/master
                        # 3. StatusUnpushed (red -): Commit is not pushed
                        normalized_unpushed_commits = {_normalize_commit_sha(sha) for sha in unpushed_commits}
                        normalized_merged_commits = {_normalize_commit_sha(sha) for sha in merged_commits}
                        
                        merged_count = 0
                        pushed_count = 0
                        unpushed_count = 0
                        
                        for commit in next_batch:
                            normalized_commit_sha = _normalize_commit_sha(commit.sha)
                            
                            # Check if merged (exists on main/master)
                            is_merged = normalized_commit_sha in normalized_merged_commits
                            commit.merged = is_merged
                            
                            # Check if unpushed
                            is_unpushed = normalized_commit_sha in normalized_unpushed_commits
                            commit.pushed = not is_unpushed
                            
                            # Count for logging
                            if is_merged:
                                merged_count += 1
                            elif is_unpushed:
                                unpushed_count += 1
                            else:
                                pushed_count += 1
                        
                        # Update UI in main thread
                        self.call_from_thread(self._update_commits_push_status_ui, next_batch)
                        _log_timing_message(f"[TIMING] update_push_status_background_batch TOTAL: Updated push status for {len(next_batch)} commits")
                    except Exception as e:
                        _log_timing_message(f"[ERROR] update_push_status_background_batch: {type(e).__name__}: {e}")
                
                # Start background thread for push status (non-blocking)
                import threading
                push_status_thread = threading.Thread(target=update_push_status_background_batch, daemon=True)
                push_status_thread.start()
        except Exception:
            # Fallback: try to use existing methods if available
            if self.active_branch:
                try:
                    if hasattr(self.git, 'list_commits_native'):
                        next_batch = self.git.list_commits_native(self.active_branch, max_count=self.page_size, skip=self.loaded_commits, timeout=10)
                    else:
                        next_batch = self.git.list_commits(self.active_branch, max_count=self.page_size, skip=self.loaded_commits)
                except Exception:
                    pass
        
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
            from pygitzen.pty_utils import should_use_pty
            
            diff_start = time.perf_counter()
            ci = self.commits[index]
            normalized_sha = _normalize_commit_sha(ci.sha)
            
            # Check if PTY streaming should be used
            if should_use_pty():
                _log_timing_message(f"[PTY] [DEBUG] show_commit_diff: Using PTY streaming for commit {normalized_sha[:8]}")
                self._show_commit_diff_pty(ci, normalized_sha)
                return
            
            # Use subprocess method (original) - only if PTY is not enabled
            _log_timing_message(f"[PTY] [DEBUG] show_commit_diff: PTY not enabled, using subprocess for commit {normalized_sha[:8]}")
            get_diff_start = time.perf_counter()
            diff = self.git.get_commit_diff(normalized_sha)
            get_diff_elapsed = time.perf_counter() - get_diff_start
            _log_timing_message(f"[TIMING] get_commit_diff: {get_diff_elapsed:.4f}s (commit: {normalized_sha[:8]})")
            show_start = time.perf_counter()
            self.patch_pane.show_commit_info(ci, diff)
            show_elapsed = time.perf_counter() - show_start
            _log_timing_message(f"[TIMING] show_commit_info: {show_elapsed:.4f}s")
            diff_total = time.perf_counter() - diff_start
            _log_timing_message(f"[TIMING] show_commit_diff TOTAL: {diff_total:.4f}s")
    
    def _load_diff_first_lines(self, normalized_sha: str, num_lines: int = 100) -> tuple[str, int]:
        """Load first N lines of diff instantly via subprocess (non-PTY, fast).
        
        Returns:
            tuple: (first_lines_text, actual_lines_count)
        """
        import subprocess
        from pathlib import Path
        
        # Get repo path
        repo_path = None
        if hasattr(self, 'repo_path'):
            repo_path = Path(self.repo_path)
        elif hasattr(self.git, 'repo_path'):
            repo_path = Path(self.git.repo_path)
        else:
            repo_path = Path(".")
        
        if not repo_path.exists():
            return ("", 0)
        
        try:
            # Use git show with head to get first N lines (fast, non-PTY)
            # Use --color=always to preserve ANSI colors
            cmd = ['git', 'show', normalized_sha, '--color=always']
            
            # Use head command to limit to first N lines
            import shutil
            if shutil.which('head'):
                # Use head command via shell to limit output
                import os
                full_cmd = f"git show {normalized_sha} --color=always | head -n {num_lines}"
                result = subprocess.run(
                    full_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=str(repo_path)
                )
            else:
                # Fallback: run git show and limit lines in Python
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=str(repo_path)
                )
                if result.returncode == 0:
                    lines = result.stdout.split('\n')
                    result.stdout = '\n'.join(lines[:num_lines])
            
            if result.returncode == 0:
                output = result.stdout
                # Find where diff starts (usually "diff --git")
                diff_start = output.find('diff --git')
                if diff_start >= 0:
                    # Extract diff part only
                    diff_text = output[diff_start:]
                    line_count = len(diff_text.split('\n'))
                    return (diff_text, min(line_count, num_lines))
                # If no diff separator found, return everything
                line_count = len(output.split('\n'))
                return (output, min(line_count, num_lines))
            else:
                return ("", 0)
        except Exception as e:
            _log_timing_message(f"[PTY] [DEBUG] Error loading first lines: {e}")
            return ("", 0)
    
    def _show_commit_diff_pty(self, commit: CommitInfo, normalized_sha: str) -> None:
        """Show commit diff using hybrid approach: first 100 lines instantly, then PTY streaming."""
        from pathlib import Path
        from pygitzen.pty_utils import stream_git_command_pty
        from rich.text import Text
        
        # Show commit header immediately (fast, no blocking)
        from datetime import datetime
        from time import timezone
        commit_datetime = datetime.fromtimestamp(commit.timestamp)
        offset_seconds = -timezone if timezone else 0
        offset_hours = offset_seconds // 3600
        offset_sign = '+' if offset_hours >= 0 else '-'
        offset_abs = abs(offset_hours)
        offset_str = f"{offset_sign}{offset_abs:02d}00"
        commit_date = commit_datetime.strftime(f"%a %b %d %H:%M:%S %Y {offset_str}")
        commit_sha = _normalize_commit_sha(commit.sha)
        
        header_text = f"""commit {commit_sha}
Author: {commit.author}
Date: {commit_date}

{commit.summary}

"""
        
        # PHASE 3: Load first 100 lines instantly (non-PTY, fast)
        import time
        _log_timing_message(f"[PTY] [DEBUG] Loading first 100 lines instantly for commit {normalized_sha[:8]}")
        first_lines_start = time.perf_counter()
        first_lines_text, first_lines_count = self._load_diff_first_lines(normalized_sha, num_lines=100)
        first_lines_elapsed = time.perf_counter() - first_lines_start
        _log_timing_message(f"[TIMING] [DIFF] First 100 lines loaded: {first_lines_elapsed*1000:.1f}ms ({first_lines_count} lines)")
        
        # Show header + first 100 lines immediately (instant feedback)
        if first_lines_text:
            # Parse ANSI to Rich Text for first lines
            from pygitzen.git_graph import parse_ansi_to_rich_text
            try:
                first_lines_rich = parse_ansi_to_rich_text(first_lines_text)
            except:
                first_lines_rich = Text(first_lines_text, style="white")
            
            # Combine header and first lines
            full_content = Text(header_text, style="white") + first_lines_rich
            self.patch_pane.show_commit_info(commit, full_content, is_partial=True)
            _log_timing_message(f"[PTY] [DEBUG] First 100 lines displayed instantly")
        else:
            # Fallback: show header with loading message
            self.patch_pane.update(Text(header_text + "Loading diff...", style="white"))
        
        # Use task manager to handle cancellation automatically (non-blocking)
        # This returns immediately, so UI selection updates instantly
        def run_diff_task(should_stop: Callable[[], bool]) -> None:
            """Stream git show output using PTY task manager."""
            current_sha = normalized_sha
            try:
                _log_timing_message(f"[PTY] [DEBUG] stream_diff_in_background: Starting for commit {current_sha[:8]}")
                
                # Get repo path - try multiple methods
                repo_path = None
                if hasattr(self, 'repo_path'):
                    repo_path = Path(self.repo_path)
                    _log_timing_message(f"[PTY] [DEBUG] Got repo_path from self.repo_path: {repo_path}")
                elif hasattr(self.git, 'repo_path'):
                    repo_path = Path(self.git.repo_path)
                    _log_timing_message(f"[PTY] [DEBUG] Got repo_path from self.git.repo_path: {repo_path}")
                else:
                    repo_path = Path(".")
                    _log_timing_message(f"[PTY] [DEBUG] Using default repo_path: {repo_path}")
                
                # Validate repo path exists
                if not repo_path.exists():
                    error_msg = f"Repository path does not exist: {repo_path}"
                    _log_timing_message(f"[PTY] [ERROR] {error_msg}")
                    raise OSError(error_msg)
                
                # Validate it's a git repo
                if not (repo_path / '.git').exists() and not (repo_path / '.git').is_file():
                    _log_timing_message(f"[PTY] [DEBUG] Warning: .git not found at {repo_path}, but continuing...")
                
                # Build git show command with color support
                cmd = ['git', 'show', normalized_sha, '--color=always']
                _log_timing_message(f"[PTY] [DEBUG] Command: {' '.join(cmd)}")
                _log_timing_message(f"[PTY] [DEBUG] Working directory: {repo_path}")
                
                # PHASE 3: Skip first 100 lines (already loaded instantly)
                # Track lines to skip and remaining lines to append
                lines_to_skip = first_lines_count if first_lines_text else 0
                lines_skipped = 0
                diff_lines_rich = []  # Store Rich Text objects for remaining lines
                # PHASE 3: Increased batch size to 50 lines for progressive appending (less frequent updates)
                batch_size = 50  # Update UI every 50 lines for better performance
                diff_start_found = False
                
                _log_timing_message(f"[PTY] [DEBUG] Starting PTY stream for git show (will skip first {lines_to_skip} lines)")
                
                # Stream output using PTY (with process callback for task manager)
                # Store process reference so we can kill it if cancelled
                current_process_ref = [None]  # Use list to allow modification in callback
                
                def set_process(process):
                    """Set process reference for cancellation."""
                    current_process_ref[0] = process
                    self._pty_diff_task_manager.set_current_process(process)
                
                line_count = 0
                pty_stream = stream_git_command_pty(
                    cmd,
                    repo_path,
                    timeout=30.0,
                    max_lines=None,
                    process_callback=set_process
                )
                try:
                    for rich_line in pty_stream:
                        # Check if task was cancelled
                        if should_stop():
                            _log_timing_message(f"[PTY] [DEBUG] Task cancelled during streaming for commit {current_sha[:8]}")
                            # Kill the process directly if we have it (kill entire process group)
                            if current_process_ref[0] is not None:
                                try:
                                    if current_process_ref[0].poll() is None:
                                        import os
                                        import signal
                                        # Kill entire process group (since start_new_session=True was used)
                                        pgid = os.getpgid(current_process_ref[0].pid)
                                        _log_timing_message(f"[PTY] [DEBUG] Killing process group {pgid} (PID: {current_process_ref[0].pid}) due to cancellation")
                                        try:
                                            os.killpg(pgid, signal.SIGTERM)
                                            try:
                                                current_process_ref[0].wait(timeout=0.2)
                                            except subprocess.TimeoutExpired:
                                                os.killpg(pgid, signal.SIGKILL)
                                                try:
                                                    current_process_ref[0].wait(timeout=0.1)
                                                except:
                                                    pass
                                        except (OSError, ProcessLookupError) as pg_error:
                                            # Check if it's just "No such process" (process already terminated)
                                            if isinstance(pg_error, ProcessLookupError) or (isinstance(pg_error, OSError) and pg_error.errno == 3):
                                                # Process already terminated - this is fine, not an error
                                                _log_timing_message(f"[PTY] [DEBUG] Process already terminated (PID: {current_process_ref[0].pid})")
                                            else:
                                                # Real error - try fallback
                                                try:
                                                    current_process_ref[0].terminate()
                                                    try:
                                                        current_process_ref[0].wait(timeout=0.1)
                                                    except subprocess.TimeoutExpired:
                                                        current_process_ref[0].kill()
                                                except (OSError, ProcessLookupError):
                                                    # Process already gone - ignore
                                                    pass
                                except (OSError, ProcessLookupError) as e:
                                    # Process already terminated - this is expected, not an error
                                    if isinstance(e, ProcessLookupError) or (isinstance(e, OSError) and e.errno == 3):
                                        _log_timing_message(f"[PTY] [DEBUG] Process already terminated (expected)")
                                    else:
                                        _log_timing_message(f"[PTY] [DEBUG] Error killing process: {e}")
                                except Exception as e:
                                    _log_timing_message(f"[PTY] [DEBUG] Unexpected error killing process: {e}")
                            # Close the generator to stop streaming (this will trigger cleanup in finally block)
                            try:
                                pty_stream.close()
                            except Exception:
                                pass
                            break
                        
                        line_count += 1
                        if line_count == 1:
                            _log_timing_message(f"[PTY] [DEBUG] First line received from PTY stream")
                        
                        # Convert Rich Text to string for diff detection (keep Rich Text for display)
                        line_str = str(rich_line)
                        
                        # Find where diff starts (usually "diff --git")
                        if not diff_start_found:
                            if 'diff --git' in line_str:
                                diff_start_found = True
                                # PHASE 3: Skip first N lines that were already loaded
                                if lines_to_skip > 0:
                                    lines_skipped = 1  # Count this line as skipped
                                    _log_timing_message(f"[PTY] [DEBUG] Diff start found, skipping first {lines_to_skip} lines")
                                    continue
                                else:
                                    # No lines to skip, start collecting
                                    diff_lines_rich.append(rich_line)
                            # Skip lines before diff starts (commit message, etc.)
                            continue
                        
                        # PHASE 3: Skip lines that were already loaded instantly
                        if lines_to_skip > 0 and lines_skipped < lines_to_skip:
                            lines_skipped += 1
                            continue
                        
                        # Collect remaining diff lines as Rich Text objects
                        diff_lines_rich.append(rich_line)
                        
                        # PHASE 3: Update UI periodically (every batch_size lines) for progressive appending
                        # This appends remaining lines to the already-displayed first 100 lines
                        if len(diff_lines_rich) % batch_size == 0:
                            # Check if task was cancelled before updating UI
                            if should_stop():
                                _log_timing_message(f"[PTY] [DEBUG] Task cancelled during batch update")
                                break
                            # PHASE 3: Combine remaining lines and append to existing content
                            from rich.text import Text
                            remaining_diff = Text()
                            for rich_line_item in diff_lines_rich:
                                remaining_diff.append(rich_line_item)
                                remaining_diff.append("\n")
                            
                            # PHASE 3: Reconstruct full content (header + first 100 lines + remaining lines)
                            # We already have header and first_lines_rich from the initial load
                            def append_remaining_lines(remaining: Text):
                                # Reconstruct full content: header + first 100 lines + remaining lines
                                if first_lines_text:
                                    # Parse first lines again (or use stored first_lines_rich)
                                    from pygitzen.git_graph import parse_ansi_to_rich_text
                                    try:
                                        first_lines_rich = parse_ansi_to_rich_text(first_lines_text)
                                    except:
                                        first_lines_rich = Text(first_lines_text, style="white")
                                    
                                    # Combine: header + first 100 lines + remaining lines
                                    full_content = Text(header_text, style="white") + first_lines_rich + remaining
                                else:
                                    # Fallback: header + remaining lines only
                                    full_content = Text(header_text, style="white") + remaining
                                
                                self.patch_pane.show_commit_info(commit, full_content, is_partial=True)
                            
                            # Update UI from main thread
                            self.call_from_thread(
                                lambda rem=remaining_diff: append_remaining_lines(rem)
                            )
                finally:
                    # Ensure generator is closed even if we break early
                    if should_stop():
                        try:
                            pty_stream.close()
                        except Exception:
                            pass
                
                # PHASE 3: Final update with all remaining diff lines (only if task wasn't cancelled)
                if not should_stop():
                    if diff_lines_rich:
                        # Combine all remaining Rich Text objects
                        from rich.text import Text
                        remaining_diff = Text()
                        for rich_line_item in diff_lines_rich:
                            remaining_diff.append(rich_line_item)
                            remaining_diff.append("\n")
                        
                        # PHASE 3: Reconstruct full content (header + first 100 lines + remaining lines)
                        def append_final_lines(remaining: Text):
                            # Reconstruct full content: header + first 100 lines + remaining lines
                            if first_lines_text:
                                # Parse first lines again (or use stored first_lines_rich)
                                from pygitzen.git_graph import parse_ansi_to_rich_text
                                try:
                                    first_lines_rich = parse_ansi_to_rich_text(first_lines_text)
                                except:
                                    first_lines_rich = Text(first_lines_text, style="white")
                                
                                # Combine: header + first 100 lines + remaining lines
                                final_content = Text(header_text, style="white") + first_lines_rich + remaining
                            else:
                                # Fallback: header + remaining lines only
                                final_content = Text(header_text, style="white") + remaining
                            
                            self.patch_pane.show_commit_info(commit, final_content, is_partial=False)
                        
                        _log_timing_message(f"[PTY] [DEBUG] PTY stream completed. Total lines: {line_count}, Remaining diff lines: {len(diff_lines_rich)}")
                        
                        # Final UI update from main thread
                        self.call_from_thread(
                            lambda rem=remaining_diff: append_final_lines(rem)
                        )
                        _log_timing_message(f"[PTY] [DEBUG] UI updated with final diff (hybrid: {first_lines_count} instant + {len(diff_lines_rich)} streamed)")
                    else:
                        _log_timing_message(f"[PTY] [WARNING] No remaining diff lines collected from PTY stream (first {first_lines_count} lines were already shown)")
                else:
                    _log_timing_message(f"[PTY] [DEBUG] Task cancelled before final update, skipping UI update")
                
            except Exception as e:
                # Log detailed error information
                import traceback
                error_type = type(e).__name__
                error_msg = str(e) if str(e) else f"{error_type}"
                error_traceback = traceback.format_exc()
                _log_timing_message(f"[PTY] [ERROR] Exception in stream_diff_in_background: {error_type}: {error_msg}")
                _log_timing_message(f"[PTY] [ERROR] Traceback:\n{error_traceback}")
                
                # Show error - NO FALLBACK, PTY must work
                if not should_stop():
                    error_text = Text(f"Error streaming diff with PTY: {error_type}: {error_msg}\n\nCheck timing.log for details.", style="red")
                    self.call_from_thread(lambda: self.patch_pane.update(error_text))
        
        # Use task manager to start the task (automatically cancels previous one)
        self._pty_diff_task_manager.new_task(run_diff_task, task_key=f"commit_diff_{normalized_sha[:8]}")
    
    def show_stash_diff(self, index: int) -> None:
        """Show stash diff in patch pane when stash is selected."""
        if 0 <= index < len(self.stashes):
            stash = self.stashes[index]
            # Switch to patch view when stash is selected
            self._view_mode = "patch"
            self.log_pane.styles.display = "none"
            self.patch_pane.styles.display = "block"
            
            # Get stash diff and stat
            try:
                # Check if method exists (Cython version might not have it)
                if hasattr(self.git, 'get_stash_diff'):
                    diff_text, stat_text = self.git.get_stash_diff(stash.index)
                else:
                    # Fallback to Python version if Cython doesn't have the method
                    from .git_service import GitService
                    # Get repo_path from git service (both Python and Cython have this)
                    repo_path = None
                    try:
                        if hasattr(self.git, 'repo_path'):
                            repo_path = self.git.repo_path
                    except (AttributeError, TypeError):
                        pass
                    if repo_path is None:
                        try:
                            repo_path = getattr(self.git, 'repo_path', None)
                        except (AttributeError, TypeError):
                            pass
                    if repo_path is None:
                        repo_path = self.repo_path if hasattr(self, 'repo_path') else "."
                    
                    if isinstance(repo_path, Path):
                        repo_path_str = str(repo_path)
                    else:
                        repo_path_str = str(repo_path) if repo_path else "."
                    
                    python_git = GitService(repo_path_str)
                    diff_text, stat_text = python_git.get_stash_diff(stash.index)
                
                self.patch_pane.show_stash_info(stash, diff_text, stat_text)
            except Exception as e:
                # If stash diff fetching fails, show error
                from rich.text import Text
                error_text = Text(f"Error loading stash diff: {type(e).__name__}: {e}", style="red")
                self.patch_pane.update(error_text)
    
    def show_tag_info(self, tag: TagInfo) -> None:
        """Show tag info and git log graph (matching Lazygit behavior)."""
        import threading
        import subprocess
        from pathlib import Path
        
        tag_start = time.perf_counter()
        _log_timing_message(f"[TIMING] show_tag_info START (tag: {tag.name})")
        
        def load_tag_info_in_thread():
            """Load tag info in background thread (non-blocking)."""
            try:
                repo_path_str = str(self.repo_path) if hasattr(self, 'repo_path') else "."
                
                # Build tag info header (matching Lazygit)
                tag_info_lines = []
                
                if tag.is_annotated:
                    # Annotated tag - get full annotation info
                    tag_info_lines.append(f"Annotated tag: {tag.name}")
                    
                    # Get tagger info and message
                    try:
                        tagger_cmd = ['git', 'for-each-ref', f'refs/tags/{tag.name}', 
                                      '--format=Tagger:     %(taggername) <%(taggeremail)>\nTaggerDate: %(taggerdate:iso)\n\n%(contents:subject)']
                        tagger_result = subprocess.run(
                            tagger_cmd,
                            capture_output=True,
                            text=True,
                            timeout=3,
                            cwd=repo_path_str
                        )
                        if tagger_result.returncode == 0:
                            tagger_info = tagger_result.stdout.strip()
                            # Filter out PGP signature (like Lazygit)
                            lines = tagger_info.split('\n')
                            filtered_lines = []
                            in_pgp_signature = False
                            for line in lines:
                                if line == "-----END PGP SIGNATURE-----":
                                    in_pgp_signature = False
                                    continue
                                if line == "-----BEGIN PGP SIGNATURE-----":
                                    in_pgp_signature = True
                                    continue
                                if not in_pgp_signature:
                                    filtered_lines.append(line)
                            tagger_info = '\n'.join(filtered_lines)
                            tag_info_lines.append(tagger_info)
                    except Exception:
                        # If we can't get tagger info, just show the message
                        if tag.message:
                            tag_info_lines.append(tag.message)
                else:
                    # Lightweight tag
                    tag_info_lines.append(f"Lightweight tag: {tag.name}")
                
                # Add separator
                tag_info_lines.append("\n---\n")
                
                # Build git log command (matching Lazygit)
                # CRITICAL: Limit commits to prevent hangs on large repos like haiku
                # Use --max-count to limit output and prevent UI blocking
                tag_ref = f"refs/tags/{tag.name}"
                log_cmd = [
                    'git', 'log',
                    '--graph',
                    '--color=always',
                    '--abbrev-commit',
                    '--decorate',
                    '--date=relative',
                    '--pretty=medium',
                    '--max-count=100',  # Limit to 100 commits to prevent hangs on large repos
                    tag_ref,
                    '--'
                ]
                
                # Check if PTY streaming should be used
                from pygitzen.pty_utils import should_use_pty, stream_git_command_pty
                from pygitzen.git_graph import parse_ansi_to_rich_text
                from rich.text import Text
                
                # Create initial Text object with tag info header
                display_text = Text()
                display_text.append('\n'.join(tag_info_lines), style="white")
                display_text.append('\n\n', style="white")
                
                # Show initial content (tag info header) immediately
                self._ui_update_queue.put(lambda: self.log_pane.update(display_text))
                
                # Try PTY streaming if available, otherwise fallback to subprocess
                git_log_lines = []
                try:
                    if should_use_pty():
                        # Stream git log output using PTY (progressive display)
                        repo_path = Path(repo_path_str)
                        # Optimized: Increased batch size from 5 to 15 lines to reduce call_from_thread overhead
                        batch_size = 15  # Update UI every 15 lines for better performance
                        
                        for rich_line in stream_git_command_pty(
                            log_cmd,
                            repo_path,
                            timeout=30.0,
                            max_lines=100  # Limit to 100 lines
                        ):
                            git_log_lines.append(rich_line)
                            
                            # Update UI periodically (every batch_size lines)
                            if len(git_log_lines) % batch_size == 0:
                                # Build current display text
                                current_display = Text()
                                current_display.append('\n'.join(tag_info_lines), style="white")
                                current_display.append('\n\n', style="white")
                                for line in git_log_lines:
                                    current_display.append(line)
                                    current_display.append('\n')
                                
                                # Update UI from main thread
                                self._ui_update_queue.put(lambda: self.log_pane.update(current_display))
                        
                        # Final update with all lines
                        final_display = Text()
                        final_display.append('\n'.join(tag_info_lines), style="white")
                        final_display.append('\n\n', style="white")
                        for line in git_log_lines:
                            final_display.append(line)
                            final_display.append('\n')
                        display_text = final_display
                        
                        # Final UI update
                        self._ui_update_queue.put(lambda: self.log_pane.update(display_text))
                    else:
                        # Fallback to subprocess (original method)
                        log_result = subprocess.run(
                            log_cmd,
                            capture_output=True,
                            text=False,  # Get bytes first to handle non-UTF-8 characters
                            timeout=30,  # Increased timeout for large repos (was 10s)
                            cwd=repo_path_str
                        )
                        
                        # Decode with error handling for non-UTF-8 characters (like haiku repo)
                        if log_result.returncode == 0:
                            try:
                                git_log_output = log_result.stdout.decode('utf-8', errors='replace')
                            except Exception:
                                # Fallback if decode fails
                                try:
                                    git_log_output = log_result.stdout.decode('utf-8', errors='ignore')
                                except Exception:
                                    git_log_output = "Error: Could not decode git log output"
                        else:
                            try:
                                error_msg = log_result.stderr.decode('utf-8', errors='replace')
                            except Exception:
                                try:
                                    error_msg = log_result.stderr.decode('utf-8', errors='ignore')
                                except Exception:
                                    error_msg = "Unknown error"
                            git_log_output = f"Error loading git log: {error_msg}"
                        
                        # Add git log with ANSI colors preserved
                        if git_log_output:
                            for line in git_log_output.split('\n'):
                                if line:
                                    try:
                                        rich_line = parse_ansi_to_rich_text(line)
                                        display_text.append(rich_line)
                                        display_text.append('\n')
                                    except Exception:
                                        # If parsing fails, strip ANSI and add as plain text
                                        from pygitzen.git_graph import strip_ansi_codes
                                        plain_line = strip_ansi_codes(line)
                                        display_text.append(plain_line + '\n', style="white")
                        
                        # UI update for subprocess fallback (final update happens below for all paths)
                except Exception as e:
                    # If PTY streaming fails, fallback to subprocess
                    _log_timing_message(f"[PTY] Tag info fallback to subprocess: {type(e).__name__}: {e}")
                    try:
                        log_result = subprocess.run(
                            log_cmd,
                            capture_output=True,
                            text=False,
                            timeout=30,
                            cwd=repo_path_str
                        )
                        
                        if log_result.returncode == 0:
                            try:
                                git_log_output = log_result.stdout.decode('utf-8', errors='replace')
                            except Exception:
                                git_log_output = log_result.stdout.decode('utf-8', errors='ignore')
                        else:
                            error_msg = log_result.stderr.decode('utf-8', errors='replace')
                            git_log_output = f"Error loading git log: {error_msg}"
                        
                        # Add git log to display
                        if git_log_output:
                            for line in git_log_output.split('\n'):
                                if line:
                                    try:
                                        rich_line = parse_ansi_to_rich_text(line)
                                        display_text.append(rich_line)
                                        display_text.append('\n')
                                    except Exception:
                                        from pygitzen.git_graph import strip_ansi_codes
                                        plain_line = strip_ansi_codes(line)
                                        display_text.append(plain_line + '\n', style="white")
                    except Exception as fallback_error:
                        error_text = Text(f"Error loading git log: {fallback_error}", style="red")
                        display_text.append(error_text)
                
                tag_elapsed = time.perf_counter() - tag_start
                _log_timing_message(f"[TIMING] show_tag_info TOTAL: {tag_elapsed:.4f}s")
                
                # Clear cached branch info when showing tag (so branch selection detection works)
                # Update UI from main thread (use queue which is thread-safe)
                def update_tag_info():
                    self.log_pane._cached_branch = ""
                    self.log_pane._native_git_log_lines = []
                    self.log_pane.update(display_text)
                self._ui_update_queue.put(update_tag_info)
            except Exception as e:
                # If tag info fetching fails, show error
                import traceback
                error_msg = f"Error loading tag info: {type(e).__name__}: {e}\n{traceback.format_exc()}"
                _log_timing_message(f"[ERROR] show_tag_info: {error_msg}")
                from rich.text import Text
                error_text = Text(f"Error loading tag info: {type(e).__name__}: {e}", style="red")
                # Clear cached branch info when showing tag error (so branch selection detection works)
                # Update UI from main thread (use queue which is thread-safe)
                def update_tag_error():
                    self.log_pane._cached_branch = ""
                    self.log_pane._native_git_log_lines = []
                    self.log_pane.update(error_text)
                self._ui_update_queue.put(update_tag_error)
        
        # Run in background thread to avoid blocking UI
        thread = threading.Thread(target=load_tag_info_in_thread, daemon=True)
        thread.start()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is self.branches_pane:
            index = event.index
            if 0 <= index < len(self.branches):
                selected_branch = self.branches[index].name
                should_reload = False
                
                import subprocess
                repo_path_str = str(self.repo_path) if hasattr(self, 'repo_path') else "."
                
                if selected_branch != self.active_branch:
                    # Different branch - always reload
                    should_reload = True
                    cache_key = f"{selected_branch}_unpushed"
                    self._remote_commits_cache.pop(cache_key, None)
                    # Clear sync status cache for the new branch (will be recalculated)
                    self._branch_sync_status_cache.pop(selected_branch, None)
                    self.active_branch = selected_branch
                else:
                    # Same branch - check if HEAD has changed (new commits were made) or remote HEAD changed (pushed)
                    should_reload = False
                    try:
                        # Check local HEAD SHA
                        head_sha_cmd = ["git", "rev-parse", selected_branch]
                        head_sha_result = subprocess.run(
                            head_sha_cmd,
                            capture_output=True,
                            text=True,
                            timeout=2,
                            cwd=repo_path_str
                        )
                        current_head_sha = None
                        if head_sha_result.returncode == 0:
                            current_head_sha = head_sha_result.stdout.strip()
                            # Check if local HEAD changed (new commits)
                            if selected_branch in self._last_head_sha:
                                if self._last_head_sha[selected_branch] != current_head_sha:
                                    # Local HEAD changed - new commits were made, reload
                                    should_reload = True
                                    _log_timing_message(f"[BRANCH] Local HEAD changed for {selected_branch}: {self._last_head_sha[selected_branch][:8]} → {current_head_sha[:8]}, reloading commits")
                                    # Clear cache for this branch
                                    cache_key = f"{selected_branch}_unpushed"
                                    self._remote_commits_cache.pop(cache_key, None)
                                    # Clear sync status cache (will be recalculated)
                                    self._branch_sync_status_cache.pop(selected_branch, None)
                            else:
                                # First time loading this branch, reload
                                should_reload = True
                        
                        # Also check if remote HEAD changed (commits were pushed)
                        if not should_reload and current_head_sha:
                            # Get upstream tracking branch
                            upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{selected_branch}@{{u}}"]
                            upstream_result = subprocess.run(
                                upstream_cmd,
                                capture_output=True,
                                text=True,
                                timeout=2,
                                cwd=repo_path_str
                            )
                            if upstream_result.returncode == 0:
                                upstream = upstream_result.stdout.strip()
                                # Get remote HEAD SHA
                                remote_head_cmd = ["git", "rev-parse", upstream]
                                remote_head_result = subprocess.run(
                                    remote_head_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=2,
                                    cwd=repo_path_str
                                )
                                if remote_head_result.returncode == 0:
                                    current_remote_head_sha = remote_head_result.stdout.strip()
                                    # Check if remote HEAD changed (commits were pushed)
                                    cache_key_remote = f"{selected_branch}_remote_head"
                                    if cache_key_remote in self._last_remote_head_sha:
                                        if self._last_remote_head_sha[cache_key_remote] != current_remote_head_sha:
                                            # Remote HEAD changed - commits were pushed, reload
                                            should_reload = True
                                            _log_timing_message(f"[BRANCH] Remote HEAD changed for {selected_branch}: {self._last_remote_head_sha[cache_key_remote][:8]} → {current_remote_head_sha[:8]}, reloading commits")
                                            # Clear cache for this branch
                                            cache_key = f"{selected_branch}_unpushed"
                                            self._remote_commits_cache.pop(cache_key, None)
                                            # Clear sync status cache (will be recalculated)
                                            self._branch_sync_status_cache.pop(selected_branch, None)
                                    else:
                                        # First time checking remote HEAD, reload to be safe
                                        should_reload = True
                    except Exception:
                        # If we can't check HEAD, reload to be safe
                        should_reload = True
                
                if should_reload:
                    # Switch to log view when branch is selected
                    self._view_mode = "log"
                    self.patch_pane.styles.display = "none"
                    self.log_pane.styles.display = "block"
                    # Load commits for the selected branch (matching lazygit - shows branch-specific commits)
                    self.load_commits(self.active_branch)
                    # Load commits with full history for feature branches (for log pane)
                    self.load_commits_for_log(self.active_branch)
                    # Refresh sync status for the selected branch
                    self._refresh_branch_sync_status(self.active_branch)
                    self.update_status_info()
                else:
                    # Same branch, no new commits - but if we're switching from patch/tag view, refresh log
                    was_patch_view = self._view_mode == "patch"
                    # Check if we're viewing tag info (tag info is shown in log view, but doesn't set _cached_branch)
                    # Tag info updates log_pane directly but doesn't set _cached_branch, so if we're in log view
                    # and _cached_branch is empty/not set, we're likely viewing tag info
                    was_tag_view = False
                    if self._view_mode == "log":
                        # Check if log pane has content but no cached branch (indicates tag info)
                        if hasattr(self.log_pane, '_cached_branch'):
                            # _cached_branch is empty string "" when tag info is shown (not set by show_tag_info)
                            was_tag_view = not self.log_pane._cached_branch or self.log_pane._cached_branch == ""
                    
                    if was_patch_view or was_tag_view:
                        # Switch to log view when branch is selected (even if same branch)
                        self._view_mode = "log"
                        self.patch_pane.styles.display = "none"
                        self.log_pane.styles.display = "block"
                        # Refresh log display even for same branch
                        self.load_commits_for_log(self.active_branch)
                    # Refresh sync status and update status info
                    self._refresh_branch_sync_status(self.active_branch)
                    self.update_status_info()
        elif event.list_view is self.commits_pane:
            # Switch to patch view when commit is selected
            self._view_mode = "patch"
            self.log_pane.styles.display = "none"
            self.patch_pane.styles.display = "block"
            self.selected_commit_index = event.index
            self.show_commit_diff(event.index)
        elif event.list_view is self.stash_pane:
            # Only show stash diff if there are actual stashes
            if self.stashes and 0 <= event.index < len(self.stashes):
                # Switch to patch view when stash is selected
                self._view_mode = "patch"
                self.log_pane.styles.display = "none"
                self.patch_pane.styles.display = "block"
                self.show_stash_diff(event.index)
            # If "No stashes" is clicked, do nothing (don't show commit diff)
        elif event.list_view is self.tags_pane:
            # Show tag info and git log when tag is selected
            if self.tags and 0 <= event.index < len(self.tags):
                selected_tag = self.tags[event.index]
                # Switch to log view when tag is selected (like Lazygit)
                self._view_mode = "log"
                self.patch_pane.styles.display = "none"
                self.log_pane.styles.display = "block"
                self.show_tag_info(selected_tag)

    def action_load_more(self) -> None:
        """Load more commits - works for both commits pane and log view."""
        if self._view_mode == "log":
            # Load more for log view
            self.load_more_commits_for_log(self.active_branch)
        else:
            # Load more for commits pane
            self.load_more_commits()
    
    def on_scroll(self, event) -> None:
        """Handle scroll events - update virtual scrolling range and auto-load more commits.
        
        Phase 2: Added pre-buffering and debouncing for smooth virtual scrolling.
        """
        import time
        widget = event.widget
        widget_id = widget.id if hasattr(widget, 'id') else None
        
        # Handle scroll for commits pane (left side)
        if widget_id == "commits-pane" or (hasattr(widget, 'id') and widget.id == "commits-pane"):
            try:
                # Phase 2: Debounce scroll events (100ms delay)
                current_time = time.perf_counter()
                if current_time - self._last_scroll_time < self._scroll_debounce_delay:
                    return  # Skip this scroll event (debouncing)
                self._last_scroll_time = current_time
                
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
                    loaded_count = len(self.commits) if hasattr(self, 'commits') and self.commits else self.loaded_commits
                    
                    # Phase 2: Pre-buffering at 80% (before the 85% trigger)
                    # This ensures commits are ready before user scrolls to them
                    if (scroll_percent >= self._prebuffer_threshold and 
                        loaded_count < self.total_commits and 
                        not self._prebuffering):
                        # Calculate how many commits ahead we need
                        commits_ahead_needed = self._prebuffer_batch_size
                        commits_available = self.total_commits - loaded_count
                        commits_to_load = min(commits_ahead_needed, commits_available)
                        
                        if commits_to_load > 0:
                            _log_timing_message(f"[TIMING] [SCROLL] [PREBUFFER] Commits pane: Pre-buffering {commits_to_load} commits ahead (scroll_percent={scroll_percent:.2f}, loaded={loaded_count}, total={self.total_commits})")
                            self._prebuffer_commits_ahead(loaded_count, commits_to_load)
                    
                    # Original logic: If scrolled near bottom (85%), auto-load more commits
                    if scroll_percent >= 0.85 and self.loaded_commits < self.total_commits:
                        scroll_handler_start = time.perf_counter()
                        _log_timing_message(f"[TIMING] [SCROLL] Commits pane: Loading more commits (scroll_percent={scroll_percent:.2f}, loaded={self.loaded_commits}, total={self.total_commits})")
                        self.load_more_commits()
                        scroll_handler_time = time.perf_counter() - scroll_handler_start
                        if scroll_handler_time > 0.05:  # More than 50ms
                            _log_timing_message(f"[TIMING] [SCROLL] [LAG] on_scroll handler took {scroll_handler_time*1000:.1f}ms - likely load_more_commits bottleneck")
            except Exception as e:
                pass  # Silently fail if scroll detection fails
        
        # Handle scroll for tags pane - virtual scrolling
        if widget_id == "tags-pane" or (hasattr(widget, 'id') and widget.id == "tags-pane"):
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
                
                # Check if we need to load more tags
                if hasattr(self.tags_pane, '_rendered_count') and hasattr(self.tags_pane, '_total_tags_count'):
                    rendered = self.tags_pane._rendered_count
                    total = self.tags_pane._total_tags_count
                    
                    if max_scroll_y > 0 and total > 0:
                        scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                        
                        # If scrolled near bottom (85%), auto-load more tags
                        if scroll_percent >= 0.85 and rendered < total:
                            _log_timing_message(f"[TIMING] [SCROLL] Tags pane: Loading more tags (scroll_percent={scroll_percent:.2f}, rendered={rendered}, total={total})")
                            self._load_more_tags()
            except Exception:
                pass  # Silently fail if scroll detection fails
        
        # Handle scroll for tags pane - virtual scrolling
        if widget_id == "tags-pane" or (hasattr(widget, 'id') and widget.id == "tags-pane"):
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
                
                # Check if we need to load more tags
                if hasattr(self.tags_pane, '_rendered_count') and hasattr(self.tags_pane, '_total_tags_count'):
                    rendered = self.tags_pane._rendered_count
                    total = self.tags_pane._total_tags_count
                    
                    if max_scroll_y > 0 and total > 0:
                        scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                        
                        # If scrolled near bottom (85%), auto-load more tags
                        if scroll_percent >= 0.85 and rendered < total:
                            _log_timing_message(f"[TIMING] [SCROLL] Tags pane: Loading more tags (scroll_percent={scroll_percent:.2f}, rendered={rendered}, total={total})")
                            self._load_more_tags()
            except Exception:
                pass  # Silently fail if scroll detection fails
        
        # Handle scroll for log view (right side) - native git log virtual scrolling
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
                    max_scroll_y = widget.virtual_size.height if hasattr(widget.virtual_size, 'height') else 0
                
                # Also try to get from the scroll container if widget is log-pane
                if widget_id == "log-pane" and hasattr(self, 'log_pane'):
                    # Find the scroll container parent
                    container = self.query_one("#patch-scroll-container", None)
                    if container and hasattr(container, 'scroll_y'):
                        scroll_y = container.scroll_y
                        max_scroll_y = container.max_scroll_y if hasattr(container, 'max_scroll_y') else 0
                
                # Check if we need to load more commits for native git log
                # Only do this if we're using native git log (have cached lines)
                if max_scroll_y > 0 and self.log_pane._native_git_log_lines:
                    scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                    
                    # If scrolled near bottom (85%), load more commits
                    if scroll_percent >= 0.85 and not self.log_pane._native_git_log_loading:
                        _log_timing_message(f"[TIMING] [SCROLL] Log pane: Loading more commits (scroll_percent={scroll_percent:.2f}, current_count={self.log_pane._native_git_log_count})")
                        # Load more commits - use same wrapper approach as load_commits_for_log
                        if self.active_branch and self.git:
                            # Get repo_path (same logic as load_commits_for_log)
                            repo_path_to_use = None
                            if hasattr(self, 'repo_path') and self.repo_path:
                                repo_path_to_use = self.repo_path
                            elif hasattr(self.git, 'repo_path'):
                                try:
                                    repo_path_to_use = self.git.repo_path
                                except:
                                    pass
                            elif hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                                try:
                                    repo_path_to_use = self.git.repo.path
                                except:
                                    pass
                            
                            # Create wrapper with repo_path
                            class GitServiceWithPath:
                                def __init__(self, git_service, repo_path):
                                    self.git_service = git_service
                                    self.repo_path = Path(repo_path) if repo_path else None
                                    if hasattr(git_service, 'repo'):
                                        self.repo = git_service.repo
                            
                            git_service_wrapper = GitServiceWithPath(self.git, repo_path_to_use or ".")
                            basic_branch_info = {"name": self.active_branch, "head_sha": None, "remote_tracking": None, "upstream": None, "is_current": False}
                            self.log_pane._show_native_git_log(self.active_branch, basic_branch_info, git_service_wrapper, append=True)
                    return  # Skip old virtual scrolling logic for native git log
                
                # OLD VIRTUAL SCROLLING LOGIC (for custom rendering - not used with native git log)
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
                                            # Normalize SHA before lookup (fix for Cython version)
                                            normalized_sha = _normalize_commit_sha(commit_sha)
                                            return self.refs_map.get(normalized_sha, {"branches": [], "remote_branches": [], "tags": [], "is_head": False, "is_merge": False, "merge_parents": []})
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


