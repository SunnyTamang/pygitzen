from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, ListItem, ListView, Static
from textual.reactive import reactive
from textual import events

from .git_service import GitService, BranchInfo, CommitInfo


class BranchList(ListView):
    def set_branches(self, branches: list[BranchInfo]) -> None:
        self.clear()
        for b in branches:
            self.append(ListItem(Static(b.name)))


class CommitList(ListView):
    def set_commits(self, commits: list[CommitInfo]) -> None:
        self.clear()
        for c in commits:
            text = f"{c.summary}  [{c.author}]"
            self.append(ListItem(Static(text)))


class DiffView(Static):
    def show_diff(self, diff_text: str) -> None:
        self.update(diff_text if diff_text else "<no diff>")


class PygitzenApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #header { dock: top; }
    #footer { dock: bottom; }
    #main { height: 1fr; }
    #columns { height: 1fr; }
    #branches { width: 30%; }
    #commits { width: 35%; }
    #diff { width: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    active_branch: reactive[str | None] = reactive(None)

    def __init__(self, repo_dir: str = ".") -> None:
        super().__init__()
        self.git = GitService(repo_dir)
        self.branches: list[BranchInfo] = []
        self.commits: list[CommitInfo] = []

    def compose(self) -> ComposeResult:
        yield Header(id="header")
        with Horizontal(id="columns"):
            self.branch_list = BranchList(id="branches")
            self.commit_list = CommitList(id="commits")
            self.diff_view = DiffView(id="diff")
            yield self.branch_list
            yield self.commit_list
            yield self.diff_view
        yield Footer(id="footer")

    def on_mount(self) -> None:
        self.refresh_data()

    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self.branches = self.git.list_branches()
        self.branch_list.set_branches(self.branches)
        if self.branches:
            self.active_branch = self.branches[0].name
            self.load_commits(self.active_branch)

    def load_commits(self, branch: str) -> None:
        self.commits = self.git.list_commits(branch)
        self.commit_list.set_commits(self.commits)
        if self.commits:
            self.show_commit_diff(0)

    def show_commit_diff(self, index: int) -> None:
        if 0 <= index < len(self.commits):
            ci = self.commits[index]
            diff = self.git.get_commit_diff(ci.sha)
            self.diff_view.show_diff(diff)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is self.branch_list:
            index = event.index
            if 0 <= index < len(self.branches):
                self.active_branch = self.branches[index].name
                self.load_commits(self.active_branch)
        elif event.list_view is self.commit_list:
            self.show_commit_diff(event.index)


def run_textual(repo_dir: str = ".") -> None:
    app = PygitzenApp(repo_dir)
    app.run()


