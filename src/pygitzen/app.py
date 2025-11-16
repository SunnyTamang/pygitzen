from __future__ import annotations

import time
import queue
from functools import wraps
from pathlib import Path

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
        
        # Create a map of normalized SHA to push status for quick lookup
        push_status_map = {}
        for commit in commits:
            commit_sha = _normalize_commit_sha(commit.sha)
            push_status_map[commit_sha] = commit.pushed
        
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
                
                # Get push status from map
                if normalized_stored_sha not in push_status_map:
                    continue
                
                pushed_status = push_status_map[normalized_stored_sha]
                
                # Get commit info from stored map (we have the commit message here)
                commit_info = self._commit_info_map.get(stored_sha)
                if not commit_info:
                    continue
                
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
                    if commit_info.merged:
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
                    self._show_native_git_log(branch, branch_info, git_service, append=append)
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
            
            # Convert each line from ANSI to Rich Text
            # Process in batches for better performance
            for line in output_lines:
                if line:  # Only process non-empty lines
                    try:
                        rich_line = parse_ansi_to_rich_text(line)
                        new_log_lines.append(rich_line)
                    except Exception:
                        # If parsing fails, strip ANSI and add as plain text
                        from pygitzen.git_graph import strip_ansi_codes
                        plain_line = strip_ansi_codes(line)
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
            # Use branch name or HEAD if branch is not available
            ref_spec = branch if branch else "HEAD"
            cmd = [
                "git", "log",
                ref_spec,  # Current branch (matching lazygit - shows branch-specific commits)
                "--oneline",  # Match lazygit
                f"--max-count={self.page_size}",  # Keep our limit of 200
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
            _log_timing_message(f"[DEBUG] load_commits: git log returncode={result.returncode}, stdout_lines={len(result.stdout.strip().split(chr(10))) if result.stdout else 0}")
            print(f"[DEBUG] load_commits: git log returncode={result.returncode}, stdout_lines={len(result.stdout.strip().split(chr(10))) if result.stdout else 0}")
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
                            merged_cmd = ["git", "rev-list", main_branch, "--max-count=1000"]
                            merged_result = subprocess.run(
                                merged_cmd,
                                capture_output=True,
                                text=True,
                                timeout=3,
                                cwd=repo_path_str
                            )
                            if merged_result.returncode == 0:
                                for sha in merged_result.stdout.strip().split("\n"):
                                    if sha.strip():
                                        merged_commits.add(sha.strip())
                    except Exception:
                        pass
                
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
                    # Assume commits NOT on main are pushed (yellow) until background thread determines otherwise
                    # This matches lazygit behavior
                    for commit in commits:
                        normalized_sha = _normalize_commit_sha(commit.sha)
                        commit.merged = normalized_sha in normalized_merged
                        # Assume pushed if not merged (will be corrected by background thread if wrong)
                        commit.pushed = normalized_sha not in normalized_merged
            
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
                                            _log_timing_message(f"[CACHE] MISS unpushed_commits_cache for {actual_ref}: fetched {len(unpushed_commits)} unpushed commits in {rev_list_elapsed:.4f}s (no @{u}, using {upstream_branch})")
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
                    merged_commits = set()
                    if main_branches:
                        for main_branch in main_branches:
                            merged_cmd = ["git", "rev-list", main_branch, "--max-count=1000"]
                            merged_result = subprocess.run(
                                merged_cmd,
                                capture_output=True,
                                text=True,
                                timeout=5,
                                cwd=repo_path_str
                            )
                            if merged_result.returncode == 0:
                                for sha in merged_result.stdout.strip().split("\n"):
                                    if sha.strip():
                                        merged_commits.add(sha.strip())
                    
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
        
        # Debug: log how many commits were loaded
        _log_timing_message(f"[DEBUG] load_commits: Loaded {len(loaded_commits)} commits from all branches")
        print(f"[DEBUG] load_commits: Loaded {len(loaded_commits)} commits from all branches, total_commits={self.total_commits}")
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self._filter_commits_by_search(self.all_commits, self._search_query)
        else:
            self.commits = loaded_commits
        
        self.loaded_commits = len(self.commits)
        
        # Debug: log before setting commits
        _log_timing_message(f"[DEBUG] load_commits: Setting {len(self.commits)} commits to commits_pane")
        print(f"[DEBUG] load_commits: Setting {len(self.commits)} commits to commits_pane")
        
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
        total_count = self.total_commits if self.total_commits > 0 else len(self.commits)
        self.commits_pane.border_title = f"Commits ({branch_name}) {len(self.commits)} of {total_count}"
    
    def _update_commits_count_ui(self, count: int) -> None:
        """Update UI to reflect commit count changes (called from background thread)."""
        self.total_commits = count
        self._update_commits_title()
    
    def _update_commits_push_status_ui(self, commits: list[CommitInfo]) -> None:
        """Update UI to reflect push status changes (called from background thread)."""
        # Update push status in place without clearing (prevents flicker during virtual scrolling)
        if commits and len(commits) > 0:
            # Find matching commits in self.commits and update their push status
            commit_shas = {c.sha: c for c in commits}
            updated_count = 0
            pushed_count_in_self = 0
            for commit in self.commits:
                if commit.sha in commit_shas:
                    commit.pushed = commit_shas[commit.sha].pushed
                    updated_count += 1
                    if commit.pushed:
                        pushed_count_in_self += 1
            
            _log_timing_message(f"[DEBUG] _update_commits_push_status_ui: Updated {updated_count}/{len(self.commits)} commits in self.commits, {pushed_count_in_self} marked as pushed")
            
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
                        merged_commits = set()
                        if main_branches:
                            for main_branch in main_branches:
                                merged_cmd = ["git", "rev-list", main_branch, "--max-count=1000"]
                                merged_result = subprocess.run(
                                    merged_cmd,
                                    capture_output=True,
                                    text=True,
                                    timeout=5,
                                    cwd=repo_path_str
                                )
                                if merged_result.returncode == 0:
                                    for sha in merged_result.stdout.strip().split("\n"):
                                        if sha.strip():
                                            merged_commits.add(sha.strip())
                        
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
            diff_start = time.perf_counter()
            ci = self.commits[index]
            get_diff_start = time.perf_counter()
            # Normalize SHA before using it
            normalized_sha = _normalize_commit_sha(ci.sha)
            diff = self.git.get_commit_diff(normalized_sha)
            get_diff_elapsed = time.perf_counter() - get_diff_start
            _log_timing_message(f"[TIMING] get_commit_diff: {get_diff_elapsed:.4f}s (commit: {normalized_sha[:8]})")
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


