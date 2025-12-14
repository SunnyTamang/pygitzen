from __future__ import annotations

import time
import queue
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

from .git_service import GitService, BranchInfo, CommitInfo, FileStatus, StashInfo, TagInfo
from .config import KeybindingConfig
from .services import BranchService, CommitService, TagService, StashService
from .handlers import BranchActionHandler
from .ui import (
    StatusPane, StagedPane, ChangesPane, BranchesPane,
    RemotesPane, TagsPane, CommitsPane, StashPane,
    LogPane, PatchPane, CommandLogPane, CommitSearchInput,
    NewBranchDialog, RenameBranchDialog, DeleteBranchDialog,
    SetUpstreamDialog, ConfirmDialog, UnboundActionsModal, AboutModal
)

# Helper functions moved to ui/panes.py
# Import them if needed for backward compatibility
# Note: These are used internally in app.py, imported directly from panes module
from .ui import panes
# Re-export for backward compatibility
format_recency = panes.format_recency
_normalize_commit_sha = panes._normalize_commit_sha
_log_timing_message = panes._log_timing_message

# Performance timing utilities
# DISABLED: Timing logs commented out for main branch

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

# All pane classes moved to ui/panes.py
# Removed: StatusPane, StagedPane, ChangesPane, BranchesPane, RemotesPane,
# TagsPane, CommitsPane, StashPane, CommitSearchInput, LogPane, PatchPane, CommandLogPane

# Load app-level bindings at module level (before class definition)
# This ensures Textual reads the correct bindings when the class is defined
_keybinding_config = KeybindingConfig()
_APP_BINDINGS = _keybinding_config.get_bindings("app")

