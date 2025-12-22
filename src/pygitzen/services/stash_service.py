"""Stash service for stash operations.

Pure business logic - no UI dependencies.
"""

import subprocess
import re
from pathlib import Path
from typing import Optional, Tuple

from ..git_service import StashInfo, GitService, FileStatus


class StashService:
    """Service for stash-related operations."""

    def __init__(self, git_service: GitService) -> None:
        """Initialize stash service.
        
        Args:
            git_service: GitService instance for git operations.
        """
        self.git = git_service
        self._git_version_cache: Optional[Tuple[int, int, int]] = None

    def _get_repo_path(self) -> Path:
        """Get repository path from git service."""
        repo_path = getattr(self.git, 'repo_path', None)
        if repo_path is None:
            if hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                repo_path = Path(self.git.repo.path)
            else:
                repo_path = Path(".")
        return Path(repo_path) if not isinstance(repo_path, Path) else repo_path

    def _get_git_version(self) -> Tuple[int, int, int]:
        """Get git version as (major, minor, patch).
        
        Returns:
            Tuple of (major, minor, patch) version numbers.
        """
        if self._git_version_cache is not None:
            return self._git_version_cache
        
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                # Parse version string like "git version 2.39.0" or "git version 2.37.1 (Apple Git-137.1)"
                version_str = result.stdout.strip()
                match = re.search(r'(\d+)\.(\d+)\.(\d+)', version_str)
                if match:
                    major = int(match.group(1))
                    minor = int(match.group(2))
                    patch = int(match.group(3))
                    self._git_version_cache = (major, minor, patch)
                    return self._git_version_cache
        except Exception:
            pass
        
        # Default to older version if we can't determine
        self._git_version_cache = (2, 0, 0)
        return self._git_version_cache

    def _is_git_version_at_least(self, major: int, minor: int, patch: int) -> bool:
        """Check if git version is at least the specified version.
        
        Args:
            major: Major version number.
            minor: Minor version number.
            patch: Patch version number.
        
        Returns:
            True if git version >= specified version.
        """
        git_major, git_minor, git_patch = self._get_git_version()
        git_version = git_major * 1000000 + git_minor * 1000 + git_patch
        required_version = major * 1000000 + minor * 1000 + patch
        return git_version >= required_version

    def load_stashes(self) -> list[StashInfo]:
        """Load all stashes from the repository.
        
        Returns:
            List of StashInfo objects.
        """
        # Check if method exists (Cython version might not have it)
        if hasattr(self.git, 'list_stashes'):
            return self.git.list_stashes()
        else:
            # Fallback: create a Python GitService instance
            from ..git_service import GitService
            
            repo_path = self._get_repo_path()
            python_git = GitService(str(repo_path))
            return python_git.list_stashes()

    def get_stash_diff(self, stash_index: int) -> Tuple[str, str]:
        """Get diff and stat for a stash.
        
        Args:
            stash_index: Stash index (0-based).
        
        Returns:
            Tuple of (diff_text, stat_text).
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            # Get stash diff (use --no-color for consistent parsing, we'll apply colors manually)
            diff_result = subprocess.run(
                ["git", "stash", "show", "-p", "--no-color", f"stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path_str,
            )
            
            # Get stash stat (use --stat --no-color for consistent parsing, we'll apply colors manually)
            stat_result = subprocess.run(
                ["git", "stash", "show", "--stat", "--no-color", f"stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path_str,
            )
            
            diff_text = diff_result.stdout if diff_result.returncode == 0 else ""
            stat_text = stat_result.stdout if stat_result.returncode == 0 else ""
            
            return (diff_text, stat_text)
        except Exception:
            return ("", "")

    def stash_all(self, message: str) -> dict:
        """Stash all changes.
        
        Args:
            message: Stash message.
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "stash", "push", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stash_keep_index(self, message: str) -> dict:
        """Stash all changes but keep index (staged files remain staged).
        
        Args:
            message: Stash message.
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "stash", "push", "--keep-index", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stash_include_untracked(self, message: str) -> dict:
        """Stash all changes including untracked files.
        
        Args:
            message: Stash message.
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "stash", "push", "--include-untracked", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stash_staged(self, message: str) -> dict:
        """Stash only staged changes.
        
        For git >= 2.35.0, uses --staged flag.
        For older versions, uses complex workaround.
        
        Args:
            message: Stash message.
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        # Check git version - --staged flag requires git >= 2.35.0
        if self._is_git_version_at_least(2, 35, 0):
            try:
                result = subprocess.run(
                    ["git", "stash", "push", "--staged", "-m", message],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=repo_path_str,
                )
                
                if result.returncode == 0:
                    return {"success": True, "error": None}
                else:
                    error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                    return {"success": False, "error": error_msg}
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # Fallback for git < 2.35.0 (complex workaround)
        # Based on lazygit's implementation
        try:
            # Step 1: Stash everything with --keep-index
            result1 = subprocess.run(
                ["git", "stash", "--keep-index"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            if result1.returncode != 0:
                return {"success": False, "error": result1.stderr.strip() or "Failed to stash with keep-index"}
            
            # Step 2: Push the actual stash with message
            result2 = subprocess.run(
                ["git", "stash", "push", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            if result2.returncode != 0:
                return {"success": False, "error": result2.stderr.strip() or "Failed to push stash"}
            
            # Step 3: Apply the previous stash (stash@{1})
            result3 = subprocess.run(
                ["git", "stash", "apply", "refs/stash@{1}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            if result3.returncode != 0:
                return {"success": False, "error": result3.stderr.strip() or "Failed to apply previous stash"}
            
            # Step 4: Reverse the diff to get only staged changes
            # Pipe stash show into apply -R (reverse)
            stash_show = subprocess.Popen(
                ["git", "stash", "show", "-p"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=repo_path_str,
            )
            apply_reverse = subprocess.run(
                ["git", "apply", "-R"],
                stdin=stash_show.stdout,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            stash_show.wait()
            if apply_reverse.returncode != 0:
                return {"success": False, "error": apply_reverse.stderr.strip() or "Failed to reverse apply"}
            
            # Step 5: Drop the temporary stash (stash@{1})
            result5 = subprocess.run(
                ["git", "stash", "drop", "refs/stash@{1}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            if result5.returncode != 0:
                return {"success": False, "error": result5.stderr.strip() or "Failed to drop temporary stash"}
            
            # Step 6: Handle untracked files that were staged (appear as 'AD' in git status)
            # Get file status to find 'AD' files
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path_str,
            )
            if status_result.returncode == 0:
                for line in status_result.stdout.strip().split('\n'):
                    if line.startswith('AD '):
                        # Unstage the file
                        file_path = line[3:].strip()
                        subprocess.run(
                            ["git", "reset", "HEAD", "--", file_path],
                            capture_output=True,
                            timeout=5,
                            cwd=repo_path_str,
                        )
            
            return {"success": True, "error": None}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stash_unstaged(self, message: str) -> dict:
        """Stash only unstaged changes.
        
        Args:
            message: Stash message.
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            # Check if there are staged files
            # If no staged files, just do regular stash
            staged_result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                capture_output=True,
                timeout=5,
                cwd=repo_path_str,
            )
            
            if staged_result.returncode != 0:
                # There are staged files, use complex workaround
                # Based on lazygit's implementation
                # Step 1: Commit staged changes temporarily
                commit_result = subprocess.run(
                    ["git", "commit", "--no-verify", "-m", "[lazygit] stashing unstaged changes"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=repo_path_str,
                )
                if commit_result.returncode != 0:
                    return {"success": False, "error": commit_result.stderr.strip() or "Failed to commit staged changes"}
                
                # Step 2: Stash everything (now includes the temporary commit)
                stash_result = subprocess.run(
                    ["git", "stash", "push", "-m", message],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=repo_path_str,
                )
                if stash_result.returncode != 0:
                    return {"success": False, "error": stash_result.stderr.strip() or "Failed to stash"}
                
                # Step 3: Reset soft to get staged changes back
                reset_result = subprocess.run(
                    ["git", "reset", "--soft", "HEAD^"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=repo_path_str,
                )
                if reset_result.returncode != 0:
                    return {"success": False, "error": reset_result.stderr.strip() or "Failed to reset"}
                
                return {"success": True, "error": None}
            else:
                # No staged files, just do regular stash
                return self.stash_all(message)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def apply_stash(self, stash_index: int) -> dict:
        """Apply a stash entry.
        
        Args:
            stash_index: Stash index (0-based).
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "stash", "apply", f"refs/stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def pop_stash(self, stash_index: int) -> dict:
        """Pop a stash entry (apply and remove).
        
        Args:
            stash_index: Stash index (0-based).
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "stash", "pop", f"refs/stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def drop_stash(self, stash_index: int) -> dict:
        """Drop a stash entry (remove without applying).
        
        Args:
            stash_index: Stash index (0-based).
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            result = subprocess.run(
                ["git", "stash", "drop", f"refs/stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def rename_stash(self, stash_index: int, new_message: str) -> dict:
        """Rename a stash entry.
        
        Args:
            stash_index: Stash index (0-based).
            new_message: New stash message.
        
        Returns:
            Dict with 'success' and 'error' keys.
        """
        repo_path_str = str(self._get_repo_path())
        
        try:
            # Get stash hash
            hash_result = subprocess.run(
                ["git", "rev-parse", f"refs/stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path_str,
            )
            
            if hash_result.returncode != 0:
                return {"success": False, "error": hash_result.stderr.strip() or "Failed to get stash hash"}
            
            stash_hash = hash_result.stdout.strip()
            
            # Drop the old stash
            drop_result = subprocess.run(
                ["git", "stash", "drop", f"refs/stash@{{{stash_index}}}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if drop_result.returncode != 0:
                return {"success": False, "error": drop_result.stderr.strip() or "Failed to drop stash"}
            
            # Store with new message
            trimmed_message = new_message.strip()
            store_cmd = ["git", "stash", "store"]
            if trimmed_message:
                store_cmd.extend(["-m", trimmed_message])
            store_cmd.append(stash_hash)
            
            store_result = subprocess.run(
                store_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path_str,
            )
            
            if store_result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = store_result.stderr.strip() or store_result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

