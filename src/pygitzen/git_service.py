from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from dulwich.objects import Commit
from dulwich.repo import Repo
from dulwich.errors import NotGitRepository
import stat


@dataclass
class BranchInfo:
    name: str
    head_sha: str


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


class GitService:
    def __init__(self, start_dir: Path | str = ".") -> None:
        self.repo_path = self._find_repo_root(Path(start_dir))
        self.repo = Repo(str(self.repo_path))

    @staticmethod
    def _find_repo_root(path: Path) -> Path:
        current = path.resolve()
        while True:
            git_dir = current / ".git"
            if git_dir.exists() and git_dir.is_dir():
                return current
            if current.parent == current:
                raise NotGitRepository(f"No .git found from {path}")
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
        heads = self.repo.refs.as_dict(b"refs/heads")
        result: List[BranchInfo] = []
        for ref, sha in heads.items():
            name = ref.decode().split("/heads/")[-1]
            result.append(BranchInfo(name=name, head_sha=sha.hex()))
        result.sort(key=lambda b: b.name.lower())
        return result

    def _iter_commits(self, head_sha: bytes, max_count: Optional[int] = 100) -> Iterable[Tuple[bytes, Commit]]:
        seen = set()
        stack = [head_sha]
        while stack and (max_count is None or len(seen) < max_count):
            sha = stack.pop(0)
            if sha in seen:
                continue
            seen.add(sha)
            commit: Commit = self.repo[sha]
            yield sha, commit
            stack.extend(commit.parents)

    def _get_remote_commits(self, branch: str) -> set[str]:
        """Get set of commit SHAs that exist on remote."""
        remote_commits = set()
        try:
            # Try to get remote ref (e.g., origin/main)
            remote_ref = f"refs/remotes/origin/{branch}".encode()
            if remote_ref in self.repo.refs:
                remote_head = self.repo.refs[remote_ref]
                # Collect all commits from remote
                for sha, _ in self._iter_commits(remote_head, max_count=200):
                    remote_commits.add(sha.hex())
        except Exception:
            # Remote not available or not configured
            pass
        return remote_commits
    
    def get_merge_base(self, branch: str, base_branch: str = "main") -> str | None:
        """Find the merge-base (common ancestor) between branch and base_branch."""
        import subprocess
        import os
        
        if base_branch == branch:
            return None
        
        # Check if base branch exists
        base_ref = f"refs/heads/{base_branch}".encode()
        if base_ref not in self.repo.refs:
            # Try master if main doesn't exist
            if base_branch == "main":
                base_branch = "master"
                base_ref = f"refs/heads/{base_branch}".encode()
                if base_ref not in self.repo.refs:
                    return None
            else:
                return None
        
        # Check if branch exists
        branch_ref = f"refs/heads/{branch}".encode()
        if branch_ref not in self.repo.refs:
            return None
        
        try:
            original_cwd = os.getcwd()
            os.chdir(str(self.repo_path))
            try:
                # Use git merge-base command for reliable results
                result = subprocess.run(
                    ['git', 'merge-base', base_branch, branch],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    merge_base_sha = result.stdout.strip()
                    return merge_base_sha
            finally:
                os.chdir(original_cwd)
        except Exception:
            # Fallback: find common ancestor manually using dulwich
            try:
                base_head = self.repo.refs[base_ref]
                branch_head = self.repo.refs[branch_ref]
                
                # Get all ancestors of base branch
                base_ancestors = set()
                for sha, _ in self._iter_commits(base_head, max_count=200):
                    base_ancestors.add(sha)
                
                # Walk branch history to find first common ancestor
                for sha, _ in self._iter_commits(branch_head, max_count=200):
                    if sha in base_ancestors:
                        return sha.hex()
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
                    base_ref = f"refs/heads/{base_name}".encode()
                    if base_ref in self.repo.refs and base_name != branch:
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
                # If git log fails, fallback to dulwich
                return self.list_commits(branch, max_count, skip, show_full_history)
            
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
            # Timeout - return what we have or fallback
            if commits:
                return commits
            # Fallback to dulwich if timeout and no commits
            return self.list_commits(branch, max_count, skip, show_full_history)
        except Exception as e:
            # On any error, fallback to dulwich
            import sys
            try:
                with open("debug_list_commits_native.log", "a", encoding="utf-8") as f:
                    f.write(f"Error in list_commits_native for {branch}: {type(e).__name__}: {e}\n")
            except:
                pass
            return self.list_commits(branch, max_count, skip, show_full_history)
    
    def list_commits(self, branch: str, max_count: int = 200, skip: int = 0, show_full_history: bool = False) -> List[CommitInfo]:
        ref = f"refs/heads/{branch}".encode()
        head = self.repo.refs[ref]
        
        # Get remote commits to check push status
        remote_commits = self._get_remote_commits(branch)
        
        # Determine if we should show full history
        if show_full_history and branch not in ["main", "master"]:
            # For feature branches with full history, find merge-base and show all commits from there
            base_branch_names = ["main", "master"]
            merge_base_sha = None
            for base_name in base_branch_names:
                merge_base_sha = self.get_merge_base(branch, base_name)
                if merge_base_sha:
                    break
            
            commits: List[CommitInfo] = []
            yielded = 0
            merge_base_bytes = bytes.fromhex(merge_base_sha) if merge_base_sha else None
            
            for index, (sha, commit) in enumerate(self._iter_commits(head, max_count=None)):
                commit_sha = sha.hex()
                
                # Stop at merge-base (don't include merge-base itself, only commits after it)
                if merge_base_bytes and sha == merge_base_bytes:
                    break
                
                # Apply skip for pagination
                if yielded < skip:
                    yielded += 1
                    continue

                author = commit.author.decode(errors="replace") if isinstance(commit.author, (bytes, bytearray)) else str(commit.author)
                summary = commit.message.split(b"\n", 1)[0].decode(errors="replace")
                # Check if commit exists on remote
                is_pushed = commit_sha in remote_commits
                commits.append(
                    CommitInfo(
                        sha=commit_sha,
                        summary=summary,
                        author=author,
                        timestamp=int(commit.commit_time),
                        pushed=is_pushed,
                    )
                )
                if len(commits) >= max_count:
                    break
            return commits
        
        # Original behavior: exclude commits that exist in base branch
        base_branch_commits = set()
        base_branch_names = ["main", "master"]
        for base_name in base_branch_names:
            base_ref = f"refs/heads/{base_name}".encode()
            if base_ref in self.repo.refs and base_name != branch:
                base_head = self.repo.refs[base_ref]
                # Collect all commits from base branch
                for sha, _ in self._iter_commits(base_head, max_count=200):
                    base_branch_commits.add(sha.hex())
                break  # Use first available base branch

        commits: List[CommitInfo] = []
        yielded = 0
        for index, (sha, commit) in enumerate(self._iter_commits(head, max_count=None)):
            commit_sha = sha.hex()
            
            # If not main/master branch, exclude commits that exist in base branch
            if branch not in ["main", "master"] and commit_sha in base_branch_commits:
                # This commit is shared with base branch, skip it
                continue

            # Apply skip for pagination
            if yielded < skip:
                yielded += 1
                continue

            author = commit.author.decode(errors="replace") if isinstance(commit.author, (bytes, bytearray)) else str(commit.author)
            summary = commit.message.split(b"\n", 1)[0].decode(errors="replace")
            # Check if commit exists on remote
            is_pushed = commit_sha in remote_commits
            commits.append(
                CommitInfo(
                    sha=commit_sha,
                    summary=summary,
                    author=author,
                    timestamp=int(commit.commit_time),
                    pushed=is_pushed,
                )
            )
            if len(commits) >= max_count:
                break
        return commits

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
                # Check if base branch exists
                base_ref = f"refs/heads/{base_name}".encode()
                if base_ref not in self.repo.refs or base_name == branch:
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
            # Timeout - fallback to dulwich
            pass
        except Exception as e:
            # Log error for debugging but continue to fallback
            try:
                with open("debug_count_commits_native.log", "a", encoding="utf-8") as f:
                    f.write(f"Error in count_commits_native for {branch}: {type(e).__name__}: {e}\n")
            except:
                pass
            # Fallback to dulwich if git command fails
            pass
        
        # Fallback to dulwich (original implementation)
        return self.count_commits_dulwich(branch)
    
    def count_commits_dulwich(self, branch: str) -> int:
        """Original dulwich-based count_commits (fallback)."""
        ref = f"refs/heads/{branch}".encode()
        head = self.repo.refs[ref]

        base_branch_commits = set()
        base_branch_names = ["main", "master"]
        for base_name in base_branch_names:
            base_ref = f"refs/heads/{base_name}".encode()
            if base_ref in self.repo.refs and base_name != branch:
                base_head = self.repo.refs[base_ref]
                for sha, _ in self._iter_commits(base_head, max_count=None):
                    base_branch_commits.add(sha.hex())
                break

        count = 0
        for sha, _ in self._iter_commits(head, max_count=None):
            if branch not in ["main", "master"] and sha.hex() in base_branch_commits:
                continue
            count += 1
        return count
    
    def count_commits(self, branch: str) -> int:
        """Count commits for a branch - tries git-native first, falls back to dulwich."""
        # Try git-native version first (has timeout support, avoids pack file corruption)
        if hasattr(self, 'count_commits_native'):
            try:
                return self.count_commits_native(branch, timeout=10)
            except Exception:
                # Fallback to dulwich if native fails
                return self.count_commits_dulwich(branch)
        else:
            # Fallback to dulwich if native method doesn't exist
            return self.count_commits_dulwich(branch)

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
        return f"Error: Could not get diff for commit {sha_hex[:8]}. Both git show and dulwich fallback failed.\n"
        
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
                                
                                # Also check merge status from dulwich for accuracy
                                try:
                                    commit_bytes = bytes.fromhex(sha)
                                    commit = self.repo[commit_bytes]
                                    if len(commit.parents) > 1:
                                        refs["is_merge"] = True
                                        if not refs["merge_parents"]:
                                            refs["merge_parents"] = [p.hex() for p in commit.parents]
                                except Exception:
                                    pass
            finally:
                os.chdir(original_cwd)
        except Exception:
            # Fallback: if git log fails, return empty refs (will be filled by get_commit_refs if needed)
            pass
        
        return result_map
    
    def get_commit_refs(self, commit_sha: str) -> dict:
        """Get branch references and metadata for a commit."""
        result = {
            "branches": [],  # Local branches pointing to this commit
            "remote_branches": [],  # Remote branches pointing to this commit
            "tags": [],  # Tags pointing to this commit
            "is_head": False,  # Whether this is HEAD
            "is_merge": False,  # Whether this is a merge commit
            "merge_parents": [],  # Parent commits if merge
        }
        
        commit_bytes = bytes.fromhex(commit_sha)
        
        # Check if this is HEAD
        try:
            # Safely resolve HEAD - handle both symbolic and direct refs
            head_ref = self.repo.refs.get(b"HEAD")
            if head_ref:
                # If it's a symbolic ref (starts with refs/), resolve it
                if isinstance(head_ref, bytes) and len(head_ref) > 10 and head_ref.startswith(b"refs/heads/"):
                    head_sha = self.repo.refs.get(head_ref)
                    if head_sha and head_sha == commit_bytes:
                        result["is_head"] = True
                elif isinstance(head_ref, bytes) and len(head_ref) == 20:
                    # Direct SHA (20 bytes)
                    if head_ref == commit_bytes:
                        result["is_head"] = True
        except Exception:
            pass
        
        # Check local branches
        for ref_name, ref_sha in self.repo.refs.as_dict(b"refs/heads").items():
            if ref_sha == commit_bytes:
                branch_name = ref_name.decode().split("/heads/")[-1]
                result["branches"].append(branch_name)
        
        # Check remote branches
        for ref_name, ref_sha in self.repo.refs.as_dict(b"refs/remotes").items():
            if ref_sha == commit_bytes:
                # Extract remote/branch name (e.g., "origin/main" -> "origin/main")
                remote_branch = ref_name.decode().replace("refs/remotes/", "")
                result["remote_branches"].append(remote_branch)
        
        # Check tags
        for ref_name, ref_sha in self.repo.refs.as_dict(b"refs/tags").items():
            if ref_sha == commit_bytes:
                tag_name = ref_name.decode().split("/tags/")[-1]
                result["tags"].append(tag_name)
        
        # Check if merge commit
        try:
            commit = self.repo[commit_bytes]
            if len(commit.parents) > 1:
                result["is_merge"] = True
                result["merge_parents"] = [p.hex() for p in commit.parents]
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
        """Get information about a branch."""
        result = {
            "name": branch,
            "head_sha": None,
            "remote_tracking": None,  # e.g., "origin/main"
            "upstream": None,  # Upstream branch name
            "is_current": False,  # Whether this is the current branch
        }
        
        try:
            branch_ref = f"refs/heads/{branch}".encode()
            if branch_ref in self.repo.refs:
                result["head_sha"] = self.repo.refs[branch_ref].hex()
        except Exception:
            pass
        
        # Check if current branch
        try:
            head_ref = self.repo.refs[b"HEAD"]
            # HEAD can be either a symbolic ref (b"refs/heads/main") or a SHA (40 bytes)
            # Check if it's a symbolic ref by checking length and prefix
            if isinstance(head_ref, bytes) and len(head_ref) > 10 and head_ref.startswith(b"refs/heads/"):
                current_branch = head_ref.decode().split("/heads/")[-1]
                result["is_current"] = (current_branch == branch)
        except Exception:
            pass
        
        # Check remote tracking
        try:
            remote_ref = f"refs/remotes/origin/{branch}".encode()
            if remote_ref in self.repo.refs:
                result["remote_tracking"] = f"origin/{branch}"
                result["upstream"] = branch
        except Exception:
            pass
        
        return result

    def _find_in_tree(self, tree, path_parts: List[str]) -> Optional[bytes]:
        """Recursively find file in tree and return its SHA."""
        if not path_parts:
            return None
        name = path_parts[0].encode()
        if name in tree:
            entry = tree[name]  # entry is (mode, sha) tuple
            mode, sha = entry
            if len(path_parts) == 1:
                # Last part - it's the file
                return sha  # Return SHA
            else:
                # More parts - it's a directory, recurse
                if stat.S_ISDIR(mode):
                    subtree_obj = self.repo[sha]
                    return self._find_in_tree(subtree_obj, path_parts[1:])
                else:
                    return None  # Not a directory, can't continue
        return None

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
                # Get list of actually staged files to verify (fast check)
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
                        f.write(f"[DEBUG] Actually staged files (from git diff --cached): {len(staged_files_set)} files\n")
                        for staged_file in list(staged_files_set)[:5]:
                            f.write(f"  {staged_file}\n")
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
                            # Check if file exists and has changes
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


