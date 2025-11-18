from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import stat


@dataclass
class BranchInfo:
    name: str
    head_sha: str
    timestamp: int = 0  # Unix timestamp of last commit (0 if not available)


@dataclass
class CommitInfo:
    sha: str
    summary: str
    author: str
    timestamp: int
    pushed: bool = False  # Whether commit exists on remote
    merged: bool = False  # Whether commit exists on main/master branch


@dataclass
class FileStatus:
    path: str
    status: str  # 'modified', 'staged', 'untracked', 'deleted', 'renamed'
    staged: bool  # Whether changes are staged
    unstaged: bool = False  # Whether changes are unstaged (for files with both)


@dataclass
class StashInfo:
    index: int  # Stash index (0 = most recent)
    branch: str  # Branch where stash was created
    message: str  # Stash message
    sha: str  # Stash commit SHA
    timestamp: int = 0  # Unix timestamp of stash creation (0 if not available)


class GitService:
    def __init__(self, start_dir: Path | str = ".") -> None:
        self.repo_path = self._find_repo_root(Path(start_dir))
        # Cache for commit counts per branch
        self._commit_count_cache: dict[str, int] = {}
        # Track HEAD SHA for cache invalidation
        self._last_head_sha: dict[str, str] = {}
        # Cache for remote commits per branch
        self._remote_commits_cache: dict[str, set[str]] = {}
        # Cache for branch info (name -> branch_info dict)
        self._branch_info_cache: dict[str, dict] = {}
        # Cache for current branch name (invalidated on checkout)
        self._current_branch_cache: str | None = None
        # Cache for base branch existence (main/master)
        self._base_branch_cache: dict[str, bool] = {}

    @staticmethod
    def _find_repo_root(path: Path) -> Path:
        current = path.resolve()
        while True:
            git_dir = current / ".git"
            if git_dir.exists() and git_dir.is_dir():
                return current
            if current.parent == current:
                raise ValueError(f"No .git found from {path}")
            current = current.parent

    def _is_ignored(self, file_path: str) -> bool:
        """Check if a file is ignored by .gitignore rules."""
        import fnmatch
        
        # Read .gitignore file
        gitignore_path = self.repo_path / ".gitignore"
        if not gitignore_path.exists():
            return False
        
        try:
            with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                gitignore_lines = f.readlines()
        except Exception:
            return False
        
        # Normalize file path (use forward slashes, relative to repo root)
        normalized_path = file_path.replace("\\", "/")
        path_parts = normalized_path.split("/")
        
        # Track if file is ignored (last matching pattern wins)
        is_ignored = False
        
        # Check each pattern in .gitignore
        for line in gitignore_lines:
            # Strip whitespace and comments
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            # Handle negation patterns
            is_negation = line.startswith("!")
            if is_negation:
                pattern = line[1:].strip()
            else:
                pattern = line
            
            if not pattern:
                continue
            
            # Remove trailing slash (directory marker, but still match files)
            pattern = pattern.rstrip("/")
            
            # Convert gitignore pattern to fnmatch pattern
            # Replace ** with * for fnmatch (simplified)
            fnmatch_pattern = pattern.replace("**", "*")
            
            # Handle patterns starting with /
            if pattern.startswith("/"):
                # Match from repository root only
                pattern = pattern[1:]
                fnmatch_pattern = fnmatch_pattern[1:]
                # Match exact path or prefix
                if fnmatch.fnmatch(normalized_path, fnmatch_pattern) or \
                   normalized_path.startswith(pattern + "/"):
                    is_ignored = not is_negation
            else:
                # Match anywhere in the path
                # Check if pattern matches any directory or file name
                matched = False
                # Check full path
                if fnmatch.fnmatch(normalized_path, fnmatch_pattern):
                    matched = True
                # Check each path segment
                for i in range(len(path_parts)):
                    check_path = "/".join(path_parts[i:])
                    if fnmatch.fnmatch(check_path, fnmatch_pattern) or \
                       fnmatch.fnmatch(path_parts[i], fnmatch_pattern):
                        matched = True
                        break
                
                if matched:
                    is_ignored = not is_negation
        
        return is_ignored

    def list_branches(self) -> List[BranchInfo]:
        """List all local branches using git for-each-ref with timestamps."""
        import subprocess
        import time
        
        result: List[BranchInfo] = []
        try:
            # Use git for-each-ref to get branches, SHAs, and commit timestamps
            # Format: name|sha|timestamp
            cmd = ['git', 'for-each-ref', 'refs/heads/', '--format=%(refname:short)|%(objectname)|%(committerdate:unix)']
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if process.returncode == 0:
                for line in process.stdout.strip().split('\n'):
                    if not line or '|' not in line:
                        continue
                    parts = line.split('|')
                    name = parts[0].strip()
                    sha = parts[1].strip() if len(parts) > 1 else ""
                    timestamp = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 0
                    result.append(BranchInfo(name=name, head_sha=sha, timestamp=timestamp))
        except Exception:
            # If git command fails, return empty list
            pass
        
        result.sort(key=lambda b: b.name.lower())
        return result

    def list_remotes(self) -> List[BranchInfo]:
        """List all remotes (not remote branches) using git remote.
        
        Returns:
            List of remotes as BranchInfo objects (name only, no SHA needed)
        """
        import subprocess
        
        result: List[BranchInfo] = []
        try:
            # Use git remote to get remote names (like Lazygit)
            cmd = ['git', 'remote']
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if process.returncode == 0:
                for line in process.stdout.strip().split('\n'):
                    remote_name = line.strip()
                    if remote_name:
                        result.append(BranchInfo(name=remote_name, head_sha="", timestamp=0))
        except Exception:
            # If git command fails, return empty list
            pass
        
        # Sort with origin first (like Lazygit)
        result.sort(key=lambda b: (b.name != "origin", b.name.lower()))
        return result

    def list_tags(self, max_count: int = 0, skip: int = 0, get_timestamps: bool = True) -> tuple[List[BranchInfo], int]:
        """List tags using git tag --list (like Lazygit).
        
        KEY FINDING: Lazygit loads ALL tags at once (no pagination) because:
        - git tag --list -n --sort=-creatordate is fast (~0.5s for 56k tags)
        - They don't show recency (no timestamps needed)
        - ASYNC mode makes it non-blocking
        
        Args:
            max_count: Maximum number of tags to return (0 = all tags, like Lazygit)
            skip: Number of tags to skip (for pagination, but Lazygit doesn't paginate)
            get_timestamps: Whether to fetch timestamps for recency display (adds overhead)
        
        Returns:
            Tuple of (list of tags, total count). Tags are sorted by -creatordate (most recent first, like Lazygit)
        """
        import subprocess
        import re
        
        result: List[BranchInfo] = []
        total_count = 0
        
        try:
            # Use EXACTLY the same command as Lazygit: git tag --list -n --sort=-creatordate
            # This is fast (~0.5s for 56k tags) because git optimizes tag listing
            # Format: "tag_name    message" or "tag_name"
            cmd = ['git', 'tag', '--list', '-n', '--sort=-creatordate']
            
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors='replace',  # Handle binary data in tag messages
                timeout=30,  # Increased timeout for very large repos (56k+ tags)
                cwd=str(self.repo_path)
            )
            
            if process.returncode == 0:
                lines = process.stdout.strip().split('\n')
                total_count = len([line for line in lines if line.strip()])
                
                # Apply pagination only if max_count is specified (Lazygit doesn't paginate)
                if max_count > 0:
                    paginated_lines = lines[skip:skip + max_count]
                else:
                    paginated_lines = lines[skip:] if skip > 0 else lines
                
                # Parse lines using the SAME regex as Lazygit
                # Lazygit uses: regexp.MustCompile(`^([^\s]+)(\s+)?(.*)$`)
                line_regex = re.compile(r'^([^\s]+)(\s+)?(.*)$')
                
                # Parse tag names and messages (same as Lazygit)
                tag_data = {}  # {tag_name: message}
                tag_names = []
                
                for line in paginated_lines:
                    if not line.strip():
                        continue
                    
                    # Parse line: "tag_name    message" or "tag_name" (same as Lazygit)
                    match = line_regex.match(line)
                    if match:
                        name = match.group(1).strip()
                        message = match.group(3).strip() if len(match.groups()) > 2 and match.group(3) else ""
                        tag_data[name] = message
                        tag_names.append(name)
                
                # Get timestamps ONLY if requested (adds overhead but needed for recency display)
                # Lazygit doesn't do this - they don't show recency for tags
                timestamp_map = {}  # {tag_name: timestamp}
                if get_timestamps and tag_names:
                    try:
                        # OPTIMIZED: Use single git for-each-ref call for all tags (faster than individual calls)
                        # Only get timestamps for the tags we're displaying (not all 56k)
                        timestamp_cmd = ['git', 'for-each-ref', '--format=%(refname:short)|%(creatordate:unix)']
                        # Add specific tag refs (only the ones we need)
                        for name in tag_names:
                            timestamp_cmd.append(f'refs/tags/{name}')
                        
                        timestamp_process = subprocess.run(
                            timestamp_cmd,
                            capture_output=True,
                            text=True,
                            errors='replace',
                            timeout=10,  # Timeout for timestamp lookup
                            cwd=str(self.repo_path)
                        )
                        
                        if timestamp_process.returncode == 0:
                            for line in timestamp_process.stdout.strip().split('\n'):
                                if '|' in line:
                                    parts = line.split('|', 1)
                                    tag_name = parts[0].strip()
                                    timestamp_str = parts[1].strip() if len(parts) > 1 else "0"
                                    if timestamp_str and timestamp_str.isdigit():
                                        timestamp_map[tag_name] = int(timestamp_str)
                    except Exception:
                        # If timestamp lookup fails, continue without timestamps (like Lazygit)
                        pass
                
                # Build result list with tags, messages, and timestamps
                for name in tag_names:
                    message = tag_data.get(name, "")
                    timestamp = timestamp_map.get(name, 0) if get_timestamps else 0
                    
                    tag_info = BranchInfo(name=name, head_sha="", timestamp=timestamp)
                    # Store message as a custom attribute (same as Lazygit stores in Tag.Message)
                    setattr(tag_info, 'message', message)
                    result.append(tag_info)
        except Exception:
            # If git command fails, return empty list
            pass
        
        # Tags are already in reverse chronological order (most recent first) from --sort=-creatordate
        return (result, total_count)

    # _iter_commits removed - no longer needed without dulwich

    def get_tag_timestamps_batch(self, tag_names: list[str]) -> dict[str, int]:
        """Get timestamps for a batch of tags (optimized for displayed tags only).
        
        Args:
            tag_names: List of tag names to get timestamps for
            
        Returns:
            Dictionary mapping tag names to timestamps (0 if not available)
        """
        import subprocess
        
        if not tag_names:
            return {}
        
        timestamp_map = {}
        try:
            # Use single git for-each-ref call for all tags (faster than individual calls)
            # Only get timestamps for the tags we're displaying (not all 56k)
            timestamp_cmd = ['git', 'for-each-ref', '--format=%(refname:short)|%(creatordate:unix)']
            # Add specific tag refs (only the ones we need)
            for name in tag_names:
                timestamp_cmd.append(f'refs/tags/{name}')
            
            timestamp_process = subprocess.run(
                timestamp_cmd,
                capture_output=True,
                text=True,
                errors='replace',
                timeout=10,  # Timeout for timestamp lookup
                cwd=str(self.repo_path)
            )
            
            if timestamp_process.returncode == 0:
                for line in timestamp_process.stdout.strip().split('\n'):
                    if '|' in line:
                        parts = line.split('|', 1)
                        tag_name = parts[0].strip()
                        timestamp_str = parts[1].strip() if len(parts) > 1 else "0"
                        if timestamp_str and timestamp_str.isdigit():
                            timestamp_map[tag_name] = int(timestamp_str)
        except Exception:
            # If timestamp lookup fails, return empty map (tags will show without recency)
            pass
        
        return timestamp_map
    
    def _get_remote_commits(self, branch: str) -> set[str]:
        """Get set of commit SHAs that exist on remote using git rev-list."""
        import subprocess
        
        # Check cache first
        cache_key = f"{branch}_remote_commits"
        if cache_key in self._remote_commits_cache:
            return self._remote_commits_cache[cache_key]
        
        remote_commits = set()
        try:
            # Use git rev-list to get commits from remote branch
            remote_branch = f"origin/{branch}"
            cmd = ['git', 'rev-list', remote_branch, '--max-count=200']
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if process.returncode == 0:
                for line in process.stdout.strip().split('\n'):
                    if line.strip():
                        remote_commits.add(line.strip())
        except Exception:
            # Remote not available or not configured
            pass
        
        # Cache the result
        self._remote_commits_cache[cache_key] = remote_commits
        return remote_commits
    
    def get_merge_base(self, branch: str, base_branch: str = "main") -> str | None:
        """Find the merge-base (common ancestor) between branch and base_branch."""
        import subprocess
        
        if base_branch == branch:
            return None
        
        # Check if base branch exists using git rev-parse
        try:
            check_base = subprocess.run(
                ['git', 'rev-parse', '--verify', base_branch],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=str(self.repo_path)
            )
            if check_base.returncode != 0:
                # Try master if main doesn't exist
                if base_branch == "main":
                    base_branch = "master"
                    check_base = subprocess.run(
                        ['git', 'rev-parse', '--verify', base_branch],
                        capture_output=True,
                        text=True,
                        timeout=1,
                        cwd=str(self.repo_path)
                    )
                    if check_base.returncode != 0:
                        return None
                else:
                    return None
        except Exception:
            return None
        
        # Check if branch exists
        try:
            check_branch = subprocess.run(
                ['git', 'rev-parse', '--verify', branch],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=str(self.repo_path)
            )
            if check_branch.returncode != 0:
                return None
        except Exception:
            return None
        
        try:
            # Use git merge-base command for reliable results
            result = subprocess.run(
                ['git', 'merge-base', base_branch, branch],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            if result.returncode == 0:
                merge_base_sha = result.stdout.strip()
                return merge_base_sha
        except Exception:
            pass
        
        return None

    def list_commits_native(self, branch: str, max_count: int = 200, skip: int = 0, show_full_history: bool = False, timeout: int = 30) -> List[CommitInfo]:
        """
        TESTING: Git-native version of list_commits using 'git log' command.
        This has timeout support and is faster for large repos than dulwich iteration.
        """
        import subprocess
        import os
        from datetime import datetime
        
        commits: List[CommitInfo] = []
        
        try:
            # Build git log command
            # Format: %H (full SHA) %x00 %an (author name) %x00 %ae (author email) %x00 %at (author timestamp) %x00 %s (subject)
            # Using null separator for reliable parsing
            cmd = [
                "git", "log",
                branch,
                f"--max-count={max_count}",
                f"--skip={skip}" if skip > 0 else None,
                "--pretty=format:%H%x00%an%x00%ae%x00%at%x00%s",
                "--no-decorate",
            ]
            # Remove None values
            cmd = [c for c in cmd if c is not None]
            
            # For feature branches, exclude commits from base branch
            if not show_full_history and branch not in ["main", "master"]:
                base_branch_names = ["main", "master"]
                for base_name in base_branch_names:
                    # Check if base branch exists using git rev-parse
                    check_base = subprocess.run(
                        ['git', 'rev-parse', '--verify', base_name],
                        capture_output=True,
                        text=True,
                        timeout=1,
                        cwd=str(self.repo_path)
                    )
                    if check_base.returncode == 0 and base_name != branch:
                        # Use git log with exclusion: branch ^base
                        cmd = [
                            "git", "log",
                            branch,
                            f"^{base_name}",  # Exclude commits from base branch
                            f"--max-count={max_count}",
                            f"--skip={skip}" if skip > 0 else None,
                            "--pretty=format:%H%x00%an%x00%ae%x00%at%x00%s",
                            "--no-decorate",
                        ]
                        cmd = [c for c in cmd if c is not None]
                        break
            
            # Run git log with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.repo_path)
            )
            
            if result.returncode != 0:
                # If git log fails, return empty list
                return []
            
            # Get remote commits for push status (reuse existing method)
            remote_commits = self._get_remote_commits(branch)
            
            # Parse output
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                
                parts = line.split("\x00")
                if len(parts) >= 5:
                    sha = parts[0].strip()
                    author_name = parts[1].strip()
                    author_email = parts[2].strip()
                    timestamp_str = parts[3].strip()
                    summary = parts[4].strip()
                    
                    # Combine author name and email
                    author = f"{author_name} <{author_email}>" if author_email else author_name
                    
                    # Parse timestamp
                    try:
                        timestamp = int(timestamp_str)
                    except ValueError:
                        timestamp = 0
                    
                    # Check if pushed
                    is_pushed = sha in remote_commits
                    
                    commits.append(
                        CommitInfo(
                            sha=sha,
                            summary=summary,
                            author=author,
                            timestamp=timestamp,
                            pushed=is_pushed,
                        )
                    )
                    
                    if len(commits) >= max_count:
                        break
            
            return commits
            
        except subprocess.TimeoutExpired:
            # Timeout - return what we have
            return commits
        except Exception as e:
            # On any error, return empty list
            try:
                with open("debug_list_commits_native.log", "a", encoding="utf-8") as f:
                    f.write(f"Error in list_commits_native for {branch}: {type(e).__name__}: {e}\n")
            except:
                pass
            return []
    
    def list_commits(self, branch: str, max_count: int = 200, skip: int = 0, show_full_history: bool = False) -> List[CommitInfo]:
        """List commits - uses native git implementation."""
        return self.list_commits_native(branch, max_count, skip, show_full_history)

    def count_commits_native(self, branch: str, timeout: int = 10) -> int:
        """
        TESTING: Git-native version of count_commits using 'git rev-list --count' command.
        This has timeout support and avoids dulwich pack file corruption issues.
        """
        import subprocess
        
        try:
            # For main/master branches, use simple count
            if branch in ["main", "master"]:
                result = subprocess.run(
                    ['git', 'rev-list', '--count', branch],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(self.repo_path)
                )
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        return int(result.stdout.strip())
                    except ValueError:
                        pass
            
            # For other branches, exclude commits from base branch
            base_branch_names = ["main", "master"]
            for base_name in base_branch_names:
                # Check if base branch exists using git rev-parse
                check_base = subprocess.run(
                    ['git', 'rev-parse', '--verify', base_name],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    cwd=str(self.repo_path)
                )
                if check_base.returncode != 0 or base_name == branch:
                    continue
                
                # Try to get merge-base
                merge_base_result = subprocess.run(
                    ['git', 'merge-base', base_name, branch],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(self.repo_path)
                )
                if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
                    merge_base = merge_base_result.stdout.strip()
                    # Count commits from merge-base to branch
                    count_result = subprocess.run(
                        ['git', 'rev-list', '--count', f'{merge_base}..{branch}'],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=str(self.repo_path)
                    )
                    if count_result.returncode == 0 and count_result.stdout.strip():
                        try:
                            return int(count_result.stdout.strip())
                        except ValueError:
                            pass
                break
        except subprocess.TimeoutExpired:
            # Timeout - return 0
            return 0
        except Exception as e:
            # Log error for debugging
            try:
                with open("debug_count_commits_native.log", "a", encoding="utf-8") as f:
                    f.write(f"Error in count_commits_native for {branch}: {type(e).__name__}: {e}\n")
            except:
                pass
            return 0
        
        return 0
    
    def count_commits(self, branch: str) -> int:
        """Count commits for a branch with caching."""
        import subprocess
        
        # Check cache first - but validate HEAD SHA hasn't changed
        if branch in self._commit_count_cache:
            # Check if HEAD has changed
            try:
                head_result = subprocess.run(
                    ['git', 'rev-parse', branch],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    cwd=str(self.repo_path)
                )
                if head_result.returncode == 0:
                    current_head_sha = head_result.stdout.strip()
                    if branch in self._last_head_sha and self._last_head_sha[branch] == current_head_sha:
                        # Cache is valid - return cached count
                        return self._commit_count_cache[branch]
                    # HEAD changed - update tracked SHA
                    self._last_head_sha[branch] = current_head_sha
            except Exception:
                # If HEAD check fails, use cache anyway (better than nothing)
                if branch in self._commit_count_cache:
                    return self._commit_count_cache[branch]
        
        # Cache miss or invalidated - fetch fresh count
        count = self.count_commits_native(branch, timeout=10)
        
        # Update cache
        self._commit_count_cache[branch] = count
        
        # Update HEAD SHA tracking
        try:
            head_result = subprocess.run(
                ['git', 'rev-parse', branch],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=str(self.repo_path)
            )
            if head_result.returncode == 0:
                self._last_head_sha[branch] = head_result.stdout.strip()
        except Exception:
            pass
        
        return count

    def get_commit_diff(self, sha_hex: str) -> str:
        """Get diff for a commit using git-native command (avoids dulwich hex_to_sha issues)."""
        import subprocess
        import re
        
        # Normalize SHA to ensure it's a proper 40-character hex string
        # Handle various formats (bytes, wrong length, etc.)
        if isinstance(sha_hex, bytes):
            if len(sha_hex) == 20:
                # Binary SHA, convert to hex
                sha_hex = sha_hex.hex()
            elif len(sha_hex) == 40:
                # Hex string as bytes, decode it
                sha_hex = sha_hex.decode('ascii')
            else:
                sha_hex = sha_hex.decode('ascii', errors='replace')
        
        sha_hex = str(sha_hex).strip()
        
        # Validate and fix SHA format
        if len(sha_hex) != 40 or not all(c in '0123456789abcdefABCDEF' for c in sha_hex):
            # Try to extract valid hex
            hex_match = re.search(r'[0-9a-fA-F]{40}', sha_hex)
            if hex_match:
                sha_hex = hex_match.group(0).lower()
            else:
                return f"Error: Invalid SHA format: {sha_hex[:20]}...\n"
        
        sha_hex = sha_hex.lower()
        
        try:
            # Use git show to get the diff (handles root commits automatically)
            # This avoids dulwich's hex_to_sha issues completely
            result = subprocess.run(
                ['git', 'show', sha_hex, '--no-color'],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self.repo_path)
            )
            
            if result.returncode == 0:
                # git show includes commit message, extract just the diff part
                # Look for the diff separator (usually starts with "diff --git")
                output = result.stdout
                diff_start = output.find('diff --git')
                if diff_start >= 0:
                    return output[diff_start:]
                # If no diff separator found, return everything (might be root commit or special case)
                return output
            else:
                # git show failed, log the error
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                try:
                    with open("debug_get_commit_diff.log", "a", encoding="utf-8") as f:
                        f.write(f"git show failed for {sha_hex}: returncode={result.returncode}, stderr={error_msg}\n")
                except:
                    pass
                # Return a helpful error message instead of trying dulwich fallback
                return f"Error: Could not get diff for commit {sha_hex[:8]}. git show failed: {error_msg[:100]}\n"
        except subprocess.TimeoutExpired:
            try:
                with open("debug_get_commit_diff.log", "a", encoding="utf-8") as f:
                    f.write(f"Timeout in git show for {sha_hex}\n")
            except:
                pass
            return f"Error: Timeout getting diff for commit {sha_hex[:8]}\n"
        except Exception as e:
            # Log error but continue to dulwich fallback
            try:
                with open("debug_get_commit_diff.log", "a", encoding="utf-8") as f:
                    f.write(f"Error in git-native get_commit_diff for {sha_hex}: {type(e).__name__}: {e}\n")
            except:
                pass
        
        # Fallback to dulwich is disabled because it causes AssertionError with hex_to_sha
        # The error has already been returned above if git show failed
        # This code should never be reached, but kept for safety
    
    def get_file_diff(self, file_path: str, staged: bool = False) -> str:
        """Get diff for a specific file (staged or unstaged changes)."""
        import subprocess
        
        try:
            if staged:
                # Show staged changes: git diff --cached <file>
                result = subprocess.run(
                    ['git', 'diff', '--cached', '--', file_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(self.repo_path)
                )
            else:
                # Show unstaged changes: git diff <file>
                result = subprocess.run(
                    ['git', 'diff', '--', file_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(self.repo_path)
                )
            
            if result.returncode == 0:
                return result.stdout if result.stdout else f"No changes in {file_path}"
            else:
                # For untracked files, git diff returns nothing, use git status instead
                if not staged:
                    # Check if file is untracked
                    status_result = subprocess.run(
                        ['git', 'status', '--porcelain', '--', file_path],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=str(self.repo_path)
                    )
                    if status_result.returncode == 0 and status_result.stdout.strip().startswith('??'):
                        # Untracked file - show file contents
                        try:
                            with open(self.repo_path / file_path, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read()
                            return f"diff --git a/{file_path} b/{file_path}\nnew file mode 100644\nindex 0000000..0000000\n--- /dev/null\n+++ b/{file_path}\n@@ -0,0 +1,{len(content.splitlines())} @@\n{content}"
                        except Exception:
                            return f"Untracked file: {file_path}"
                
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                return f"Error: Could not get diff for {file_path}. {error_msg[:100]}\n"
        except subprocess.TimeoutExpired:
            return f"Error: Timeout getting diff for {file_path}\n"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}\n"
        
        # OLD DULWICH FALLBACK (DISABLED - causes AssertionError)
        # try:
        #     # Ensure sha_hex is valid before converting to bytes
        #     if len(sha_hex) != 40 or not all(c in '0123456789abcdef' for c in sha_hex):
        #         return f"Error: Invalid SHA format for dulwich fallback: {sha_hex[:20]}...\n"
        #     
        #     sha = bytes.fromhex(sha_hex)
        #     commit: Commit = self.repo[sha]
        #     parents = commit.parents
        #     
        #     from dulwich.patch import write_tree_diff
        #     from dulwich.objects import Tree
        #     import io
        #
        #     buf = io.BytesIO()
        #     
        #     # Get Tree objects from tree SHAs (commit.tree and parent.tree are binary SHAs)
        #     # These need to be converted to Tree objects, not passed as binary SHAs
        #     commit_tree = self.repo[commit.tree] if commit.tree else None
        #     
        #     if not parents:
        #         # Root commit (no parent) - show all files as additions
        #         # Use empty tree (all zeros) as parent to show all files as new
        #         empty_tree = Tree()
        #         if commit_tree and isinstance(commit_tree, Tree):
        #             write_tree_diff(buf, self.repo.object_store, empty_tree, commit_tree)
        #     else:
        #         # Regular commit - show diff between parent and commit
        #         parent = self.repo[parents[0]]
        #         parent_tree = self.repo[parent.tree] if parent.tree else Tree()
        #         if commit_tree and isinstance(commit_tree, Tree) and isinstance(parent_tree, Tree):
        #             write_tree_diff(buf, self.repo.object_store, parent_tree, commit_tree)
        #     
        #     diff_text = buf.getvalue().decode(errors="replace")
        #     return diff_text
        # except Exception as e:
        #     # If dulwich also fails, return error message
        #     try:
        #         with open("debug_get_commit_diff.log", "a", encoding="utf-8") as f:
        #             f.write(f"Error in dulwich fallback get_commit_diff for {sha_hex}: {type(e).__name__}: {e}\n")
        #             import traceback
        #             f.write(f"Traceback:\n{traceback.format_exc()}\n")
        #     except:
        #         pass
        #     return f"Error: Could not get diff for commit {sha_hex[:8]}\n"
    
    def get_commit_refs_from_git_log(self, branch: str, commit_shas: List[str]) -> dict[str, dict]:
        """
        Get refs for multiple commits at once using git log (LazyGit optimization).
        Uses git log with %D format to get refs in a single call instead of per-commit lookups.
        
        Returns a dict mapping commit_sha -> refs dict.
        """
        import subprocess
        import os
        
        if not commit_shas:
            return {}
        
        result_map = {}
        
        # Initialize all commits with empty refs
        for sha in commit_shas:
            result_map[sha] = {
                "branches": [],
                "remote_branches": [],
                "tags": [],
                "is_head": False,
                "is_merge": False,
                "merge_parents": [],
            }
        
        try:
            original_cwd = os.getcwd()
            os.chdir(str(self.repo_path))
            try:
                # Use git log with %D format (ref names) - similar to LazyGit's approach
                # Format: %H (hash) %x00 %D (ref names) %x00 %P (parents)
                # This gets refs for all commits in one call
                cmd = [
                    "git", "log",
                    branch,
                    f"--max-count={len(commit_shas)}",
                    "--oneline",
                    "--pretty=format:%H%x00%D%x00%P%x00%s",
                    "--decorate-refs=refs/heads/*",
                    "--decorate-refs=refs/remotes/*",
                    "--decorate-refs=refs/tags/*",
                ]
                
                process = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=str(self.repo_path)
                )
                
                if process.returncode == 0:
                    # Parse output: each line is: SHA\x00REFS\x00PARENTS\x00SUMMARY
                    for line in process.stdout.strip().split("\n"):
                        if not line:
                            continue
                        parts = line.split("\x00")
                        if len(parts) >= 3:
                            sha = parts[0].strip()
                            refs_str = parts[1].strip() if len(parts) > 1 else ""
                            parents_str = parts[2].strip() if len(parts) > 2 else ""
                            
                            if sha in result_map:
                                # Parse refs string (e.g., "HEAD -> master, tag: v0.15.2, origin/main")
                                refs = result_map[sha]
                                
                                # Check if HEAD
                                if "HEAD" in refs_str:
                                    refs["is_head"] = True
                                
                                # Parse branches, remote branches, and tags
                                # Format: "HEAD -> master, tag: v0.15.2, origin/main"
                                ref_parts = [p.strip() for p in refs_str.split(",")]
                                for ref_part in ref_parts:
                                    ref_part = ref_part.strip()
                                    if not ref_part:
                                        continue
                                    
                                    # Skip HEAD -> part
                                    if "HEAD ->" in ref_part:
                                        # Extract branch name after "->"
                                        branch_name = ref_part.split("->")[-1].strip()
                                        if branch_name and branch_name not in refs["branches"]:
                                            refs["branches"].append(branch_name)
                                    elif ref_part.startswith("tag: "):
                                        # Tag: "tag: v0.15.2"
                                        tag_name = ref_part.replace("tag: ", "").strip()
                                        if tag_name and tag_name not in refs["tags"]:
                                            refs["tags"].append(tag_name)
                                    elif "/" in ref_part and not ref_part.startswith("tag:"):
                                        # Remote branch: "origin/main"
                                        if ref_part not in refs["remote_branches"]:
                                            refs["remote_branches"].append(ref_part)
                                    elif ref_part and not ref_part.startswith("HEAD"):
                                        # Local branch (without HEAD ->)
                                        if ref_part not in refs["branches"]:
                                            refs["branches"].append(ref_part)
                                
                                # Check if merge commit (multiple parents)
                                if parents_str:
                                    parent_list = [p.strip() for p in parents_str.split() if p.strip()]
                                    if len(parent_list) > 1:
                                        refs["is_merge"] = True
                                        refs["merge_parents"] = parent_list
            finally:
                os.chdir(original_cwd)
        except Exception:
            # Fallback: if git log fails, return empty refs (will be filled by get_commit_refs if needed)
            pass
        
        return result_map
    
    def get_commit_refs(self, commit_sha: str) -> dict:
        """Get branch references and metadata for a commit using native git commands."""
        import subprocess
        
        result = {
            "branches": [],  # Local branches pointing to this commit
            "remote_branches": [],  # Remote branches pointing to this commit
            "tags": [],  # Tags pointing to this commit
            "is_head": False,  # Whether this is HEAD
            "is_merge": False,  # Whether this is a merge commit
            "merge_parents": [],  # Parent commits if merge
        }
        
        try:
            # Check if this is HEAD
            head_result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=str(self.repo_path)
            )
            if head_result.returncode == 0 and head_result.stdout.strip() == commit_sha:
                result["is_head"] = True
            
            # Get branches containing this commit
            branch_result = subprocess.run(
                ['git', 'branch', '--contains', commit_sha],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=str(self.repo_path)
            )
            if branch_result.returncode == 0:
                for line in branch_result.stdout.strip().split('\n'):
                    branch = line.strip().lstrip('*').strip()
                    if branch:
                        result["branches"].append(branch)
            
            # Get remote branches containing this commit
            remote_branch_result = subprocess.run(
                ['git', 'branch', '-r', '--contains', commit_sha],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=str(self.repo_path)
            )
            if remote_branch_result.returncode == 0:
                for line in remote_branch_result.stdout.strip().split('\n'):
                    remote_branch = line.strip()
                    if remote_branch:
                        result["remote_branches"].append(remote_branch)
            
            # Get tags containing this commit
            tag_result = subprocess.run(
                ['git', 'tag', '--contains', commit_sha],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=str(self.repo_path)
            )
            if tag_result.returncode == 0:
                for line in tag_result.stdout.strip().split('\n'):
                    tag = line.strip()
                    if tag:
                        result["tags"].append(tag)
            
            # Check if merge commit (multiple parents)
            parents_result = subprocess.run(
                ['git', 'rev-list', '--parents', '-n', '1', commit_sha],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=str(self.repo_path)
            )
            if parents_result.returncode == 0:
                parts = parents_result.stdout.strip().split()
                if len(parts) > 2:  # commit_sha + at least 2 parents
                    result["is_merge"] = True
                    result["merge_parents"] = parts[1:]  # Skip commit_sha, get parents
        except Exception:
            pass
        
        return result
    
    def get_commit_message_full(self, commit_sha: str) -> dict:
        """
        Get full commit message and parse Signed-off-by lines.
        Returns dict with 'message' (full body) and 'signed_off_by' (list of signers).
        """
        import subprocess
        result = {
            "message": "",
            "signed_off_by": []
        }
        
        try:
            # Use git show to get full commit message
            process = subprocess.run(
                ['git', 'show', '--format=%B', '--no-patch', commit_sha],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if process.returncode == 0:
                full_message = process.stdout.strip()
                result["message"] = full_message
                
                # Parse Signed-off-by lines
                lines = full_message.split('\n')
                for line in lines:
                    line_stripped = line.strip()
                    if line_stripped.startswith('Signed-off-by:'):
                        # Extract signer info (name and email)
                        signer = line_stripped[len('Signed-off-by:'):].strip()
                        result["signed_off_by"].append(signer)
        except Exception:
            # If git command fails, return empty
            pass
        
        return result
    
    def get_branch_info(self, branch: str) -> dict:
        """Get information about a branch using native git commands (with caching)."""
        import subprocess
        
        # Check cache first
        if branch in self._branch_info_cache:
            cached_info = self._branch_info_cache[branch].copy()
            # Update is_current from current branch cache (may have changed)
            if self._current_branch_cache is not None:
                cached_info["is_current"] = (self._current_branch_cache == branch)
            return cached_info
        
        result = {
            "name": branch,
            "head_sha": None,
            "remote_tracking": None,  # e.g., "origin/main"
            "upstream": None,  # Upstream branch name
            "is_current": False,  # Whether this is the current branch
        }
        
        # Get current branch (cached)
        if self._current_branch_cache is None:
            try:
                current_branch_result = subprocess.run(
                    ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    cwd=str(self.repo_path)
                )
                if current_branch_result.returncode == 0:
                    self._current_branch_cache = current_branch_result.stdout.strip()
            except Exception:
                pass
        
        result["is_current"] = (self._current_branch_cache == branch) if self._current_branch_cache else False
        
        try:
            # Get branch SHA
            sha_result = subprocess.run(
                ['git', 'rev-parse', branch],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=str(self.repo_path)
            )
            if sha_result.returncode == 0:
                result["head_sha"] = sha_result.stdout.strip()
        except Exception:
            pass
        
        # Check remote tracking
        try:
            upstream_result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', f'{branch}@{{u}}'],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=str(self.repo_path)
            )
            if upstream_result.returncode == 0:
                upstream = upstream_result.stdout.strip()
                result["remote_tracking"] = upstream
                # Extract branch name from upstream (e.g., "origin/main" -> "main")
                if '/' in upstream:
                    result["upstream"] = upstream.split('/')[-1]
        except Exception:
            pass
        
        # Cache the result (excluding is_current which may change)
        cache_entry = result.copy()
        self._branch_info_cache[branch] = cache_entry
        
        return result
    
    def get_branch_sync_status(self, branch: str) -> dict:
        """Get sync status (behind/ahead counts) for a branch relative to its upstream.
        Returns dict with 'behind', 'ahead', 'synced', and 'upstream' keys.
        """
        import subprocess
        
        result = {
            "behind": 0,
            "ahead": 0,
            "synced": False,
            "upstream": None,
        }
        
        try:
            # Get upstream tracking branch
            upstream_result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', f'{branch}@{{u}}'],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=str(self.repo_path)
            )
            
            if upstream_result.returncode != 0:
                # No upstream configured
                return result
            
            upstream = upstream_result.stdout.strip()
            result["upstream"] = upstream
            
            # Use git rev-list --left-right to get commits that are in one but not the other
            # Format: < for commits in branch but not upstream (ahead)
            #         > for commits in upstream but not branch (behind)
            rev_list_cmd = ['git', 'rev-list', '--left-right', '--count', f'{upstream}...{branch}']
            rev_list_result = subprocess.run(
                rev_list_cmd,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if rev_list_result.returncode == 0:
                # Output format: "behind_count\tahead_count"
                parts = rev_list_result.stdout.strip().split('\t')
                if len(parts) == 2:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                    result["behind"] = behind
                    result["ahead"] = ahead
                    result["synced"] = (behind == 0 and ahead == 0)
        except Exception:
            # If calculation fails, return default values
            pass
        
        return result
    
    def invalidate_branch_info_cache(self, branch: str | None = None) -> None:
        """Invalidate branch info cache (call on branch checkout)."""
        if branch:
            self._branch_info_cache.pop(branch, None)
        else:
            self._branch_info_cache.clear()
        # Also invalidate current branch cache
        self._current_branch_cache = None

    # _find_in_tree removed - no longer needed without dulwich

    def get_file_status(self) -> List[FileStatus]:
        """Optimized version using native git status --porcelain (10x faster than dulwich)."""
        import subprocess
        
        # Try native git status first (much faster for large repos)
        try:
            # Use git status --porcelain for fast, parseable output
            # Format: XY filename (X=index, Y=working tree)
            result = subprocess.run(
                ['git', 'status', '--porcelain', '-u'],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self.repo_path)
            )
            # Debug: Log if git command fails
            if result.returncode != 0:
                try:
                    with open("debug_git_status.log", "a", encoding="utf-8") as f:
                        f.write(f"[ERROR] git status failed: returncode={result.returncode}, stderr={result.stderr}\n")
                except:
                    pass
            if result.returncode == 0:
                # Get list of actually staged files to verify (needed for edge cases)
                # Sometimes git status --porcelain shows "M " (staged) even when nothing is staged
                # This happens when the index was reset but git status hasn't updated
                staged_files_set = set()
                try:
                    staged_result = subprocess.run(
                        ['git', 'diff', '--cached', '--name-only'],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=str(self.repo_path)
                    )
                    if staged_result.returncode == 0:
                        staged_files_set = set(staged_result.stdout.strip().split('\n')) if staged_result.stdout.strip() else set()
                except Exception:
                    pass  # If verification fails, continue without it
                
                files = []
                output_lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
                # Debug: Log raw output
                try:
                    with open("debug_git_status.log", "a", encoding="utf-8") as f:
                        f.write(f"[DEBUG] Native git status output ({len(output_lines)} lines):\n")
                        for line in output_lines[:10]:  # Log first 10 lines
                            f.write(f"  {line}\n")
                except:
                    pass
                for line in output_lines:
                    if not line.strip():
                        continue
                    # Parse porcelain format: XY filename
                    # X = index status, Y = working tree status
                    # Common values: M=modified, A=added, D=deleted, R=renamed, C=copied, ??=untracked
                    # Format is usually "XY filename" (2 status chars + space + filename)
                    # But can also be "M filename" where M+space is status, filename starts immediately
                    if len(line) < 3:  # Need at least 2 status chars + 1 char filename
                        continue
                    # Ensure we have exactly 2 status chars - handle edge cases
                    if line[0] == '?' and line[1] == '?':
                        # Untracked file: "?? filename"
                        status_code = '??'
                        filename = line[3:].strip() if len(line) > 3 and line[2] == ' ' else line[2:].strip()
                    elif len(line) >= 2:
                        # Normal format: "XY filename" where XY are status chars
                        status_code = line[:2]
                        # Filename starts after the 2 status chars and 1 space (index 3)
                        # But handle case where space is part of status code (e.g., "M filename" where M+space is status)
                        if len(line) > 2:
                            if line[2] == ' ':
                                # Standard format: "XY filename" with space separator
                                filename = line[3:].strip()
                            else:
                                # Edge case: "M filename" where M+space is status code, filename starts immediately
                                # This happens when git shows "M " (staged) but without proper separator
                                # Extract filename starting from index 2 (after status code)
                                filename = line[2:].strip()
                        else:
                            # Line too short, skip
                            continue
                    else:
                        continue
                    # Handle renamed files: "R  old -> new"
                    if ' -> ' in filename:
                        filename = filename.split(' -> ')[1]
                    
                    index_status = status_code[0]
                    working_status = status_code[1]
                    
                    # Determine staged/unstaged flags based on git porcelain format
                    # X = index status, Y = working tree status
                    # ' ' = no change, 'M' = modified, 'A' = added, 'D' = deleted, '?' = untracked
                    # 
                    # IMPORTANT: If X='M' and Y=' ' (staged only), but git diff --cached shows nothing,
                    # this might be a git state issue. We'll trust git status for now, but the logic
                    # should handle both cases correctly.
                    
                    # Staged: X is not space and not '?' (has changes in index)
                    staged = index_status != ' ' and index_status != '?'
                    # Unstaged: Y is not space and not '?' (has changes in working tree)
                    # BUT: For '??' (untracked), both are '?' but it should be unstaged=True
                    if index_status == '?' and working_status == '?':
                        # Untracked file - not staged, but should show in Changes pane
                        unstaged = True
                        staged = False  # Ensure untracked files are not marked as staged
                    else:
                        unstaged = working_status != ' ' and working_status != '?'
                    
                    # CRITICAL FIX: Verify staged status with git diff --cached
                    # Sometimes git status --porcelain shows "M " (staged) even when nothing is staged
                    # This happens when the index was reset but git status hasn't updated
                    if staged and filename not in staged_files_set:
                        # File is marked as staged in status, but not actually staged
                        # This is a git state inconsistency - treat as unstaged
                        staged = False
                        # If it was showing as staged-only, it must have unstaged changes
                        if not unstaged:
                            # If working status was ' ' (no unstaged), but file isn't staged,
                            # it means the file has changes but they're not staged
                            unstaged = True
                    
                    # Determine status string
                    if index_status == 'D' or working_status == 'D':
                        status = "deleted"
                    elif index_status == '?' and working_status == '?':
                        # Untracked file
                        status = "untracked"
                    elif index_status == 'A':
                        # Added to index (staged)
                        status = "staged"
                    elif index_status == 'R':
                        status = "renamed"
                    elif index_status == 'C':
                        status = "copied"
                    elif index_status == 'M' or working_status == 'M':
                        status = "modified"
                    else:
                        status = "modified"
                    
                    files.append(FileStatus(
                        path=filename,
                        status=status,
                        staged=staged,
                        unstaged=unstaged
                    ))
                
                # Sort by path
                files.sort(key=lambda f: f.path)
                # Debug: Log parsed files to verify
                try:
                    with open("debug_git_status.log", "a", encoding="utf-8") as f:
                        f.write(f"[DEBUG] Native git status parsed {len(files)} files\n")
                        for file_status in files[:10]:  # Log first 10
                            f.write(f"  {file_status.path}: status={file_status.status}, staged={file_status.staged}, unstaged={file_status.unstaged}\n")
                except:
                    pass
                
                # Filter to only include files with changes (same logic as Cython version)
                files_with_changes = []
                for f in files:
                    # Only include files with actual changes
                    if f.staged or f.unstaged:
                        # File has staged or unstaged changes - include it
                        files_with_changes.append(f)
                    elif f.status == "untracked":
                        # Untracked file - always include it (already checked for ignore when created, unstaged=True set at creation)
                        files_with_changes.append(f)
                    elif f.status == "deleted":
                        # Deleted file - include it
                        files_with_changes.append(f)
                    elif f.status == "staged":
                        # New file (staged) - include it
                        files_with_changes.append(f)
                
                return files_with_changes
        except Exception as e:
            # Fallback to dulwich if git command fails
            # Log the error for debugging
            try:
                import traceback
                with open("debug_git_status.log", "a", encoding="utf-8") as f:
                    f.write(f"[ERROR] Native git status failed: {type(e).__name__}: {e}\n")
                    f.write(f"Traceback:\n{traceback.format_exc()}\n")
            except:
                pass
        
        # Fallback: Return empty list if git command fails (don't use slow dulwich)
        # This ensures consistent behavior and avoids performance issues
        return []

    def list_stashes(self) -> List[StashInfo]:
        """Get list of stashes using git stash list command.
        Optimized: Doesn't fetch SHA (lazy-loaded when needed for performance).
        """
        import subprocess
        import re
        from pathlib import Path
        
        stashes: List[StashInfo] = []
        
        try:
            # Ensure repo_path is a Path object and resolve it
            if isinstance(self.repo_path, str):
                repo_path = Path(self.repo_path).resolve()
            else:
                repo_path = Path(self.repo_path).resolve()
            
            # Get the full stash list for parsing message and branch
            stash_list_result = subprocess.run(
                ['git', 'stash', 'list'],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(repo_path)
            )
            
            if stash_list_result.returncode == 0 and stash_list_result.stdout.strip():
                # Parse each line
                for line in stash_list_result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    
                    # Parse format: stash@{index}: [WIP on ]branch: message
                    # Example: "stash@{0}: WIP on feature/stash-display-and-keybindings: message here"
                    # Or: "stash@{0}: On feature/stash-display-and-keybindings: message here"
                    # Handle both "WIP on branch: message" and "On branch: message" formats
                    match = re.match(r'stash@\{(\d+)\}:\s*(?:WIP on |On )?([^:]+?):\s*(.+)', line)
                    if match:
                        index = int(match.group(1))
                        branch = match.group(2).strip()
                        message = match.group(3).strip()
                        
                        # Get timestamp for this stash using git show
                        timestamp = 0
                        try:
                            timestamp_result = subprocess.run(
                                ['git', 'show', '-s', '--format=%at', f'stash@{{{index}}}'],
                                capture_output=True,
                                text=True,
                                timeout=2,
                                cwd=str(repo_path)
                            )
                            if timestamp_result.returncode == 0 and timestamp_result.stdout.strip():
                                timestamp_str = timestamp_result.stdout.strip()
                                if timestamp_str.isdigit():
                                    timestamp = int(timestamp_str)
                        except Exception:
                            # If timestamp fetch fails, continue with 0
                            pass
                        
                        # Don't fetch SHA here - it's expensive and only needed when showing details
                        # SHA will be fetched lazily if needed
                        stashes.append(StashInfo(
                            index=index,
                            branch=branch,
                            message=message,
                            sha="",  # Empty SHA - can be fetched later if needed
                            timestamp=timestamp
                        ))
        except Exception:
            # If git command fails, return empty list
            pass
        
        return stashes
    
    def get_stash_diff(self, stash_index: int) -> tuple[str, str]:
        """
        Get diff and stat for a stash using git stash show command.
        Returns tuple of (diff_text, stat_text).
        """
        import subprocess
        from pathlib import Path
        
        # Ensure repo_path is a Path object and resolve it
        if isinstance(self.repo_path, str):
            repo_path = Path(self.repo_path).resolve()
        else:
            repo_path = Path(self.repo_path).resolve()
        
        diff_text = ""
        stat_text = ""
        
        try:
            # Get stash stat (summary of changes) using --stat flag
            # Use --color=always to preserve git's native colors
            stat_result = subprocess.run(
                ['git', 'stash', 'show', f'stash@{{{stash_index}}}', '--stat', '--color=always'],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(repo_path)
            )
            
            if stat_result.returncode == 0:
                stat_text = stat_result.stdout.strip()
            
            # Get stash diff using -p flag with --color=always to preserve git's native colors
            # This shows the full patch/diff output from git
            diff_result = subprocess.run(
                ['git', 'stash', 'show', f'stash@{{{stash_index}}}', '-p', '--color=always'],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(repo_path)
            )
            
            if diff_result.returncode == 0:
                diff_text = diff_result.stdout.strip()
        except Exception:
            # If git command fails, return empty strings
            pass
        
        return (diff_text, stat_text)
    
    def is_tag_annotated(self, tag_name: str) -> bool:
        """Check if a tag is annotated (vs lightweight).
        
        Args:
            tag_name: Name of the tag (without refs/tags/ prefix)
        
        Returns:
            True if tag is annotated, False if lightweight
        """
        import subprocess
        from pathlib import Path
        
        try:
            result = subprocess.run(
                ['git', 'cat-file', '-t', f'refs/tags/{tag_name}'],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if result.returncode == 0:
                # Annotated tags have type "tag", lightweight tags have type "commit"
                return result.stdout.strip() == "tag"
        except Exception:
            pass
        
        return False
    
    def get_tag_annotation_info(self, tag_name: str) -> str:
        """Get annotation information for an annotated tag (without tag message).
        
        Args:
            tag_name: Name of the tag (without refs/tags/ prefix)
        
        Returns:
            String with tagger info only (no tag message), or empty string if not annotated
        """
        import subprocess
        from pathlib import Path
        
        try:
            result = subprocess.run(
                ['git', 'for-each-ref', 
                 '--format=Tagger:     %(taggername) %(taggeremail)%0aTaggerDate: %(taggerdate)',
                 f'refs/tags/{tag_name}'],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if result.returncode == 0:
                output = result.stdout.strip()
                # Filter out PGP signature (between BEGIN and END markers)
                lines = output.split('\n')
                filtered_lines = []
                in_pgp = False
                for line in lines:
                    if line == "-----END PGP SIGNATURE-----":
                        in_pgp = False
                        continue
                    if line == "-----BEGIN PGP SIGNATURE-----":
                        in_pgp = True
                        continue
                    if not in_pgp:
                        filtered_lines.append(line)
                return '\n'.join(filtered_lines)
        except Exception:
            pass
        
        return ""
    
    def get_remote_urls(self, remote_name: str) -> list[str]:
        """Get all URLs for a remote.
        
        Args:
            remote_name: Name of the remote (e.g., "origin")
        
        Returns:
            List of URLs for the remote
        """
        import subprocess
        from pathlib import Path
        
        urls = []
        try:
            result = subprocess.run(
                ['git', 'remote', 'get-url', '--all', remote_name],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.repo_path)
            )
            
            if result.returncode == 0:
                urls = [url.strip() for url in result.stdout.strip().split('\n') if url.strip()]
        except Exception:
            pass
        
        return urls


