"""Branch service for branch operations and sync status calculation.

Pure business logic - no UI dependencies.
"""

import subprocess
from pathlib import Path
from typing import Optional

from ..git_service import BranchInfo, GitService


class BranchService:
    """Service for branch-related operations."""

    def __init__(self, git_service: GitService, repo_path: Path | str) -> None:
        """Initialize branch service.
        
        Args:
            git_service: GitService instance for git operations.
            repo_path: Repository root path (for subprocess calls).
        """
        self.git = git_service
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path

    def calculate_branch_sync_status(self, branch: str) -> dict:
        """Calculate sync status (behind/ahead counts) for a branch.
        
        Args:
            branch: Branch name to calculate sync status for.
        
        Returns:
            Dict with keys: 'behind', 'ahead', 'synced', 'upstream'
        """
        repo_path_str = str(self.repo_path)
        sync_status = {"behind": 0, "ahead": 0, "synced": False, "upstream": None}

        try:
            # Get upstream tracking branch
            upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{u}}"]
            upstream_result = subprocess.run(
                upstream_cmd,
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )

            if upstream_result.returncode == 0:
                upstream = upstream_result.stdout.strip()
                sync_status["upstream"] = upstream

                # Calculate behind/ahead using git rev-list --left-right --count
                # Format: <behind>	<ahead> (tab-separated)
                rev_list_cmd = [
                    "git",
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{upstream}...{branch}",
                ]
                rev_list_result = subprocess.run(
                    rev_list_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=repo_path_str,
                )

                if rev_list_result.returncode == 0:
                    parts = rev_list_result.stdout.strip().split("\t")
                    if len(parts) == 2:
                        behind = (
                            int(parts[0].strip())
                            if parts[0].strip().isdigit()
                            else 0
                        )
                        ahead = (
                            int(parts[1].strip())
                            if parts[1].strip().isdigit()
                            else 0
                        )
                        sync_status["behind"] = behind
                        sync_status["ahead"] = ahead
                        sync_status["synced"] = (behind == 0 and ahead == 0)
        except Exception:
            # If sync status calculation fails, return default values
            pass

        return sync_status

    def calculate_all_branches_sync_status(
        self, branches: list[BranchInfo]
    ) -> dict[str, dict]:
        """Calculate sync status for all branches.
        
        Args:
            branches: List of BranchInfo objects.
        
        Returns:
            Dict mapping branch name -> sync status dict.
        """
        sync_status_map: dict[str, dict] = {}

        for branch in branches:
            branch_name = branch.name
            sync_status = self.calculate_branch_sync_status(branch_name)
            sync_status_map[branch_name] = sync_status

        return sync_status_map

    def create_branch(
        self, name: str, base: str | None = None, git_service: Optional[GitService] = None
    ) -> dict:
        """Create a new branch.
        
        Args:
            name: Name of the new branch.
            base: Base branch to create from (None = current branch).
            git_service: GitService instance (optional, uses self.git if not provided).
        
        Returns:
            Dict with 'success', 'error', 'branch' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            if base:
                cmd = ["git", "checkout", "-b", name, base]
            else:
                cmd = ["git", "checkout", "-b", name]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None, "branch": name}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg, "branch": None}
        except Exception as e:
            return {"success": False, "error": str(e), "branch": None}

    def delete_branch(
        self, name: str, force: bool = False, git_service: Optional[GitService] = None
    ) -> dict:
        """Delete a local branch.
        
        Args:
            name: Branch name to delete.
            force: If True, force delete even if not merged.
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            cmd = ["git", "branch"]
            if force:
                cmd.append("-D")
            else:
                cmd.append("-d")
            cmd.append(name)

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def delete_remote_branch(
        self, name: str, remote: str = "origin", git_service: Optional[GitService] = None
    ) -> dict:
        """Delete a remote branch.
        
        Args:
            name: Branch name to delete.
            remote: Remote name (default: "origin").
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            cmd = ["git", "push", remote, "--delete", name]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def has_remote_tracking(self, branch_name: str) -> tuple[bool, str | None]:
        """Check if a branch has a remote tracking branch.
        
        Args:
            branch_name: Local branch name.
        
        Returns:
            Tuple of (has_remote: bool, remote_name: str | None).
            remote_name is in format "origin/branch-name" if exists.
        """
        repo_path_str = str(self.repo_path)
        
        try:
            # Check for upstream tracking branch
            upstream_cmd = ["git", "rev-parse", "--abbrev-ref", f"{branch_name}@{{u}}"]
            upstream_result = subprocess.run(
                upstream_cmd,
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )
            
            if upstream_result.returncode == 0:
                upstream = upstream_result.stdout.strip()
                return (True, upstream)
            
            # Fallback: Check if remote ref exists
            remote_ref_cmd = ["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch_name}"]
            remote_ref_result = subprocess.run(
                remote_ref_cmd,
                capture_output=True,
                text=True,
                timeout=2,
                cwd=repo_path_str,
            )
            
            if remote_ref_result.returncode == 0:
                return (True, f"origin/{branch_name}")
            
            return (False, None)
        except Exception:
            return (False, None)

    def rename_branch(
        self, old: str, new: str, git_service: Optional[GitService] = None
    ) -> dict:
        """Rename a branch.
        
        Args:
            old: Current branch name.
            new: New branch name.
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error', 'branch' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            cmd = ["git", "branch", "-m", old, new]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None, "branch": new}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg, "branch": None}
        except Exception as e:
            return {"success": False, "error": str(e), "branch": None}

    def checkout_branch(
        self, name: str, git_service: Optional[GitService] = None
    ) -> dict:
        """Checkout a branch.
        
        Args:
            name: Branch name to checkout.
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error', 'branch' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            cmd = ["git", "checkout", name]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None, "branch": name}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg, "branch": None}
        except Exception as e:
            return {"success": False, "error": str(e), "branch": None}

    def merge_branch(
        self, source: str, target: str, git_service: Optional[GitService] = None
    ) -> dict:
        """Merge source branch into target branch.
        
        Args:
            source: Source branch to merge from.
            target: Target branch to merge into (current branch).
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            # First checkout target branch
            checkout_cmd = ["git", "checkout", target]
            checkout_result = subprocess.run(
                checkout_cmd,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path_str,
            )

            if checkout_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Failed to checkout {target}: {checkout_result.stderr.strip()}",
                }

            # Then merge source into target
            merge_cmd = ["git", "merge", source]
            merge_result = subprocess.run(
                merge_cmd, capture_output=True, text=True, timeout=30, cwd=repo_path_str
            )

            if merge_result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = merge_result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def push_branch(
        self, name: str, git_service: Optional[GitService] = None
    ) -> dict:
        """Push a branch to remote.
        
        Args:
            name: Branch name to push.
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            cmd = ["git", "push", "origin", name]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_upstream(
        self, branch: str, upstream: str, git_service: Optional[GitService] = None
    ) -> dict:
        """Set upstream tracking branch.
        
        Args:
            branch: Local branch name.
            upstream: Upstream branch (e.g., "origin/main").
            git_service: GitService instance (optional).
        
        Returns:
            Dict with 'success', 'error' keys.
        """
        git = git_service or self.git
        repo_path_str = str(self.repo_path)

        try:
            cmd = ["git", "branch", "--set-upstream-to", upstream, branch]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, cwd=repo_path_str
            )

            if result.returncode == 0:
                return {"success": True, "error": None}
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                return {"success": False, "error": error_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

