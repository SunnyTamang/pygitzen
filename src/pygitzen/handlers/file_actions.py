"""File action handlers.

Handles all file-related keybinding actions (stage, unstage, etc.).
"""

from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import PygitzenApp


class FileActionHandler:
    """Handler for file-related actions.
    
    This class coordinates between the UI (app) and git operations.
    It handles the logic for file operations triggered by keybindings.
    """
    
    def __init__(self, app: "PygitzenApp") -> None:
        """Initialize file action handler.
        
        Args:
            app: The PygitzenApp instance (for accessing UI and git operations).
        """
        self.app = app
    
    def stage_file(self, file_path: str) -> None:
        """Stage a file.
        
        Args:
            file_path: Path to the file to stage.
        """
        repo_path = self.app.repo_path if hasattr(self.app, 'repo_path') else Path(".")
        try:
            result = subprocess.run(
                ["git", "add", "--", file_path],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(repo_path)
            )
            
            if result.returncode == 0:
                # Refresh file status
                self.app.load_file_status_background()
                # Show notification
                self.app.notify(f"Staged: {file_path}", severity="success", timeout=2.0)
                # Update command log
                if hasattr(self.app, 'command_log_pane'):
                    self.app.command_log_pane.update_log(f"Staged: {file_path}")
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                self.app.notify(f"Failed to stage '{file_path}': {error_msg}", severity="error", timeout=3.0)
                # Update command log with error
                if hasattr(self.app, 'command_log_pane'):
                    self.app.command_log_pane.update_log(f"Failed to stage '{file_path}': {error_msg}")
        except Exception as e:
            self.app.notify(f"Error staging '{file_path}': {str(e)}", severity="error", timeout=3.0)
            # Update command log with error
            if hasattr(self.app, 'command_log_pane'):
                self.app.command_log_pane.update_log(f"Error staging '{file_path}': {str(e)}")
    
    def unstage_file(self, file_path: str) -> None:
        """Unstage a file.
        
        Args:
            file_path: Path to the file to unstage.
        """
        repo_path = self.app.repo_path if hasattr(self.app, 'repo_path') else Path(".")
        try:
            result = subprocess.run(
                ["git", "reset", "HEAD", "--", file_path],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(repo_path)
            )
            
            if result.returncode == 0:
                # Refresh file status
                self.app.load_file_status_background()
                # Show notification
                self.app.notify(f"Unstaged: {file_path}", severity="success", timeout=2.0)
                # Update command log
                if hasattr(self.app, 'command_log_pane'):
                    self.app.command_log_pane.update_log(f"Unstaged: {file_path}")
            else:
                error_msg = result.stderr.strip() or "Unknown error"
                self.app.notify(f"Failed to unstage '{file_path}': {error_msg}", severity="error", timeout=3.0)
                # Update command log with error
                if hasattr(self.app, 'command_log_pane'):
                    self.app.command_log_pane.update_log(f"Failed to unstage '{file_path}': {error_msg}")
        except Exception as e:
            self.app.notify(f"Error unstaging '{file_path}': {str(e)}", severity="error", timeout=3.0)
            # Update command log with error
            if hasattr(self.app, 'command_log_pane'):
                self.app.command_log_pane.update_log(f"Error unstaging '{file_path}': {str(e)}")
    
    def show_file_diff(self, file_path: str, staged: bool = False, untracked: bool = False) -> None:
        """Show file diff in patch pane.
        
        Args:
            file_path: Path to the file.
            staged: Whether to show staged diff (True) or unstaged diff (False).
            untracked: Whether the file is untracked.
        """
        if not hasattr(self.app, 'file_service'):
            self.app.notify("File service not available", severity="error", timeout=2.0)
            return
        
        # Get file diff from service
        diff_text, stat_text = self.app.file_service.get_file_diff(file_path, staged=staged, untracked=untracked)
        
        # Show in patch pane
        if hasattr(self.app, 'patch_pane') and hasattr(self.app.patch_pane, 'show_file_info'):
            self.app.patch_pane.show_file_info(file_path, diff_text, stat_text, staged=staged, untracked=untracked)
        else:
            self.app.notify("Patch pane not available", severity="error", timeout=2.0)

