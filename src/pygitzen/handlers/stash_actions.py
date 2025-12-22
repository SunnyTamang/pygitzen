"""Stash action handlers.

Handles all stash-related keybinding actions (stash, apply, pop, drop, etc.).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import PygitzenApp


class StashActionHandler:
    """Handler for stash-related actions.
    
    This class coordinates between the UI (app) and stash services.
    It handles the logic for stash operations triggered by keybindings.
    """
    
    def __init__(self, app: "PygitzenApp") -> None:
        """Initialize stash action handler.
        
        Args:
            app: The PygitzenApp instance (for accessing UI and services).
        """
        self.app = app
    
    def stash(self) -> None:
        """Quick stash all changes.
        
        This action is triggered when 's' is pressed while Files/Staged/Changes panes have focus.
        Shows a dialog to get stash message, then creates the stash.
        """
        from ..ui.dialogs import StashMessageDialog
        
        def on_dialog_result(message: str | None) -> None:
            """Handle dialog result."""
            if message is None:
                # User cancelled
                return
            
            # Perform stash using stash service
            result = self.app.stash_service.stash_all(message)
            
            if result["success"]:
                self.app.notify(
                    "Stashed changes",
                    severity="success",
                    timeout=2.0
                )
                self.app.command_log_pane.update_log(f"Stashed: {message or '(no message)'}")
                
                # Refresh UI to show the new stash
                self.app.load_stashes_background()
                self.app.load_file_status_background()
            else:
                error_msg = result.get("error", "Unknown error")
                error_message = f"Failed to stash changes: {error_msg}"
                self.app.notify(
                    error_message,
                    severity="error",
                    timeout=5.0
                )
                self.app.command_log_pane.update_log(error_message)
        
        # Show dialog to get stash message
        dialog = StashMessageDialog()
        self.app.push_screen(dialog, callback=on_dialog_result)
    
    def stash_options(self) -> None:
        """Show stash options menu.
        
        This action is triggered when 'S' is pressed while Files/Staged/Changes panes have focus.
        Shows a menu with different stash options.
        """
        from ..ui.dialogs import StashOptionsMenuDialog, StashMessageDialog
        
        def on_menu_result(option: str | None) -> None:
            """Handle menu selection."""
            if not option:
                # User cancelled
                return
            
            # Map option to stash function
            stash_functions = {
                "all": self.app.stash_service.stash_all,
                "keep-index": self.app.stash_service.stash_keep_index,
                "untracked": self.app.stash_service.stash_include_untracked,
                "staged": self.app.stash_service.stash_staged,
                "unstaged": self.app.stash_service.stash_unstaged,
            }
            
            stash_func = stash_functions.get(option)
            if not stash_func:
                return
            
            # Validate prerequisites for some options
            if option == "staged":
                # Check if there are staged files
                try:
                    files = self.app.git.get_file_status()
                    staged_files = [f for f in files if f.staged]
                    if not staged_files:
                        self.app.notify(
                            "No staged files to stash",
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
            
            # Show message dialog
            def on_message_result(message: str | None) -> None:
                """Handle message dialog result."""
                if message is None:
                    # User cancelled
                    return
                
                # Perform stash
                result = stash_func(message)
                
                if result["success"]:
                    option_names = {
                        "all": "Stashed all changes",
                        "keep-index": "Stashed all changes (kept index)",
                        "untracked": "Stashed including untracked files",
                        "staged": "Stashed staged changes",
                        "unstaged": "Stashed unstaged changes",
                    }
                    success_msg = option_names.get(option, "Stashed changes")
                    self.app.notify(
                        success_msg,
                        severity="success",
                        timeout=2.0
                    )
                    self.app.command_log_pane.update_log(f"{success_msg}: {message or '(no message)'}")
                    
                    # Refresh UI
                    self.app.load_stashes_background()
                    self.app.load_file_status_background()
                else:
                    error_msg = result.get("error", "Unknown error")
                    error_message = f"Failed to stash: {error_msg}"
                    self.app.notify(
                        error_message,
                        severity="error",
                        timeout=5.0
                    )
                    self.app.command_log_pane.update_log(error_message)
            
            # Show message dialog
            dialog = StashMessageDialog()
            self.app.push_screen(dialog, callback=on_message_result)
        
        # Show stash options menu
        menu = StashOptionsMenuDialog()
        self.app.push_screen(menu, callback=on_menu_result)
    
    def apply(self) -> None:
        """Apply a stash entry.
        
        This action is triggered when '<space>' is pressed while Stash pane has focus.
        Shows confirmation, then applies the stash.
        """
        # Only handle if stash pane has focus
        if not hasattr(self.app, 'stash_pane') or not self.app.stash_pane.has_focus:
            return
        
        # Get selected stash index
        selected_index = self.app.stash_pane.index
        if selected_index is None or selected_index < 0:
            self.app.notify("No stash selected", severity="warning")
            return
        
        # Get stashes
        stashes = self.app.stashes if hasattr(self.app, 'stashes') else []
        if selected_index >= len(stashes):
            self.app.notify("Invalid stash selection", severity="warning")
            return
        
        stash = stashes[selected_index]
        
        # Show confirmation
        from ..ui.dialogs import StashConfirmDialog
        
        def on_confirm(confirmed: bool) -> None:
            """Handle confirmation."""
            if not confirmed:
                return
            
            # Apply stash
            result = self.app.stash_service.apply_stash(stash.index)
            
            if result["success"]:
                self.app.notify(
                    f"Applied stash: {stash.message}",
                    severity="success",
                    timeout=2.0
                )
                self.app.command_log_pane.update_log(f"Applied stash@{stash.index}: {stash.message}")
                
                # Refresh UI
                self.app.load_stashes_background()
                self.app.load_file_status_background()
            else:
                error_msg = result.get("error", "Unknown error")
                error_message = f"Failed to apply stash: {error_msg}"
                self.app.notify(
                    error_message,
                    severity="error",
                    timeout=5.0
                )
                self.app.command_log_pane.update_log(error_message)
        
        dialog = StashConfirmDialog(
            title="Stash apply",
            message="Are you sure you want to apply this stash entry?",
            stash_name=stash.name,
            confirm_text="Apply",
            cancel_text="Cancel"
        )
        self.app.push_screen(dialog, callback=on_confirm)
    
    def pop(self) -> None:
        """Pop a stash entry (apply and remove).
        
        This action is triggered when 'g' is pressed while Stash pane has focus.
        Shows confirmation, then pops the stash.
        """
        # Only handle if stash pane has focus
        if not hasattr(self.app, 'stash_pane') or not self.app.stash_pane.has_focus:
            return
        
        # Get selected stash index
        selected_index = self.app.stash_pane.index
        if selected_index is None or selected_index < 0:
            self.app.notify("No stash selected", severity="warning")
            return
        
        # Get stashes
        stashes = self.app.stashes if hasattr(self.app, 'stashes') else []
        if selected_index >= len(stashes):
            self.app.notify("Invalid stash selection", severity="warning")
            return
        
        stash = stashes[selected_index]
        
        # Show confirmation
        from ..ui.dialogs import StashConfirmDialog
        
        def on_confirm(confirmed: bool) -> None:
            """Handle confirmation."""
            if not confirmed:
                return
            
            # Pop stash
            result = self.app.stash_service.pop_stash(stash.index)
            
            if result["success"]:
                self.app.notify(
                    f"Popped stash: {stash.message}",
                    severity="success",
                    timeout=2.0
                )
                self.app.command_log_pane.update_log(f"Popped stash@{stash.index}: {stash.message}")
                
                # Refresh UI
                self.app.load_stashes_background()
                self.app.load_file_status_background()
            else:
                error_msg = result.get("error", "Unknown error")
                error_message = f"Failed to pop stash: {error_msg}"
                self.app.notify(
                    error_message,
                    severity="error",
                    timeout=5.0
                )
                self.app.command_log_pane.update_log(error_message)
        
        dialog = StashConfirmDialog(
            title="Stash pop",
            message="Are you sure you want to pop this stash entry?",
            stash_name=stash.name,
            confirm_text="Pop",
            cancel_text="Cancel"
        )
        self.app.push_screen(dialog, callback=on_confirm)
    
    def drop(self) -> None:
        """Drop a stash entry (remove without applying).
        
        This action is triggered when 'd' is pressed while Stash pane has focus.
        Shows confirmation, then drops the stash.
        """
        # Only handle if stash pane has focus
        if not hasattr(self.app, 'stash_pane') or not self.app.stash_pane.has_focus:
            return
        
        # Get selected stash index
        selected_index = self.app.stash_pane.index
        if selected_index is None or selected_index < 0:
            self.app.notify("No stash selected", severity="warning")
            return
        
        # Get stashes
        stashes = self.app.stashes if hasattr(self.app, 'stashes') else []
        if selected_index >= len(stashes):
            self.app.notify("Invalid stash selection", severity="warning")
            return
        
        stash = stashes[selected_index]
        
        # Show confirmation
        from ..ui.dialogs import StashConfirmDialog
        
        def on_confirm(confirmed: bool) -> None:
            """Handle confirmation."""
            if not confirmed:
                return
            
            # Drop stash
            result = self.app.stash_service.drop_stash(stash.index)
            
            if result["success"]:
                self.app.notify(
                    f"Dropped stash: {stash.message}",
                    severity="success",
                    timeout=2.0
                )
                self.app.command_log_pane.update_log(f"Dropped stash@{stash.index}: {stash.message}")
                
                # Refresh UI
                self.app.load_stashes_background()
            else:
                error_msg = result.get("error", "Unknown error")
                error_message = f"Failed to drop stash: {error_msg}"
                self.app.notify(
                    error_message,
                    severity="error",
                    timeout=5.0
                )
                self.app.command_log_pane.update_log(error_message)
        
        dialog = StashConfirmDialog(
            title="Stash drop",
            message="Are you sure you want to drop this stash entry?",
            stash_name=stash.name,
            warning="This action cannot be undone.",
            confirm_text="Drop",
            cancel_text="Cancel"
        )
        self.app.push_screen(dialog, callback=on_confirm)
    
    def rename(self) -> None:
        """Rename a stash entry.
        
        This action is triggered when 'r' is pressed while Stash pane has focus.
        Shows a dialog to get new message, then renames the stash.
        """
        # Only handle if stash pane has focus
        if not hasattr(self.app, 'stash_pane') or not self.app.stash_pane.has_focus:
            return
        
        # Get selected stash index
        selected_index = self.app.stash_pane.index
        if selected_index is None or selected_index < 0:
            self.app.notify("No stash selected", severity="warning")
            return
        
        # Get stashes
        stashes = self.app.stashes if hasattr(self.app, 'stashes') else []
        if selected_index >= len(stashes):
            self.app.notify("Invalid stash selection", severity="warning")
            return
        
        stash = stashes[selected_index]
        
        # Build full stash details string (matching lazygit format: stash@{index}: name)
        # Use the exact name from git stash list to preserve original format
        stash_details = f"stash@{{{stash.index}}}: {stash.name}"
        
        # Show dialog to get new message
        from ..ui.dialogs import StashRenameDialog
        
        def on_dialog_result(edited_text: str | None) -> None:
            """Handle dialog result."""
            if not edited_text:
                # User cancelled
                return
            
            edited_text = edited_text.strip()
            if not edited_text:
                self.app.notify("Stash details cannot be empty", severity="warning")
                return
            
            # Extract message from edited text
            # User can edit: "On branch: message" or just "message"
            # We need to extract just the message part (everything after "On branch: " or just use the whole thing)
            # import re
            # # Try to match "On branch: message" format
            # match = re.match(r'(?:On |O |WIP on )?[^:]+:\s*(.+)', edited_text)
            # if match:
            #     # Has branch prefix, extract message
            #     new_message = match.group(1).strip()
            # else:
            #     # No branch prefix, use entire text as message
            #     new_message = edited_text.strip()
            
            # TEMPORARY: Use entire edited text as message (regex commented out for testing)
            new_message = edited_text.strip()
            
            if not new_message:
                self.app.notify("Stash message cannot be empty", severity="warning")
                return
            
            # Rename stash
            # Note: We use stash.index (the actual git stash index) not selected_index
            # because after rename, indices shift and we need the correct one
            result = self.app.stash_service.rename_stash(stash.index, new_message)
            
            if result["success"]:
                self.app.notify(
                    f"Renamed stash: {new_message}",
                    severity="success",
                    timeout=2.0
                )
                self.app.command_log_pane.update_log(f"Renamed stash@{stash.index}: {new_message}")
                
                # Refresh stash list synchronously to ensure UI updates immediately
                # After rename, the renamed stash becomes stash@{0}, so we need to refresh
                # and reselect it to update the diff pane
                try:
                    stashes = self.app.stash_service.load_stashes()
                    self.app.stashes = stashes
                    self.app.stash_pane.set_stashes(stashes)
                    
                    # Reselect stash at index 0 (renamed stash becomes the most recent)
                    if stashes:
                        self.app.stash_pane.index = 0
                        # Explicitly refresh the diff pane with the updated stash
                        self.app.show_stash_diff(0)
                except Exception as e:
                    # If synchronous refresh fails, fall back to background refresh
                    self.app.load_stashes_background()
                
                # Also refresh file status in case anything changed
                self.app.load_file_status_background()
            else:
                error_msg = result.get("error", "Unknown error")
                error_message = f"Failed to rename stash: {error_msg}"
                self.app.notify(
                    error_message,
                    severity="error",
                    timeout=5.0
                )
                self.app.command_log_pane.update_log(error_message)
        
        # Show dialog with full stash details
        # The editable part is the stash.name (everything after "stash@{X}: ")
        dialog = StashRenameDialog(
            stash_details=stash_details,
            current_message=stash.name  # Pass the full stash name (editable part)
        )
        self.app.push_screen(dialog, callback=on_dialog_result)

