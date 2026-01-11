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
                # Update patch pane if this file is currently displayed
                if (hasattr(self.app, 'patch_pane') and 
                    hasattr(self.app.patch_pane, '_current_file_path') and
                    self.app.patch_pane._current_file_path == file_path and
                    not self.app.patch_pane._current_file_staged):
                    # File is currently shown and was unstaged, now it's staged - update patch pane
                    if hasattr(self.app, 'file_service'):
                        diff_text, stat_text = self.app.file_service.get_file_diff(file_path, staged=True, untracked=False)
                        self.app.patch_pane.show_file_info(file_path, diff_text, stat_text, staged=True, untracked=False)
                
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
                # Update patch pane if this file is currently displayed
                if (hasattr(self.app, 'patch_pane') and 
                    hasattr(self.app.patch_pane, '_current_file_path') and
                    self.app.patch_pane._current_file_path == file_path and
                    self.app.patch_pane._current_file_staged):
                    # File is currently shown and was staged, now it's unstaged - update patch pane
                    if hasattr(self.app, 'file_service'):
                        # Check if file is untracked
                        files = self.app.git.get_file_status()
                        file_status = next((f for f in files if f.path == file_path), None)
                        untracked = file_status.status == "untracked" if file_status else False
                        diff_text, stat_text = self.app.file_service.get_file_diff(file_path, staged=False, untracked=untracked)
                        self.app.patch_pane.show_file_info(file_path, diff_text, stat_text, staged=False, untracked=untracked)
                
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
    
    def discard_file(self, file_path: str, staged: bool = False, untracked: bool = False) -> None:
        """Discard changes for a file.
        
        Args:
            file_path: Path to the file.
            staged: Whether to discard staged changes (True) or unstaged changes (False).
            untracked: Whether the file is untracked.
        """
        if not hasattr(self.app, 'file_service'):
            self.app.notify("File service not available", severity="error", timeout=2.0)
            return
        
        # Get file status to determine what to discard
        files = self.app.git.get_file_status()
        file_status = next((f for f in files if f.path == file_path), None)
        
        if not file_status:
            self.app.notify(f"File '{file_path}' not found", severity="error", timeout=2.0)
            return
        
        # Check if file has both staged and unstaged changes
        # If discarding from StagedPane, only discard staged
        # If discarding from ChangesPane, discard unstaged (and staged if both exist)
        is_untracked = file_status.status == "untracked"
        
        # For files with both staged and unstaged changes, we need to handle both
        # Check if file appears in both staged and changes lists
        # Use the unstaged flag, not just status string, as files with both changes
        # might have status="staged" but unstaged=True
        has_staged_changes = any(f.path == file_path and f.staged for f in files)
        has_unstaged_changes = any(f.path == file_path and (f.unstaged or f.status == "untracked") for f in files)
        
        success = True
        errors = []
        
        # If discarding staged changes, unstage first (git reset)
        if staged and has_staged_changes:
            result = self.app.file_service.discard_file_changes(file_path, staged=True, untracked=False)
            if not result["success"]:
                success = False
                errors.append(result.get("error", "Unknown error"))
        
        # If discarding unstaged changes (from ChangesPane)
        if not staged and (has_unstaged_changes or is_untracked):
            result = self.app.file_service.discard_file_changes(file_path, staged=False, untracked=is_untracked)
            if not result["success"]:
                success = False
                errors.append(result.get("error", "Unknown error"))
        
        if success:
            # Clear patch pane if this file is currently displayed
            if (hasattr(self.app, 'patch_pane') and 
                hasattr(self.app.patch_pane, '_current_file_path') and
                self.app.patch_pane._current_file_path == file_path):
                # Clear the patch pane since file changes are discarded
                from rich.text import Text
                self.app.patch_pane.update(Text(f"{file_path} - Changes discarded", style="dim"))
            
            # Refresh file status
            self.app.load_file_status_background()
            # Show notification - use file_status directly for accurate change type
            if staged:
                change_type = "staged"
            elif is_untracked:
                change_type = "untracked"
            elif file_status.unstaged:
                change_type = "unstaged"
            else:
                change_type = "unstaged"  # Default fallback
            self.app.notify(f"Discarded {change_type} changes: {file_path}", severity="success", timeout=2.0)
            # Update command log
            if hasattr(self.app, 'command_log_pane'):
                self.app.command_log_pane.update_log(f"Discarded {change_type} changes: {file_path}")
        else:
            error_msg = "; ".join(errors) if errors else "Unknown error"
            self.app.notify(f"Failed to discard changes for '{file_path}': {error_msg}", severity="error", timeout=3.0)
            # Update command log with error
            if hasattr(self.app, 'command_log_pane'):
                self.app.command_log_pane.update_log(f"Failed to discard changes for '{file_path}': {error_msg}")

