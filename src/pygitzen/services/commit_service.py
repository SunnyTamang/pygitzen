"""Commit service for commit operations and filtering.

Pure business logic - no UI dependencies.
"""

from typing import Optional

from ..git_service import CommitInfo, GitService


class CommitService:
    """Service for commit-related operations."""

    def __init__(self, git_service: GitService) -> None:
        """Initialize commit service.
        
        Args:
            git_service: GitService instance for git operations.
        """
        self.git = git_service

    def load_commits(
        self, branch: str, max_count: int = 200, skip: int = 0
    ) -> list[CommitInfo]:
        """Load commits for a branch.
        
        Args:
            branch: Branch name to load commits from.
            max_count: Maximum number of commits to load.
            skip: Number of commits to skip (for pagination).
        
        Returns:
            List of CommitInfo objects.
        """
        return self.git.list_commits(branch, max_count=max_count, skip=skip)

    def count_commits(self, branch: str) -> int:
        """Count total commits for a branch.
        
        Args:
            branch: Branch name to count commits for.
        
        Returns:
            Total number of commits.
        """
        return self.git.count_commits(branch)

    def fuzzy_match(self, query: str, text: str) -> float:
        """Simple fuzzy matching algorithm. Returns a score between 0 and 1.
        
        Args:
            query: Search query string.
            text: Text to search in.
        
        Returns:
            Score between 0.0 and 1.0 (higher = better match).
        """
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
        for i in range(len(query)):
            for j in range(i + 1, len(query) + 1):
                substring = query[i:j]
                if substring in text_lower:
                    max_match = max(max_match, len(substring))

        if max_match > 0:
            # Score based on how much of the query matched
            return max_match / len(query) * 0.3

        return 0.0

    def filter_commits(
        self, commits: list[CommitInfo], query: str
    ) -> list[CommitInfo]:
        """Filter commits using fuzzy search on commit messages.
        
        Args:
            commits: List of commits to filter.
            query: Search query string.
        
        Returns:
            Filtered list of commits, sorted by match score (best first).
        """
        if not query or not query.strip():
            return commits

        query = query.strip().lower()

        # Score each commit
        scored_commits = []
        for commit in commits:
            # Search in summary (commit message)
            summary_score = self.fuzzy_match(query, commit.summary)

            # Search in author name
            author_score = self.fuzzy_match(query, commit.author)

            # Combine scores (summary is more important)
            combined_score = summary_score * 0.8 + author_score * 0.2

            if combined_score > 0:
                scored_commits.append((combined_score, commit))

        # Sort by score (highest first)
        scored_commits.sort(key=lambda x: x[0], reverse=True)

        # Return just the commits (without scores)
        return [commit for _, commit in scored_commits]

    def get_commit_diff(self, sha: str) -> str:
        """Get diff for a commit.
        
        Args:
            sha: Commit SHA.
        
        Returns:
            Diff text as string.
        """
        return self.git.get_commit_diff(sha)

