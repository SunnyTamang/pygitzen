"""Git repository watcher for real-time change detection (Lazygit-style).

This module provides a GitWatcher class that monitors Git repository changes
and triggers UI updates automatically, similar to Lazygit's real-time detection.
"""

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass
from enum import Enum


class ChangeType(Enum):
    """Types of Git repository changes."""
    NEW_COMMIT = "new_commit"
    NEW_TAG = "new_tag"
    TAG_DELETED = "tag_deleted"
    BRANCH_CREATED = "branch_created"
    BRANCH_DELETED = "branch_deleted"
    FILE_STAGED = "file_staged"
    FILE_UNSTAGED = "file_unstaged"
    FILE_CHANGED = "file_changed"
    COMMIT_PUSHED = "commit_pushed"


@dataclass
class ChangeEvent:
    """Represents a detected Git repository change."""
    change_type: ChangeType
    branch: Optional[str] = None
    tag: Optional[str] = None
    file: Optional[str] = None
    timestamp: float = 0.0


class GitWatcher:
    """Monitors Git repository for changes and triggers callbacks.
    
    This class implements real-time change detection similar to Lazygit:
    - HEAD SHA polling (detects new commits)
    - Refs monitoring (branches, tags)
    - Working directory monitoring (staging, file changes)
    - Remote polling (detects pushed commits)
    """
    
    def __init__(
        self,
        repo_path: Path | str,
        on_change: Callable[[ChangeEvent], None],
        head_poll_interval: float = 1.5,
        remote_poll_interval: float = 8.0,
        use_watchdog: bool = True
    ):
        """Initialize GitWatcher.
        
        Args:
            repo_path: Path to Git repository
            on_change: Callback function called when changes are detected
            head_poll_interval: Seconds between HEAD SHA polls (default: 1.5s)
            remote_poll_interval: Seconds between remote branch polls (default: 8.0s)
            use_watchdog: Whether to use watchdog library for file system events (default: True)
        """
        self.repo_path = Path(repo_path)
        self.on_change = on_change
        self.head_poll_interval = head_poll_interval
        self.remote_poll_interval = remote_poll_interval
        self.use_watchdog = use_watchdog
        
        # State tracking
        self._last_head_sha: dict[str, str] = {}  # branch -> SHA
        self._last_remote_head_sha: dict[str, str] = {}  # branch -> SHA
        self._last_refs_state: dict[str, set[str]] = {}  # ref_type -> set of refs
        self._last_index_mtime: float = 0.0
        
        # Threading
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._is_running = False
        
        # Watchdog (optional)
        self._watchdog_observer = None
        self._GitRefsHandler = None
        if self.use_watchdog:
            try:
                from watchdog.observers import Observer
                from watchdog.events import FileSystemEventHandler
                self._watchdog_available = True
                self._FileSystemEventHandler = FileSystemEventHandler
                # Define handler class inline
                class GitRefsHandler(FileSystemEventHandler):
                    def __init__(self, watcher):
                        super().__init__()
                        self.watcher = watcher
                    
                    def on_modified(self, event):
                        self._handle_event(event)
                    
                    def on_created(self, event):
                        self._handle_event(event)
                    
                    def on_deleted(self, event):
                        self._handle_event(event)
                    
                    def _handle_event(self, event):
                        if event.is_directory:
                            return
                        path = Path(event.src_path)
                        path_str = str(path)
                        
                        # Check if it's a ref change
                        if "refs/heads" in path_str:
                            branch = path.name
                            if event.event_type in ("created", "modified"):
                                change_event = ChangeEvent(
                                    change_type=ChangeType.BRANCH_CREATED if event.event_type == "created" else ChangeType.NEW_COMMIT,
                                    branch=branch,
                                    timestamp=time.time()
                                )
                                self.watcher.on_change(change_event)
                            elif event.event_type == "deleted":
                                change_event = ChangeEvent(
                                    change_type=ChangeType.BRANCH_DELETED,
                                    branch=branch,
                                    timestamp=time.time()
                                )
                                self.watcher.on_change(change_event)
                        
                        elif "refs/tags" in path_str:
                            tag = path.name
                            if event.event_type in ("created", "modified"):
                                change_event = ChangeEvent(
                                    change_type=ChangeType.NEW_TAG,
                                    tag=tag,
                                    timestamp=time.time()
                                )
                                self.watcher.on_change(change_event)
                            elif event.event_type == "deleted":
                                change_event = ChangeEvent(
                                    change_type=ChangeType.TAG_DELETED,
                                    tag=tag,
                                    timestamp=time.time()
                                )
                                self.watcher.on_change(change_event)
                        
                        # Check if it's index change
                        elif path.name == "index" and ".git" in path_str:
                            change_event = ChangeEvent(
                                change_type=ChangeType.FILE_STAGED,
                                timestamp=time.time()
                            )
                            self.watcher.on_change(change_event)
                        
                        # Check if it's a working directory file change (not in .git directory)
                        elif ".git" not in path_str:
                            # Only trigger on file changes (not directory changes)
                            if not event.is_directory:
                                # Filter out common non-source files
                                ignored_extensions = {'.pyc', '.pyo', '.pyd', '.so', '.dylib', '.dll', '.exe', '.log', '.tmp', '.swp', '.DS_Store'}
                                if path.suffix.lower() not in ignored_extensions:
                                    change_event = ChangeEvent(
                                        change_type=ChangeType.FILE_CHANGED,
                                        file=str(path.relative_to(self.watcher.repo_path)),
                                        timestamp=time.time()
                                    )
                                    self.watcher.on_change(change_event)
                
                self._GitRefsHandler = GitRefsHandler
            except ImportError:
                self._watchdog_available = False
                self.use_watchdog = False
        else:
            self._watchdog_available = False
    
    def start(self) -> None:
        """Start monitoring in background thread."""
        if self._is_running:
            return
        
        self._stop_event.clear()
        self._is_running = True
        
        # Initialize state
        self._initialize_state()
        
        # Start monitoring thread
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="GitWatcher"
        )
        self._monitor_thread.start()
        
        # Start watchdog if available
        if self.use_watchdog and self._watchdog_available:
            self._start_watchdog()
    
    def stop(self) -> None:
        """Stop monitoring."""
        if not self._is_running:
            return
        
        self._stop_event.set()
        self._is_running = False
        
        # Stop watchdog
        if self._watchdog_observer:
            self._watchdog_observer.stop()
            self._watchdog_observer.join(timeout=1.0)
            self._watchdog_observer = None
        
        # Wait for thread
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None
    
    def _initialize_state(self) -> None:
        """Initialize state tracking from current repository state."""
        try:
            # Get current HEAD SHA
            current_branch = self._get_current_branch()
            if current_branch:
                head_sha = self._get_head_sha()
                if head_sha:
                    self._last_head_sha[current_branch] = head_sha
            
            # Get current refs state
            self._last_refs_state["branches"] = set(self._get_branch_refs())
            self._last_refs_state["tags"] = set(self._get_tag_refs())
            
            # Get index mtime
            index_path = self.repo_path / ".git" / "index"
            if index_path.exists():
                self._last_index_mtime = index_path.stat().st_mtime
        except Exception:
            pass  # Ignore errors during initialization
    
    def _monitor_loop(self) -> None:
        """Main monitoring loop running in background thread."""
        last_head_check = 0.0
        last_remote_check = 0.0
        last_refs_check = 0.0
        last_index_check = 0.0
        
        while not self._stop_event.is_set():
            current_time = time.perf_counter()
            
            try:
                # HEAD SHA polling (every head_poll_interval)
                if current_time - last_head_check >= self.head_poll_interval:
                    self._check_head_changes()
                    last_head_check = current_time
                
                # Remote polling (every remote_poll_interval)
                if current_time - last_remote_check >= self.remote_poll_interval:
                    self._check_remote_changes()
                    last_remote_check = current_time
                
                # Refs polling (every 3 seconds, if not using watchdog)
                if not self.use_watchdog:
                    if current_time - last_refs_check >= 3.0:
                        self._check_refs_changes()
                        last_refs_check = current_time
                
                # Index polling (every 1 second, if not using watchdog)
                if not self.use_watchdog:
                    if current_time - last_index_check >= 1.0:
                        self._check_index_changes()
                        last_index_check = current_time
                
                # Sleep briefly to avoid busy-waiting
                time.sleep(0.1)
            except Exception:
                # Continue monitoring even if one check fails
                time.sleep(0.5)
    
    def _check_head_changes(self) -> None:
        """Check if HEAD SHA has changed (new commit made)."""
        try:
            current_branch = self._get_current_branch()
            if not current_branch:
                return
            
            current_head_sha = self._get_head_sha()
            if not current_head_sha:
                return
            
            last_sha = self._last_head_sha.get(current_branch)
            if last_sha and last_sha != current_head_sha:
                # HEAD changed - new commit made
                event = ChangeEvent(
                    change_type=ChangeType.NEW_COMMIT,
                    branch=current_branch,
                    timestamp=time.time()
                )
                self.on_change(event)
            
            # Update state
            self._last_head_sha[current_branch] = current_head_sha
        except Exception:
            pass  # Ignore errors
    
    def _check_remote_changes(self) -> None:
        """Check if remote branch HEAD has changed (commits pushed)."""
        try:
            current_branch = self._get_current_branch()
            if not current_branch:
                return
            
            remote_branch = f"origin/{current_branch}"
            remote_sha = self._get_remote_head_sha(remote_branch)
            if not remote_sha:
                return
            
            last_remote_sha = self._last_remote_head_sha.get(current_branch)
            if last_remote_sha and last_remote_sha != remote_sha:
                # Remote changed - commits pushed
                event = ChangeEvent(
                    change_type=ChangeType.COMMIT_PUSHED,
                    branch=current_branch,
                    timestamp=time.time()
                )
                self.on_change(event)
            
            # Update state
            self._last_remote_head_sha[current_branch] = remote_sha
        except Exception:
            pass  # Ignore errors
    
    def _check_refs_changes(self) -> None:
        """Check if refs (branches/tags) have changed."""
        try:
            # Check branches
            current_branches = set(self._get_branch_refs())
            last_branches = self._last_refs_state.get("branches", set())
            
            new_branches = current_branches - last_branches
            deleted_branches = last_branches - current_branches
            
            for branch in new_branches:
                event = ChangeEvent(
                    change_type=ChangeType.BRANCH_CREATED,
                    branch=branch,
                    timestamp=time.time()
                )
                self.on_change(event)
            
            for branch in deleted_branches:
                event = ChangeEvent(
                    change_type=ChangeType.BRANCH_DELETED,
                    branch=branch,
                    timestamp=time.time()
                )
                self.on_change(event)
            
            # Check tags
            current_tags = set(self._get_tag_refs())
            last_tags = self._last_refs_state.get("tags", set())
            
            new_tags = current_tags - last_tags
            deleted_tags = last_tags - current_tags
            
            for tag in new_tags:
                event = ChangeEvent(
                    change_type=ChangeType.NEW_TAG,
                    tag=tag,
                    timestamp=time.time()
                )
                self.on_change(event)
            
            for tag in deleted_tags:
                event = ChangeEvent(
                    change_type=ChangeType.TAG_DELETED,
                    tag=tag,
                    timestamp=time.time()
                )
                self.on_change(event)
            
            # Update state
            self._last_refs_state["branches"] = current_branches
            self._last_refs_state["tags"] = current_tags
        except Exception:
            pass  # Ignore errors
    
    def _check_index_changes(self) -> None:
        """Check if .git/index has changed (staging changes)."""
        try:
            index_path = self.repo_path / ".git" / "index"
            if not index_path.exists():
                return
            
            current_mtime = index_path.stat().st_mtime
            if current_mtime != self._last_index_mtime:
                # Index changed - files staged/unstaged
                # Note: We can't determine if it's staged or unstaged from mtime alone
                # This will trigger a file status refresh
                event = ChangeEvent(
                    change_type=ChangeType.FILE_STAGED,  # Generic - will refresh files pane
                    timestamp=time.time()
                )
                self.on_change(event)
                self._last_index_mtime = current_mtime
        except Exception:
            pass  # Ignore errors
    
    def _start_watchdog(self) -> None:
        """Start watchdog file system observer."""
        if not self._watchdog_available or not self._GitRefsHandler:
            return
        
        from watchdog.observers import Observer
        
        handler = self._GitRefsHandler(self)
        self._watchdog_observer = Observer()
        # Watch .git/refs for branch/tag changes
        self._watchdog_observer.schedule(
            handler,
            str(self.repo_path / ".git" / "refs"),
            recursive=True
        )
        # Watch .git directory for index changes
        self._watchdog_observer.schedule(
            handler,
            str(self.repo_path / ".git"),
            recursive=False
        )
        # Watch working directory for new/changed files
        self._watchdog_observer.schedule(
            handler,
            str(self.repo_path),
            recursive=True
        )
        self._watchdog_observer.start()
    
    
    # Git command helpers
    def _get_current_branch(self) -> Optional[str]:
        """Get current branch name."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=1.0,
                cwd=str(self.repo_path)
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
    
    def _get_head_sha(self) -> Optional[str]:
        """Get current HEAD SHA."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=1.0,
                cwd=str(self.repo_path)
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
    
    def _get_remote_head_sha(self, remote_branch: str) -> Optional[str]:
        """Get remote branch HEAD SHA."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", remote_branch],
                capture_output=True,
                text=True,
                timeout=2.0,
                cwd=str(self.repo_path)
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
    
    def _get_branch_refs(self) -> list[str]:
        """Get list of branch names."""
        try:
            result = subprocess.run(
                ["git", "branch", "--format=%(refname:short)"],
                capture_output=True,
                text=True,
                timeout=2.0,
                cwd=str(self.repo_path)
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        except Exception:
            pass
        return []
    
    def _get_tag_refs(self) -> list[str]:
        """Get list of tag names."""
        try:
            result = subprocess.run(
                ["git", "tag", "--list"],
                capture_output=True,
                text=True,
                timeout=2.0,
                cwd=str(self.repo_path)
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        except Exception:
            pass
        return []

