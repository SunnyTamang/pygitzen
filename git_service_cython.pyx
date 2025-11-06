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
            if remote_ref in self.repo.refs:
                remote_head = self.repo.refs[remote_ref]
                for sha, _ in self._iter_commits_optimized(remote_head, 1000):
                    remote_commits.add(sha.hex())
        except Exception:
            pass
        return remote_commits
    
    def list_commits(self, str branch, int max_count=200, int skip=0):
        """Optimized Cython version of list_commits."""
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
                for sha, _ in self._iter_commits_optimized(base_head, 1000):
                    base_branch_commits.add(sha.hex())
                break
        
        commits = []
        yielded = 0
        
        for index, (sha, commit) in enumerate(self._iter_commits_optimized(head, -1)):
            commit_sha = sha.hex()
            
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
            # Change to repo directory
            original_cwd = os.getcwd()
            os.chdir(str(self.repo_path))
            
            try:
                # Use git rev-list --count for main/master branches
                if branch in ["main", "master"]:
                    result = subprocess.run(
                        ['git', 'rev-list', '--count', branch],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if result.returncode == 0:
                        return int(result.stdout.strip())
                
                # For other branches, use git rev-list with exclusion
                base_branch_names = ["main", "master"]
                for base_name in base_branch_names:
                    if base_name != branch:
                        # Try to get merge-base
                        merge_base_result = subprocess.run(
                            ['git', 'merge-base', base_name, branch],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        if merge_base_result.returncode == 0:
                            merge_base = merge_base_result.stdout.strip()
                            # Count commits from merge-base to branch
                            count_result = subprocess.run(
                                ['git', 'rev-list', '--count', f'{merge_base}..{branch}'],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            if count_result.returncode == 0:
                                return int(count_result.stdout.strip())
                        break
            finally:
                os.chdir(original_cwd)
        except Exception:
            # Fallback to Python iteration if Git command fails
            pass
        
        # Fallback: Python-based counting (original algorithm)
        ref = f"refs/heads/{branch}".encode()
        head = self.repo.refs[ref]
        
        base_branch_commits = set()
        base_branch_names = ["main", "master"]
        
        for base_name in base_branch_names:
            base_ref = f"refs/heads/{base_name}".encode()
            if base_ref in self.repo.refs and base_name != branch:
                base_head = self.repo.refs[base_ref]
                for sha, _ in self._iter_commits_optimized(base_head, -1):
                    base_branch_commits.add(sha.hex())
                break
        
        count = 0
        for sha, _ in self._iter_commits_optimized(head, -1):
            commit_sha = sha.hex()
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
    
    def get_commit_diff(self, str sha_hex):
        """Get diff for a commit."""
        sha = bytes.fromhex(sha_hex)
        commit = self.repo[sha]
        parents = commit.parents
        
        from dulwich.patch import write_tree_diff
        import io
        
        buf = io.BytesIO()
        
        if not parents:
            # Root commit (no parent) - show all files as additions
            from dulwich.objects import Tree
            empty_tree = Tree()
            write_tree_diff(buf, self.repo.object_store, empty_tree, commit.tree)
        else:
            # Regular commit - show diff between parent and commit
            parent = self.repo[parents[0]]
            write_tree_diff(buf, self.repo.object_store, parent.tree, commit.tree)
        
        diff_text = buf.getvalue().decode(errors="replace")
        return diff_text
    
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
        """Optimized Cython version of get_file_status with early skipping."""
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
            head_ref = self.repo.refs[b"HEAD"]
            head_commit = self.repo[head_ref]
            head_tree = self.repo[head_commit.tree]
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
                                    files.append(FileStatus(path=rel_path, status="untracked", staged=False, unstaged=False))
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
                # Untracked file - include it only if not ignored
                if not self._is_ignored(f.path):
                    files_with_changes.append(f)
            elif f.status == "deleted":
                # Deleted file - include it
                files_with_changes.append(f)
            elif f.status == "staged":
                # New file (staged) - include it
                files_with_changes.append(f)
        
        files_with_changes.sort(key=lambda f: f.path)
        
        return files_with_changes


