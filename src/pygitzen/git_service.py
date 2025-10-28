from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from dulwich.objects import Commit
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

    def list_branches(self) -> List[BranchInfo]:
        heads = self.repo.refs.as_dict(b"refs/heads")
        result: List[BranchInfo] = []
        for ref, sha in heads.items():
            name = ref.decode().split("/heads/")[-1]
            result.append(BranchInfo(name=name, head_sha=sha.hex()))
        result.sort(key=lambda b: b.name.lower())
        return result

    def _iter_commits(self, head_sha: bytes, max_count: int = 100) -> Iterable[Tuple[bytes, Commit]]:
        seen = set()
        stack = [head_sha]
        while stack and len(seen) < max_count:
            sha = stack.pop(0)
            if sha in seen:
                continue
            seen.add(sha)
            commit: Commit = self.repo[sha]
            yield sha, commit
            stack.extend(commit.parents)

    def list_commits(self, branch: str, max_count: int = 200) -> List[CommitInfo]:
        ref = f"refs/heads/{branch}".encode()
        head = self.repo.refs[ref]
        commits: List[CommitInfo] = []
        for sha, commit in self._iter_commits(head, max_count=max_count):
            author = commit.author.decode(errors="replace") if isinstance(commit.author, (bytes, bytearray)) else str(commit.author)
            summary = commit.message.split(b"\n", 1)[0].decode(errors="replace")
            commits.append(
                CommitInfo(
                    sha=sha.hex(),
                    summary=summary,
                    author=author,
                    timestamp=int(commit.commit_time),
                )
            )
        return commits

    def get_commit_diff(self, sha_hex: str) -> str:
        sha = bytes.fromhex(sha_hex)
        commit: Commit = self.repo[sha]
        parents = commit.parents
        if not parents:
            return "<root commit>"
        parent = self.repo[parents[0]]

        # Generate a unified diff between parent tree and commit tree
        from dulwich.diff_tree import tree_changes
        from dulwich.patch import write_tree_diff
        import io

        changes = tree_changes(self.repo.object_store, parent.tree, commit.tree)
        buf = io.BytesIO()
        write_tree_diff(buf, self.repo.object_store, changes, parent.tree, commit.tree)
        return buf.getvalue().decode(errors="replace")


