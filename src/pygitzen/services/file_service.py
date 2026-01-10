"""File service for file-related operations.

Pure business logic - no UI dependencies.
"""

import subprocess
from pathlib import Path
from typing import Tuple


class FileService:
    """Service for file-related operations."""

    def __init__(self, repo_path: Path | str) -> None:
        """Initialize file service.
        
        Args:
            repo_path: Repository root path.
        """
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path

    def get_file_diff(self, file_path: str, staged: bool = False, untracked: bool = False) -> Tuple[str, str]:
        """Get diff for a file.
        
        Args:
            file_path: Path to the file.
            staged: Whether to show staged diff (default: False for unstaged).
            untracked: Whether the file is untracked (default: False).
        
        Returns:
            Tuple of (diff_text, stat_text). Both are empty strings on error.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            # For untracked files, use --no-index to compare against /dev/null
            if untracked and not staged:
                # Show untracked file as new file
                result = subprocess.run(
                    ["git", "diff", "--no-index", "--", "/dev/null", file_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str,
                )
            else:
                # For tracked files, use regular diff
                cmd = ["git", "diff"]
                if staged:
                    cmd.append("--cached")
                cmd.extend(["--", file_path])
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str,
                )
            
            if result.returncode == 0:
                diff_text = result.stdout
            else:
                # For untracked files, git diff --no-index returns non-zero
                # but still outputs the diff, so check stdout
                if untracked and result.stdout:
                    diff_text = result.stdout
                else:
                    diff_text = ""
            
            # Get stat summary
            stat_result = subprocess.run(
                ["git", "diff", "--stat"] + (["--cached"] if staged else []) + ["--", file_path],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path_str,
            )
            
            stat_text = stat_result.stdout if stat_result.returncode == 0 else ""
            
            return (diff_text, stat_text)
        except Exception:
            return ("", "")
    
    def discard_file_changes(self, file_path: str, staged: bool = False, untracked: bool = False) -> dict:
        """Discard changes for a file.
        
        Args:
            file_path: Path to the file.
            staged: Whether to discard staged changes (True) or unstaged changes (False).
            untracked: Whether the file is untracked.
        
        Returns:
            Dictionary with 'success' (bool) and 'error' (str) keys.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            # For untracked files, remove the file
            if untracked:
                from pathlib import Path
                full_path = self.repo_path / file_path
                if full_path.exists():
                    full_path.unlink()
                    return {"success": True, "error": ""}
                else:
                    return {"success": False, "error": "File does not exist"}
            
            # For staged changes, use git reset
            if staged:
                result = subprocess.run(
                    ["git", "reset", "--", file_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str,
                )
            else:
                # For unstaged changes, use git checkout
                result = subprocess.run(
                    ["git", "checkout", "--", file_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str,
                )
            
            if result.returncode == 0:
                return {"success": True, "error": ""}
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