class PygitzenApp(App):
    CSS_PATH = "styles/app.tcss"

    # BINDINGS loaded from config at module level
    BINDINGS = _APP_BINDINGS

    active_branch: reactive[str | None] = reactive(None)
    selected_commit_index: reactive[int] = reactive(0)

    def __init__(self, repo_dir: str = ".", use_cython: bool = True) -> None:
        import sys
        init_start = time.perf_counter()
        _log_timing_message(f"[TIMING] ===== PygitzenApp.__init__ START =====")
        
        # Initialize keybinding config for use in on_mount (for pane bindings)
        # Note: App-level bindings are already loaded at module level
        self.keybinding_config = KeybindingConfig()
        
        # Call super().__init__() - BINDINGS already set at class definition time
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
            
            # Initialize services
            self.repo_path = Path(repo_dir) if isinstance(repo_dir, str) else repo_dir
            self.branch_service = BranchService(self.git, self.repo_path)
            self.commit_service = CommitService(self.git)
            self.tag_service = TagService(self.git)
            self.stash_service = StashService(self.git)
            
            # Initialize action handlers
            self.branch_actions = BranchActionHandler(self)
            
            self.branches: list[BranchInfo] = []
            self.remotes: list[BranchInfo] = []
            self.tags: list[TagInfo] = []  # Tags for tags pane
            self.commits: list[CommitInfo] = []  # Commits for commits pane (left side)
            self.stashes: list[StashInfo] = []  # Stashes for stash pane
            self.all_commits: list[CommitInfo] = []  # Store all commits for search (commits pane)
            self.log_commits: list[CommitInfo] = []  # Commits for log pane (right side) - separate from commits pane
            self.page_size = 200  # For commits pane
            # Reasonable limit to prevent blocking (dulwich iteration is slow for 78k+ commits)
            self.log_initial_size = 200  # Load 200 commits initially (can load more via pagination)
            self.total_commits = 0
            self.loaded_commits = 0
            self._loading_commits = False
            self._loading_file_status = False
            self._loading_stashes = False
            self._loading_tags = False
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
            
            # Cache branch sync status (behind/ahead counts)
            self._branch_sync_status_cache: dict[str, dict] = {}

            # real-time change detection (GitWatcher)
            self._git_watcher = None # initialized during the app mount 
            
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
        
        # Note: Pane-specific bindings are now loaded at module level in panes.py
        # No need to set them here anymore
        
        # Check for unbound actions and show notification if any exist
        unbound = self.keybinding_config.get_unbound_actions("app")
        if unbound:
            # Show notification at top of screen
            self.notify(
                f"{len(unbound)} action(s) are unbound. Press 'u' for details.",
                title="Unbound Actions Detected",
                severity="warning",
                timeout=5.0  # Show for 5 seconds
            )
        
        # Initialize view mode - will be set by refresh_data_fast
        self._view_mode = "log"  # Default to log view (branch view)
        # self.refresh_data()
        self.refresh_data_fast()
        
        # Print Cython status to console for debugging (not shown in UI)
        version_info = " (Cython)" if self._using_cython else " (Python)"
        git_service_type = type(self.git).__name__
        print(f"[DEBUG] Cython status: {self._using_cython}, GitService type: {git_service_type}")
        
        # Set up periodic check for virtual scrolling expansion (fallback if scroll events don't fire)
        # This ensures virtual scrolling works even if scroll events aren't being captured
        # Check more frequently (0.2s) for more responsive virtual scrolling
        self.set_interval(0.2, self._check_virtual_scroll_expansion)
        self.set_interval(0.2, self._check_commits_pane_scroll)  # Check commits pane scrolling

        # Start GitWatcher for real-time change detection 
        self._start_git_watcher()
        
        # Set up periodic processing of UI update queue from background threads
        self.set_interval(0.05, self._process_ui_update_queue)  # Check every 50ms
        
        mount_elapsed = time.perf_counter() - mount_start
        _log_timing_message(f"[TIMING] ===== on_mount TOTAL: {mount_elapsed:.4f}s =====")

    def _start_git_watcher(self) -> None:
        """Start GitWatcher for real-time change detection"""
        try:
            from pygitzen.git_watcher import GitWatcher, ChangeEvent, ChangeType

            def handle_change(event: ChangeEvent) -> None:
                """Handle Git Repo chnange events"""

                # Refreshing only refresh affected panes
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

                # --------later will add other changes detection as well ---------------------

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

    def on_unmount(self) -> None:
        """Stop GitWatcher when app unmounts."""
        if self._git_watcher:
            self._git_watcher.stop()
            _log_timing_message("[GITWATCHER] Stopped")

        # unbound = self.keybinding_config.get_unbound_actions("app")
        # print(f"\n[UNBOUND KEYBINDINGS] Found {len(unbound)} unbound action(s):")
        # for action_info in unbound:
        #     print(f"  - {action_info['action']} (was bound to '{action_info['was_key']}') - {action_info['description']}")
    
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
    
    def action_show_unbound(self) -> None:
        """Show unbound actions in a floating modal window."""
        # Get unbound actions for all panes
        unbound_app = self.keybinding_config.get_unbound_actions("app")
        unbound_branches = self.keybinding_config.get_unbound_actions("branches")
        unbound_commits = self.keybinding_config.get_unbound_actions("commits")
        unbound_stash = self.keybinding_config.get_unbound_actions("stash")
        unbound_tags = self.keybinding_config.get_unbound_actions("tags")
        unbound_remotes = self.keybinding_config.get_unbound_actions("remotes")
        
        # Combine all unbound actions
        all_unbound = (
            unbound_app + unbound_branches + unbound_commits +
            unbound_stash + unbound_tags + unbound_remotes
        )
        
        # Only proceed if there are unbound actions
        if not all_unbound:
            return
        
        # Create and push the modal screen
        modal = UnboundActionsModal(
            all_unbound,
            self.keybinding_config.config_path
        )
        
        # Push the modal screen (non-blocking, callback-based)
        self.push_screen(modal)

    def action_show_about(self) -> None:
        """Show About information in a floating modal window."""
        modal = AboutModal()
        self.push_screen(modal)
    
    def action_checkout(self) -> None:
        """Checkout a branch from the branches pane.
        
        This action is triggered when 'c' is pressed while the branches pane has focus.
        Delegates to BranchActionHandler for the actual implementation.
        """
        self.branch_actions.checkout()
    
    def action_new_branch(self) -> None:
        """Create a new branch.
        
        This action is triggered when 'n' is pressed while the branches pane has focus.
        Delegates to BranchActionHandler for the actual implementation.
        """
        self.branch_actions.create()
    
    def action_delete_branch(self) -> None:
        """Delete a branch.
        
        This action is triggered when 'd' is pressed while the branches pane has focus.
        Delegates to BranchActionHandler for the actual implementation.
        """
        self.branch_actions.delete()
    
    def action_rename_branch(self) -> None:
        """Rename a branch.
        
        This action is triggered when 'r' is pressed while the branches pane has focus.
        Delegates to BranchActionHandler for the actual implementation.
        """
        self.branch_actions.rename()
    
    def _get_current_branch_name(self) -> str | None:
        """Return the currently checked-out branch name, or None if it can't be determined.
        
        Uses `git rev-parse --abbrev-ref HEAD` so that we always respect the real
        repository HEAD, even if branches are ordered by recency elsewhere.
        """
        import subprocess
        
        repo_path_str = str(self.repo_path) if hasattr(self, "repo_path") else "."
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
                # Detached HEAD will return "HEAD" – treat that as no named branch
                if branch and branch != "HEAD":
                    return branch
        except Exception:
            pass
        return None
    

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
        # Detect the *actual* currently checked-out branch from Git HEAD
        current_branch_from_git = self._get_current_branch_name()
        
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
                    # Branch was deleted, fall back to the actual current branch if available,
                    # otherwise use the first branch in the list.
                    branch_names = [b.name for b in self.branches]
                    if current_branch_from_git and current_branch_from_git in branch_names:
                        self.active_branch = current_branch_from_git
                        branch_index = branch_names.index(current_branch_from_git)
                    else:
                        self.active_branch = self.branches[0].name
                        branch_index = 0
                    self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                    self.branches_pane.index = branch_index
                    self.branches_pane.highlighted = branch_index
            else:
                # No previous branch, pick the actual current branch if we can,
                # otherwise fall back to the first branch (existing behavior).
                branch_names = [b.name for b in self.branches]
                if current_branch_from_git and current_branch_from_git in branch_names:
                    self.active_branch = current_branch_from_git
                    branch_index = branch_names.index(current_branch_from_git)
                else:
                    self.active_branch = self.branches[0].name
                    branch_index = 0
                self.branches_pane.set_branches(self.branches, self.active_branch)
                self.branches_pane.index = branch_index
                self.branches_pane.highlighted = branch_index

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
        current_branch_from_git = self._get_current_branch_name()
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
                    # Branch was deleted, fall back to the actual current branch if available,
                    # otherwise use the first branch.
                    branch_names = [b.name for b in self.branches]
                    if current_branch_from_git and current_branch_from_git in branch_names:
                        self.active_branch = current_branch_from_git
                        branch_index = branch_names.index(current_branch_from_git)
                    else:
                        self.active_branch = self.branches[0].name
                        branch_index = 0
                    self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                    self.branches_pane.index = branch_index
                    self.branches_pane.highlighted = branch_index
            else:
                # No previous branch, prefer the actual current branch if we know it.
                branch_names = [b.name for b in self.branches]
                if current_branch_from_git and current_branch_from_git in branch_names:
                    self.active_branch = current_branch_from_git
                    branch_index = branch_names.index(current_branch_from_git)
                else:
                    self.active_branch = self.branches[0].name
                    branch_index = 0
                self.branches_pane.set_branches(self.branches, self.active_branch, self._branch_sync_status_cache)
                self.branches_pane.index = branch_index
                self.branches_pane.highlighted = branch_index

            
            self.load_commits(self.active_branch)
            self.update_status_info()

    def _calculate_branch_sync_status(self, branch: str) -> dict:
        """Calculate sync status (behind/ahead counts) for a branch.
        
        Returns dict with keys: 'behind', 'ahead', 'synced', 'upstream'
        """
        # Use BranchService for sync status calculation
        return self.branch_service.calculate_branch_sync_status(branch)
    
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
                # Use BranchService to calculate all at once
                sync_status_map = self.branch_service.calculate_all_branches_sync_status(self.branches)
                
                # Update cache
                updated_count = 0
                for branch_name, sync_status in sync_status_map.items():
                    if branch_name not in self._branch_sync_status_cache:
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
            self.status_pane.update_status(self.active_branch, str(self.repo_path), current_sync)
        
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
        
        # Command log update removed - no longer showing refresh messages

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
            score = self.commit_service.fuzzy_match(query, commit.summary)
            # Also search in author name
            author_score = self.commit_service.fuzzy_match(query, commit.author) * 0.5
            # Also search in SHA
            sha_score = self.commit_service.fuzzy_match(query, commit.sha) * 0.3
            
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
        loaded_commits = self.commit_service.load_commits(branch, max_count=self.page_size, skip=0)
        self.all_commits = loaded_commits.copy()  # Store all commits for search
        
        # Apply search filter if there's a search query
        if self._search_query:
            self.commits = self.commit_service.filter_commits(self.all_commits, self._search_query)
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
                count = self.commit_service.count_commits(branch)
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
                # Use StashService (handles Cython/Python fallback internally)
                stashes = self.stash_service.load_stashes()
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
                # Use TagService (handles Cython/Python fallback internally)
                tags = self.tag_service.load_tags()
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
                pass
                
                # Update UI from main thread on error (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self._update_file_status_ui([]))
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
            # Command log update removed - no longer showing refresh messages
            
            update_elapsed = time.perf_counter() - update_start
            _log_timing_message(f"[TIMING]   _update_file_status_ui (limited to {display_count}): {update_elapsed:.4f}s")
        except Exception as e:
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
                    self.commits = self.commit_service.filter_commits(self.all_commits, self._search_query)
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

    def load_commits(self, branch: str) -> None:
        """Load all commits from all branches (not branch-specific)."""
        import subprocess
        from datetime import datetime
        
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
            
            # Run git log with timeout
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
            self.commits = self.commit_service.filter_commits(self.all_commits, self._search_query)
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
            diff_start = time.perf_counter()
            ci = self.commits[index]
            # Using LazyGit approach: show_commit_info now gets everything from git show --stat -p
            # No need to call get_commit_diff separately
            show_start = time.perf_counter()
            self.patch_pane.show_commit_info(ci, "", git_service=self.git)
            show_elapsed = time.perf_counter() - show_start
            _log_timing_message(f"[TIMING] show_commit_info: {show_elapsed:.4f}s")
            diff_total = time.perf_counter() - diff_start
            _log_timing_message(f"[TIMING] show_commit_diff TOTAL: {diff_total:.4f}s")
    
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
                # Use StashService (handles Cython/Python fallback internally)
                diff_text, stat_text = self.stash_service.get_stash_diff(stash.index)
                
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
                
                # Get git log output (use bytes first, then decode with error handling for non-UTF-8 characters)
                # Increased timeout for large repos (haiku can take longer)
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
                
                # Parse ANSI colors from git log output
                from pygitzen.git_graph import parse_ansi_to_rich_text
                from rich.text import Text
                
                # Create Text object with tag info and git log
                display_text = Text()
                display_text.append('\n'.join(tag_info_lines), style="white")
                display_text.append('\n\n', style="white")
                
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
                
                tag_elapsed = time.perf_counter() - tag_start
                _log_timing_message(f"[TIMING] show_tag_info TOTAL: {tag_elapsed:.4f}s")
                
                # Update UI from main thread (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self.log_pane.update(display_text))
            except Exception as e:
                # If tag info fetching fails, show error
                import traceback
                error_msg = f"Error loading tag info: {type(e).__name__}: {e}\n{traceback.format_exc()}"
                _log_timing_message(f"[ERROR] show_tag_info: {error_msg}")
                from rich.text import Text
                error_text = Text(f"Error loading tag info: {type(e).__name__}: {e}", style="red")
                # Update UI from main thread (use queue which is thread-safe)
                self._ui_update_queue.put(lambda: self.log_pane.update(error_text))
        
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

                # Always switch to log view when a branch is explicitly selected,
                # even if it's the same branch as before (e.g., user has just
                # viewed a tag and now wants the branch log back).
                self._view_mode = "log"
                self.patch_pane.styles.display = "none"
                self.log_pane.styles.display = "block"

                if should_reload:
                    # Load commits for the selected branch (matching lazygit - shows branch-specific commits)
                    self.load_commits(self.active_branch)
                    # Load commits with full history for feature branches (for log pane)
                    self.load_commits_for_log(self.active_branch)
                    # Refresh sync status for the selected branch
                    self._refresh_branch_sync_status(self.active_branch)
                    self.update_status_info()
                else:
                    # Same branch, no new commits - still ensure branch log is visible
                    # (e.g., after viewing a tag or switching tabs), and refresh sync
                    # status and status pane.
                    self.load_commits_for_log(self.active_branch)
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
                    self.commits = self.commit_service.filter_commits(self.all_commits, self._search_query)
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


