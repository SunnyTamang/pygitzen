"""Stash service for stash operations.

Pure business logic - no UI dependencies.
"""

from typing import Optional, Tuple

from ..git_service import StashInfo, GitService


class StashService:
    """Service for stash-related operations."""

    def __init__(self, git_service: GitService) -> None:
        """Initialize stash service.
        
        Args:
            git_service: GitService instance for git operations.
        """
        self.git = git_service

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
            
            # Get repo_path from git_service
            repo_path = getattr(self.git, 'repo_path', None)
            if repo_path is None:
                if hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                    repo_path = self.git.repo.path
                else:
                    repo_path = "."
            
            repo_path_str = str(repo_path) if repo_path else "."
            python_git = GitService(repo_path_str)
            return python_git.list_stashes()

    def get_stash_diff(self, stash_index: int) -> Tuple[str, str]:
        """Get diff and stat for a stash.
        
        Args:
            stash_index: Stash index (0-based).
        
        Returns:
            Tuple of (diff_text, stat_text).
        """
        import subprocess
        from pathlib import Path
        
        # Get repo_path
        repo_path = getattr(self.git, 'repo_path', None)
        if repo_path is None:
            if hasattr(self.git, 'repo') and hasattr(self.git.repo, 'path'):
                repo_path = self.git.repo.path
            else:
                repo_path = "."
        
        repo_path_str = str(repo_path) if repo_path else "."
        
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

