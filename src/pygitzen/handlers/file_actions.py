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

