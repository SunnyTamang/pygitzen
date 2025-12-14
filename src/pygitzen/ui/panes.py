"""UI panes module for pygitzen.

Contains all pane widgets extracted from app.py for separation of concerns.
"""

from __future__ import annotations

import time
import subprocess
import re
from pathlib import Path
from typing import Optional

from textual.widgets import ListView, Static, Input, ListItem
from textual.binding import Binding
from rich.text import Text
from rich.console import Group
from rich.syntax import Syntax

from ..git_service import GitService, BranchInfo, CommitInfo, FileStatus, StashInfo, TagInfo
from ..config import KeybindingConfig

# Import git_graph utilities if available
try:
    from ..git_graph import parse_ansi_to_rich_text, strip_ansi_codes, convert_graph_prefix_to_rich
except ImportError:
    # Fallback if git_graph not available
    def parse_ansi_to_rich_text(line): return Text(line)
    def strip_ansi_codes(text): return text
    def convert_graph_prefix_to_rich(text): return Text(text)

# This will help load the custom/default keybindings on module level 
_keybinding_config = KeybindingConfig()
_BRANCHES_BINDINGS = _keybinding_config.get_bindings("branches")
_COMMITS_BINDINGS = _keybinding_config.get_bindings("commits")

# Helper functions
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
    # DISABLED: Timing logs commented out for main branch
    # This is a no-op function for now
    pass



# StatusPane
class StatusPane(Static):
    """Status pane showing current branch and repo info."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Status"
    
    def update_status(self, branch: str, repo_path: str | Path, sync_status: dict | None = None) -> None:
        """Update status pane with branch info and optional sync status.
        
        Args:
            branch: Branch name
            repo_path: Repository path (str or Path object)
            sync_status: Optional dict with 'behind', 'ahead', 'synced', 'upstream' keys
        """
        from rich.text import Text
        from pathlib import Path
        
        # Handle both str and Path objects
        if isinstance(repo_path, Path):
            repo_name = repo_path.name
        else:
            repo_name = str(repo_path).split('/')[-1]
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




# StagedPane
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




# ChangesPane
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




# BranchesPane
class BranchesPane(ListView):
    """Branches pane showing local branches."""

    ### Setting up the branch pane wise keybindings 
    # BINDINGS = [
    #     Binding("c", "checkout", "Checkout"),
    #     Binding("space", "select", "Select"),
    #     Binding("enter", "select", "Select"),
    #     Binding("n", "new_branch", "New"),
    #     Binding("d", "delete_branch", "Delete"),
    #     Binding("r", "rename_branch", "Rename"),
    #     Binding("m", "merge_branch", "Merge"),
    #     Binding("p", "push_branch", "Push"),
    #     Binding("u", "set_upstream", "Upstream"),
    # ]
    BINDINGS=_BRANCHES_BINDINGS
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Local branches"
    
    def action_new_branch(self) -> None:
        """Delegate new_branch action to the app.
        
        This allows the action to be found when the branches pane has focus.
        Textual will look for actions on the widget first, then walk up to the app.
        """
        # Get the app instance and call its action
        app = self.app
        if app and hasattr(app, 'action_new_branch'):
            app.action_new_branch()
    
    def action_delete_branch(self) -> None:
        """Delegate delete_branch action to the app.
        
        This allows the action to be found when the branches pane has focus.
        Textual will look for actions on the widget first, then walk up to the app.
        """
        # Get the app instance and call its action
        app = self.app
        if app and hasattr(app, 'action_delete_branch'):
            app.action_delete_branch()
    
    def action_rename_branch(self) -> None:
        """Delegate rename_branch action to the app.
        
        This allows the action to be found when the branches pane has focus.
        Textual will look for actions on the widget first, then walk up to the app.
        """
        # Get the app instance and call its action
        app = self.app
        if app and hasattr(app, 'action_rename_branch'):
            app.action_rename_branch()
    
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




# RemotesPane
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




# TagsPane
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




# CommitsPane
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
        
        # Store commit SHAs and commit info for in-place updates
        self._commit_shas = []
        self._commit_info_map = {}  # SHA -> CommitInfo for quick lookup
        
        # Virtual scrolling: limit initial commits to 200 for performance
        # ListView has built-in virtual scrolling, but we still need to limit initial DOM elements
        initial_limit = 200
        commits_to_render = commits[:initial_limit] if len(commits) > initial_limit else commits
        
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
            
            self.append(ListItem(Static(text)))
    
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




# StashPane
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




# CommitSearchInput
class CommitSearchInput(Input):
    """Search input for filtering commits by message."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.placeholder = "Search commits... (fuzzy search)"
        self.border_title = "Search"
        




