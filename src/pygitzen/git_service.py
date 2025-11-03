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
                for sha, _ in self._iter_commits(remote_head, max_count=1000):
                    remote_commits.add(sha.hex())
        except Exception:
            # Remote not available or not configured
            pass
        return remote_commits

    def list_commits(self, branch: str, max_count: int = 200, skip: int = 0) -> List[CommitInfo]:
        ref = f"refs/heads/{branch}".encode()
        head = self.repo.refs[ref]
        
        # Get remote commits to check push status
        remote_commits = self._get_remote_commits(branch)
        
        # Get commits from base branch (main or master) to exclude shared history
        base_branch_commits = set()
        base_branch_names = ["main", "master"]
        for base_name in base_branch_names:
            base_ref = f"refs/heads/{base_name}".encode()
            if base_ref in self.repo.refs and base_name != branch:
                base_head = self.repo.refs[base_ref]
                # Collect all commits from base branch
                for sha, _ in self._iter_commits(base_head, max_count=1000):
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

    def count_commits(self, branch: str) -> int:
        """Count commits for a branch, applying the same base-branch exclusion."""
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

    def get_commit_diff(self, sha_hex: str) -> str:
        sha = bytes.fromhex(sha_hex)
        commit: Commit = self.repo[sha]
        parents = commit.parents
        
        from dulwich.patch import write_tree_diff
        import io

        buf = io.BytesIO()
        
        if not parents:
            # Root commit (no parent) - show all files as additions
            # Use empty tree (all zeros) as parent to show all files as new
            from dulwich.objects import Tree
            empty_tree = Tree()
            write_tree_diff(buf, self.repo.object_store, empty_tree, commit.tree)
        else:
            # Regular commit - show diff between parent and commit
            parent = self.repo[parents[0]]
            write_tree_diff(buf, self.repo.object_store, parent.tree, commit.tree)
        
        diff_text = buf.getvalue().decode(errors="replace")
        
        return diff_text

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
        """Get status of files in working directory."""
        from dulwich.index import Index
        from dulwich.objects import Blob
        
        files: List[FileStatus] = []
        
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
        
        # Get HEAD tree
        try:
            head_ref = self.repo.refs[b"HEAD"]
            head_commit = self.repo[head_ref]
            head_tree = self.repo[head_commit.tree]
        except Exception:
            head_tree = None
        
        # Track files we've processed
        processed_files = set()
        
        # Check files in index (staged)
        for path, entry in index_entries.items():
            processed_files.add(path)
            full_path = self.repo_path / path
            
            if not full_path.exists():
                # File deleted
                files.append(FileStatus(path=path, status="deleted", staged=True, unstaged=False))
                continue
            
            # Calculate working directory file sha using dulwich
            try:
                with open(full_path, "rb") as f:
                    file_data = f.read()
                    file_sha = calculate_blob_sha(file_data)
            except Exception:
                continue
            
            index_sha = entry.sha
            
            # Check if staged (different from HEAD or new)
            head_sha = None
            if head_tree:
                path_parts = path.split("/")
                try:
                    head_sha = self._find_in_tree(head_tree, path_parts)
                except (KeyError, TypeError):
                    head_sha = None
            
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
        def walk_directory(path: Path, base: Path):
            """Recursively walk directory."""
            # Skip .git directory itself, but not repo root (which contains .git)
            if path.name == ".git" and path.is_dir():
                return
            
            try:
                for item in path.iterdir():
                    if item.name.startswith("."):
                        continue
                    
                    if item.is_dir():
                        walk_directory(item, base)
                    elif item.is_file():
                        rel_path = str(item.relative_to(base)).replace("\\", "/")
                        
                        if rel_path not in processed_files:
                            # File not in index, check if it's tracked in HEAD or untracked
                            head_sha = None
                            if head_tree:
                                path_parts = rel_path.split("/")
                                try:
                                    head_sha = self._find_in_tree(head_tree, path_parts)
                                except (KeyError, TypeError):
                                    head_sha = None
                            
                            if head_sha is not None:
                                # File is tracked in HEAD but not in index (modified, not staged)
                                # Calculate working directory file SHA using dulwich
                                with open(item, "rb") as f:
                                    file_data = f.read()
                                    file_sha = calculate_blob_sha(file_data)
                                
                                if head_sha != file_sha:
                                    # Modified from HEAD, not staged
                                    files.append(FileStatus(path=rel_path, status="modified", staged=False, unstaged=True))
                                # else: file matches HEAD, nothing to show
                            else:
                                # Not in HEAD, so it's untracked
                                # Only add if not ignored by .gitignore
                                if not self._is_ignored(rel_path):
                                    files.append(FileStatus(path=rel_path, status="untracked", staged=False, unstaged=False))
                        else:
                            # File is in index, check if modified in working directory (unstaged changes)
                            if rel_path in index_entries:
                                entry = index_entries[rel_path]
                                try:
                                    # Calculate working directory file SHA using dulwich
                                    with open(item, "rb") as f:
                                        file_data = f.read()
                                        file_sha = calculate_blob_sha(file_data)
                                    
                                    # Get index SHA (staged version)
                                    index_sha = entry.sha
                                    
                                    # Get HEAD SHA to verify file is actually different
                                    head_sha = None
                                    if head_tree:
                                        path_parts = rel_path.split("/")
                                        try:
                                            head_sha = self._find_in_tree(head_tree, path_parts)
                                        except (KeyError, TypeError):
                                            head_sha = None
                                    
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
        
        # Only return files with actual changes (modified, staged, untracked, deleted, etc.)
        # Filter out files that match HEAD exactly (no changes) - matching VSCode behavior
        # A file should only appear if it has:
        # - Staged changes (index != HEAD) -> staged=True
        # - Unstaged changes (working != HEAD and working != index) -> unstaged=True
        # - Is untracked (not in index, not in HEAD) -> status="untracked"
        # - Is deleted (in index but not in working) -> status="deleted", staged=True
        # - Is new (not in HEAD, in index) -> status="staged", staged=True
        files_with_changes = []
        for f in files:
            # Only include files with actual changes
            # Files that are up to date (HEAD == index == working) will NOT have staged or unstaged flags
            if f.staged or f.unstaged:
                # File has staged or unstaged changes - include it
                files_with_changes.append(f)
            elif f.status == "untracked":
                # Untracked file - include it only if not ignored
                if not self._is_ignored(f.path):
                    files_with_changes.append(f)
            elif f.status == "deleted":
                # Deleted file - include it
                files_with_changes.append(f)
            elif f.status == "staged":
                # New file (staged) - include it
                # Note: This should already have staged=True, but include for safety
                files_with_changes.append(f)
            # All other files (including files up to date with HEAD) are excluded
        
        files_with_changes.sort(key=lambda f: f.path)
        
        return files_with_changes


