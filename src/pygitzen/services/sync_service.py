"""Sync service for push and pull operations.

This module provides the business logic for synchronizing local branches with
remote repositories. It handles push, pull, and fetch operations without any
dependencies on the UI layer, making it easy to test and reuse.
"""

import subprocess
from pathlib import Path
from typing import Optional

from ..git_service import GitService


class SyncService:
    """Service for sync operations (push/pull)."""

    def __init__(self, git_service: GitService, repo_path: Path | str | None = None) -> None:
        """Initialize sync service.
        
        Args:
            git_service: GitService instance for git operations.
            repo_path: Repository root path (for subprocess calls). If None, uses git_service.repo_path.
        """
        self.git = git_service
        if repo_path:
            self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path
        else:
            self.repo_path = Path(getattr(git_service, 'repo_path', '.'))

    def get_upstream(self, branch_name: str) -> tuple[bool, str | None, str | None]:
        """Get upstream remote and branch configuration for a branch.
        
        This method reads the git configuration directly to determine if a branch
        has an upstream configured. We check the config rather than using rev-parse
        because config works even when the remote branch doesn't exist yet, which
        is important for handling cases where upstream is set but the remote
        hasn't been fetched or doesn't have that branch.
        
        Args:
            branch_name: Local branch name to check.
        
        Returns:
            A tuple containing:
            - has_upstream: True if upstream is configured, False otherwise
            - remote_name: Remote name like "origin", or None if not configured
            - branch_name: Remote branch name like "main", or None if not configured
        """
        repo_path_str = str(self.repo_path)
        
        try:
            # Read the upstream configuration from git config. We check both
            # branch.{name}.remote and branch.{name}.merge to determine if
            # upstream tracking is fully configured
            remote_cmd = ["git", "config", "--get", f"branch.{branch_name}.remote"]
            remote_result = subprocess.run(
                remote_cmd,
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )
            
            merge_cmd = ["git", "config", "--get", f"branch.{branch_name}.merge"]
            merge_result = subprocess.run(
                merge_cmd,
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )
            
            # Both remote and merge must be configured for a valid upstream
            if remote_result.returncode == 0 and merge_result.returncode == 0:
                remote = remote_result.stdout.strip()
                merge_ref = merge_result.stdout.strip()
                
                # The merge ref is stored as "refs/heads/branch-name" in config,
                # but we want just the branch name for our purposes
                if merge_ref.startswith("refs/heads/"):
                    branch = merge_ref[len("refs/heads/"):]
                else:
                    branch = merge_ref
                
                # Skip local remotes (remote = ".") as they're not real upstreams
                if remote and remote != ".":
                    return (True, remote, branch)
            
            return (False, None, None)
        except Exception:
            # If anything goes wrong reading the config, assume no upstream
            return (False, None, None)

    def get_suggested_remote(self) -> str:
        """Get a suggested remote name for upstream configuration.
        
        This method provides a sensible default remote name when prompting the
        user to set upstream. It prefers "origin" as that's the conventional
        default remote name, but falls back to the first available remote if
        origin doesn't exist.
        
        Returns:
            The suggested remote name, typically "origin" or the first remote
            found in the repository configuration.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            # Query git for the list of configured remotes
            cmd = ["git", "remote"]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=2, cwd=repo_path_str
            )
            
            if result.returncode == 0:
                remotes = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]
                # Most repositories use "origin" as the primary remote, so prefer it
                if "origin" in remotes:
                    return "origin"
                # If origin isn't available, use whatever remote exists
                if remotes:
                    return remotes[0]
            
            # Default fallback - most users expect "origin" even if not configured
            return "origin"
        except Exception:
            # If we can't query remotes for any reason, default to origin
            return "origin"

    def parse_upstream(self, upstream: str) -> tuple[str, str]:
        """Parse upstream string into remote and branch.
        
        Args:
            upstream: Upstream string in format "remote branch-name" (e.g., "origin main").
        
        Returns:
            Tuple of (remote: str, branch: str).
        
        Raises:
            ValueError: If upstream format is invalid.
        """
        parts = upstream.strip().split()
        if len(parts) != 2:
            raise ValueError("Invalid upstream format. Expected: 'remote branch-name' (e.g., 'origin main')")
        
        return (parts[0], parts[1])

    def push(
        self,
        branch_name: str,
        remote: str | None = None,
        remote_branch: str | None = None,
        force: bool = False,
        force_with_lease: bool = False,
        set_upstream: bool = False,
    ) -> dict:
        """Push a branch to remote.
        
        Args:
            branch_name: Local branch name to push.
            remote: Remote name (e.g., "origin"). If None, uses upstream remote or "origin".
            remote_branch: Remote branch name. If None, uses branch_name.
            force: If True, use --force flag.
            force_with_lease: If True, use --force-with-lease flag.
            set_upstream: If True, use --set-upstream flag.
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            cmd = ["git", "push"]
            
            # Add force flags
            if force_with_lease:
                cmd.append("--force-with-lease")
            elif force:
                cmd.append("--force")
            
            # Add upstream flag
            if set_upstream:
                cmd.append("--set-upstream")
            
            # Add remote and branch
            if remote and remote_branch:
                # Format: git push [--set-upstream] origin refs/heads/local:remote
                cmd.extend([remote, f"refs/heads/{branch_name}:{remote_branch}"])
            elif remote:
                # Format: git push [--set-upstream] origin (uses current branch)
                cmd.append(remote)
            # If no remote specified, git push will use upstream or push.default
            
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, cwd=repo_path_str
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def pull(
        self,
        branch_name: str,
        remote: str | None = None,
        remote_branch: str | None = None,
        fast_forward_only: bool = False,
    ) -> dict:
        """Pull changes from remote.
        
        Args:
            branch_name: Local branch name to pull into.
            remote: Remote name (e.g., "origin"). If None, uses upstream remote.
            remote_branch: Remote branch name. If None, uses branch_name.
            fast_forward_only: If True, use --ff-only flag.
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            cmd = ["git", "pull", "--no-edit"]
            
            if fast_forward_only:
                cmd.append("--ff-only")
            
            # Add remote and branch if specified
            if remote and remote_branch:
                cmd.extend([remote, remote_branch])
            elif remote:
                cmd.append(remote)
            
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, cwd=repo_path_str
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def fetch(self, fetch_all: bool = False) -> dict:
        """Fetch changes from remote.
        
        Args:
            fetch_all: If True, fetch from all remotes (--all flag).
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            cmd = ["git", "fetch"]
            
            if fetch_all:
                cmd.append("--all")
            
            # Prevent writing to FETCH_HEAD to avoid conflicts when multiple
            # pull operations might be running concurrently
            cmd.append("--no-write-fetch-head")
            
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, cwd=repo_path_str
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