# LogPane
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
                    
                    # Pass git_service directly to _show_native_git_log (it should already have repo_path)
                    # Don't validate path existence here - let git command handle it (it will fail gracefully)
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
        """
        from rich.text import Text
        from rich.console import Group
        from pathlib import Path
        import subprocess
        from pygitzen.git_graph import parse_ansi_to_rich_text
        
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
                timeout=5  # Short timeout for fast feedback
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
            
            # Deduplicate merge commits - track seen commit SHAs to avoid showing same commit twice
            seen_commit_shas = set()
            skip_until_empty = False
            
            # Convert each line from ANSI to Rich Text
            # Process in batches for better performance
            for line in output_lines:
                if not line:  # Empty line
                    if skip_until_empty:
                        # End of duplicate commit block - stop skipping
                        skip_until_empty = False
                    new_log_lines.append(Text())
                    continue
                
                if skip_until_empty:
                    # Skip lines until we hit an empty line (end of commit block)
                    continue
                
                # Check if this is a commit line (contains "commit" followed by SHA)
                from pygitzen.git_graph import strip_ansi_codes
                plain_line = strip_ansi_codes(line)
                
                # Match: "commit" followed by whitespace and then a hex SHA (7-40 chars)
                import re
                commit_match = re.search(r'\bcommit\s+([0-9a-f]{7,40})', plain_line, re.IGNORECASE)
                
                if commit_match:
                    commit_sha = commit_match.group(1)
                    # Check if we've seen this commit SHA before
                    if commit_sha in seen_commit_shas:
                        # Duplicate commit - skip this entire commit block
                        skip_until_empty = True
                        continue
                    else:
                        # New commit - mark as seen and add the line
                        seen_commit_shas.add(commit_sha)
                
                # Add the line (either commit line or part of commit block)
                try:
                    rich_line = parse_ansi_to_rich_text(line)
                    new_log_lines.append(rich_line)
                except Exception:
                    new_log_lines.append(Text(plain_line, style="white"))
            
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
            
            # Update the pane
            if self._native_git_log_lines:
                full_content = Group(*self._native_git_log_lines)
                self.update(full_content)
            else:
                self.update(Text())
            
            # Update cache
            self._cached_branch = branch
            self._cached_branch_info = branch_info.copy()
            
        except Exception as e:
            # On error, show error message
            error_text = Text()
            error_text.append(f"Error showing native git log: {e}\n", style="red")
            self.update(error_text)
        finally:
            self._native_git_log_loading = False
    
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




# PatchPane
class PatchPane(Static):
    """Patch pane showing commit details and diff."""
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Patch"
    
    def show_commit_info(self, commit: CommitInfo, diff_text: str, git_service=None) -> None:
        """
        Show commit info in patch pane using LazyGit approach: git show --stat -p <hash>
        This single command provides: commit header, full message, diffstat, and diff.
        """
        from rich.text import Text
        from rich.syntax import Syntax
        from rich.console import Group
        import subprocess
        import sys
        
        # Normalize SHA format (fix for Cython version hex-encoded ASCII issue)
        commit_sha = _normalize_commit_sha(commit.sha)
        
        # Use LazyGit approach: single git show --stat -p command
        # This provides everything: commit header, full message, diffstat, and diff
        if git_service is None:
            # Fallback to basic display if no git_service
            header_text_obj = Text()
            header_text_obj.append(f"commit {commit_sha}\n", style="white")
            header_text_obj.append(f"Author: {commit.author}\n", style="white")
            header_text_obj.append(f"\n{commit.summary}\n\n", style="white")
            if diff_text:
                try:
                    syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
                    full_content = Group(header_text_obj, syntax)
                except:
                    diff_text_obj = Text(diff_text, style="white")
                    full_content = header_text_obj + diff_text_obj
            else:
                full_content = header_text_obj
            self.update(full_content)
            return
        
        try:
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
            
            # Convert to Path object if we got something
            if repo_path:
                from pathlib import Path
                if isinstance(repo_path, str):
                    repo_path = Path(repo_path)
                elif not isinstance(repo_path, Path):
                    repo_path = Path(str(repo_path))
            else:
                # Last resort: try to get from app instance via widget tree
                from pathlib import Path
                try:
                    # Try to access app instance through widget tree
                    app = self.app
                    if app and hasattr(app, 'repo_path'):
                        repo_path_value = app.repo_path
                        if repo_path_value:
                            if isinstance(repo_path_value, str):
                                repo_path = Path(repo_path_value)
                            elif isinstance(repo_path_value, Path):
                                repo_path = repo_path_value
                            else:
                                repo_path = Path(str(repo_path_value))
                        else:
                            repo_path = Path(".")
                    else:
                        repo_path = Path(".")
                except:
                    # Final fallback to current directory (shouldn't happen in normal usage)
                    repo_path = Path(".")
            
            # Use git show --stat --decorate -p (LazyGit approach)
            # --stat: shows diffstat
            # --decorate: shows branch and tag refs in commit header
            # -p: shows full patch/diff
            # --no-color: avoid ANSI codes (we'll use Rich for syntax highlighting)
            result = subprocess.run(
                ['git', 'show', '--stat', '--decorate', '-p', '--no-color', commit_sha],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=str(repo_path)
            )
            
            if result.returncode != 0:
                # If git show fails, fallback to basic display
                fallback_text = Text()
                fallback_text.append(f"commit {commit_sha}\n", style="white")
                fallback_text.append(f"Author: {commit.author}\n", style="white")
                fallback_text.append(f"\n{commit.summary}\n\n", style="white")
                if diff_text:
                    fallback_text.append(diff_text[:1000], style="white")
                self.update(fallback_text)
                return
            
            output = result.stdout
            
            if not output or len(output.strip()) == 0:
                # Empty output - show error
                error_text = Text()
                error_text.append(f"commit {commit_sha}\n", style="white")
                error_text.append(f"Author: {commit.author}\n", style="white")
                error_text.append(f"\n{commit.summary}\n\n", style="white")
                error_text.append("Error: git show returned empty output\n", style="red")
                self.update(error_text)
                return
            
            # Display the git show output with proper formatting
            # Convert output to Text object line by line to preserve all content
            display_text = Text()
            
            # Process all lines from git show output
            for line in output.split('\n'):
                # Basic color coding for better readability
                # Check for file path markers FIRST (before generic +/- checks)
                if line.startswith('---') or line.startswith('+++'):
                    display_text.append(line + '\n', style="yellow")
                elif line.startswith('@@'):
                    display_text.append(line + '\n', style="blue")
                elif line.startswith('+') and not line.startswith('+++'):
                    # Check if this is a visual diffstat line (only +, -, and spaces)
                    stripped = line.strip()
                    if stripped and all(c in '+- ' for c in stripped):
                        # Visual diffstat line - color character by character
                        for char in line:
                            if char == '+':
                                display_text.append(char, style="green")
                            elif char == '-':
                                display_text.append(char, style="red")
                            else:
                                display_text.append(char, style="white")
                        display_text.append('\n')
                    else:
                        # Regular added code line
                        display_text.append(line + '\n', style="green")
                elif line.startswith('-') and not line.startswith('---'):
                    # Removed code line
                    display_text.append(line + '\n', style="red")
                elif line.startswith('commit '):
                    # Parse commit header line with colored refs (matching LazyGit)
                    # Format: "commit <hash> (HEAD -> branch, tag: v0.2.2, origin/main, main)"
                    # Colors: commit/hash/tag → yellow, HEAD -> → cyan, branch → green, origin/ → red
                    import re
                    
                    # Match: "commit <hash> (refs...)" or "commit <hash>" (no refs)
                    # Hash can be any length hex characters (case insensitive)
                    match = re.match(r'^(commit\s+[a-fA-F0-9]+)(\s*\((.*)\))?$', line)
                    if match:
                        # Commit and hash → yellow
                        display_text.append(match.group(1), style="yellow")
                        
                        # Parse refs inside parentheses if present
                        if match.group(3):  # Has refs
                            display_text.append(' (', style="yellow")
                            
                            refs_str = match.group(3)
                            if refs_str:
                                # Split by comma, but be careful with nested structures
                                refs = [r.strip() for r in refs_str.split(',')]
                                
                                for i, ref in enumerate(refs):
                                    if i > 0:
                                        display_text.append(', ', style="yellow")
                                    
                                    # Check for HEAD -> branch
                                    if ref.startswith('HEAD -> '):
                                        display_text.append('HEAD -> ', style="cyan")
                                        branch_name = ref[8:]  # Remove "HEAD -> "
                                        display_text.append(branch_name, style="green")
                                    elif ref == 'HEAD':
                                        display_text.append('HEAD', style="cyan")
                                    # Check for tag:
                                    elif ref.startswith('tag: '):
                                        display_text.append(ref, style="yellow")
                                    # Check for origin/ (remote branch)
                                    elif ref.startswith('origin/'):
                                        display_text.append(ref, style="red")
                                    # Local branch (default)
                                    else:
                                        display_text.append(ref, style="green")
                            
                            display_text.append(')', style="yellow")
                        
                        display_text.append('\n')
                    else:
                        # Fallback: just color entire line yellow
                        display_text.append(line + '\n', style="yellow")
                elif line.startswith('Merge:'):
                    display_text.append(line + '\n', style="yellow")
                elif line.startswith('Author:') or line.startswith('Date:'):
                    display_text.append(line + '\n', style="cyan")
                elif line.startswith('diff --git'):
                    display_text.append(line + '\n', style="cyan")
                elif line.startswith('index '):
                    display_text.append(line + '\n', style="dim white")
                else:
                    # Regular text (including commit message, diffstat file lines, summary lines, etc.)
                    # Check if this is a diffstat file line with + symbols (e.g., "file.py | 54 +++++")
                    if '|' in line and ('+' in line or '-' in line):
                        # Parse diffstat file line: file path part is white, + symbols are green, - symbols are red
                        parts = line.split('|')
                        if len(parts) == 2:
                            # First part (file path and count) is white
                            display_text.append(parts[0] + '|', style="white")
                            # Second part (the visual diffstat) - color + green and - red
                            for char in parts[1]:
                                if char == '+':
                                    display_text.append(char, style="green")
                                elif char == '-':
                                    display_text.append(char, style="red")
                                else:
                                    display_text.append(char, style="white")
                            display_text.append('\n')
                        else:
                            # Fallback: just append as white
                            display_text.append(line + '\n', style="white")
                    else:
                        # Regular text (commit message, diffstat summary, etc.)
                        display_text.append(line + '\n', style="white")
            
            # Update the patch pane with all content
            self.update(display_text)
            return
            # commit <hash> (optional refs)
            # Merge: <parent1> <parent2> (if merge commit)
            # Author: <author>
            # Date: <date>
            # (blank line)
            # <commit message (multiline, may be indented with 4 spaces)>
            # (blank line)
            # <diffstat>
            # (blank line)
            # <diff starting with "diff --git">
            
            commit_header_line = ""
            merge_line = None
            author_line = ""
            date_line = ""
            message_lines = []
            diffstat_lines = []
            diff_start_idx = -1
            
            i = 0
            # Parse commit header line (may include refs in parentheses)
            if i < len(lines) and lines[i].startswith('commit '):
                commit_header_line = lines[i]
                i += 1
            
            # Parse Merge: line if present
            if i < len(lines) and lines[i].startswith('Merge:'):
                merge_line = lines[i]
                i += 1
            
            # Parse Author: line
            if i < len(lines) and lines[i].startswith('Author:'):
                author_line = lines[i]
                i += 1
            
            # Parse Date: line
            if i < len(lines) and lines[i].startswith('Date:'):
                date_line = lines[i]
                i += 1
            
            # Skip blank line after Date
            if i < len(lines) and not lines[i].strip():
                i += 1
            
            # Extract commit message (until we hit "---" separator, diffstat, or diff)
            message_start = i
            while i < len(lines):
                line = lines[i]
                
                # Check for "---" separator (git show adds this before diffstat)
                if line.strip() == '---':
                    i += 1  # Skip the separator line
                    break
                
                # Check if this is the start of diffstat (file line with | or "X files changed")
                stripped = line.strip()
                if stripped and (('|' in stripped and ('+' in stripped or '-' in stripped)) or 'files changed' in line.lower()):
                    # Found diffstat start
                    break
                
                # Check if this is the start of actual diff
                if line.startswith('diff --git'):
                    diff_start_idx = i
                    break
                
                # This is part of the commit message
                # Strip leading 4 spaces if present (git show indents messages)
                if line.startswith('    '):
                    message_lines.append(line[4:])
                else:
                    message_lines.append(line)
                
                i += 1
            
            # Extract diffstat (if present)
            # After message extraction, we might be at "---" separator or already at diffstat
            if i < len(lines) and diff_start_idx == -1:
                # Skip "---" separator if we're at it
                if i < len(lines) and lines[i].strip() == '---':
                    i += 1
                
                # We're at diffstat, collect all diffstat lines until we hit the diff
                # Diffstat structure: file lines with |, then summary line with "files changed", then empty line, then diff
                while i < len(lines):
                    line = lines[i]
                    
                    # Check if this is the start of actual diff
                    if line.startswith('diff --git'):
                        diff_start_idx = i
                        break
                    
                    # Check if this is a diffstat line
                    stripped = line.strip()
                    is_diffstat_line = False
                    if stripped:
                        # File line with pipe and +/- signs
                        if '|' in stripped and ('+' in stripped or '-' in stripped):
                            is_diffstat_line = True
                        # Summary line (e.g., "1 file changed, 84 insertions(+), 23 deletions(-)")
                        elif 'files changed' in line.lower() or 'file changed' in line.lower():
                            is_diffstat_line = True
                    
                    if is_diffstat_line:
                        diffstat_lines.append(line)
                    elif not stripped:
                        # Empty line - check if next line is diff (if so, we're done) or more diffstat
                        if i + 1 < len(lines):
                            next_line = lines[i + 1]
                            if next_line.startswith('diff --git'):
                                # Next is diff, we're done with diffstat
                                i += 1
                                break
                            # Otherwise, continue to next line (might be summary line)
                    
                    i += 1
            
            # Extract diff (everything from diff_start_idx to end)
            diff_text_parsed = ""
            if diff_start_idx >= 0:
                diff_text_parsed = '\n'.join(lines[diff_start_idx:])
            
            # Build header with commit info
            header_text_obj = Text()
            
            # Commit header line
            if commit_header_line:
                header_text_obj.append(commit_header_line + "\n", style="white")
            else:
                header_text_obj.append(f"commit {commit_sha}\n", style="white")
            
            # Merge line if present
            if merge_line:
                header_text_obj.append(merge_line + "\n", style="white")
            
            # Author line
            if author_line:
                header_text_obj.append(author_line + "\n", style="white")
            else:
                header_text_obj.append(f"Author: {commit.author}\n", style="white")
            
            # Date line
            if date_line:
                header_text_obj.append(date_line + "\n", style="white")
            
            # Blank line before message
            header_text_obj.append("\n", style="white")
            
            # Full commit message (multiline)
            if message_lines:
                # Remove trailing empty lines from message
                while message_lines and not message_lines[-1].strip():
                    message_lines.pop()
                
                for msg_line in message_lines:
                    header_text_obj.append(msg_line, style="white")
                    header_text_obj.append("\n", style="white")
            else:
                # Fallback to summary if message extraction failed
                header_text_obj.append(commit.summary + "\n", style="white")
            
            # Blank line after message
            header_text_obj.append("\n", style="white")
            
            # Diffstat (if present)
            if diffstat_lines:
                for diffstat_line in diffstat_lines:
                    header_text_obj.append(diffstat_line, style="white")
                    header_text_obj.append("\n", style="white")
                header_text_obj.append("\n", style="white")
            
            # Diff content with manual color formatting (avoiding Group/Syntax for now)
            if diff_text_parsed:
                diff_lines = diff_text_parsed.split('\n')
                diff_text_obj = Text()
                for line in diff_lines:
                    if line.startswith('+'):
                        diff_text_obj.append(line + '\n', style="green")
                    elif line.startswith('-'):
                        diff_text_obj.append(line + '\n', style="red")
                    elif line.startswith('@@'):
                        diff_text_obj.append(line + '\n', style="blue")
                    elif line.startswith('diff --git'):
                        diff_text_obj.append(line + '\n', style="cyan")
                    elif line.startswith('index '):
                        diff_text_obj.append(line + '\n', style="dim white")
                    elif line.startswith('---') or line.startswith('+++'):
                        diff_text_obj.append(line + '\n', style="yellow")
                    else:
                        diff_text_obj.append(line + '\n', style="white")
                full_content = header_text_obj + diff_text_obj
            else:
                # No diff - just show header with message and diffstat
                full_content = header_text_obj
            
            # Always update with content
            self.update(full_content)
            
        except Exception as e:
            # If anything fails, fallback to basic display
            # Log error for debugging
            import sys
            import traceback
            print(f"[DEBUG] show_commit_info error: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            
            # Simple fallback that definitely works
            try:
                fallback_text = Text()
                fallback_text.append(f"commit {commit_sha}\n", style="white")
                fallback_text.append(f"Author: {commit.author}\n", style="white")
                fallback_text.append(f"\n{commit.summary}\n\n", style="white")
                if diff_text:
                    fallback_text.append(diff_text[:500], style="white")  # Limit to avoid huge content
                    if len(diff_text) > 500:
                        fallback_text.append("\n... (truncated)", style="dim white")
                self.update(fallback_text)
            except Exception as fallback_err:
                # Last resort - show error message
                error_text = Text()
                error_text.append(f"Error displaying commit: {fallback_err}\n", style="red")
                error_text.append(f"Commit: {commit_sha[:8]}\n", style="white")
                self.update(error_text)
    
    def show_stash_info(self, stash: StashInfo, diff_text: str, stat_text: str = "") -> None:
        """Show stash details and diff in the patch pane with proper color coding."""
        from rich.text import Text
        import re
        
        # Create stash header (matching commit format)
        full_content = Text()
        full_content.append(f"stash@{stash.index}: On {stash.branch}: {stash.message}\n", style="yellow")
        full_content.append("\n", style="white")
        
        # Strip ANSI codes from both stat and diff
        stat_text_clean = re.sub(r'\x1b\[[0-9;]*m', '', stat_text) if stat_text else ""
        diff_text_clean = re.sub(r'\x1b\[[0-9;]*m', '', diff_text) if diff_text else ""
        
        # Note: git stash show --stat returns only stat, git stash show -p returns only diff
        # They are separate, so no need to strip stat from diff_text
        
        # Add stat summary if available (with color coding, matching commit diff format)
        if stat_text_clean:
            # Process stat lines with proper coloring
            stat_lines = stat_text_clean.split('\n')
            for line in stat_lines:
                cleaned_line = line.lstrip()
                if not cleaned_line:
                    full_content.append("\n", style="white")
                    continue
                
                # Check if this is a diffstat file line with + symbols (e.g., "file.py | 54 +++++")
                if '|' in cleaned_line and ('+' in cleaned_line or '-' in cleaned_line):
                    # Parse diffstat file line: file path part is white, + symbols are green, - symbols are red
                    parts = cleaned_line.split('|')
                    if len(parts) == 2:
                        # First part (file path and count) is white
                        full_content.append(parts[0] + '|', style="white")
                        # Second part (the visual diffstat) - color + green and - red
                        for char in parts[1]:
                            if char == '+':
                                full_content.append(char, style="green")
                            elif char == '-':
                                full_content.append(char, style="red")
                            else:
                                full_content.append(char, style="white")
                        full_content.append("\n")
                    else:
                        # Fallback: just append as white
                        full_content.append(cleaned_line + "\n", style="white")
                elif 'files changed' in cleaned_line.lower() or 'file changed' in cleaned_line.lower():
                    # Summary line - color it white
                    full_content.append(cleaned_line + "\n", style="white")
                else:
                    # Regular stat line
                    full_content.append(cleaned_line + "\n", style="white")
            
            full_content.append("\n", style="white")
        
        # Display diff with manual color coding (same as commit diffs)
        if diff_text_clean:
            # Process diff line by line with proper color coding
            diff_lines = diff_text_clean.split('\n')
            for line in diff_lines:
                # Skip empty lines but preserve them
                if not line.strip():
                    full_content.append("\n", style="white")
                    continue
                
                # Apply color coding based on line content (matching commit diff logic)
                # Check for file path markers FIRST (before generic +/- checks)
                if line.startswith('---') or line.startswith('+++'):
                    full_content.append(line + '\n', style="yellow")
                elif line.startswith('@@'):
                    full_content.append(line + '\n', style="blue")
                elif line.startswith('+') and not line.startswith('+++'):
                    # Check if this is a visual diffstat line (only +, -, and spaces)
                    stripped = line.strip()
                    if stripped and all(c in '+- ' for c in stripped):
                        # Visual diffstat line - color character by character
                        for char in line:
                            if char == '+':
                                full_content.append(char, style="green")
                            elif char == '-':
                                full_content.append(char, style="red")
                            else:
                                full_content.append(char, style="white")
                        full_content.append('\n')
                    else:
                        # Regular added code line
                        full_content.append(line + '\n', style="green")
                elif line.startswith('-') and not line.startswith('---'):
                    # Removed code line
                    full_content.append(line + '\n', style="red")
                elif line.startswith('diff --git'):
                    full_content.append(line + '\n', style="cyan")
                elif line.startswith('index '):
                    full_content.append(line + '\n', style="dim white")
                else:
                    # Regular text line (including context lines)
                    full_content.append(line + '\n', style="white")
        else:
            # No diff available
            full_content.append("No diff available\n", style="dim white")
        
        self.update(full_content)




# CommandLogPane
class CommandLogPane(Static):
    """Command log pane showing tips and messages."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Command log"
        self.border_subtitle = self._get_version_footer()  # Will be set in on_mount when app is available
        self._last_message = ""  # Store last message for footer refresh
        # Initialize with default tips
        self.update_log("")
    
    def on_mount(self) -> None:
        """Called when widget is mounted - app is now accessible, set border subtitle."""
        # Now self.app is available, so we can get the version info
        self.border_subtitle = self._get_version_footer()
        #--------------------------------
        # Fuck it! I will set the style here.
        # One ket point I noticed [] these characters doesnot work for subtitles. Very weird.
        # If you clone my repo and run the app, you will see the difference.
        # Welcome in advance!
        # If returned as return f"\[{version_label}]" then it works
        # but if returned as return f"[{version_label}"] then it does not show.
        # I might check the docs later.
        #--------------------------------
        # Set style via styles (dimmed and gray color)
        self.styles.border_subtitle_style = "dim"
        self.styles.border_subtitle_color = "#888888"
    
    def _get_version_footer(self) -> str:
        """Get the version footer text (Cython/Python)."""
        try:
            app = self.app
            if app and hasattr(app, '_using_cython'):
                version_label = "Cython" if app._using_cython else "Python"
                return f"{version_label}"
        except (AttributeError, TypeError):
            pass
        return ""
    
    def update_log(self, message: str) -> None:
        from rich.text import Text
        self._last_message = message  # Store for potential refresh
        text = Text()
        text.append("You can hide/focus this panel by pressing '@'\n", style="white")
        text.append("Random tip: ", style="white")
        text.append("`git commit`", style="cyan")
        text.append(" is really just the programmer equivalent of saving your game.\n", style="white")
        text.append("Always do it before embarking on an ambitious change!\n", style="white")
        if message:
            text.append(f"\n{message}\n", style="white")

