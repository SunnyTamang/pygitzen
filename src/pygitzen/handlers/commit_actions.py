"""Commit action handler.

Handles commit-related keybinding actions (create commit, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import PygitzenApp


class CommitActionHandler:
    """Handler for commit-related actions."""

    def __init__(self, app: "PygitzenApp") -> None:
        """Initialize commit action handler.
        
        Args:
            app: Main application instance.
        """
        self.app = app

    def create(self) -> None:
        """Create a commit.
        
        This action is triggered when 'c' is pressed while there are staged changes.
        Shows a dialog to get commit message, then creates the commit.
        """
        # Check if there are staged files
        try:
            files = self.app.git.get_file_status()
            staged_files = [f for f in files if f.staged]
            
            if not staged_files:
                self.app.notify(
                    "No staged files to commit. Stage files first.",
                    severity="warning",
                    timeout=3.0
                )
                return
        except Exception:
            self.app.notify(
                "Failed to check staged files",
                severity="error",
                timeout=3.0
            )
            return
        
        # Show dialog to get commit message
        from ..ui.dialogs import CommitDialog
        
        def on_dialog_result(result: tuple[str, str] | None) -> None:
            """Handle dialog result."""
            if not result:
                # User cancelled
                return
            
            summary, description = result
            
            # Validate commit message
            if not summary.strip():
                self.app.notify(
                    "Commit message cannot be empty",
                    severity="warning",
                    timeout=3.0
                )
                return
            
            # Perform commit using commit service
            commit_result = self.app.commit_service.create_commit(
                summary=summary,
                description=description,
                no_verify=False
            )
            
            if commit_result["success"]:
                sha = commit_result.get("sha", "")
                sha_display = f" ({sha[:7]})" if sha else ""
                message = f"Committed changes{sha_display}"
                
                self.app.notify(
                    message,
                    severity="success",
                    timeout=2.0
                )
                self.app.command_log_pane.update_log(f"Committed: {summary}{sha_display}")
                
                # Refresh UI to show the new commit
                self.app.refresh_data_fast()
                
                # Update commits pane to show the new commit
                if self.app.active_branch:
                    self.app.load_commits(self.app.active_branch)
                    self.app.load_commits_for_log(self.app.active_branch)
            else:
                # Show error notification
                error_msg = commit_result.get("error", "Unknown error")
                error_message = f"Failed to create commit: {error_msg}"
                self.app.notify(
                    error_message,
                    severity="error",
                    timeout=5.0
                )
                self.app.command_log_pane.update_log(error_message)
        
        # Get staged files count
        staged_count = len([f for f in self.app.git.get_file_status() if f.staged])
        
        # Show the dialog
        dialog = CommitDialog(staged_files_count=staged_count)
        self.app.push_screen(dialog, callback=on_dialog_result)

