from __future__ import annotations

import time
import queue
import threading
from functools import wraps
from pathlib import Path

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

from .git_service import GitService, BranchInfo, CommitInfo, FileStatus, StashInfo

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

# Python-only version - Cython removed

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
        self._parent_app = None  # Will be set by parent
        self._files: list[FileStatus] = []  # Store files for diff access
        self._last_index = None  # Track index changes
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)
    
    def set_on_render_to_main(self, callback: callable) -> None:
        """Set callback for automatic patch updates (lazygit GetOnRenderToMain pattern)."""
        self._on_render_to_main = callback
    
    def update_files(self, files: list[FileStatus]) -> None:
        """Update the staged files list."""
        self.clear()
        
        # Filter only staged files
        staged_files = [
            f for f in files
            if f.staged and f.status in ["modified", "staged", "deleted", "renamed", "copied", "submodule"]
        ]
        
        # Store files for diff access
        self._files = staged_files
        
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
    
    def watch_index(self, index: int | None) -> None:
        """Watch for index changes and auto-update patch panel."""
        self._update_patch_for_index(index)
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) and auto-update patch panel."""
        if highlighted is not None:
            self._update_patch_for_index(highlighted)
    
    def _update_patch_for_index(self, index: int | None) -> None:
        """Update patch panel for the given index."""
        if index is not None and index != self._last_index and self._parent_app:
            self._last_index = index
            if 0 <= index < len(self._files):
                # Switch to patch view and show file diff
                self._parent_app._view_mode = "patch"
                self._parent_app.log_pane.styles.display = "none"
                self._parent_app.patch_pane.styles.display = "block"
                self._parent_app.show_file_diff(self._files[index].path, staged=True)
            # Call automatic patch update callback (lazygit pattern)
            if self._on_render_to_main:
                try:
                    self._on_render_to_main()
                except Exception:
                    pass


class ChangesPane(ListView):
    """Changes pane showing files with unstaged changes."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Changes"
        self.show_cursor = False
        self._parent_app = None  # Will be set by parent
        self._files: list[FileStatus] = []  # Store files for diff access
        self._last_index = None  # Track index changes
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)
    
    def set_on_render_to_main(self, callback: callable) -> None:
        """Set callback for automatic patch updates (lazygit GetOnRenderToMain pattern)."""
        self._on_render_to_main = callback
    
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
        
        # Store files for diff access
        self._files = unstaged_files
        
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
    
    def watch_index(self, index: int | None) -> None:
        """Watch for index changes and auto-update patch panel."""
        self._update_patch_for_index(index)
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) and auto-update patch panel."""
        if highlighted is not None:
            self._update_patch_for_index(highlighted)
    
    def _update_patch_for_index(self, index: int | None) -> None:
        """Update patch panel for the given index."""
        if index is not None and index != self._last_index and self._parent_app:
            self._last_index = index
            if 0 <= index < len(self._files):
                # Switch to patch view and show file diff
                self._parent_app._view_mode = "patch"
                self._parent_app.log_pane.styles.display = "none"
                self._parent_app.patch_pane.styles.display = "block"
                self._parent_app.show_file_diff(self._files[index].path, staged=False)
            # Call automatic patch update callback (lazygit pattern)
            if self._on_render_to_main:
                try:
                    self._on_render_to_main()
                except Exception:
                    pass


class BranchesPane(ListView):
    """Branches pane showing local branches."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Local branches"
        self._last_highlighted = None
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) to update visual highlighting."""
        if highlighted is not None:
            # Remove highlight from previous item
            if self._last_highlighted is not None and self._last_highlighted < len(self.children):
                try:
                    item = self.children[self._last_highlighted]
                    if isinstance(item, ListItem):
                        item.remove_class("highlighted-branch")
                except:
                    pass
            
            # Add highlight to current item
            if highlighted < len(self.children):
                try:
                    item = self.children[highlighted]
                    if isinstance(item, ListItem):
                        item.add_class("highlighted-branch")
                        self._last_highlighted = highlighted
                except:
                    pass
    
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
            recency = format_recency(branch.timestamp)
            if recency:
                text.append(f"{recency} ", style="dim white")
            
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
            text.append("  ", style="white")
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
        self._tags: list[BranchInfo] = []  # Store all loaded tags
        self._loaded_tags_count = 0  # How many tags we've loaded
        self._total_tags_count = 0  # Total number of tags available
        self._page_size = 200  # Load 200 tags at a time
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)
        self._last_highlighted = None
        self._rendered_count = 0  # Track how many tags are actually rendered in UI
    
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
    
    def set_tags(self, tags: list[BranchInfo], total_count: int = 0, append: bool = False) -> None:
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
            # Calculate max recency width (e.g., "1mo" = 3 chars)
            max_recency_width = 0
            for tag in tags_to_render:
                if tag.timestamp > 0:
                    recency = format_recency(tag.timestamp)
                    if recency:
                        max_recency_width = max(max_recency_width, len(recency))
            
            # Calculate max tag name width for alignment
            max_name_width = max(len(tag.name) for tag in tags_to_render) if tags_to_render else 0
            # Add some padding for better readability
            max_name_width = max(max_name_width, 15)  # Minimum width for alignment
        else:
            max_recency_width = 0
            max_name_width = 15
        
        # Only render the limited subset (not all 59k tags)
        for tag in tags_to_render:
            from rich.text import Text
            text = Text()
            
            # Add recency with fixed width (right-aligned, like Lazygit)
            if tag.timestamp > 0:
                recency = format_recency(tag.timestamp)
                if recency:
                    # Right-align recency in fixed-width column
                    text.append(f"{recency:>{max_recency_width}} ", style="dim white")
                else:
                    # Empty recency, add padding
                    text.append(" " * (max_recency_width + 1), style="dim white")
            else:
                # No timestamp, add padding
                text.append(" " * (max_recency_width + 1), style="dim white")
            
            # Add tag name with fixed width (left-aligned, like Lazygit column 1)
            text.append(f"{tag.name:<{max_name_width}} ", style="white")
            
            # Add tag message (like Lazygit column 2) - shown in yellow
            message = getattr(tag, 'message', '')
            if message:
                text.append(message, style="yellow")
            
            item = ListItem(Static(text))
            self.append(item)
        
        # Update rendered count
        self._rendered_count = len(self.children)
    
    def append_tags(self, tags: list[BranchInfo]) -> None:
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
        self._last_focused_index = None  # Remember last selected index when focus is lost
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)

    def set_branch(self, branch: str) -> None:
        """Update title to show which branch commits are displayed."""
        self.border_title = f"Commits ({branch})"
    
    def set_on_render_to_main(self, callback: callable) -> None:
        """Set callback for automatic patch updates (lazygit GetOnRenderToMain pattern)."""
        self._on_render_to_main = callback
    
    def watch_index(self, index: int | None) -> None:
        """Watch for index changes and auto-update patch panel."""
        self._update_patch_for_index(index)
        self._update_highlighting(index)
        # Update title when selection changes
        if self._parent_app:
            self._parent_app._update_commits_title()
        # Call automatic patch update callback (lazygit pattern)
        if self._on_render_to_main:
            try:
                self._on_render_to_main()
            except Exception:
                pass
    
    def watch_highlighted(self, highlighted: int | None) -> None:
        """Watch for highlighted changes (arrow keys) and auto-update patch panel."""
        # Arrow keys update highlighted, update patch
        if highlighted is not None:
            self._update_patch_for_index(highlighted)
            self._update_highlighting(highlighted)
            # Update title when selection changes
            if self._parent_app:
                self._parent_app._update_commits_title()
            # Call automatic patch update callback (lazygit pattern)
            if self._on_render_to_main:
                try:
                    self._on_render_to_main()
                except Exception:
                    pass
    
    def _update_highlighting(self, index: int | None) -> None:
        """Update visual highlighting by adding/removing classes."""
        # Only highlight if commits pane has focus
        # Exception: if we're highlighting index 0 and last_highlighted is None, allow it
        # This handles the case where on_focus sets highlighting before has_focus is fully updated
        if not self.has_focus and not (index == 0 and self._last_highlighted is None):
            # Clear all highlights if pane doesn't have focus
            if self._last_highlighted is not None and self._last_highlighted < len(self.children):
                try:
                    item = self.children[self._last_highlighted]
                    if isinstance(item, ListItem):
                        item.remove_class("highlighted-commit")
                except:
                    pass
            self._last_highlighted = None
            return
        
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
    
    def on_blur(self) -> None:
        """Handle blur event - clear highlighting and remember last selected index."""
        # Remember the current selection before clearing
        if self.index is not None:
            self._last_focused_index = self.index
        
        # Clear highlighting when pane loses focus
        if self._last_highlighted is not None and self._last_highlighted < len(self.children):
            try:
                item = self.children[self._last_highlighted]
                if isinstance(item, ListItem):
                    item.remove_class("highlighted-commit")
            except:
                pass
        self._last_highlighted = None
    
    def _update_patch_for_index(self, index: int | None) -> None:
        """Update patch panel for the given index."""
        if index is not None and self._parent_app:
            # Always update if index is valid (even if same, to ensure patch is shown on focus)
            if index != self._last_index:
                self._last_index = index
            self._parent_app.selected_commit_index = index
            self._parent_app.show_commit_diff(index)
            # Update title to reflect selected commit number
            self._parent_app._update_commits_title()
    
    def on_focus(self) -> None:
        """Handle focus event - restore previous selection or use first commit, show patch."""
        if len(self.children) > 0:
            # Restore previous selection if available, otherwise use first commit
            if self._last_focused_index is not None and 0 <= self._last_focused_index < len(self.children):
                # Restore the last selected index
                selected_idx = self._last_focused_index
            elif self._parent_app and self._parent_app.selected_commit_index >= 0:
                # Use the app's stored selected index if available
                selected_idx = self._parent_app.selected_commit_index
                # Ensure index is within bounds
                if selected_idx >= len(self.children):
                    selected_idx = 0
            else:
                # No previous selection, use first commit
                selected_idx = 0
            
            # Set index and highlighted to restore selection
            self.index = selected_idx
            self.highlighted = selected_idx
            
            # Force update patch panel by calling _update_patch_for_index
            # This ensures patch is shown even if index hasn't changed
            # Reset _last_index temporarily to force update
            self._last_index = None
            self._update_patch_for_index(selected_idx)
            
            # Update highlighting - call after a brief delay to ensure focus is fully established
            # Use call_later to ensure highlighting happens after focus is set
            self.call_later(self._update_highlighting, selected_idx)
            
            # Also call immediately in case call_later isn't needed
            self._update_highlighting(selected_idx)
            
            # Ensure patch pane is visible (in case view mode was changed)
            if self._parent_app:
                # Switch to patch view if not already
                if self._parent_app._view_mode != "patch":
                    self._parent_app._view_mode = "patch"
                    self._parent_app.log_pane.styles.display = "none"
                    self._parent_app.patch_pane.styles.display = "block"
    
    def set_commits(self, commits: list[CommitInfo]) -> None:
        self.clear()
        self._last_highlighted = None  # Reset highlighting tracker
        
        # Store commit SHAs and commit info for in-place updates
        self._commit_shas = []
        self._commit_info_map = {}  # SHA -> CommitInfo for quick lookup
        
        # CRITICAL: Limit initial commits to prevent UI blocking on large repos
        # Show only first 50 commits initially, rest will be loaded on scroll (virtual scrolling)
        # This matches Lazygit's behavior - fast initial render, load more on demand
        initial_limit = 50  # Reduced from 200 to prevent blocking
        commits_to_render = commits[:initial_limit] if len(commits) > initial_limit else commits
        
        # Store all commits for later (virtual scrolling will load more)
        self._all_commits = commits
        
        for commit in commits_to_render:
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
            # 1. Merged (green ✓): Commit exists on main/master
            # 2. Pushed (yellow ↑): Commit is pushed but NOT merged
            # 3. Unpushed (red -): Commit is not pushed
            if commit.merged:
                text.append("✓ ", style="green")  # StatusMerged
            elif hasattr(commit, 'pushed'):
                if commit.pushed:
                    text.append("↑ ", style="yellow")  # StatusPushed
                else:
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
            
            self.append(ListItem(Static(text)))

    def append_commits(self, commits: list[CommitInfo]) -> None:
        # Initialize _commit_shas and _commit_info_map if not exists
        if not hasattr(self, '_commit_shas'):
            self._commit_shas = []
        if not hasattr(self, '_commit_info_map'):
            self._commit_info_map = {}
        
        for commit in commits:
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
            
            # Don't show push status initially to avoid flicker
            # The background thread will update it after checking remote
            
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
    
    def update_push_status_in_place(self, commits: list[CommitInfo]) -> None:
        """Update push status for existing commits without clearing the list."""
        if not commits or len(commits) == 0:
            return
        
        # Create a map of normalized SHA to commit info (including both pushed and merged status)
        commit_status_map = {}
        for commit in commits:
            commit_sha = _normalize_commit_sha(commit.sha)
            commit_status_map[commit_sha] = {
                'pushed': commit.pushed,
                'merged': commit.merged
            }
            # Also update the stored commit info map so future lookups have correct status
            if hasattr(self, '_commit_info_map'):
                self._commit_info_map[commit_sha] = commit
        
        # Check if we have stored commit SHAs
        if not hasattr(self, '_commit_shas') or len(self._commit_shas) == 0:
            return
        
        # Check if we have stored commit info map
        if not hasattr(self, '_commit_info_map'):
            self._commit_info_map = {}
        
        # Update items in place using stored SHAs
        from rich.text import Text
        
        updated_ui_count = 0
        for i, item in enumerate(self.children):
            try:
                # Check if we have a stored SHA for this index
                if i >= len(self._commit_shas):
                    continue
                
                stored_sha = self._commit_shas[i]
                normalized_stored_sha = _normalize_commit_sha(stored_sha)
                
                # Get status from map (both pushed and merged)
                if normalized_stored_sha not in commit_status_map:
                    continue
                
                status = commit_status_map[normalized_stored_sha]
                pushed_status = status['pushed']
                merged_status = status['merged']
                
                # Get commit info from stored map (we have the commit message here)
                commit_info = self._commit_info_map.get(stored_sha)
                if not commit_info:
                    continue
                
                # Update commit_info with latest status (so it's correct for display)
                commit_info.pushed = pushed_status
                commit_info.merged = merged_status
                
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
                    if merged_status:
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
            except Exception:
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
        self._on_render_to_main: callable | None = None  # Callback for automatic patch updates (lazygit pattern)
    
    def set_on_render_to_main(self, callback: callable) -> None:
        """Set callback for automatic patch updates (lazygit GetOnRenderToMain pattern)."""
        self._on_render_to_main = callback
    
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
                # Add continuation lines with proper indentation
                # Calculate indent: recency (if any) + "stash@{N}: " + branch + ": "
                indent_prefix = ""
                if recency:
                    indent_prefix = " " * (len(recency) + 1)  # recency + space
                indent_prefix += "     "  # Additional indent for continuation
                for i, line in enumerate(lines[1:], 1):
                    text.append(f"\n{indent_prefix}", style="white")  # Properly indented continuation lines
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
            # Call automatic patch update callback (lazygit pattern)
            if self._on_render_to_main:
                try:
                    self._on_render_to_main()
                except Exception:
                    pass


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
        self._native_git_log_lines: list = []  # Cached lines from git log (all parsed lines)
        self._native_git_log_count = 50  # Current limit for git log (how many commits to fetch)
        self._native_git_log_loading = False  # Prevent concurrent loads
        self._native_git_log_rendered_count = 0  # How many lines we've rendered so far
        # PTY streaming mode - enabled by default for better performance
        # Can be disabled via environment variable: PYGITZEN_USE_PTY=0
        import os
        self._use_pty_streaming = os.getenv('PYGITZEN_USE_PTY', '1') != '0'  # Enabled by default
        self._pty_streaming_active = False  # Track if PTY streaming is currently active
        if not self._use_pty_streaming:
            _log_timing_message("[INFO] PTY streaming mode DISABLED (set PYGITZEN_USE_PTY=0)")
        # Start with blank log - don't update here, let it be empty initially
    
    def show_branch_log(self, branch: str, commits: list[CommitInfo], branch_info: dict, git_service, append: bool = False, total_commits_count_override: int = None) -> None:
        """
        Display native git log --graph --color=always output for a branch.
        Only loads when user clicks on a branch.
        """
        from rich.text import Text
        from pathlib import Path
        
        # Only show native git log if we have git_service with repo_path
        if git_service is not None:
            # Check if git_service has repo_path attribute
            repo_path = None
            try:
                # Debug: log what we're receiving
                try:
                    with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                        f.write(f"DEBUG show_branch_log: Received git_service type={type(git_service)} for branch={branch}\n")
                        f.write(f"git_service type name: {type(git_service).__name__}\n")
                        f.write(f"hasattr repo_path: {hasattr(git_service, 'repo_path')}\n")
                        # Try to get repo_path to see if it exists
                        try:
                            test_repo_path = getattr(git_service, 'repo_path', 'NOT_FOUND')
                            f.write(f"getattr repo_path: {test_repo_path}\n")
                        except Exception as e:
                            f.write(f"getattr repo_path failed: {e}\n")
                except:
                    pass
                
                # Try multiple ways to get repo_path (works for both cython and non-cython)
                # Method 1: Direct attribute access (works for both, including cython cdef attributes and wrappers)
                try:
                    repo_path = git_service.repo_path
                    # Verify it's not None or empty
                    if not repo_path or (isinstance(repo_path, str) and not repo_path.strip()):
                        repo_path = None
                    else:
                        # Debug: log successful access
                        try:
                            with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                                f.write(f"SUCCESS show_branch_log: Found repo_path={repo_path} via direct access for branch={branch}\n\n")
                        except:
                            pass
                except (AttributeError, TypeError) as e:
                    repo_path = None
                    # Debug: log failure
                    try:
                        with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                            f.write(f"FAILED show_branch_log: Direct access failed: {e} for branch={branch}\n")
                    except:
                        pass
                
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
                    
                    # Pass git_service directly to _show_native_git_log (it should already have repo_path)
                    # Don't validate path existence here - let git command handle it (it will fail gracefully)
                    # Use PTY streaming (default) or fallback to subprocess if disabled
                    if self._use_pty_streaming:
                        self._show_native_git_log(branch, branch_info, git_service, append=append)
                    else:
                        self._show_native_git_log_subprocess(branch, branch_info, git_service, append=append)
                else:
                    # No repo_path found or invalid - log for debugging
                    try:
                        with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                            f.write(f"DEBUG: No valid repo_path found for branch={branch}\n")
                            f.write(f"repo_path value: {repo_path}\n")
                            f.write(f"git_service type: {type(git_service)}\n")
                            f.write(f"hasattr repo_path: {hasattr(git_service, 'repo_path')}\n")
                            # Try to get repo_path directly
                            try:
                                repo_path_attr = getattr(git_service, 'repo_path', 'NOT_FOUND')
                                f.write(f"Direct access repo_path: {repo_path_attr}\n")
                                f.write(f"repo_path type: {type(repo_path_attr)}\n")
                            except Exception as e:
                                f.write(f"Direct access failed: {e}\n")
                            f.write(f"git_service dir (repo-related): {[x for x in dir(git_service) if 'repo' in x.lower()]}\n\n")
                    except:
                        pass
                    self.update(Text())
            except Exception as e:
                # On any error, show empty and log the error for debugging
                import traceback
                try:
                    with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                        f.write(f"Error in show_branch_log (branch={branch}): {e}\n")
                        f.write(f"git_service type: {type(git_service)}\n")
                        f.write(f"git_service attrs: {dir(git_service)}\n")
                        f.write(f"Traceback:\n{traceback.format_exc()}\n\n")
                except:
                    pass
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
    
    def _show_native_git_log_subprocess(self, branch: str, branch_info: dict, git_service, append: bool = False) -> None:
        """
        Display native git log using subprocess (fallback when PTY is disabled).
        This shows exactly what git outputs, preserving all colors and formatting.
        Supports virtual scrolling - loads more commits as user scrolls.
        """
        from rich.text import Text
        from rich.console import Group
        from pathlib import Path
        import subprocess
        
        # Prevent concurrent loads
        if self._native_git_log_loading:
            return
        self._native_git_log_loading = True
        
        try:
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
            
            # Run git command with error handling for encoding issues
            # Use shorter timeout for faster failure
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=False,  # Get bytes first
                cwd=str(repo_path),
                timeout=20  # Longer timeout for large repos (haiku has 69k+ commits)
            )
            
            # Decode with error handling for non-UTF-8 characters
            # Use errors='replace' to handle any invalid UTF-8 bytes
            output_text = result.stdout.decode('utf-8', errors='replace')
            error_text = result.stderr.decode('utf-8', errors='replace')
            
            # Create a simple result-like object with decoded text
            class DecodedResult:
                def __init__(self, returncode, stdout, stderr):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr
            
            result = DecodedResult(result.returncode, output_text, error_text)
            
            if result.returncode != 0:
                # Show error message
                error_text = Text()
                error_text.append(f"Error running git log: {result.stderr}\n", style="red")
                self.update(error_text)
                self._native_git_log_loading = False
                return
        
            # Parse ANSI-colored output and convert to Rich Text
            # Process the entire output at once for better performance
            if not output_text.strip():
                # No output, show empty
                self.update(Text())
                self._native_git_log_loading = False
                return
            
            # Split into lines and process
            output_lines = output_text.split('\n')
            new_log_lines = []
            
            # Limit initial processing to prevent blocking on large repos
            # Process first 100 lines immediately, rest in background
            max_initial_lines = 100
            lines_to_process = output_lines[:max_initial_lines] if len(output_lines) > max_initial_lines else output_lines
            remaining_lines = output_lines[max_initial_lines:] if len(output_lines) > max_initial_lines else []
            
            # Convert each line from ANSI to Rich Text using Text.from_ansi() (faster than manual parsing)
            # Process initial batch for immediate display
            for line in lines_to_process:
                if line:  # Only process non-empty lines
                    try:
                        # Use Rich's built-in ANSI parser (much faster than manual parsing)
                        rich_line = Text.from_ansi(line)
                        new_log_lines.append(rich_line)
                    except Exception:
                        # If parsing fails, strip ANSI and add as plain text
                        from pygitzen.git_graph import strip_ansi_codes
                        plain_line = strip_ansi_codes(line)
                        new_log_lines.append(Text(plain_line, style="white"))
            
            # Store remaining lines for background processing
            if remaining_lines:
                self._pending_log_lines = remaining_lines
            
            # If appending, only add new lines (skip already loaded ones)
            if append and self._native_git_log_lines:
                # Count existing content lines (excluding header and empty line)
                existing_content_lines = len(self._native_git_log_lines) - 2  # Subtract header and empty line
                
                # Only add lines that weren't in the previous load
                if existing_content_lines < len(new_log_lines):
                    # Add only the new lines (skip the ones we already have)
                    new_lines_to_add = new_log_lines[existing_content_lines:]
                    self._native_git_log_lines.extend(new_lines_to_add)
            else:
                # First load - build full content with header
                log_lines = []
                # Add header
                header = self._build_header(branch, branch_info)
                log_lines.append(header)
                log_lines.append(Text())  # Empty line
                log_lines.extend(new_log_lines)
                self._native_git_log_lines = log_lines
            
            # Update the pane - queue the update instead of calling directly
            # This ensures it runs on the main thread and doesn't block
            if hasattr(self, 'app') and self.app is not None:
                # Queue the UI update to run on main thread
                def update_ui():
                    try:
                        if self._native_git_log_lines:
                            # Virtual scrolling: render all available lines (they're already limited to 100 initially)
                            # The virtual scroll mechanism will load more when user scrolls
                            full_content = Group(*self._native_git_log_lines)
                            self.update(full_content)
                            # Track how many lines we've rendered
                            self._native_git_log_rendered_count = len(self._native_git_log_lines)
                        else:
                            self.update(Text())
                            self._native_git_log_rendered_count = 0
                        
                        # Update cache
                        self._cached_branch = branch
                        self._cached_branch_info = branch_info.copy()
                    except Exception as e:
                        _log_timing_message(f"[WARNING] Error in queued UI update: {type(e).__name__}: {e}")
                
                # Queue the update if app has the queue
                try:
                    if hasattr(self.app, '_ui_update_queue'):
                        self.app._ui_update_queue.put(update_ui)
                    else:
                        # Fallback: try direct update
                        update_ui()
                except RuntimeError as e:
                    # RuntimeError often indicates event loop issues
                    error_msg = str(e).lower()
                    if "no running event loop" in error_msg or "event loop" in error_msg:
                        _log_timing_message(f"[WARNING] Event loop not available for log pane update: {e}")
                        # Don't show error to user - this is a timing issue that will resolve
                    else:
                        # Re-raise if it's a different RuntimeError
                        raise
                except Exception as queue_error:
                    # Other queue errors - log but don't crash
                    _log_timing_message(f"[WARNING] Error queueing log pane update: {type(queue_error).__name__}: {queue_error}")
            else:
                # Widget not mounted - skip update
                _log_timing_message(f"[WARNING] Skipping log pane update - widget not mounted")
            
        except Exception as e:
            # On error, show error message - but only if we can safely update
            try:
                # Check if widget is mounted before trying to update
                if not hasattr(self, 'app') or self.app is None:
                    _log_timing_message(f"[ERROR] Cannot show error message - widget not mounted: {e}")
                    return
                
                error_text = Text()
                error_text.append(f"Error showing native git log: {e}\n", style="red")
                self.update(error_text)
            except RuntimeError as update_error:
                # If update fails due to event loop, just log it
                error_msg = str(update_error).lower()
                if "no running event loop" in error_msg or "event loop" in error_msg:
                    _log_timing_message(f"[ERROR] Event loop not available when showing error: {e} (update_error: {update_error})")
                else:
                    _log_timing_message(f"[ERROR] Error showing native git log: {e} (update_error: {update_error})")
            except Exception as update_error:
                _log_timing_message(f"[ERROR] Error showing native git log: {e} (update_error: {update_error})")
        finally:
            self._native_git_log_loading = False
    
    def _show_native_git_log(self, branch: str, branch_info: dict, git_service, append: bool = False) -> None:
        """
        Display native git log using PTY streaming (like Lazygit).
        Streams output as it's generated and updates UI incrementally.
        This is the default implementation for better performance.
        """
        import pty
        import os
        import select
        import subprocess
        import threading
        import fcntl
        from rich.text import Text
        from rich.console import Group
        from pathlib import Path
        
        # Prevent concurrent loads
        if self._native_git_log_loading or self._pty_streaming_active:
            return
        
        self._native_git_log_loading = True
        self._pty_streaming_active = True
        
        try:
            # Get repo path (same logic as _show_native_git_log)
            repo_path = None
            
            try:
                if hasattr(git_service, 'repo_path'):
                    repo_path = git_service.repo_path
            except (AttributeError, TypeError):
                pass
            
            if repo_path is None:
                try:
                    repo_path = getattr(git_service, 'repo_path', None)
                except (AttributeError, TypeError):
                    pass
            
            if repo_path is None:
                try:
                    if hasattr(git_service, 'repo'):
                        repo = getattr(git_service, 'repo', None)
                        if repo and hasattr(repo, 'path'):
                            repo_path = getattr(repo, 'path', None)
                except (AttributeError, TypeError):
                    pass
            
            if repo_path:
                if isinstance(repo_path, str):
                    repo_path = Path(repo_path)
                elif not isinstance(repo_path, Path):
                    repo_path = Path(str(repo_path))
            else:
                repo_path = Path(".")
            
            # Resolve "." to absolute path
            if str(repo_path) == ".":
                repo_path = Path(".").resolve()
            
            # If appending, increase the limit; otherwise reset
            if not append:
                self._native_git_log_count = 50
                self._native_git_log_lines = []
            else:
                self._native_git_log_count += 50
            
            # Build git command (using Lazygit's default format)
            # Limit to reasonable number of commits to prevent long execution times
            max_commits = min(self._native_git_log_count, 100)  # Cap at 100 commits max
            cmd = [
                'git', 'log',
                '--graph',
                '--color=always',
                '--abbrev-commit',
                '--decorate',
                '--date=relative',
                '--pretty=medium',
                f'-{max_commits}'
            ]
            
            # Add branch if specified
            if branch and branch.strip():
                if branch.startswith('refs/'):
                    cmd.append(branch)
                elif '/' in branch:
                    cmd.append(f'refs/heads/{branch}')
                else:
                    cmd.append(branch)
            
            cmd.append('--')
            
            # Set up environment for PTY
            env = os.environ.copy()
            env['TERM'] = 'dumb'  # Tell git we're a simple terminal
            env['GIT_PAGER'] = 'cat'  # Disable pager, output directly
            
            # Build header
            header = self._build_header(branch, branch_info)
            
            # Initialize lines list
            if not append:
                self._native_git_log_lines = [header, Text()]  # Header + empty line
            
            def stream_in_background():
                """Background thread that streams output from PTY."""
                master_fd = None
                process = None
                
                try:
                    # Create master/slave PTY pair
                    master_fd, slave_fd = pty.openpty()
                    
                    # Start the process with PTY
                    # Note: We can't use timeout parameter with Popen, so we'll handle timeout in wait()
                    process = subprocess.Popen(
                        cmd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        cwd=str(repo_path),
                        env=env,
                        start_new_session=True
                    )
                    
                    # Close slave_fd in parent (we use master_fd)
                    os.close(slave_fd)
                    
                    # Set non-blocking mode for streaming
                    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                    
                    buffer = b''
                    lines_collected = []
                    update_counter = 0
                    
                    # Stream output line by line
                    while process.poll() is None or buffer:
                        # Check if process is done and no more data
                        if process.poll() is not None and not buffer:
                            # Try one more read
                            try:
                                data = os.read(master_fd, 4096)
                                if not data:
                                    break
                                buffer += data
                            except OSError:
                                break
                        
                        # Read available data
                        try:
                            ready, _, _ = select.select([master_fd], [], [], 0.1)
                            if ready:
                                data = os.read(master_fd, 4096)
                                if not data:
                                    if process.poll() is not None:
                                        break
                                    continue
                                buffer += data
                        except (OSError, ValueError):
                            # No data available or error
                            if process.poll() is not None:
                                break
                            continue
                        
                        # Process complete lines
                        while b'\n' in buffer:
                            line_bytes, buffer = buffer.split(b'\n', 1)
                            line = line_bytes.decode('utf-8', errors='replace')
                            
                            if line.strip():  # Skip empty lines
                                # Parse ANSI to Rich Text (still needed for Textual)
                                try:
                                    rich_line = Text.from_ansi(line)
                                    lines_collected.append(rich_line)
                                    update_counter += 1
                                    
                                    # Update UI every 10 lines (balance between responsiveness and performance)
                                    if update_counter % 10 == 0:
                                        if hasattr(self, 'app') and self.app is not None:
                                            def update_ui():
                                                try:
                                                    # Append new lines
                                                    self._native_git_log_lines.extend(lines_collected)
                                                    lines_collected.clear()
                                                    
                                                    # Update UI
                                                    full_content = Group(*self._native_git_log_lines)
                                                    self.update(full_content)
                                                except Exception as e:
                                                    _log_timing_message(f"[WARNING] Error in PTY UI update: {type(e).__name__}: {e}")
                                            
                                            # Queue the update
                                            try:
                                                if hasattr(self.app, '_ui_update_queue'):
                                                    self.app._ui_update_queue.put(update_ui)
                                                else:
                                                    # Fallback: try direct update
                                                    if hasattr(self.app, 'call_from_thread'):
                                                        self.app.call_from_thread(update_ui)
                                                    else:
                                                        update_ui()
                                            except Exception as queue_error:
                                                _log_timing_message(f"[WARNING] Error queueing PTY UI update: {type(queue_error).__name__}: {queue_error}")
                                
                                except Exception as e:
                                    # Fallback: strip ANSI and use plain text
                                    try:
                                        from pygitzen.git_graph import strip_ansi_codes
                                        plain = strip_ansi_codes(line)
                                        rich_line = Text(plain, style="white")
                                        lines_collected.append(rich_line)
                                        update_counter += 1
                                    except Exception:
                                        pass  # Skip this line if parsing fails
                    
                    # Wait for process to finish (with timeout)
                    if process.poll() is None:
                        try:
                            # Wait with timeout (30 seconds max)
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            # Kill the process if it times out
                            process.kill()
                            _log_timing_message("[WARNING] git log --graph process timed out, killed")
                        finally:
                            # Cancel alarm if it was set
                            try:
                                signal.alarm(0)
                            except (AttributeError, ValueError):
                                pass
                    
                    # Final update with any remaining lines
                    if lines_collected:
                        if hasattr(self, 'app') and self.app is not None:
                            def final_update():
                                try:
                                    # Append remaining lines
                                    self._native_git_log_lines.extend(lines_collected)
                                    
                                    # Update UI
                                    full_content = Group(*self._native_git_log_lines)
                                    self.update(full_content)
                                    self._native_git_log_rendered_count = len(self._native_git_log_lines)
                                    
                                    # Update cache
                                    self._cached_branch = branch
                                    self._cached_branch_info = branch_info.copy()
                                except Exception as e:
                                    _log_timing_message(f"[WARNING] Error in PTY final UI update: {type(e).__name__}: {e}")
                            
                            try:
                                if hasattr(self.app, '_ui_update_queue'):
                                    self.app._ui_update_queue.put(final_update)
                                else:
                                    if hasattr(self.app, 'call_from_thread'):
                                        self.app.call_from_thread(final_update)
                                    else:
                                        final_update()
                            except Exception as queue_error:
                                _log_timing_message(f"[WARNING] Error queueing PTY final UI update: {type(queue_error).__name__}: {queue_error}")
                
                except Exception as e:
                    # Show error message
                    if hasattr(self, 'app') and self.app is not None:
                        def show_error():
                            try:
                                error_text = Text()
                                error_text.append(f"Error streaming git log: {e}\n", style="red")
                                self.update(error_text)
                            except Exception:
                                pass
                        
                        try:
                            if hasattr(self.app, '_ui_update_queue'):
                                self.app._ui_update_queue.put(show_error)
                            else:
                                if hasattr(self.app, 'call_from_thread'):
                                    self.app.call_from_thread(show_error)
                                else:
                                    show_error()
                        except Exception:
                            pass
                    
                    _log_timing_message(f"[ERROR] Error in PTY streaming: {type(e).__name__}: {e}")
                
                finally:
                    # Cleanup
                    if master_fd is not None:
                        try:
                            os.close(master_fd)
                        except Exception:
                            pass
                    
                    if process is not None and process.poll() is None:
                        try:
                            process.terminate()
                            process.wait()
                        except Exception:
                            pass
                    
                    self._native_git_log_loading = False
                    self._pty_streaming_active = False
            
            # Start streaming in background thread
            thread = threading.Thread(target=stream_in_background, daemon=True)
            thread.start()
            
        except Exception as e:
            # On error, show error message
            try:
                if hasattr(self, 'app') and self.app is not None:
                    error_text = Text()
                    error_text.append(f"Error starting PTY streaming: {e}\n", style="red")
                    self.update(error_text)
            except Exception:
                pass
            
            _log_timing_message(f"[ERROR] Error starting PTY streaming: {type(e).__name__}: {e}")
            self._native_git_log_loading = False
            self._pty_streaming_active = False
    
    def _stream_git_command_pty(self, cmd: list, repo_path: Path, target_widget, prefix: str = "", update_interval: int = 10) -> None:
        """
        Generic PTY streaming helper for git commands.
        Streams output from a git command to a target widget incrementally.
        
        Args:
            cmd: Git command as list (e.g., ['git', 'diff', '--color=always', 'file.txt'])
            repo_path: Repository path
            target_widget: Widget to update (e.g., self.patch_pane)
            prefix: Optional prefix text to add before output
            update_interval: Update UI every N lines (default: 10)
        """
        import pty
        import os
        import select
        import subprocess
        import threading
        import fcntl
        from rich.text import Text
        from rich.console import Group
        
        def stream_in_background():
            """Background thread that streams output from PTY."""
            master_fd = None
            process = None
            
            try:
                # Set up environment for PTY
                env = os.environ.copy()
                env['TERM'] = 'dumb'
                env['GIT_PAGER'] = 'cat'
                
                # Create master/slave PTY pair
                master_fd, slave_fd = pty.openpty()
                
                # Start the process with PTY
                process = subprocess.Popen(
                    cmd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(repo_path),
                    env=env,
                    start_new_session=True
                )
                
                os.close(slave_fd)
                
                # Set non-blocking mode for streaming
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                
                buffer = b''
                lines_collected = []
                update_counter = 0
                
                # Initialize content with prefix if provided
                if prefix:
                    lines_collected.append(Text(prefix))
                    lines_collected.append(Text())  # Empty line
                
                # Stream output line by line
                while process.poll() is None or buffer:
                    if process.poll() is not None and not buffer:
                        try:
                            data = os.read(master_fd, 4096)
                            if not data:
                                break
                            buffer += data
                        except OSError:
                            break
                    
                    try:
                        ready, _, _ = select.select([master_fd], [], [], 0.1)
                        if ready:
                            data = os.read(master_fd, 4096)
                            if not data:
                                if process.poll() is not None:
                                    break
                                continue
                            buffer += data
                    except (OSError, ValueError):
                        if process.poll() is not None:
                            break
                        continue
                    
                    # Process complete lines
                    while b'\n' in buffer:
                        line_bytes, buffer = buffer.split(b'\n', 1)
                        line = line_bytes.decode('utf-8', errors='replace')
                        
                        if line.strip():
                            try:
                                rich_line = Text.from_ansi(line)
                                lines_collected.append(rich_line)
                                update_counter += 1
                                
                                # Update UI periodically
                                if update_counter % update_interval == 0:
                                    if hasattr(target_widget, 'app') and target_widget.app is not None:
                                        def update_ui():
                                            try:
                                                full_content = Group(*lines_collected)
                                                target_widget.update(full_content)
                                            except Exception as e:
                                                _log_timing_message(f"[WARNING] Error in PTY UI update: {type(e).__name__}: {e}")
                                        
                                        try:
                                            if hasattr(target_widget.app, '_ui_update_queue'):
                                                target_widget.app._ui_update_queue.put(update_ui)
                                            else:
                                                if hasattr(target_widget.app, 'call_from_thread'):
                                                    target_widget.app.call_from_thread(update_ui)
                                                else:
                                                    update_ui()
                                        except Exception as queue_error:
                                            _log_timing_message(f"[WARNING] Error queueing PTY UI update: {type(queue_error).__name__}: {queue_error}")
                            except Exception as e:
                                try:
                                    from pygitzen.git_graph import strip_ansi_codes
                                    plain = strip_ansi_codes(line)
                                    rich_line = Text(plain, style="white")
                                    lines_collected.append(rich_line)
                                    update_counter += 1
                                except Exception:
                                    pass
                
                if process.poll() is None:
                    process.wait()
                
                # Final update with remaining lines
                if lines_collected:
                    if hasattr(target_widget, 'app') and target_widget.app is not None:
                        def final_update():
                            try:
                                full_content = Group(*lines_collected)
                                target_widget.update(full_content)
                            except Exception as e:
                                _log_timing_message(f"[WARNING] Error in PTY final UI update: {type(e).__name__}: {e}")
                        
                        try:
                            if hasattr(target_widget.app, '_ui_update_queue'):
                                target_widget.app._ui_update_queue.put(final_update)
                            else:
                                if hasattr(target_widget.app, 'call_from_thread'):
                                    target_widget.app.call_from_thread(final_update)
                                else:
                                    final_update()
                        except Exception as queue_error:
                            _log_timing_message(f"[WARNING] Error queueing PTY final UI update: {type(queue_error).__name__}: {queue_error}")
            
            except Exception as e:
                if hasattr(target_widget, 'app') and target_widget.app is not None:
                    def show_error():
                        try:
                            error_text = Text()
                            error_text.append(f"Error streaming git command: {e}\n", style="red")
                            target_widget.update(error_text)
                        except Exception:
                            pass
                    
                    try:
                        if hasattr(target_widget.app, '_ui_update_queue'):
                            target_widget.app._ui_update_queue.put(show_error)
                        else:
                            if hasattr(target_widget.app, 'call_from_thread'):
                                target_widget.app.call_from_thread(show_error)
                            else:
                                show_error()
                    except Exception:
                        pass
                
                _log_timing_message(f"[ERROR] Error in PTY streaming: {type(e).__name__}: {e}")
            
            finally:
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except Exception:
                        pass
                
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                        process.wait()
                    except Exception:
                        pass
        
        # Start streaming in background thread
        thread = threading.Thread(target=stream_in_background, daemon=True)
        thread.start()
    
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
        
        # Normalize SHA format (fix for Cython version hex-encoded ASCII issue)
        commit_sha = _normalize_commit_sha(commit.sha)
        
        # Debug: Log if SHA was fixed
        if commit.sha != commit_sha:
            try:
                with open("debug_sha_format.log", "a", encoding="utf-8") as f:
                    f.write(f"FIXED SHA: original={repr(commit.sha)}, normalized={commit_sha}\n")
            except:
                pass
        
        # Create commit header
        header_text = f"""commit {commit_sha}
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
    
    def show_file_info(self, file_path: str, diff_text: str, staged: bool = False) -> None:
        """Show file diff in patch pane."""
        from rich.text import Text
        from rich.syntax import Syntax
        from rich.console import Group
        
        # Create file header
        status_label = "Staged" if staged else "Unstaged"
        header_text = f"{status_label} changes: {file_path}\n\n"
        
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
    
    /* General highlight rule - but specific panes override this */
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
    
    #commits-pane:focus ListItem.highlighted-commit {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }
    
    #commits-pane ListItem.highlighted-commit:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
    }
    
    /* Note: Highlighting when not focused is handled in code via on_blur() */

    /* Hover and highlight styling for branches, remotes, and tags panes */
    #branches-pane ListItem:hover,
    #remotes-pane ListItem:hover,
    #tags-pane ListItem:hover {
        background: #357ABD;
        color: #ffffff;
    }
    
    /* Blue highlighting for branches, remotes, and tags panes - using custom classes like commits pane */
    #branches-pane ListItem.highlighted-branch,
    #remotes-pane ListItem.highlighted-remote,
    #tags-pane ListItem.highlighted-tag {
        background: #357ABD; /* blue for strong contrast */
        color: #ffffff;
        text-style: bold;
    }
    
    #branches-pane ListItem.highlighted-branch:focus,
    #remotes-pane ListItem.highlighted-remote:focus,
    #tags-pane ListItem.highlighted-tag:focus {
        background: #2f6aa3; /* slightly darker when focused */
        color: #ffffff;
        text-style: bold;
    }
    
    #branches-pane:focus ListItem.highlighted-branch,
    #remotes-pane:focus ListItem.highlighted-remote,
    #tags-pane:focus ListItem.highlighted-tag {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }
    
    #branches-pane ListItem.highlighted-branch > Static,
    #remotes-pane ListItem.highlighted-remote > Static,
    #tags-pane ListItem.highlighted-tag > Static {
        background: transparent;
        color: #ffffff;
    }
    
    #branches-pane ListItem:hover > Static,
    #remotes-pane ListItem:hover > Static,
    #tags-pane ListItem:hover > Static {
        background: transparent;
        color: #ffffff;
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
        Binding("[", "prev_tab", "Prev Tab"),
        Binding("]", "next_tab", "Next Tab"),
    ]

    active_branch: reactive[str | None] = reactive(None)
    selected_commit_index: reactive[int] = reactive(0)

    def __init__(self, repo_dir: str = ".") -> None:
        import sys
        init_start = time.perf_counter()
        _log_timing_message(f"[TIMING] ===== PygitzenApp.__init__ START =====")
        
        super().__init__()
        from dulwich.errors import NotGitRepository
        try:
            # Use Python GitService (Cython removed)
            python_init_start = time.perf_counter()
            self.git = GitService(repo_dir)
            python_init_elapsed = time.perf_counter() - python_init_start
            _log_timing_message(f"[TIMING] GitService.__init__: {python_init_elapsed:.4f}s")
            self.git_python = self.git  # Same instance
            self.branches: list[BranchInfo] = []
            self.commits: list[CommitInfo] = []  # Commits for commits pane (left side)
            self.stashes: list[StashInfo] = []  # Stashes for stash pane
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
            self._loading_stashes = False
            self._search_query: str = ""
            self._view_mode: str = "patch"  # "patch" or "log"
            
            # Thread-safe queue for UI updates from background threads
            self._ui_update_queue = queue.Queue()
            
            # WaitGroup pattern (similar to lazygit's waitForIntro)
            # Blocks background operations until UI is fully initialized
            self._ui_ready = threading.Event()
            self._ui_ready.clear()  # Start as not ready
            
            # Synchronization barrier for data loading (similar to lazygit's WaitGroup)
            # Tracks: commits, stashes, files (3 operations)
            self._data_loading_barrier = threading.Barrier(3, action=self._on_all_data_loaded)
            self._data_loading_complete = threading.Event()
            self._data_loading_complete.clear()
            
            # PHASE 2: Cache with proper invalidation
            # Cache commit counts per branch
            self._commit_count_cache: dict[str, int] = {}
            # Cache remote branch existence per branch
            self._remote_branch_cache: dict[str, bool] = {}
            # Cache remote commits per branch (set of commit SHAs)
            self._remote_commits_cache: dict[str, set[str]] = {}
            # Cache merged commits (shared across all branches - commits on main/master)
            self._merged_commits_cache: set[str] = set()
            
            # Cache for branch sync status (branch name -> sync status dict)
            self._branch_sync_status_cache: dict[str, dict] = {}
            
            # Track HEAD SHA for invalidation detection
            # Maps branch -> HEAD SHA (for local branches)
            self._last_head_sha: dict[str, str] = {}
            # Maps branch -> remote HEAD SHA (for remote branches)
            self._last_remote_head_sha: dict[str, str] = {}
            
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
                # Set parent app reference for file panes
                self.staged_pane._parent_app = self
                self.changes_pane._parent_app = self
                
                # Create branches/remotes/tags panes
                self.branches_pane = BranchesPane(id="branches-pane")
                self.remotes_pane = RemotesPane(id="remotes-pane")
                self.tags_pane = TagsPane(id="tags-pane")
                
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
        version_info = " (Python)"
        self.command_log_pane.update_log(f"pygitzen started{version_info}")
        # self.refresh_data()
        self.refresh_data_fast()
        
        # Set up periodic check for virtual scrolling expansion (fallback if scroll events don't fire)
        # This ensures virtual scrolling works even if scroll events aren't being captured
        # Check more frequently (0.2s) for more responsive virtual scrolling
        self.set_interval(0.2, self._check_virtual_scroll_expansion)
        self.set_interval(0.2, self._check_commits_pane_scroll)  # Check commits pane scrolling
        self.set_interval(0.2, self._check_tags_pane_scroll)  # Check tags pane scrolling
        
        # Set up periodic processing of UI update queue from background threads
        self.set_interval(0.05, self._process_ui_update_queue)  # Check every 50ms
        
        # Set up periodic footer update to reflect current focus
        self.set_interval(0.1, self._update_footer)
        
        # Watch for tab changes to load tags lazily (only when tags tab is selected)
        if hasattr(self, 'branches_tabbed'):
            self.watch(self.branches_tabbed, "active", self._on_tab_changed)
        
        mount_elapsed = time.perf_counter() - mount_start
        _log_timing_message(f"[TIMING] ===== on_mount TOTAL: {mount_elapsed:.4f}s =====")
        
        # Signal that UI is ready (similar to lazygit's waitForIntro.Done())
        # This allows background operations to proceed
        self._ui_ready.set()
        _log_timing_message("[TIMING] UI ready - background operations can now proceed")
        
        # Set automatic patch update callbacks (lazygit GetOnRenderToMain pattern)
        # These are set after UI is ready to ensure all panes are initialized
        self.commits_pane.set_on_render_to_main(self._get_commits_render_to_main())
        self.stash_pane.set_on_render_to_main(self._get_stash_render_to_main())
        self.staged_pane.set_on_render_to_main(self._get_staged_render_to_main())
        self.changes_pane.set_on_render_to_main(self._get_changes_render_to_main())
        self.tags_pane.set_on_render_to_main(self._get_tags_render_to_main())
        self.remotes_pane.set_on_render_to_main(self._get_remotes_render_to_main())
    
    def _process_ui_update_queue(self) -> None:
        """Process UI updates from background threads (called periodically from main thread)."""
        try:
            # Process updates in batches to prevent blocking
            processed_count = 0
            max_updates_per_cycle = 5  # Limit to prevent blocking on large queues
            while processed_count < max_updates_per_cycle:
                try:
                    update_func = self._ui_update_queue.get_nowait()
                    update_start = time.perf_counter()
                    update_func()
                    update_elapsed = time.perf_counter() - update_start
                    processed_count += 1
                    if update_elapsed > 0.1:  # Log slow updates
                        _log_timing_message(f"[UI_QUEUE] Slow update #{processed_count}: {update_elapsed:.4f}s")
                except queue.Empty:
                    break
            if processed_count > 0:
                _log_timing_message(f"[UI_QUEUE] Processed {processed_count} updates")
        except Exception as e:
            _log_timing_message(f"[UI_QUEUE] ERROR processing queue: {type(e).__name__}: {e}")
            import traceback
            _log_timing_message(f"[UI_QUEUE] TRACEBACK:\n{traceback.format_exc()}")
    
    def _check_tags_pane_scroll(self) -> None:
        """Periodically check if we need to load more tags in tags pane (fallback if scroll events don't fire)."""
        try:
            if hasattr(self, 'tags_pane') and self.tags_pane._total_tags_count > 0:
                if self.tags_pane._loaded_tags_count < self.tags_pane._total_tags_count:
                    # Check scroll position
                    try:
                        scroll_y = self.tags_pane.scroll_y if hasattr(self.tags_pane, 'scroll_y') else 0
                        max_scroll_y = self.tags_pane.max_scroll_y if hasattr(self.tags_pane, 'max_scroll_y') else 0
                        
                        if max_scroll_y > 0:
                            scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                            
                            # If scrolled near bottom (85%), load more tags
                            if scroll_percent >= 0.85:
                                _log_timing_message(f"[TIMING] [PERIODIC] Tags pane: Loading more tags (scroll_percent={scroll_percent:.2f}, loaded={self.tags_pane._loaded_tags_count}, total={self.tags_pane._total_tags_count})")
                                self.load_more_tags()
                    except Exception:
                        pass
        except Exception:
            pass
    
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
                    
                    # Check if we have more lines available to render (from pending lines)
                    has_pending_lines = hasattr(self.log_pane, '_pending_log_lines') and self.log_pane._pending_log_lines
                    
                    # If scrolled near bottom (85%), either render more pending lines or load more commits
                    if scroll_percent >= 0.85:
                        if has_pending_lines:
                            # Process pending lines in chunks
                            _log_timing_message(f"[TIMING] [PERIODIC CHECK] Log pane: Processing pending lines (scroll_percent={scroll_percent:.2f}, pending={len(self.log_pane._pending_log_lines)})")
                            # Process next 50 lines from pending
                            from rich.text import Text
                            chunk_size = 50
                            lines_to_process = self.log_pane._pending_log_lines[:chunk_size]
                            remaining_pending = self.log_pane._pending_log_lines[chunk_size:]
                            
                            new_rich_lines = []
                            for line in lines_to_process:
                                if line:
                                    try:
                                        # Use Rich's built-in ANSI parser (faster than manual parsing)
                                        rich_line = Text.from_ansi(line)
                                        new_rich_lines.append(rich_line)
                                    except Exception:
                                        from pygitzen.git_graph import strip_ansi_codes
                                        plain_line = strip_ansi_codes(line)
                                        new_rich_lines.append(Text(plain_line, style="white"))
                            
                            # Append new lines to existing ones
                            if self.log_pane._native_git_log_lines:
                                self.log_pane._native_git_log_lines.extend(new_rich_lines)
                            else:
                                self.log_pane._native_git_log_lines = new_rich_lines
                            
                            # Update pending lines
                            self.log_pane._pending_log_lines = remaining_pending
                            
                            # Queue UI update
                            def update_with_more_lines():
                                if self.log_pane._native_git_log_lines:
                                    from rich.console import Group
                                    full_content = Group(*self.log_pane._native_git_log_lines)
                                    self.log_pane.update(full_content)
                                    self.log_pane._native_git_log_rendered_count = len(self.log_pane._native_git_log_lines)
                            
                            if hasattr(self, '_ui_update_queue'):
                                self._ui_update_queue.put(update_with_more_lines)
                        else:
                            # No pending lines, load more commits from git
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
                                self.log_pane._show_native_git_log(self.active_branch, basic_branch_info, git_service_wrapper, append=True)
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
    
    def action_prev_tab(self) -> None:
        """Handle previous tab action - switch to previous tab in branches/remotes/tags."""
        try:
            tabbed_content = self.query_one("#branches-tabbed", None)
            if tabbed_content:
                # Get current active tab
                current_tab = tabbed_content.active
                tabs = ["branches-tab", "remotes-tab", "tags-tab"]
                if current_tab in tabs:
                    current_index = tabs.index(current_tab)
                    # Switch to previous tab (wrap around)
                    prev_index = (current_index - 1) % len(tabs)
                    tabbed_content.active = tabs[prev_index]
        except Exception as e:
            _log_timing_message(f"[ACTION] Error switching to previous tab: {type(e).__name__}: {e}")
    
    def action_next_tab(self) -> None:
        """Handle next tab action - switch to next tab in branches/remotes/tags."""
        try:
            tabbed_content = self.query_one("#branches-tabbed", None)
            if tabbed_content:
                # Get current active tab
                current_tab = tabbed_content.active
                tabs = ["branches-tab", "remotes-tab", "tags-tab"]
                if current_tab in tabs:
                    current_index = tabs.index(current_tab)
                    # Switch to next tab (wrap around)
                    next_index = (current_index + 1) % len(tabs)
                    tabbed_content.active = tabs[next_index]
        except Exception as e:
            _log_timing_message(f"[ACTION] Error switching to next tab: {type(e).__name__}: {e}")
    
    def _on_tab_changed(self, active_tab: str) -> None:
        """Handle tab change - load tags lazily when tags tab is selected."""
        if active_tab == "tags-tab":
            # Check if tags are already loaded
            if not hasattr(self.tags_pane, '_tags') or len(self.tags_pane._tags) == 0:
                # Load tags in background (lazy loading - like Lazygit loads them async)
                _log_timing_message("[TAGS] Loading tags lazily (tags tab selected)")
                self.load_tags_background()
    
    def _update_tabbed_border(self) -> None:
        """Update tabbed panel border to green when any child pane has focus.
        Also automatically focus the active tab's content pane when tabbed panel receives focus."""
        try:
            if not hasattr(self, 'branches_tabbed'):
                return
            
            # Check if any child pane has focus
            has_focus = (
                self.branches_pane.has_focus or
                (hasattr(self, 'remotes_pane') and self.remotes_pane.has_focus) or
                (hasattr(self, 'tags_pane') and self.tags_pane.has_focus)
            )
            
            # If tabbed container has focus but no child pane has focus, focus the active tab's content
            if self.branches_tabbed.has_focus and not has_focus:
                # Get the active tab and focus the corresponding pane
                active_tab = getattr(self.branches_tabbed, 'active', None)
                if active_tab == "branches-tab":
                    self.branches_pane.focus()
                elif active_tab == "remotes-tab":
                    if hasattr(self, 'remotes_pane'):
                        self.remotes_pane.focus()
                elif active_tab == "tags-tab":
                    if hasattr(self, 'tags_pane'):
                        self.tags_pane.focus()
            
            # Update border color based on focus
            if has_focus or self.branches_tabbed.has_focus:
                self.branches_tabbed.styles.border = ("solid", "green")
            else:
                self.branches_tabbed.styles.border = ("solid", "white")
        except Exception:
            # Silently fail if border update fails
            pass
    
    def _update_footer(self) -> None:
        """Update footer with context-appropriate actions based on focused panel."""
        try:
            footer = self.query_one("Footer", None)
            if not footer:
                return
            
            # Update tabbed border based on focus
            self._update_tabbed_border()
            
            # Determine which panel has focus
            footer_text = ""
            
            if self.staged_pane.has_focus or self.changes_pane.has_focus:
                # Files focused
                footer_text = "Stage: <space> | Discard: d | Reset: D | Quit: q | Refresh: r"
            elif (self.branches_pane.has_focus or 
                  (hasattr(self, 'remotes_pane') and self.remotes_pane.has_focus) or 
                  (hasattr(self, 'tags_pane') and self.tags_pane.has_focus)):
                # Branches/Remotes/Tags focused
                footer_text = "Checkout: c | New: n | Delete: D | Prev Tab: [ | Next Tab: ] | Quit: q | Refresh: r"
            elif self.commits_pane.has_focus:
                # Commits focused
                footer_text = "Show diff: <enter> | Search: / | Quit: q | Refresh: r"
            elif self.stash_pane.has_focus:
                # Stash focused
                footer_text = "Apply: a | Pop: p | Drop: d | Quit: q | Refresh: r"
            else:
                # Default footer
                footer_text = "Quit: q | Refresh: r | Navigate: j/k | Select: <space>/<enter>"
            
            # Update footer if text changed
            if hasattr(footer, '_footer_text') and footer._footer_text == footer_text:
                return  # No change needed
            
            footer._footer_text = footer_text
            # Update footer using Textual's footer API
            from rich.text import Text
            footer_text_obj = Text(footer_text, style="white")
            footer.update(footer_text_obj)
        except Exception as e:
            # Silently fail if footer update fails
            pass

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
                        current_sync = self._branch_sync_status_cache.get(self.active_branch) if self.active_branch else None
                        self.status_pane.update_status(self.active_branch, self.repo_path, current_sync)
                    # Load heavy operations in background
                    # DISABLED: Don't load commit count separately - we use len(commits) from load_commits() (matching Lazygit)
                    # This eliminates race conditions where git.count_commits() returns wrong values
                    # self.load_commits_count_background(self.active_branch)
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
                        current_sync = self._branch_sync_status_cache.get(self.active_branch) if self.active_branch else None
                        self.status_pane.update_status(self.active_branch, self.repo_path, current_sync)
                    # Load heavy operations in background
                    # DISABLED: Don't load commit count separately - we use len(commits) from load_commits() (matching Lazygit)
                    # This eliminates race conditions where git.count_commits() returns wrong values
                    # self.load_commits_count_background(self.active_branch)
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
        
        # CRITICAL: Clear current branch cache to ensure fresh data during refresh
        # This prevents stale cache from causing wrong ref_spec in load_commits()
        if hasattr(self.git, '_current_branch_cache'):
            self.git._current_branch_cache = None
            _log_timing_message("[DEBUG] refresh_data_fast: Cleared _current_branch_cache")
        
        # CRITICAL: Clear commit count cache to prevent stale cached values (like 2) from overwriting correct counts (like 62)
        # This cache can have stale values from previous sessions or different branches
        if hasattr(self, '_commit_count_cache'):
            self._commit_count_cache.clear()
            _log_timing_message("[DEBUG] refresh_data_fast: Cleared _commit_count_cache")
        
        # Preserve current branch selection before refreshing
        previous_branch = self.active_branch
        
        # Load branches immediately (fast, ~0.1s)
        branch_start = time.perf_counter()
        self.branches = self.git.list_branches()
        branch_elapsed = time.perf_counter() - branch_start
        _log_timing_message(f"list_branches: {branch_elapsed:.4f}s")
        
        # Load remotes, tags, and sync status in background (parallel)
        # OPTIMIZATION: Load tags WITHOUT timestamps first (fast, like Lazygit ~0.6s for 56k tags)
        # Lazygit loads ALL tags at once (no pagination) because git tag --list is fast
        # Timestamps add ~0.9s overhead - load them in background after initial display
        self.load_remotes_background()
        self.load_tags_background(get_timestamps=False)  # Fast initial load (like Lazygit)
        self.load_branch_sync_status_background()
        
        # Load timestamps in background after initial tag load (for recency display)
        # This is optional - tags work fine without timestamps (Lazygit doesn't show recency)
        # IMPORTANT: Only update timestamps for already-displayed tags, don't reload all 56k tags
        def load_tag_timestamps_later():
            """Load timestamps for tags in background (optional, for recency display)."""
            self._ui_ready.wait()
            time.sleep(0.5)  # Wait a bit for initial tag load to complete
            if hasattr(self, 'tags_pane') and len(self.tags_pane._tags) > 0:
                # Only update timestamps for tags that are already displayed (first page)
                # Don't reload all 56k tags - that would block the UI
                displayed_tags = self.tags_pane._tags[:self.tags_pane._page_size]  # Only first page
                if displayed_tags:
                    _log_timing_message(f"[TIMING] [BACKGROUND] Loading timestamps for {len(displayed_tags)} displayed tags...")
                    # Fetch timestamps in batch for displayed tags only
                    tag_names = [tag.name for tag in displayed_tags]
                    timestamp_map = self.git.get_tag_timestamps_batch(tag_names)
                    # Update tags with timestamps
                    for tag in displayed_tags:
                        if tag.name in timestamp_map:
                            tag.timestamp = timestamp_map[tag.name]
                    # Update UI with timestamps (only for displayed tags)
                    self._ui_update_queue.put(lambda: self._update_tags_ui_with_timestamps(displayed_tags))
        
        # Explicitly import threading here to avoid UnboundLocalError
        # (threading is imported at module level, but Python may see it as local due to other local imports)
        import threading as _threading_module
        thread = _threading_module.Thread(target=load_tag_timestamps_later, daemon=True)
        thread.start()
        
        if self.branches:
            # Get current branch (matching Lazygit's approach)
            # Lazygit uses git branch --show-current (CurrentBranchName())
            # Current branch is always first in the list (matching Lazygit's GetCheckedOutRef() which returns branches[0])
            current_branch = self.git.get_current_branch()
            
            # Determine which branch to select (matching Lazygit behavior)
            branch_to_select = None
            branch_index = None
            
            if current_branch:
                # Priority 1: Use current branch (matching Lazygit - always select current branch on startup)
                branch_names = [b.name for b in self.branches]
                if current_branch in branch_names:
                    branch_to_select = current_branch
                    branch_index = branch_names.index(current_branch)
                else:
                    # Current branch not in list (shouldn't happen, but fallback)
                    branch_to_select = self.branches[0].name
                    branch_index = 0
            elif previous_branch:
                # Priority 2: Restore previous branch if it still exists
                branch_names = [b.name for b in self.branches]
                if previous_branch in branch_names:
                    branch_to_select = previous_branch
                    branch_index = branch_names.index(previous_branch)
                else:
                    # Branch was deleted, fall back to first branch
                    branch_to_select = self.branches[0].name
                    branch_index = 0
            else:
                # Priority 3: Fall back to first branch
                branch_to_select = self.branches[0].name
                branch_index = 0
            
            # Set the active branch and update UI
            self.active_branch = branch_to_select
            self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
            # Ensure BranchesPane ListView selection matches (set after list is populated)
            self.branches_pane.index = branch_index
            self.branches_pane.highlighted = branch_index

            # Initialize commits pane with empty state (show UI immediately)
            self.commits = []
            # CRITICAL: Don't reset total_commits here - preserve it until load_commits() sets the new value
            # This prevents the count from showing "1 of 0" or "1 of 2" during refresh
            # self.total_commits will be set by load_commits() when it completes
            self.commits_pane.clear()
            self.commits_pane.border_title = f"Commits ({self.active_branch})" if self.active_branch else "Commits (HEAD)"
            
            # On initial load, show log view for the selected branch
            # BUT don't load the log graph on startup (matches Lazygit behavior)
            # The log graph will load when user selects a branch or interacts with the UI
            self._view_mode = "log"
            self.patch_pane.styles.display = "none"
            self.log_pane.styles.display = "block"
            
            # Show empty log pane initially (matches Lazygit - no expensive git log --graph on startup)
            from rich.text import Text
            empty_text = Text()
            self.log_pane.update(empty_text)
            
            # Load commits in background (non-blocking) - this allows UI to appear immediately
            # Similar to lazygit: wait for UI to be ready before starting background work
            # NOTE: We only load the commit LIST, not the log graph (matches Lazygit)
            def load_commits_background():
                """Load commits in background thread (waits for UI to be ready)."""
                try:
                    # Wait for UI to be ready (similar to lazygit's waitForIntro.Wait())
                    self._ui_ready.wait()
                    
                    commits_load_start = time.perf_counter()
                    # Store active branch to avoid race conditions
                    branch_to_load = self.active_branch
                    
                    # Load commits data (load_commits() handles thread-safe UI updates internally)
                    # This loads the commit LIST (git log --oneline), not the graph
                    self.load_commits(branch_to_load)
                    commits_load_elapsed = time.perf_counter() - commits_load_start
                    _log_timing_message(f"load_commits (background): {commits_load_elapsed:.4f}s")
                    
                    # DON'T load log graph on startup (matches Lazygit behavior exactly)
                    # Lazygit only loads the log graph when user explicitly views it (e.g., clicks branch)
                    # The log graph (git log --graph) is expensive and will be loaded lazily
                    # when the user selects a branch via action_down/action_up or explicitly views the log
                    
                    # Signal completion to barrier (similar to lazygit's wg.Done())
                    try:
                        self._data_loading_barrier.wait()
                        _log_timing_message("[TIMING] Commits loading completed (barrier)")
                    except threading.BrokenBarrierError:
                        _log_timing_message("[TIMING] Commits loading: barrier was broken")
                except Exception as e:
                    _log_timing_message(f"[ERROR] Background load_commits failed: {type(e).__name__}: {e}")
                    # Still signal barrier even on error
                    try:
                        self._data_loading_barrier.wait()
                    except (threading.BrokenBarrierError, Exception):
                        pass
            
            # Start background thread for commits loading
            import threading
            commits_thread = threading.Thread(target=load_commits_background, daemon=True)
            commits_thread.start()
            
            # DON'T load log graph on startup at all (matches Lazygit exactly)
            # The log graph will only load when user explicitly selects a branch
            # This prevents any blocking operations during startup
            
            # Update status pane immediately with current branch (matching Lazygit)
            # Lazygit shows current branch in status pane (refreshStatus() uses GetCheckedOutRef())
            if self.active_branch:
                current_sync = self._branch_sync_status_cache.get(self.active_branch) if self.active_branch else None
                self.status_pane.update_status(self.active_branch, self.repo_path, current_sync)
            
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
            
            # Load heavy operations in background (non-blocking)
            # Store branch for background workers
            self._pending_branch = self.active_branch
            # DISABLED: Don't load commit count separately - we use len(commits) from load_commits() (matching Lazygit)
            # self.load_commits_count_background(self.active_branch)
            self.load_file_status_background()
            self.load_stashes_background()
            
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
        
        # Update branches pane
        if self.branches:
            self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
        
        # Stashes are loaded in background (not here to avoid blocking)
        
        # Update command log
        # self.command_log_pane.update_log("Repository refreshed successfully!")
        # Update command log
        version_info = " (Python)"
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
                        # Debug log
                        try:
                            with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                                f.write(f"DEBUG load_commits_for_log: Using self.repo_path={repo_path_to_use} for branch={branch}\n")
                        except:
                            pass
                except Exception as e:
                    try:
                        with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                            f.write(f"DEBUG load_commits_for_log: Error getting self.repo_path: {e}\n")
                    except:
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
                    # Debug: verify repo_path is set
                    if not self.repo_path or str(self.repo_path) == ".":
                        try:
                            with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                                f.write(f"WARNING: GitServiceWithPath created with invalid repo_path: {repo_path}\n")
                                f.write(f"self.repo_path value: {self.repo_path}\n")
                        except:
                            pass
            
            git_service_wrapper = GitServiceWithPath(self.git, repo_path_to_use)
            
            # Debug: verify wrapper has repo_path before passing
            try:
                wrapper_repo_path = getattr(git_service_wrapper, 'repo_path', None)
                if wrapper_repo_path:
                    with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                        f.write(f"SUCCESS: GitServiceWithPath wrapper created with repo_path={wrapper_repo_path} for branch={branch}\n")
                        f.write(f"wrapper type: {type(git_service_wrapper)}\n")
                        f.write(f"wrapper.repo_path type: {type(wrapper_repo_path)}\n\n")
                else:
                    with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                        f.write(f"ERROR: GitServiceWithPath wrapper missing repo_path for branch={branch}\n")
                        f.write(f"repo_path_to_use was: {repo_path_to_use}\n")
                        f.write(f"self.repo_path was: {getattr(self, 'repo_path', 'NOT_SET')}\n\n")
            except Exception as e:
                try:
                    with open("debug_log_pane.log", "a", encoding="utf-8") as f:
                        f.write(f"ERROR checking wrapper: {e}\n\n")
                except:
                    pass
            
            # Queue show_branch_log to run in background thread (it already handles UI updates via queue)
            # This prevents blocking the main thread when loading large repos
            def show_log_in_background():
                try:
                    self.log_pane.show_branch_log(branch, [], basic_branch_info, git_service_wrapper, append=not reset)
                    show_log_elapsed = time.perf_counter() - show_log_start
                    _log_timing_message(f"  show_branch_log (native git): {show_log_elapsed:.4f}s")
                except Exception as e:
                    _log_timing_message(f"  show_branch_log error: {type(e).__name__}: {e}")
            
            # Run in background thread to avoid blocking
            import threading as _threading_module
            thread = _threading_module.Thread(target=show_log_in_background, daemon=True)
            thread.start()
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
        
        # DISABLED: Don't load commit count separately - we use len(commits) from load_commits() (matching Lazygit)
        # The count is already set by load_commits() via len(commits)
        # if self.log_pane._total_commits_count == 0:
        #     self.load_commits_count_background(branch)
        
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
            """Count commits in background thread (non-blocking, waits for UI to be ready)."""
            # Wait for UI to be ready (similar to lazygit's waitForIntro.Wait())
            self._ui_ready.wait()
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
                # CRITICAL: Only update if the new count is greater than current, or if current is 0
                # This prevents overwriting a correct count (62) with a wrong count (2) from git.count_commits()
                # The count from load_commits() (len(commits)) is more accurate than git.count_commits()
                if count > self.total_commits or self.total_commits == 0:
                    self.total_commits = count
                else:
                    # Ignore lower count - keep the higher value (from load_commits())
                    return  # Don't update title if we're ignoring the count
                
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
            """Load stashes in background thread (non-blocking, waits for UI to be ready)."""
            # Wait for UI to be ready (similar to lazygit's waitForIntro.Wait())
            self._ui_ready.wait()
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
                # Get stashes using Python GitService
                stashes = self.git.list_stashes()
                get_stashes_elapsed = time.perf_counter() - get_stashes_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   list_stashes: {get_stashes_elapsed:.4f}s ({len(stashes)} stashes)")
                
                # Update UI from main thread (use queue which is thread-safe)
                stashes_copy = stashes.copy()
                self._ui_update_queue.put(lambda: self._update_stashes_ui(stashes_copy))
                
                stash_total = time.perf_counter() - stash_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_stashes_background TOTAL: {stash_total:.4f}s")
                
                # Signal completion to barrier (similar to lazygit's wg.Done())
                try:
                    self._data_loading_barrier.wait()
                    _log_timing_message("[TIMING] Stashes loading completed (barrier)")
                except threading.BrokenBarrierError:
                    _log_timing_message("[TIMING] Stashes loading: barrier was broken")
            except Exception as e:
                # If stash fetching fails, show empty
                import traceback
                try:
                    with open("debug_stash.log", "a", encoding="utf-8") as f:
                        f.write(f"Error loading stashes (background): {type(e).__name__}: {e}\n")
                        f.write(f"Traceback:\n{traceback.format_exc()}\n")
                except:
                    pass
                
                # Update UI from main thread on error (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self._update_stashes_ui([]))
                
                # Still signal barrier even on error
                try:
                    self._data_loading_barrier.wait()
                except (threading.BrokenBarrierError, Exception):
                    pass
        
        thread = threading.Thread(target=load_stashes_in_thread, daemon=True)
        thread.start()
    
    def load_remotes_background(self) -> None:
        """Load remotes in background (non-blocking)."""
        if getattr(self, '_loading_remotes', False):
            return
        
        self._loading_remotes = True
        
        def load_remotes_in_thread():
            """Load remotes in background thread."""
            self._ui_ready.wait()
            remotes_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_remotes_background START")
            try:
                remotes = self.git.list_remotes()
                get_remotes_elapsed = time.perf_counter() - remotes_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   list_remotes: {get_remotes_elapsed:.4f}s ({len(remotes)} remotes)")
                
                # Update UI from main thread
                remotes_copy = remotes.copy()
                self._ui_update_queue.put(lambda: self._update_remotes_ui(remotes_copy))
                
                remotes_total = time.perf_counter() - remotes_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_remotes_background TOTAL: {remotes_total:.4f}s")
            except Exception as e:
                import traceback
                _log_timing_message(f"[TIMING] [BACKGROUND] Error loading remotes: {type(e).__name__}: {e}")
                self._ui_update_queue.put(lambda: self._update_remotes_ui([]))
            finally:
                self._loading_remotes = False
        
        thread = threading.Thread(target=load_remotes_in_thread, daemon=True)
        thread.start()
    
    def load_tags_background(self, skip: int = 0, append: bool = False, get_timestamps: bool = True) -> None:
        """Load tags in background (non-blocking), like Lazygit.
        
        KEY: Lazygit loads ALL tags at once (no pagination) because git tag --list is fast.
        We do the same, but optionally get timestamps for recency display (adds overhead).
        
        Args:
            skip: Number of tags to skip (for pagination, but Lazygit doesn't paginate)
            append: If True, append to existing tags; if False, replace
            get_timestamps: Whether to fetch timestamps (adds overhead, Lazygit doesn't do this)
        """
        if getattr(self, '_loading_tags', False):
            return
        
        self._loading_tags = True
        
        def load_tags_in_thread():
            """Load tags in background thread (like Lazygit's ASYNC refresh)."""
            self._ui_ready.wait()
            tags_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_tags_background START (skip={skip}, append={append}, get_timestamps={get_timestamps})")
            try:
                # For virtual scrolling: load first page initially, then load more on scroll
                # Initial load: load first page (200 tags) + get total count
                # Append load: load next page (200 tags) for virtual scrolling
                if append:
                    # Loading more for virtual scrolling - load next page
                    max_count = self.tags_pane._page_size
                else:
                    # Initial load: get total count first, then load first page
                    # First get total count without loading all tags
                    _, total_count = self.git.list_tags(max_count=0, skip=0, get_timestamps=False)
                    # Now load only first page
                    max_count = self.tags_pane._page_size
                
                tags, total_count_returned = self.git.list_tags(max_count=max_count, skip=skip, get_timestamps=get_timestamps)
                # Use the total_count we got earlier if this is initial load, otherwise use returned count
                if not append and total_count == 0:
                    # If we didn't get total_count earlier, use returned count
                    total_count = total_count_returned
                elif append:
                    # When appending, keep the existing total_count
                    total_count = self.tags_pane._total_tags_count if hasattr(self.tags_pane, '_total_tags_count') else total_count_returned
                get_tags_elapsed = time.perf_counter() - tags_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   list_tags: {get_tags_elapsed:.4f}s ({len(tags)} tags, total={total_count})")
                
                # Update UI from main thread
                tags_copy = tags.copy()
                self._ui_update_queue.put(lambda: self._update_tags_ui(tags_copy, total_count, append))
                
                tags_total = time.perf_counter() - tags_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_tags_background TOTAL: {tags_total:.4f}s")
            except Exception as e:
                import traceback
                _log_timing_message(f"[TIMING] [BACKGROUND] Error loading tags: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                self._ui_update_queue.put(lambda: self._update_tags_ui([], 0, append))
            finally:
                self._loading_tags = False
        
        thread = threading.Thread(target=load_tags_in_thread, daemon=True)
        thread.start()
    
    def _update_remotes_ui(self, remotes: list) -> None:
        """Update remotes pane UI (called from main thread)."""
        try:
            self.remotes_pane.set_remotes(remotes)
        except Exception as e:
            _log_timing_message(f"[UI_UPDATE] Error updating remotes UI: {type(e).__name__}: {e}")
    
    def _update_tags_ui(self, tags: list, total_count: int = 0, append: bool = False) -> None:
        """Update tags pane UI (called from main thread)."""
        try:
            self.tags_pane.set_tags(tags, total_count=total_count, append=append)
        except Exception as e:
            _log_timing_message(f"[UI_UPDATE] Error updating tags UI: {type(e).__name__}: {e}")
    
    def _update_tags_ui_with_timestamps(self, tags: list) -> None:
        """Update only the displayed tags with timestamps (without reloading all tags).
        
        This is used to add recency information to already-displayed tags without
        blocking the UI by reloading all 56k tags.
        """
        try:
            # Update the internal tag list with new timestamps
            tag_dict = {tag.name: tag for tag in tags}
            for existing_tag in self.tags_pane._tags[:len(tags)]:
                if existing_tag.name in tag_dict:
                    existing_tag.timestamp = tag_dict[existing_tag.name].timestamp
            
            # Re-render only the displayed items by calling set_tags with just the displayed tags
            # This will clear and re-render only the first page, not all 56k tags
            displayed_count = min(len(tags), len(self.tags_pane._tags))
            if displayed_count > 0:
                displayed_tags = self.tags_pane._tags[:displayed_count]
                # Save the rest of the tags
                remaining_tags = self.tags_pane._tags[displayed_count:]
                # Clear and re-render only displayed items
                self.tags_pane.clear()
                self.tags_pane._tags = displayed_tags + remaining_tags
                # Only render the displayed items (not all 56k)
                self.tags_pane.set_tags(displayed_tags, append=True)
        except Exception as e:
            _log_timing_message(f"[UI_UPDATE] Error updating tags UI with timestamps: {type(e).__name__}: {e}")
    
    def load_more_tags(self) -> None:
        """Load more tags for virtual scrolling."""
        if self.tags_pane._loaded_tags_count < self.tags_pane._total_tags_count:
            skip = self.tags_pane._loaded_tags_count
            # Don't load timestamps for virtual scrolling to keep it fast and consistent with initial load
            self.load_tags_background(skip=skip, append=True, get_timestamps=False)
    
    def load_branch_sync_status_background(self) -> None:
        """Load sync status for all branches in background (non-blocking)."""
        if getattr(self, '_loading_sync_status', False):
            return
        
        self._loading_sync_status = True
        
        def load_sync_status_in_thread():
            """Load sync status in background thread."""
            self._ui_ready.wait()
            sync_start = time.perf_counter()
            _log_timing_message(f"[TIMING] [BACKGROUND] load_branch_sync_status_background START")
            try:
                # Get list of branches
                branches = getattr(self, 'branches', [])
                if not branches:
                    return
                
                sync_status_map = {}
                for branch in branches:
                    try:
                        sync_status = self.git.get_branch_sync_status(branch.name)
                        sync_status_map[branch.name] = sync_status
                    except Exception as e:
                        _log_timing_message(f"[TIMING] [BACKGROUND] Error getting sync status for {branch.name}: {type(e).__name__}: {e}")
                        # Use default empty status
                        sync_status_map[branch.name] = {"behind": 0, "ahead": 0, "synced": False, "upstream": None}
                
                get_sync_elapsed = time.perf_counter() - sync_start
                _log_timing_message(f"[TIMING] [BACKGROUND]   get_branch_sync_status for {len(branches)} branches: {get_sync_elapsed:.4f}s")
                
                # Update cache and UI from main thread
                sync_status_map_copy = sync_status_map.copy()
                self._ui_update_queue.put(lambda: self._update_sync_status_ui(sync_status_map_copy))
                
                sync_total = time.perf_counter() - sync_start
                _log_timing_message(f"[TIMING] [BACKGROUND] load_branch_sync_status_background TOTAL: {sync_total:.4f}s")
            except Exception as e:
                import traceback
                _log_timing_message(f"[TIMING] [BACKGROUND] Error loading sync status: {type(e).__name__}: {e}")
            finally:
                self._loading_sync_status = False
        
        thread = threading.Thread(target=load_sync_status_in_thread, daemon=True)
        thread.start()
    
    def _update_sync_status_ui(self, sync_status_map: dict[str, dict]) -> None:
        """Update branches pane and status pane with sync status (called from main thread)."""
        try:
            # Update cache
            self._branch_sync_status_cache = sync_status_map
            
            # Update branches pane if it exists and has branches
            if hasattr(self, 'branches') and self.branches:
                self.branches_pane.set_branches(self.branches, self.active_branch, sync_status_map)
            
            # Update status pane with current branch sync status
            if self.active_branch and self.active_branch in sync_status_map:
                current_sync_status = sync_status_map[self.active_branch]
                self.status_pane.update_status(self.active_branch, self.repo_path, current_sync_status)
        except Exception as e:
            _log_timing_message(f"[UI_UPDATE] Error updating sync status UI: {type(e).__name__}: {e}")
    
    def load_file_status_background(self) -> None:
        """Load file status in background (non-blocking)."""
        if self._loading_file_status:
            return
        
        self._loading_file_status = True
        
        # Use a thread to load files asynchronously without blocking the UI
        # This ensures commits can display immediately while files load in background
        import threading
        
        def load_files_in_thread():
            """Load files in background thread (non-blocking, waits for UI to be ready)."""
            # Wait for UI to be ready (similar to lazygit's waitForIntro.Wait())
            self._ui_ready.wait()
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
                
                # Signal completion to barrier (similar to lazygit's wg.Done())
                try:
                    self._data_loading_barrier.wait()
                    _log_timing_message("[TIMING] Files loading completed (barrier)")
                except threading.BrokenBarrierError:
                    _log_timing_message("[TIMING] Files loading: barrier was broken")
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
                
                # Still signal barrier even on error
                try:
                    self._data_loading_barrier.wait()
                except (threading.BrokenBarrierError, Exception):
                    pass
        
        # Start thread immediately - doesn't block UI
        thread = threading.Thread(target=load_files_in_thread, daemon=True)
        thread.start()
    
    def _on_all_data_loaded(self) -> None:
        """Callback when all data loading threads complete (called by barrier)."""
        _log_timing_message("[TIMING] ===== All data loading threads completed =====")
        self._data_loading_complete.set()
    
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
            version_info = " (Python)"
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

    def load_commits(self, branch: str) -> None:
        """Load all commits from all branches (not branch-specific)."""
        import subprocess
        from datetime import datetime
        
        # Debug: log that function was called
        _log_timing_message(f"[DEBUG] load_commits CALLED with branch={branch}")
        print(f"[DEBUG] load_commits CALLED with branch={branch}")
        
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
            # Determine ref spec (matching lazygit's approach)
            # For current branch: use "HEAD" (matching lazygit's refForLog())
            # For other branches: use full ref path "refs/heads/branch-name" (matching lazygit's FullRefName())
            if not branch:
                ref_spec = "HEAD"
            else:
                # ALWAYS use get_current_branch() as source of truth (not self.active_branch which may be stale)
                # This ensures we use HEAD for the current branch even if active_branch hasn't been set yet
                current_branch = self.git.get_current_branch()
                _log_timing_message(f"[DEBUG] load_commits: branch={branch}, current_branch={current_branch}, active_branch={self.active_branch}")
                
                # Check if this is the current branch (prioritize get_current_branch() over self.active_branch)
                if branch == current_branch:
                    # Current branch - use HEAD (matching lazygit)
                    ref_spec = "HEAD"
                    _log_timing_message(f"[DEBUG] load_commits: Using HEAD for current branch '{branch}'")
                else:
                    # Other branch - use full ref path (matching lazygit)
                    if branch.startswith("refs/"):
                        ref_spec = branch  # Already a full ref path
                    else:
                        ref_spec = f"refs/heads/{branch}"
                    _log_timing_message(f"[DEBUG] load_commits: Using ref_spec='{ref_spec}' for branch '{branch}'")
            
            cmd = [
                "git", "log",
                ref_spec,  # Current branch (matching lazygit - shows branch-specific commits)
                "--oneline",  # Match lazygit
                "-300",  # Match lazygit's limit (300 commits initially)
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
            
            # Debug: log repo_path and command
            _log_timing_message(f"[DEBUG] load_commits: repo_path={repo_path_str}, cmd={cmd}")
            print(f"[DEBUG] load_commits: repo_path={repo_path_str}")
            
            # Run git log with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str
            )
            
            # Debug: log result
            stdout_line_count = len(result.stdout.strip().split("\n")) if result.stdout else 0
            _log_timing_message(f"[DEBUG] load_commits: git log returncode={result.returncode}, stdout_lines={stdout_line_count}, ref_spec={ref_spec}")
            print(f"[DEBUG] load_commits: git log returncode={result.returncode}, stdout_lines={stdout_line_count}, ref_spec={ref_spec}")
            if result.returncode != 0:
                print(f"[DEBUG] load_commits: git log stderr={result.stderr}")
            
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
                
                # Debug: log parsing results
                _log_timing_message(f"[DEBUG] load_commits: Parsed {len(commits)} commits from {stdout_line_count} stdout lines")
                print(f"[DEBUG] load_commits: Parsed {len(commits)} commits from {stdout_line_count} stdout lines")
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
                
                # Get merged commits from cache or fetch if needed
                if self._merged_commits_cache:
                    # Use cached merged commits (fast!)
                    merged_commits = self._merged_commits_cache
                    _log_timing_message(f"[CACHE] HIT merged_commits_cache: {len(merged_commits)} merged commits")
                else:
                    # Cache MISS - fetch merged commits from main branches
                    merged_commits = set()
                    for main_branch in ["origin/main", "origin/master"]:
                        try:
                            check_main = subprocess.run(
                                ["git", "rev-parse", "--verify", main_branch],
                                capture_output=True,
                                text=True,
                                timeout=1,
                                cwd=repo_path_str
                            )
                            if check_main.returncode == 0:
                                # Fetch ALL commits from main/master (no limit)
                                # This ensures commits beyond 1000 are correctly identified as merged
                                # Performance: Cached after first fetch, so only slow on initial load
                                merged_cmd = ["git", "rev-list", main_branch]
                                merged_result = subprocess.run(
                                    merged_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,  # Increased timeout for large repos
                                    cwd=repo_path_str
                                )
                                if merged_result.returncode == 0:
                                    for sha in merged_result.stdout.strip().split("\n"):
                                        if sha.strip():
                                            merged_commits.add(sha.strip())
                        except Exception:
                            pass
                    # Cache the merged commits for future use
                    self._merged_commits_cache = merged_commits
                    _log_timing_message(f"[CACHE] MISS merged_commits_cache: fetched {len(merged_commits)} merged commits")
                
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
                    # Assume commits are pushed (yellow) until background thread determines otherwise
                    # This matches lazygit behavior
                    for commit in commits:
                        normalized_sha = _normalize_commit_sha(commit.sha)
                        commit.merged = normalized_sha in normalized_merged
                        # Assume pushed (will be corrected by background thread if wrong)
                        # Note: merged commits should show green checkmark, not yellow arrow
                        commit.pushed = True
            
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
                    
                    # DISABLED: Don't fetch commit count separately - we use len(commits) from load_commits() (matching Lazygit)
                    # The commit count is already set correctly by load_commits() via len(commits)
                    # This eliminates unnecessary work and race conditions
                    # 
                    # # Update commit count - check cache first
                    # count_start = time.perf_counter()
                    # if actual_ref in self._commit_count_cache and not cache_invalidated_count:
                    #     # Cache HIT
                    #     count = self._commit_count_cache[actual_ref]
                    #     count_elapsed = time.perf_counter() - count_start
                    #     _log_timing_message(f"[CACHE] HIT commit_count_cache for {actual_ref}: {count} (saved {count_elapsed:.4f}s) - CALLING _update_commits_count_ui({count})")
                    #     print(f"[CACHE] HIT commit_count_cache for {actual_ref}: {count} - CALLING _update_commits_count_ui({count})")
                    #     self.call_from_thread(self._update_commits_count_ui, count)
                    # else:
                    #     # Cache MISS or INVALIDATED - fetch fresh data
                    #     try:
                    #         count_cmd = ["git", "rev-list", "--count", ref_spec]
                    #         count_result = subprocess.run(
                    #             count_cmd,
                    #             capture_output=True,
                    #             text=True,
                    #             timeout=10,
                    #             cwd=repo_path_str
                    #         )
                    #         count_elapsed = time.perf_counter() - count_start
                    #         if count_result.returncode == 0:
                    #             count = int(count_result.stdout.strip())
                    #             # Cache the result
                    #             self._commit_count_cache[actual_ref] = count
                    #             # Update tracked HEAD SHA
                    #             if current_head_sha:
                    #                 self._last_head_sha[actual_ref] = current_head_sha
                    #             # Update UI in main thread
                    #             cache_reason = "INVALIDATED" if cache_invalidated_count else "MISS"
                    #             _log_timing_message(f"[CACHE] {cache_reason} commit_count_cache for {actual_ref}: fetched {count} in {count_elapsed:.4f}s - CALLING _update_commits_count_ui({count})")
                    #             print(f"[CACHE] {cache_reason} commit_count_cache for {actual_ref}: fetched {count} - CALLING _update_commits_count_ui({count})")
                    #             self.call_from_thread(self._update_commits_count_ui, count)
                    #         else:
                    #             _log_timing_message(f"[TIMING] git rev-list --count {ref_spec}: {count_elapsed:.4f}s (ERROR: {count_result.stderr})")
                    #     except Exception as count_e:
                    #         count_elapsed = time.perf_counter() - count_start
                    #         _log_timing_message(f"[TIMING] git rev-list --count {ref_spec}: {count_elapsed:.4f}s (EXCEPTION: {type(count_e).__name__}: {count_e})")
                    
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
                                
                                # Lazygit's EXACT logic from commit_loader.go line 119:
                                # unpushedCommitHashes = self.getReachableHashes(
                                #     opts.RefForPushedStatus.FullRefName(),
                                #     append([]string{opts.RefForPushedStatus.RefName() + "@{u}"}, mainBranches...)
                                # )
                                # 
                                # This means it ALWAYS tries to use branch_name@{u}, even if it doesn't exist
                                # If @{u} doesn't exist, git rev-list will fail and getReachableHashes returns empty set
                                
                                # Get branch short name for @{u} (Lazygit uses RefName(), not FullRefName())
                                branch_name_short = actual_ref.replace("refs/heads/", "")
                                if branch_name_short == "HEAD":
                                    # For HEAD, get the actual branch name
                                    head_result = subprocess.run(
                                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                        capture_output=True,
                                        text=True,
                                        timeout=2,
                                        cwd=repo_path_str
                                    )
                                    if head_result.returncode == 0:
                                        branch_name_short = head_result.stdout.strip()
                                
                                # Lazygit uses: branch_name@{u} as the first not_ref
                                upstream_ref = f"{branch_name_short}@{{u}}"
                                not_refs = [upstream_ref] + main_branches
                                
                                # This is Lazygit's exact command - it will fail if @{u} doesn't exist
                                # When it fails, getReachableHashes returns empty set, which means status = StatusPushed (YELLOW)
                                unpushed_cmd = ["git", "rev-list", actual_ref]
                                for not_ref in not_refs:
                                    unpushed_cmd.extend(["--not", not_ref])
                                
                                unpushed_result = subprocess.run(
                                    unpushed_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                    cwd=repo_path_str
                                )
                                rev_list_elapsed = time.perf_counter() - rev_list_start
                                
                                if unpushed_result.returncode == 0:
                                    # Command succeeded - we have unpushed commits
                                    for sha in unpushed_result.stdout.strip().split("\n"):
                                        if sha.strip():
                                            unpushed_commits.add(sha.strip())
                                    self._remote_commits_cache[cache_key] = unpushed_commits
                                    cache_reason = "INVALIDATED" if cache_invalidated_remote_branch else "MISS"
                                    _log_timing_message(f"[CACHE] {cache_reason} unpushed_commits_cache for {actual_ref}: fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s (using {upstream_ref})")
                                else:
                                    # Command failed (e.g., @{u} doesn't exist) - return empty set
                                    # Empty set means: unpushedCommitHashes.Includes(commit) = false
                                    # Therefore status = StatusPushed (YELLOW)
                                    unpushed_commits = set()  # Empty set = all commits are assumed pushed (yellow)
                                    self._remote_commits_cache[cache_key] = unpushed_commits
                                    _log_timing_message(f"[CACHE] MISS unpushed_commits_cache for {actual_ref}: {upstream_ref} doesn't exist, assuming all commits are pushed (yellow) - matching Lazygit behavior")
                            except Exception as e:
                                rev_list_elapsed = time.perf_counter() - rev_list_start
                                _log_timing_message(f"[TIMING] Error getting unpushed commits for {actual_ref}: {type(e).__name__}: {e} in {rev_list_elapsed:.4f}s")
                    
                    # Get merged commits from cache or fetch if needed
                    if self._merged_commits_cache:
                        # Use cached merged commits (fast!)
                        merged_commits = self._merged_commits_cache
                        _log_timing_message(f"[CACHE] HIT merged_commits_cache (background): {len(merged_commits)} merged commits")
                    else:
                        # Cache MISS - fetch merged commits from main branches
                        merged_commits = set()
                        main_branches = []
                        for main_branch in ["origin/main", "origin/master"]:
                            try:
                                check_main = subprocess.run(
                                    ["git", "rev-parse", "--verify", main_branch],
                                    capture_output=True,
                                    text=True,
                                    timeout=1,
                                    cwd=repo_path_str
                                )
                                if check_main.returncode == 0:
                                    main_branches.append(main_branch)
                            except Exception:
                                pass
                        
                        if main_branches:
                            for main_branch in main_branches:
                                # Fetch ALL commits from main/master (no limit)
                                # This ensures commits beyond 1000 are correctly identified as merged
                                merged_cmd = ["git", "rev-list", main_branch]
                                merged_result = subprocess.run(
                                    merged_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,  # Increased timeout for large repos
                                    cwd=repo_path_str
                                )
                                if merged_result.returncode == 0:
                                    for sha in merged_result.stdout.strip().split("\n"):
                                        if sha.strip():
                                            merged_commits.add(sha.strip())
                        # Cache the merged commits for future use
                        self._merged_commits_cache = merged_commits
                        _log_timing_message(f"[CACHE] MISS merged_commits_cache (background): fetched {len(merged_commits)} merged commits")
                    
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
                    
                    _log_timing_message(f"[DEBUG] Three-tier status (lazygit approach): {merged_count} merged (✓ green), {pushed_count} pushed (↑ yellow), {unpushed_count} unpushed (- red)")
                    
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
        
        # Debug logging removed - issue is fixed
        
        # OPTIMIZATION: Show commits to UI immediately (critical path)
        # CRITICAL: Queue the UI update to prevent blocking - set_commits can be slow for many commits
        # This ensures the UI remains responsive during startup even for large repos
        def update_commits_ui():
            # set_commits now limits to 50 commits initially to prevent blocking
            self.commits_pane.set_commits(self.commits)
            self._update_commits_title()
            if self.commits:
                self.selected_commit_index = 0
                # Reset the last index tracker so the first commit shows
                self.commits_pane._last_index = None
                # Ensure the ListView selection and highlighting match our index
                self.commits_pane.index = 0
                self.commits_pane.highlighted = 0
        
        # Always queue UI update to prevent blocking (even if on main thread, queue it)
        # This ensures the UI remains responsive during startup
        def update_with_highlighting():
            update_commits_ui()
            # Apply highlighting to first item (after commits are set)
            if self.commits:
                self.commits_pane._update_highlighting(0)
        
        if hasattr(self, '_ui_update_queue'):
            self._ui_update_queue.put(update_with_highlighting)
        else:
            # Fallback: use call_from_thread if queue not available
            import threading
            if threading.current_thread() is threading.main_thread():
                update_with_highlighting()
            else:
                self.call_from_thread(update_with_highlighting)
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
        # Debug logging removed - issue is fixed, no longer needed
        
        # Show current branch (matching lazygit behavior)
        branch_name = self.active_branch if self.active_branch else "HEAD"
        # CRITICAL: Always use self.total_commits if it's set (even if 0, to show correct count)
        # Only fall back to len(self.commits) if total_commits hasn't been set yet (still None/0 from initialization)
        # This ensures we show the correct count even if self.commits is limited for virtual scrolling
        if self.total_commits > 0:
            total_count = self.total_commits
            reason = "using total_commits (> 0)"
        elif hasattr(self, 'commits') and len(self.commits) > 0:
            # Fallback: use len(self.commits) only if total_commits not set yet
            total_count = len(self.commits)
            reason = f"fallback to len(commits)={len(self.commits)} (total_commits is 0)"
        else:
            # No commits loaded yet
            total_count = 0
            reason = "no commits loaded"
        
        # Show selected commit number (1-indexed) of total commits
        # Use selected_commit_index + 1 for 1-indexed display, or 1 if no selection
        selected_number = (self.selected_commit_index + 1) if self.selected_commit_index >= 0 and self.commits else 1
        
        # Debug logging removed - issue is fixed
        
        self.commits_pane.border_title = f"Commits ({branch_name}) {selected_number} of {total_count}"
    
    def _update_commits_count_ui(self, count: int) -> None:
        """Update UI to reflect commit count changes (called from background thread)."""
        # Debug logging removed - issue is fixed
        old_total = self.total_commits
        
        # CRITICAL: Only update if the new count is greater than current, or if current is 0
        # This prevents overwriting a correct count (62) with a stale cached value (2)
        # The count from load_commits() (len(commits)) is more accurate than the cached value
        if count > self.total_commits or self.total_commits == 0:
            self.total_commits = count
        else:
            # Ignore lower count - keep the higher value (from load_commits())
            return  # Don't update title if we're ignoring the count
        self._update_commits_title()
    
    def _update_commits_push_status_ui(self, commits: list[CommitInfo]) -> None:
        """Update UI to reflect push status changes (called from background thread)."""
        # Update push status in place without clearing (prevents flicker during virtual scrolling)
        if commits and len(commits) > 0:
            # Find matching commits in self.commits and update their push AND merged status
            commit_shas = {c.sha: c for c in commits}
            updated_count = 0
            pushed_count_in_self = 0
            merged_count_in_self = 0
            unpushed_count_in_self = 0
            for commit in self.commits:
                if commit.sha in commit_shas:
                    # Update both pushed and merged status (required for three-tier display)
                    commit.pushed = commit_shas[commit.sha].pushed
                    commit.merged = commit_shas[commit.sha].merged
                    updated_count += 1
                    if commit.merged:
                        merged_count_in_self += 1
                    elif commit.pushed:
                        pushed_count_in_self += 1
                    else:
                        unpushed_count_in_self += 1
            
            _log_timing_message(f"[DEBUG] _update_commits_push_status_ui: Updated {updated_count}/{len(self.commits)} commits in self.commits: {merged_count_in_self} merged (✓ green), {pushed_count_in_self} pushed (↑ yellow), {unpushed_count_in_self} unpushed (- red)")
            
            # Update the commits pane display in place (no clearing)
            self.commits_pane.update_push_status_in_place(commits)

    def load_more_commits(self) -> None:
        """Load more commits for the current branch (matching lazygit behavior)."""
        import subprocess
        
        # If searching, don't load more - we're filtering existing commits
        if self._search_query:
            return
        if not self.active_branch:
            return
        if self.loaded_commits >= self.total_commits:
            return
        
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
                                # Lazygit's EXACT logic from commit_loader.go line 119 (same as in load_commits):
                                # unpushedCommitHashes = self.getReachableHashes(
                                #     opts.RefForPushedStatus.FullRefName(),
                                #     append([]string{opts.RefForPushedStatus.RefName() + "@{u}"}, mainBranches...)
                                # )
                                
                                # Get branch short name for @{u} (Lazygit uses RefName(), not FullRefName())
                                branch_name_short = actual_ref.replace("refs/heads/", "")
                                if branch_name_short == "HEAD":
                                    # For HEAD, get the actual branch name
                                    head_result = subprocess.run(
                                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                        capture_output=True,
                                        text=True,
                                        timeout=2,
                                        cwd=repo_path_str
                                    )
                                    if head_result.returncode == 0:
                                        branch_name_short = head_result.stdout.strip()
                                
                                # Lazygit uses: branch_name@{u} as the first not_ref
                                upstream_ref = f"{branch_name_short}@{{u}}"
                                not_refs = [upstream_ref] + main_branches
                                
                                # This is Lazygit's exact command - it will fail if @{u} doesn't exist
                                unpushed_cmd = ["git", "rev-list", actual_ref]
                                for not_ref in not_refs:
                                    unpushed_cmd.extend(["--not", not_ref])
                                
                                unpushed_result = subprocess.run(
                                    unpushed_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                    cwd=repo_path_str
                                )
                                rev_list_elapsed = time.perf_counter() - rev_list_start
                                
                                if unpushed_result.returncode == 0:
                                    # Command succeeded - we have unpushed commits
                                    for sha in unpushed_result.stdout.strip().split("\n"):
                                        if sha.strip():
                                            unpushed_commits.add(sha.strip())
                                    self._remote_commits_cache[cache_key] = unpushed_commits
                                    cache_reason = "INVALIDATED" if cache_invalidated_remote_branch else "MISS"
                                    _log_timing_message(f"[CACHE] {cache_reason} unpushed_commits_cache for {actual_ref} (load_more): fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s (using {upstream_ref})")
                                else:
                                    # Command failed (e.g., @{u} doesn't exist) - return empty set
                                    # Empty set means: unpushedCommitHashes.Includes(commit) = false
                                    # Therefore status = StatusPushed (YELLOW)
                                    unpushed_commits = set()  # Empty set = all commits are assumed pushed (yellow)
                                    self._remote_commits_cache[cache_key] = unpushed_commits
                                    _log_timing_message(f"[CACHE] MISS unpushed_commits_cache for {actual_ref} (load_more): {upstream_ref} doesn't exist, assuming all commits are pushed (yellow) - matching Lazygit behavior")
                            except Exception as e:
                                rev_list_elapsed = time.perf_counter() - rev_list_start
                                _log_timing_message(f"[TIMING] Error getting unpushed commits for {actual_ref} (load_more): {type(e).__name__}: {e} in {rev_list_elapsed:.4f}s")
                        
                        # Get merged commits from cache or fetch if needed
                        if self._merged_commits_cache:
                            # Use cached merged commits (fast!)
                            merged_commits = self._merged_commits_cache
                            _log_timing_message(f"[CACHE] HIT merged_commits_cache (load_more): {len(merged_commits)} merged commits")
                        else:
                            # Cache MISS - fetch merged commits from main branches
                            merged_commits = set()
                            if main_branches:
                                for main_branch in main_branches:
                                    # Fetch ALL commits from main/master (no limit)
                                    # This ensures commits beyond 1000 are correctly identified as merged
                                    merged_cmd = ["git", "rev-list", main_branch]
                                    merged_result = subprocess.run(
                                        merged_cmd,
                                        capture_output=True,
                                        text=True,
                                        timeout=10,  # Increased timeout for large repos
                                        cwd=repo_path_str
                                    )
                                    if merged_result.returncode == 0:
                                        for sha in merged_result.stdout.strip().split("\n"):
                                            if sha.strip():
                                                merged_commits.add(sha.strip())
                            # Cache the merged commits for future use
                            self._merged_commits_cache = merged_commits
                            _log_timing_message(f"[CACHE] MISS merged_commits_cache (load_more): fetched {len(merged_commits)} merged commits")
                        
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
                        
                        _log_timing_message(f"[DEBUG] Three-tier status (load_more, lazygit approach): {merged_count} merged (✓ green), {pushed_count} pushed (↑ yellow), {unpushed_count} unpushed (- red)")
                        
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
            from pathlib import Path
            diff_start = time.perf_counter()
            ci = self.commits[index]
            # Normalize SHA before using it
            normalized_sha = _normalize_commit_sha(ci.sha)
            
            # Use PTY streaming if enabled (default)
            if self.log_pane._use_pty_streaming:
                # Build git show command with colors
                cmd = ['git', 'show', '--color=always', '--stat', '--decorate', '-p', normalized_sha]
                repo_path = Path(self.repo_path) if hasattr(self, 'repo_path') else Path(".")
                self.log_pane._stream_git_command_pty(cmd, repo_path, self.patch_pane, update_interval=10)
            else:
                # Fallback to subprocess
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
    
    def show_stash_diff(self, index: int) -> None:
        """Show stash diff in patch pane when stash is selected."""
        if 0 <= index < len(self.stashes):
            from pathlib import Path
            stash = self.stashes[index]
            # Switch to patch view when stash is selected
            self._view_mode = "patch"
            self.log_pane.styles.display = "none"
            self.patch_pane.styles.display = "block"
            
            # Use PTY streaming if enabled (default)
            if self.log_pane._use_pty_streaming:
                try:
                    # Build git stash show command with colors (like Lazygit)
                    cmd = [
                        'git', 'stash', 'show',
                        '-p', '--stat', '-u',
                        '--color=always',
                        f'stash@{{{stash.index}}}'
                    ]
                    repo_path = Path(self.repo_path) if hasattr(self, 'repo_path') else Path(".")
                    prefix = f"stash@{stash.index}: On {stash.branch}: {stash.message}\n\n"
                    self.log_pane._stream_git_command_pty(cmd, repo_path, self.patch_pane, prefix=prefix, update_interval=10)
                except Exception as e:
                    from rich.text import Text
                    error_text = Text(f"Error loading stash diff: {type(e).__name__}: {e}", style="red")
                    self.patch_pane.update(error_text)
            else:
                # Fallback to subprocess
                try:
                    # Get stash diff using Python GitService
                    diff_text, stat_text = self.git.get_stash_diff(stash.index)
                    self.patch_pane.show_stash_info(stash, diff_text, stat_text)
                except Exception as e:
                    # If stash diff fetching fails, show error
                    from rich.text import Text
                    error_text = Text(f"Error loading stash diff: {type(e).__name__}: {e}", style="red")
                    self.patch_pane.update(error_text)
    
    def show_file_diff(self, file_path: str, staged: bool = False) -> None:
        """Show file diff in patch pane when file is selected."""
        from pathlib import Path
        # Use PTY streaming if enabled (default)
        if self.log_pane._use_pty_streaming:
            try:
                # Build git diff command with colors (like Lazygit)
                if staged:
                    cmd = ['git', 'diff', '--cached', '--color=always', '--', file_path]
                    prefix = f"Staged changes: {file_path}\n\n"
                else:
                    cmd = ['git', 'diff', '--color=always', '--', file_path]
                    prefix = f"Unstaged changes: {file_path}\n\n"
                
                repo_path = Path(self.repo_path) if hasattr(self, 'repo_path') else Path(".")
                self.log_pane._stream_git_command_pty(cmd, repo_path, self.patch_pane, prefix=prefix, update_interval=10)
            except Exception as e:
                from rich.text import Text
                error_text = Text(f"Error loading file diff: {type(e).__name__}: {e}", style="red")
                self.patch_pane.update(error_text)
        else:
            # Fallback to subprocess
            try:
                # Get file diff using GitService
                diff_text = self.git.get_file_diff(file_path, staged=staged)
                self.patch_pane.show_file_info(file_path, diff_text, staged=staged)
            except Exception as e:
                # If file diff fetching fails, show error
                from rich.text import Text
                error_text = Text(f"Error loading file diff: {type(e).__name__}: {e}", style="red")
                self.patch_pane.update(error_text)
    
    def _get_commits_render_to_main(self) -> callable:
        """Get callback for automatic commit patch updates (lazygit GetOnRenderToMain pattern)."""
        def render_to_main() -> None:
            if self.commits_pane.index is not None and 0 <= self.commits_pane.index < len(self.commits):
                self.show_commit_diff(self.commits_pane.index)
        return render_to_main
    
    def _get_stash_render_to_main(self) -> callable:
        """Get callback for automatic stash patch updates (lazygit GetOnRenderToMain pattern)."""
        def render_to_main() -> None:
            if self.stash_pane.index is not None and 0 <= self.stash_pane.index < len(self.stashes):
                self.show_stash_diff(self.stash_pane.index)
        return render_to_main
    
    def _get_staged_render_to_main(self) -> callable:
        """Get callback for automatic staged file patch updates (lazygit GetOnRenderToMain pattern)."""
        def render_to_main() -> None:
            if self.staged_pane.index is not None and 0 <= self.staged_pane.index < len(self.staged_pane._files):
                self.show_file_diff(self.staged_pane._files[self.staged_pane.index].path, staged=True)
        return render_to_main
    
    def _get_changes_render_to_main(self) -> callable:
        """Get callback for automatic unstaged file patch updates (lazygit GetOnRenderToMain pattern)."""
        def render_to_main() -> None:
            if self.changes_pane.index is not None and 0 <= self.changes_pane.index < len(self.changes_pane._files):
                self.show_file_diff(self.changes_pane._files[self.changes_pane.index].path, staged=False)
        return render_to_main
    
    def _get_tags_render_to_main(self) -> callable:
        """Get callback for automatic tag patch updates (lazygit GetOnRenderToMain pattern)."""
        def render_to_main() -> None:
            if self.tags_pane.index is not None and 0 <= self.tags_pane.index < len(self.tags_pane._tags):
                self.show_tag_info(self.tags_pane._tags[self.tags_pane.index].name)
        return render_to_main
    
    def _get_remotes_render_to_main(self) -> callable:
        """Get callback for automatic remote patch updates (lazygit GetOnRenderToMain pattern)."""
        def render_to_main() -> None:
            if self.remotes_pane.index is not None and 0 <= self.remotes_pane.index < len(self.remotes_pane._remotes):
                self.show_remote_info(self.remotes_pane._remotes[self.remotes_pane.index].name)
        return render_to_main
    
    def show_tag_info(self, tag_name: str) -> None:
        """Show tag info and git log graph in patch pane when tag is selected.
        Uses non-PTY approach (like Lazygit's RunCommandTaskWithPrefix) for better performance during scrolling.
        """
        from pathlib import Path
        import subprocess
        import threading
        import os
        from rich.text import Text
        from rich.console import Group
        
        # Switch to patch view when tag is selected
        self._view_mode = "patch"
        self.log_pane.styles.display = "none"
        self.patch_pane.styles.display = "block"
        
        def load_tag_info_in_background():
            """Load tag info and git log in background thread (non-blocking, like Lazygit)."""
            # Wait for UI to be ready (like other background operations)
            if hasattr(self, '_ui_ready'):
                self._ui_ready.wait()
            
            try:
                _log_timing_message(f"[TAG_INFO] Loading tag info for {tag_name} in background thread")
                
                # Check if tag is annotated
                is_annotated = self.git.is_tag_annotated(tag_name)
                _log_timing_message(f"[TAG_INFO] Tag {tag_name} is annotated: {is_annotated}")
                
                # Build tag info prefix
                if is_annotated:
                    annotation_info = self.git.get_tag_annotation_info(tag_name)
                    prefix = f"Annotated tag: {tag_name}\n\n"
                    if annotation_info:
                        prefix += annotation_info + "\n\n"
                    prefix += "---\n\n"
                else:
                    prefix = f"Lightweight tag: {tag_name}\n\n---\n\n"
                
                # Build git log graph command (like Lazygit's GetGraphCmdObj)
                # Limit output to 200 commits to prevent huge outputs and encoding issues
                repo_path = Path(self.repo_path) if hasattr(self, 'repo_path') else Path(".")
                cmd = [
                    'git', 'log',
                    '--graph',
                    '--color=always',
                    '--abbrev-commit',
                    '--decorate',
                    '--date=relative',
                    '--pretty=medium',
                    '-200',  # Limit to 200 commits (like Lazygit limits commits pane)
                    f'refs/tags/{tag_name}',
                    '--'
                ]
                
                _log_timing_message(f"[TAG_INFO] Running git log command for tag {tag_name} (limited to 200 commits)")
                
                # Run command and collect output (non-PTY, like Lazygit's RunCommandTaskWithPrefix)
                # Use errors='replace' to handle binary data in commit messages
                process = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors='replace',  # Replace invalid UTF-8 bytes instead of failing
                    timeout=20,  # Timeout for large repos
                    cwd=str(repo_path),
                    env={**os.environ, 'TERM': 'dumb', 'GIT_PAGER': 'cat'}
                )
                
                _log_timing_message(f"[TAG_INFO] Git log command completed: returncode={process.returncode}, stdout_len={len(process.stdout) if process.stdout else 0}")
                
                # Build content with prefix + git log output
                lines = [Text(prefix)]
                
                if process.returncode == 0 and process.stdout:
                    # Parse ANSI output to Rich Text
                    line_count = 0
                    for line in process.stdout.split('\n'):
                        if line.strip():
                            try:
                                rich_line = Text.from_ansi(line)
                                lines.append(rich_line)
                                line_count += 1
                            except Exception:
                                # Fallback: plain text if ANSI parsing fails
                                lines.append(Text(line, style="white"))
                                line_count += 1
                    _log_timing_message(f"[TAG_INFO] Parsed {line_count} lines from git log output")
                else:
                    # Show error if command failed
                    error_msg = process.stderr if process.stderr else f"Git command failed with return code {process.returncode}"
                    _log_timing_message(f"[TAG_INFO] Git command failed: {error_msg}")
                    lines.append(Text(f"Error: {error_msg}", style="red"))
                
                # Update UI from main thread (single update, no frequent updates during scroll)
                # Capture lines in closure to ensure they're available when update_ui is called
                lines_to_display = lines.copy()
                def update_ui():
                    try:
                        _log_timing_message(f"[TAG_INFO] Updating UI with {len(lines_to_display)} lines")
                        full_content = Group(*lines_to_display)
                        patch_pane_ref.update(full_content)
                        _log_timing_message(f"[TAG_INFO] UI update completed successfully")
                    except Exception as e:
                        import traceback
                        _log_timing_message(f"[ERROR] Error updating tag info UI: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                        error_text = Text(f"Error loading tag info: {type(e).__name__}: {e}", style="red")
                        patch_pane_ref.update(error_text)
                
                # Queue UI update (non-blocking) - use the same pattern as other background operations
                _log_timing_message(f"[TAG_INFO] Queueing UI update (has _ui_update_queue: {hasattr(self, '_ui_update_queue')})")
                
                # Use UI update queue (same pattern as load_tags_background, load_remotes_background, etc.)
                if hasattr(self, '_ui_update_queue') and self._ui_update_queue is not None:
                    try:
                        self._ui_update_queue.put(update_ui)
                        _log_timing_message(f"[TAG_INFO] UI update queued successfully")
                    except Exception as queue_error:
                        _log_timing_message(f"[ERROR] Failed to queue UI update: {type(queue_error).__name__}: {queue_error}")
                        # Fallback: use Textual's call_from_thread if available
                        try:
                            if hasattr(self, 'call_from_thread'):
                                self.call_from_thread(update_ui)
                            else:
                                # Use call_later as fallback
                                self.call_later(update_ui)
                        except Exception as fallback_error:
                            _log_timing_message(f"[ERROR] Fallback UI update failed: {type(fallback_error).__name__}: {fallback_error}")
                else:
                    # No queue available, use Textual's thread-safe method
                    _log_timing_message(f"[TAG_INFO] No UI update queue, using Textual call_later")
                    try:
                        self.call_later(update_ui)
                    except Exception as call_error:
                        _log_timing_message(f"[ERROR] call_later failed: {type(call_error).__name__}: {call_error}")
                        # Last resort: try direct call (might fail if not on main thread)
                        try:
                            update_ui()
                        except Exception as direct_error:
                            _log_timing_message(f"[ERROR] Direct UI update failed: {type(direct_error).__name__}: {direct_error}")
                        
            except Exception as e:
                import traceback
                error_type = type(e).__name__
                error_str = str(e)
                error_msg = f"[ERROR] Error loading tag info: {error_type}: {error_str}\n{traceback.format_exc()}"
                _log_timing_message(error_msg)
                
                # Capture error info in closure to avoid NameError
                error_type_captured = error_type
                error_str_captured = error_str
                def show_error():
                    error_text = Text(f"Error loading tag info: {error_type_captured}: {error_str_captured}", style="red")
                    patch_pane_ref.update(error_text)
                
                if hasattr(self, '_ui_update_queue') and self._ui_update_queue is not None:
                    try:
                        self._ui_update_queue.put(show_error)
                    except Exception:
                        if hasattr(self, 'call_from_thread'):
                            self.call_from_thread(show_error)
                        else:
                            show_error()
                else:
                    if hasattr(self, 'call_from_thread'):
                        self.call_from_thread(show_error)
                    else:
                        show_error()
        
        # Show loading indicator immediately
        self.patch_pane.update(Text(f"Loading tag info for {tag_name}...", style="dim white"))
        
        # Capture patch_pane reference to ensure it's available in the background thread
        patch_pane_ref = self.patch_pane
        
        # Load in background thread (non-blocking, like Lazygit)
        thread = threading.Thread(target=load_tag_info_in_background, daemon=True)
        thread.start()
    
    def show_remote_info(self, remote_name: str) -> None:
        """Show remote info (name and URLs) in patch pane when remote is selected."""
        # Switch to patch view when remote is selected
        self._view_mode = "patch"
        self.log_pane.styles.display = "none"
        self.patch_pane.styles.display = "block"
        
        try:
            # Get remote URLs
            urls = self.git.get_remote_urls(remote_name)
            
            # Build remote info display (like Lazygit)
            from rich.text import Text
            info_text = Text()
            info_text.append(remote_name, style="green")
            info_text.append("\nUrls:\n", style="white")
            
            if urls:
                for url in urls:
                    info_text.append(url, style="white")
                    info_text.append("\n", style="white")
            else:
                info_text.append("No URLs configured", style="dim white")
            
            self.patch_pane.update(info_text)
        except Exception as e:
            from rich.text import Text
            error_text = Text(f"Error loading remote info: {type(e).__name__}: {e}", style="red")
            self.patch_pane.update(error_text)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is self.branches_pane:
            index = event.index
            if 0 <= index < len(self.branches):
                self.active_branch = self.branches[index].name
                # Switch to log view when branch is selected
                self._view_mode = "log"
                self.patch_pane.styles.display = "none"
                self.log_pane.styles.display = "block"
                # Load commits for the selected branch (matching lazygit - shows branch-specific commits)
                self.load_commits(self.active_branch)
                # Load commits with full history for feature branches (for log pane)
                self.load_commits_for_log(self.active_branch)
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
        
        # Handle scroll for tags pane (virtual scrolling)
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
                if max_scroll_y > 0 and self.tags_pane._total_tags_count > 0:
                    scroll_percent = scroll_y / max_scroll_y if max_scroll_y > 0 else 0
                    
                    # If scrolled near bottom (85%), auto-load more tags
                    if scroll_percent >= 0.85 and self.tags_pane._loaded_tags_count < self.tags_pane._total_tags_count:
                        _log_timing_message(f"[TIMING] [SCROLL] Tags pane: Loading more tags (scroll_percent={scroll_percent:.2f}, loaded={self.tags_pane._loaded_tags_count}, total={self.tags_pane._total_tags_count})")
                        self.load_more_tags()
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


