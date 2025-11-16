# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat

from dulwich.repo import Repo
from dulwich.errors import NotGitRepository


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
    pushed: bool = False


@dataclass
class FileStatus:
    path: str
    status: str  # 'modified', 'staged', 'untracked', 'deleted', 'renamed'
    staged: bool  # Whether changes are staged
    unstaged: bool = False  # Whether changes are unstaged (for files with both)


cdef class GitServiceCython:
    cdef object repo_path
    cdef object repo
    
    def __init__(self, start_dir):
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
    
    def _iter_commits_optimized(self, bytes head_sha, int max_count):
        """Optimized Cython version of commit iteration."""
        seen = set()
        stack = [head_sha]
        count = 0
        
        while stack and (max_count < 0 or count < max_count):
            sha = stack.pop(0)
            if sha in seen:
                continue
            seen.add(sha)
            commit = self.repo[sha]
            count += 1
            yield sha, commit
            stack.extend(commit.parents)
    
    def _get_remote_commits(self, str branch):
        """Get set of commit SHAs that exist on remote."""
        remote_commits = set()
        try:
            remote_ref = f"refs/remotes/origin/{branch}".encode()
            try:
                remote_head = self.repo.refs[remote_ref]
                for sha, _ in self._iter_commits_optimized(remote_head, 200):
                    remote_commits.add(sha.hex())
            except (KeyError, AttributeError, TypeError):
                pass
        except Exception:
            pass
        return remote_commits
    
    def list_commits(self, str branch, int max_count=200, int skip=0, show_full_history=False):
        """Optimized Cython version of list_commits."""
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
            
            commits = []
            yielded = 0
            merge_base_bytes = bytes.fromhex(merge_base_sha) if merge_base_sha else None
            
            for index, (sha, commit) in enumerate(self._iter_commits_optimized(head, -1)):
                # Ensure commit_sha is always a hex string (not bytes)
                # sha from _iter_commits_optimized is always bytes, so convert to hex
                if isinstance(sha, bytes):
                    commit_sha = sha.hex()
                elif hasattr(sha, 'hex'):
                    # If it has a hex method, use it
                    commit_sha = sha.hex()
                else:
                    # Last resort: convert to string, but this should not happen
                    commit_sha = str(sha) if not isinstance(sha, str) else sha
                    # Validate it's a hex string
                    if not all(c in '0123456789abcdefABCDEF' for c in commit_sha):
                        # If not hex, try to get hex representation
                        if isinstance(sha, (int, float)):
                            commit_sha = format(int(sha), '040x')
                        else:
                            # Log error
                            try:
                                with open("debug_cython_sha.log", "a", encoding="utf-8") as f:
                                    f.write(f"WARNING: Unexpected sha type: {type(sha)}, value: {repr(sha)}\n")
                            except:
                                pass
                
                # Stop at merge-base (don't include merge-base itself, only commits after it)
                if merge_base_bytes and sha == merge_base_bytes:
                    break
                
                # Apply skip for pagination
                if yielded < skip:
                    yielded += 1
                    continue
                
                author = commit.author.decode(errors="replace") if isinstance(commit.author, (bytes, bytearray)) else str(commit.author)
                summary = commit.message.split(b"\n", 1)[0].decode(errors="replace")
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
        # Get commits from base branch (main or master) to exclude shared history
        base_branch_commits = set()
        base_branch_names = ["main", "master"]
        
        for base_name in base_branch_names:
            if base_name != branch:
                base_ref = f"refs/heads/{base_name}".encode()
                try:
                    base_head = self.repo.refs[base_ref]
                    for sha, _ in self._iter_commits_optimized(base_head, 200):
                        base_branch_commits.add(sha.hex())
                    break
                except (KeyError, AttributeError, TypeError):
                    continue
        
        commits = []
        yielded = 0
        
        for index, (sha, commit) in enumerate(self._iter_commits_optimized(head, -1)):
            # Ensure commit_sha is always a hex string (not bytes)
            # sha from _iter_commits_optimized is always bytes, so convert to hex
            if isinstance(sha, bytes):
                commit_sha = sha.hex()
            elif hasattr(sha, 'hex'):
                # If it has a hex method, use it
                commit_sha = sha.hex()
            else:
                # Last resort: convert to string, but this should not happen
                commit_sha = str(sha) if not isinstance(sha, str) else sha
                # Validate it's a hex string
                if not all(c in '0123456789abcdefABCDEF' for c in commit_sha):
                    # If not hex, try to get hex representation
                    if isinstance(sha, (int, float)):
                        commit_sha = format(int(sha), '040x')
                    else:
                        # Log error
                        try:
                            with open("debug_cython_sha.log", "a", encoding="utf-8") as f:
                                f.write(f"WARNING: Unexpected sha type: {type(sha)}, value: {repr(sha)}\n")
                        except:
                            pass
            
            # If not main/master branch, exclude commits that exist in base branch
            if branch not in ["main", "master"] and commit_sha in base_branch_commits:
                continue
            
            # Apply skip for pagination
            if yielded < skip:
                yielded += 1
                continue
            
            author = commit.author.decode(errors="replace") if isinstance(commit.author, (bytes, bytearray)) else str(commit.author)
            summary = commit.message.split(b"\n", 1)[0].decode(errors="replace")
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
    
    def count_commits(self, str branch):
        """Count commits for a branch using Git's native command (fastest, no caching)."""
        import subprocess
        import os
        
        # Try to use Git's native counting first (much faster)
        try:
            # Use cwd parameter instead of chdir for better reliability
            # Use git rev-list --count for main/master branches (fastest)
            if branch in ["main", "master"]:
                result = subprocess.run(
                    ['git', 'rev-list', '--count', branch],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(self.repo_path)
                )
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        return int(result.stdout.strip())
                    except ValueError:
                        pass
            
            # For other branches, use git rev-list with exclusion
            if branch not in ["main", "master"]:
                base_branch_names = ["main", "master"]
                for base_name in base_branch_names:
                    # Try to get merge-base
                    merge_base_result = subprocess.run(
                        ['git', 'merge-base', base_name, branch],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        cwd=str(self.repo_path)
                    )
                    if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
                        merge_base = merge_base_result.stdout.strip()
                        # Count commits from merge-base to branch
                        count_result = subprocess.run(
                            ['git', 'rev-list', '--count', f'{merge_base}..{branch}'],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            cwd=str(self.repo_path)
                        )
                        if count_result.returncode == 0 and count_result.stdout.strip():
                            try:
                                return int(count_result.stdout.strip())
                            except ValueError:
                                pass
                    break
        except Exception as e:
            # Log error for debugging but continue to fallback
            import sys
            try:
                with open("debug_count_commits.log", "a") as f:
                    f.write(f"Error in native git count_commits for {branch}: {e}\n")
            except:
                pass
            # Fallback to Python iteration if Git command fails
            pass
        
        # Fallback: Python-based counting (original algorithm)
        ref = f"refs/heads/{branch}".encode()
        head = self.repo.refs[ref]
        
        base_branch_commits = set()
        base_branch_names = ["main", "master"]
        
        for base_name in base_branch_names:
            if base_name != branch:
                base_ref = f"refs/heads/{base_name}".encode()
                try:
                    base_head = self.repo.refs[base_ref]
                    for sha, _ in self._iter_commits_optimized(base_head, -1):
                        base_branch_commits.add(sha.hex())
                    break
                except (KeyError, AttributeError, TypeError):
                    continue
        
        count = 0
        for sha, _ in self._iter_commits_optimized(head, -1):
            # Ensure commit_sha is always a hex string (not bytes)
            if isinstance(sha, bytes):
                commit_sha = sha.hex()
            else:
                commit_sha = str(sha) if not isinstance(sha, str) else sha
            if branch not in ["main", "master"] and commit_sha in base_branch_commits:
                continue
            count += 1
        
        return count
    
    def list_branches(self):
        """List all branches - optimized."""
        # Use direct refs access instead of as_dict for better performance
        result = []
        
        # Get all head refs directly
        refs_prefix = b"refs/heads/"
        for ref_name in self.repo.refs.keys():
            if ref_name.startswith(refs_prefix):
                sha = self.repo.refs[ref_name]
                # Extract branch name (more efficient)
                branch_name = ref_name[len(refs_prefix):].decode(errors="replace")
                result.append(BranchInfo(name=branch_name, head_sha=sha.hex()))
        
        # Use a helper function instead of lambda for Cython compatibility
        def get_branch_name(branch):
            return branch.name.lower()
        
        result.sort(key=get_branch_name)
        return result
    
    def get_merge_base(self, str branch, str base_branch="main"):
        """Find the merge-base (common ancestor) between branch and base_branch."""
        import subprocess
        import os
        
        if base_branch == branch:
            return None
        
        # Check if base branch exists
        base_ref = f"refs/heads/{base_branch}".encode()
        try:
            _ = self.repo.refs[base_ref]
        except (KeyError, AttributeError, TypeError):
            # Try master if main doesn't exist
            if base_branch == "main":
                base_branch = "master"
                base_ref = f"refs/heads/{base_branch}".encode()
                try:
                    _ = self.repo.refs[base_ref]
                except (KeyError, AttributeError, TypeError):
                    return None
            else:
                return None
        
        # Check if branch exists
        branch_ref = f"refs/heads/{branch}".encode()
        try:
            _ = self.repo.refs[branch_ref]
        except (KeyError, AttributeError, TypeError):
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
                for sha, _ in self._iter_commits_optimized(base_head, 1000):
                    base_ancestors.add(sha)
                
                # Walk branch history to find first common ancestor
                for sha, _ in self._iter_commits_optimized(branch_head, 200):
                    if sha in base_ancestors:
                        return sha.hex()
            except Exception:
                pass
        
        return None
    
    def get_commit_refs_from_git_log(self, str branch, list commit_shas):
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
                    # Normalize commit_shas to lowercase for case-insensitive matching
                    normalized_result_map = {sha.lower(): refs for sha, refs in result_map.items()}
                    
                    for line in process.stdout.strip().split("\n"):
                        if not line:
                            continue
                        parts = line.split("\x00")
                        if len(parts) >= 3:
                            sha = parts[0].strip().lower()  # Normalize to lowercase
                            refs_str = parts[1].strip() if len(parts) > 1 else ""
                            parents_str = parts[2].strip() if len(parts) > 2 else ""
                            
                            if sha in normalized_result_map:
                                # Parse refs string (e.g., "HEAD -> master, tag: v0.15.2, origin/main")
                                # Get the original SHA key (case-preserved) from normalized map
                                original_sha = None
                                for orig_sha in result_map.keys():
                                    if orig_sha.lower() == sha:
                                        original_sha = orig_sha
                                        break
                                
                                if original_sha:
                                    refs = result_map[original_sha]
                                
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
                                        # Tag: "tag: v0.15.2" or "tag: v2.52.0-rc0"
                                        tag_name = ref_part.replace("tag: ", "").strip()
                                        if tag_name and tag_name not in refs["tags"]:
                                            refs["tags"].append(tag_name)
                                    elif "tag:" in ref_part.lower():
                                        # Handle case where tag might be in different format
                                        # Extract tag name after "tag:" (case insensitive)
                                        tag_parts = ref_part.split(":", 1)
                                        if len(tag_parts) > 1:
                                            tag_name = tag_parts[1].strip()
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
    
    def get_commit_refs(self, str commit_sha):
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
        
        # Check local branches - use keys() iteration instead of as_dict()
        # as_dict() causes segfault in Cython, so we iterate keys directly
        try:
            refs_prefix = b"refs/heads/"
            for ref_name in self.repo.refs.keys():
                if ref_name.startswith(refs_prefix):
                    try:
                        ref_sha = self.repo.refs[ref_name]
                        if ref_sha == commit_bytes:
                            branch_name = ref_name[len(refs_prefix):].decode(errors="replace")
                            result["branches"].append(branch_name)
                    except (KeyError, AttributeError, TypeError):
                        continue
        except Exception:
            pass
        
        # Check remote branches
        try:
            refs_prefix = b"refs/remotes/"
            for ref_name in self.repo.refs.keys():
                if ref_name.startswith(refs_prefix):
                    try:
                        ref_sha = self.repo.refs[ref_name]
                        if ref_sha == commit_bytes:
                            remote_branch = ref_name[len(refs_prefix):].decode(errors="replace")
                            result["remote_branches"].append(remote_branch)
                    except (KeyError, AttributeError, TypeError):
                        continue
        except Exception:
            pass
        
        # Check tags
        try:
            refs_prefix = b"refs/tags/"
            for ref_name in self.repo.refs.keys():
                if ref_name.startswith(refs_prefix):
                    try:
                        ref_sha = self.repo.refs[ref_name]
                        if ref_sha == commit_bytes:
                            tag_name = ref_name[len(refs_prefix):].decode(errors="replace")
                            result["tags"].append(tag_name)
                    except (KeyError, AttributeError, TypeError):
                        continue
        except Exception:
            pass
        
        # Check if merge commit
        try:
            commit = self.repo[commit_bytes]
            if len(commit.parents) > 1:
                result["is_merge"] = True
                result["merge_parents"] = [p.hex() for p in commit.parents]
        except Exception:
            pass
        
        return result
    
    def get_branch_info(self, str branch):
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
            try:
                result["head_sha"] = self.repo.refs[branch_ref].hex()
            except (KeyError, AttributeError, TypeError):
                pass
        except Exception:
            pass
        
        # Check if current branch
        try:
            from dulwich import refs as dulwich_refs
            # Use dulwich's follow to resolve symbolic refs safely
            try:
                head_ref = dulwich_refs.follow(self.repo, b"HEAD")
                if head_ref and isinstance(head_ref, bytes) and head_ref.startswith(b"refs/heads/"):
                    current_branch = head_ref.decode().split("/heads/")[-1]
                    result["is_current"] = (current_branch == branch)
            except (KeyError, AttributeError, TypeError, ValueError):
                pass
        except Exception:
            pass
        
        # Check remote tracking
        try:
            remote_ref = f"refs/remotes/origin/{branch}".encode()
            try:
                # Try to access the ref - if it exists, we'll get it, otherwise KeyError
                _ = self.repo.refs[remote_ref]
                result["remote_tracking"] = f"origin/{branch}"
                result["upstream"] = branch
            except (KeyError, AttributeError, TypeError):
                pass
        except Exception:
            pass
        
        return result
    
    def get_commit_diff(self, str sha_hex):
        """Get diff for a commit using git-native command (avoids dulwich hex_to_sha issues)."""
        import subprocess
        import re
        
        # Ensure sha_hex is a proper hex string (normalize if needed)
        # Handle case where it might be bytes or wrong format
        if isinstance(sha_hex, bytes):
            # If it's already bytes, check if it's 20 bytes (binary) or 40 bytes (hex string as bytes)
            if len(sha_hex) == 20:
                # It's a binary SHA, convert to hex
                sha_hex = sha_hex.hex()
            elif len(sha_hex) == 40:
                # It's a hex string as bytes, decode it
                sha_hex = sha_hex.decode('ascii')
            else:
                # Try to decode as string
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
        
        # Use git show to get the diff (avoids dulwich's hex_to_sha issues completely)
        try:
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
                        f.write(f"[CYTHON] git show failed for {sha_hex}: returncode={result.returncode}, stderr={error_msg}\n")
                except:
                    pass
                return f"Error: Could not get diff for commit {sha_hex[:8]}. git show failed: {error_msg[:100]}\n"
        except subprocess.TimeoutExpired:
            try:
                with open("debug_get_commit_diff.log", "a", encoding="utf-8") as f:
                    f.write(f"[CYTHON] Timeout in git show for {sha_hex}\n")
            except:
                pass
            return f"Error: Timeout getting diff for commit {sha_hex[:8]}\n"
        except Exception as e:
            # Log error
            try:
                with open("debug_get_commit_diff.log", "a", encoding="utf-8") as f:
                    f.write(f"[CYTHON] Error in git-native get_commit_diff for {sha_hex}: {type(e).__name__}: {e}\n")
            except:
                pass
            return f"Error: Could not get diff for commit {sha_hex[:8]}. Exception: {type(e).__name__}\n"
    
    def _find_in_tree(self, tree, path_parts):
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
    
    def _is_ignored(self, str file_path):
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
    
    def _build_head_file_map(self, tree, base_path=""):
        """Build a file-to-SHA map from HEAD tree in a single pass (no caching)."""
        file_map = {}
        
        def traverse_tree(current_tree, path_prefix):
            # Iterate through tree entries (same way as _find_in_tree)
            for name in current_tree:
                entry = current_tree[name]  # entry is (mode, sha) tuple
                mode, sha = entry
                name_str = name.decode() if isinstance(name, bytes) else name
                current_path = f"{path_prefix}/{name_str}" if path_prefix else name_str
                
                if stat.S_ISDIR(mode):
                    # It's a directory, recurse into it
                    subtree = self.repo[sha]
                    traverse_tree(subtree, current_path)
                else:
                    # It's a file, add to map
                    file_map[current_path] = sha
        
        traverse_tree(tree, base_path)
        return file_map
    
    def _compile_gitignore_patterns(self):
        """Compile .gitignore patterns once per call (no caching)."""
        import fnmatch
        
        gitignore_path = self.repo_path / ".gitignore"
        if not gitignore_path.exists():
            return []
        
        try:
            with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                gitignore_lines = f.readlines()
        except Exception:
            return []
        
        compiled_patterns = []
        for line in gitignore_lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            is_negation = line.startswith("!")
            if is_negation:
                pattern = line[1:].strip()
            else:
                pattern = line
            
            if not pattern:
                continue
            
            pattern = pattern.rstrip("/")
            fnmatch_pattern = pattern.replace("**", "*")
            
            compiled_patterns.append({
                'pattern': pattern,
                'fnmatch_pattern': fnmatch_pattern,
                'is_negation': is_negation,
                'is_root': pattern.startswith("/")
            })
        
        return compiled_patterns
    
    def _is_path_ignored(self, file_path: str, compiled_patterns: list) -> bool:
        """Check if a file path matches compiled .gitignore patterns."""
        import fnmatch
        
        if not compiled_patterns:
            return False
        
        normalized_path = file_path.replace("\\", "/")
        path_parts = normalized_path.split("/")
        is_ignored = False
        
        for pattern_info in compiled_patterns:
            pattern = pattern_info['pattern']
            fnmatch_pattern = pattern_info['fnmatch_pattern']
            is_negation = pattern_info['is_negation']
            is_root = pattern_info['is_root']
            
            if is_root:
                # Match from repository root only
                root_pattern = pattern[1:] if pattern.startswith("/") else pattern
                root_fnmatch = fnmatch_pattern[1:] if fnmatch_pattern.startswith("/") else fnmatch_pattern
                
                if fnmatch.fnmatch(normalized_path, root_fnmatch) or \
                   normalized_path.startswith(root_pattern + "/"):
                    is_ignored = not is_negation
            else:
                # Match anywhere in the path
                matched = False
                if fnmatch.fnmatch(normalized_path, fnmatch_pattern):
                    matched = True
                else:
                    for i in range(len(path_parts)):
                        check_path = "/".join(path_parts[i:])
                        if fnmatch.fnmatch(check_path, fnmatch_pattern) or \
                           fnmatch.fnmatch(path_parts[i], fnmatch_pattern):
                            matched = True
                            break
                
                if matched:
                    is_ignored = not is_negation
        
        return is_ignored
    
    def _is_directory_ignored(self, dir_path: str, compiled_patterns: list) -> bool:
        """Check if a directory should be skipped (ignored)."""
        # Common ignored directories (check before .gitignore) - fast check
        common_ignored = {'.git', 'node_modules', '__pycache__', '.pytest_cache', 
                         '.mypy_cache', '.venv', 'venv', 'env', '.env', 'dist', 
                         'build', '.tox', '.eggs'}
        
        dir_name = Path(dir_path).name
        if dir_name in common_ignored:
            return True
        
        # Check if directory name ends with ignored pattern
        if dir_name.endswith('.egg-info'):
            return True
        
        # Check .gitignore patterns
        if self._is_path_ignored(dir_path + "/", compiled_patterns):
            return True
        
        return False
    
    def get_file_status(self):
        """Optimized version using native git status --porcelain (10x faster than dulwich)."""
        import subprocess
        import os
        
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
                return files
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
        
        # Fallback: Original dulwich-based implementation
        from dulwich.index import Index
        from dulwich.objects import Blob
        
        files = []
        
        # Read the index (staged files)
        try:
            index = Index(str(self.repo_path / ".git" / "index"))
            index_entries = {path.decode(errors="replace"): entry for path, entry in index.items()}
        except Exception:
            index_entries = {}
        
        def calculate_blob_sha(file_data: bytes) -> bytes:
            """Calculate Git blob SHA using dulwich's method."""
            blob = Blob()
            blob.data = file_data
            return blob.id
        
        # Get HEAD commit and tree
        try:
            from dulwich import refs as dulwich_refs
            # Use dulwich's read_ref which safely handles symbolic refs
            try:
                head_sha = dulwich_refs.read_ref(self.repo, b"HEAD")
                if head_sha and len(head_sha) == 20:
                    head_commit = self.repo[head_sha]
                    head_tree = self.repo[head_commit.tree]
                else:
                    head_tree = None
            except (KeyError, AttributeError, TypeError, ValueError):
                head_tree = None
        except Exception:
            head_tree = None
        
        # Build file-to-SHA map from HEAD tree in ONE pass (no caching - just efficient)
        head_file_map = {}
        if head_tree:
            head_file_map = self._build_head_file_map(head_tree)
        
        # Compile .gitignore patterns once per call (no caching)
        compiled_gitignore = self._compile_gitignore_patterns()
        
        # Track files we've processed
        processed_files = set()
        
        # Check files in index (staged) - optimized with mtime check
        for path, entry in index_entries.items():
            processed_files.add(path)
            full_path = self.repo_path / path
            
            if not full_path.exists():
                # File deleted
                files.append(FileStatus(path=path, status="deleted", staged=True, unstaged=False))
                continue
            
            # Calculate working directory file SHA (always needed to check unstaged changes)
            try:
                with open(full_path, "rb") as f:
                    file_data = f.read()
                    file_sha = calculate_blob_sha(file_data)
            except Exception:
                continue
            
            index_sha = entry.sha
            head_sha = head_file_map.get(path)
            
            # Check if staged (index differs from HEAD or new file)
            # Only add as staged if index differs from HEAD (has staged changes)
            if head_sha is not None:
                if head_sha != index_sha:
                    # Staged changes (index differs from HEAD)
                    files.append(FileStatus(path=path, status="modified", staged=True, unstaged=False))
                # else: index matches HEAD, no staged changes, but might have unstaged changes (checked in walk_directory)
            else:
                # New file (not in HEAD) - always staged
                files.append(FileStatus(path=path, status="staged", staged=True, unstaged=False))
        
        # Check working directory for untracked and unstaged modified files
        # Optimized directory walking with early skipping
        def walk_directory(path: Path, base: Path, current_path: str = ""):
            """Recursively walk directory with early skipping of ignored directories."""
            # Skip .git directory itself
            if path.name == ".git" and path.is_dir():
                return
            
            # Check if this directory should be skipped (early exit)
            if current_path:
                if self._is_directory_ignored(current_path, compiled_gitignore):
                    return  # Skip entire directory tree
            
            try:
                for item in path.iterdir():
                    # Skip hidden files/directories early
                    if item.name.startswith("."):
                        continue
                    
                    if item.is_dir():
                        # Build path for directory
                        dir_path = f"{current_path}/{item.name}" if current_path else item.name
                        # Recursively walk (will check if directory is ignored)
                        walk_directory(item, base, dir_path)
                    elif item.is_file():
                        rel_path = str(item.relative_to(base)).replace("\\", "/")
                        
                        if rel_path not in processed_files:
                            # File not in index, check if it's tracked in HEAD or untracked
                            head_sha = head_file_map.get(rel_path)
                            
                            if head_sha is not None:
                                # File is tracked in HEAD but not in index (modified, not staged)
                                # Calculate working directory file SHA to verify changes
                                try:
                                    with open(item, "rb") as f:
                                        file_data = f.read()
                                        file_sha = calculate_blob_sha(file_data)
                                    
                                    if head_sha != file_sha:
                                        # Modified from HEAD, not staged
                                        files.append(FileStatus(path=rel_path, status="modified", staged=False, unstaged=True))
                                except Exception:
                                    pass
                            else:
                                # Not in HEAD, so it's untracked
                                # Only add if not ignored by .gitignore
                                if not self._is_path_ignored(rel_path, compiled_gitignore):
                                    # Untracked files should have unstaged=True to show in Changes pane
                                    files.append(FileStatus(path=rel_path, status="untracked", staged=False, unstaged=True))
                        else:
                            # File is in index, check if modified in working directory (unstaged changes)
                            if rel_path in index_entries:
                                entry = index_entries[rel_path]
                                try:
                                    # Always calculate SHA to check for unstaged changes
                                    # (working directory might differ from index even if mtime unchanged)
                                    with open(item, "rb") as f:
                                        file_data = f.read()
                                        file_sha = calculate_blob_sha(file_data)
                                    
                                    # Get index SHA (staged version)
                                    index_sha = entry.sha
                                    
                                    # Get HEAD SHA
                                    head_sha = head_file_map.get(rel_path)
                                    
                                    # If working directory differs from index, there are unstaged modifications
                                    # But only add if the file actually differs from HEAD (has actual changes)
                                    # If working == HEAD exactly, don't show it (file is up to date)
                                    if index_sha != file_sha:
                                        # Only add if file differs from HEAD (has actual changes)
                                        # Exclude files where working directory matches HEAD exactly (up to date)
                                        if head_sha is None:
                                            # Not in HEAD, so it's a change
                                            if not any(f.path == rel_path and not f.staged for f in files):
                                                files.append(FileStatus(path=rel_path, status="modified", staged=False, unstaged=True))
                                        elif file_sha != head_sha:
                                            # File differs from HEAD, so it has unstaged changes
                                            if not any(f.path == rel_path and not f.staged for f in files):
                                                files.append(FileStatus(path=rel_path, status="modified", staged=False, unstaged=True))
                                        # else: file_sha == head_sha, meaning working directory matches HEAD exactly
                                        # Even though working != index, if working == HEAD, the file is up to date, don't show
                                except Exception:
                                    pass
            except PermissionError:
                pass
        
        walk_directory(self.repo_path, self.repo_path)
        
        # Combine entries for same file path to show both staged and unstaged status
        file_dict: dict[str, FileStatus] = {}
        for file_status in files:
            if file_status.path in file_dict:
                # File already exists - merge statuses
                existing = file_dict[file_status.path]
                # If one is staged and one is unstaged, combine them
                if existing.staged != file_status.staged:
                    # File has both staged and unstaged changes
                    file_dict[file_status.path] = FileStatus(
                        path=file_status.path,
                        status="modified",  # Show as modified
                        staged=True,  # Has staged changes
                        unstaged=True  # Has unstaged changes
                    )
                # Otherwise keep the more specific status
                elif file_status.status == "modified" or existing.status != "modified":
                    file_dict[file_status.path] = file_status
            else:
                file_dict[file_status.path] = file_status
        
        # Convert back to list and filter out files that are up to date with the branch
        files = list(file_dict.values())
        
        # Only return files with actual changes
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
        
        files_with_changes.sort(key=lambda f: f.path)
        
        return files_with_changes


