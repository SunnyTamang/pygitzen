"""Tag service for tag operations.

Pure business logic - no UI dependencies.
"""

from typing import Optional

from ..git_service import TagInfo, GitService


class TagService:
    """Service for tag-related operations."""

    def __init__(self, git_service: GitService) -> None:
        """Initialize tag service.
        
        Args:
            git_service: GitService instance for git operations.
        """
        self.git = git_service

    def load_tags(self) -> list[TagInfo]:
        """Load all tags from the repository.
        
        Returns:
            List of TagInfo objects.
        """
        # Check if method exists (Cython version might not have it)
        if hasattr(self.git, 'list_tags'):
            return self.git.list_tags()
        else:
            # Fallback: create a Python GitService instance
            # This handles the case where Cython version doesn't have list_tags
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
            return python_git.list_tags()

    def get_tag_info(self, tag: str) -> dict:
        """Get detailed information about a tag.
        
        Args:
            tag: Tag name.
        
        Returns:
            Dict with tag information (message, commit, etc.).
        """
        # This would use git_service methods if available
        # For now, return basic info
        return {"name": tag, "message": None}

