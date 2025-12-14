"""Branch action handlers.

Handles all branch-related keybinding actions (checkout, create, delete, etc.).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import PygitzenApp


class BranchActionHandler:
    """Handler for branch-related actions.
    
    This class coordinates between the UI (app) and branch services.
    It handles the logic for branch operations triggered by keybindings.
    """
    
    def __init__(self, app: "PygitzenApp") -> None:
        """Initialize branch action handler.
        
        Args:
            app: The PygitzenApp instance (for accessing UI and services).
        """
        self.app = app
    
    def checkout(self) -> None:
        """Checkout a branch from the branches pane.
        
        This action is triggered when 'c' is pressed while the branches pane has focus.
        It checks out the currently selected branch.
        """
        # Only handle checkout if branches pane has focus
        if not self.app.branches_pane.has_focus:
            return
        
        # Get the selected branch index
        selected_index = self.app.branches_pane.index
        if selected_index is None or selected_index < 0 or selected_index >= len(self.app.branches):
            self.app.notify("No branch selected", severity="warning")
            return
        
        # Get the branch name
        branch_to_checkout = self.app.branches[selected_index].name
        
        # Check if already on this branch
        current_branch = self.app._get_current_branch_name()
        if branch_to_checkout == current_branch:
            self.app.notify(f"Already on branch '{branch_to_checkout}'", severity="info")
            return
        
        # Perform checkout using branch service
        result = self.app.branch_service.checkout_branch(branch_to_checkout, self.app.git)
        
        if result["success"]:
            # Update active branch
            self.app.active_branch = branch_to_checkout
            
            # Show success notification
            self.app.notify(
                f"Switched to branch '{branch_to_checkout}'",
                severity="success",
                timeout=2.0
            )
            
            # Refresh UI to reflect the checkout
            # This will update branches, status, commits, etc.
            self.app.refresh_data_fast()
        else:
            # Show error notification
            error_msg = result.get("error", "Unknown error")
            self.app.notify(
                f"Failed to checkout '{branch_to_checkout}': {error_msg}",
                severity="error",
                timeout=5.0
            )
    
    def create(self) -> None:
        """Create a new branch.
        
        This action is triggered when 'n' is pressed while the branches pane has focus.
        Shows a dialog to get the branch name, then creates the branch.
        """
        # Note: The focus check is removed because in TabbedContent, focus might be on
        # the container rather than the pane itself. The binding on BranchesPane will
        # ensure this is only triggered when appropriate.
        
        # Get the selected branch as base (if any)
        base_branch = None
        selected_index = self.app.branches_pane.index
        if selected_index is not None and 0 <= selected_index < len(self.app.branches):
            base_branch = self.app.branches[selected_index].name
        
        # Show dialog to get branch name
        from ..ui.dialogs import NewBranchDialog
        
        def on_dialog_result(branch_name: str | None) -> None:
            """Handle dialog result."""
            if not branch_name:
                # User cancelled
                return
            
            # Validate branch name (basic check)
            branch_name = branch_name.strip()
            if not branch_name:
                self.app.notify("Branch name cannot be empty", severity="warning")
                return
            
            # Perform branch creation using branch service
            result = self.app.branch_service.create_branch(
                branch_name,
                base=base_branch,
                git_service=self.app.git
            )
            
            if result["success"]:
                # git checkout -b always checks out the new branch, so update active_branch
                created_branch = result.get("branch", branch_name)
                self.app.active_branch = created_branch
                
                # Show success notification
                if base_branch:
                    self.app.notify(
                        f"Created branch '{created_branch}' from '{base_branch}'",
                        severity="success",
                        timeout=2.0
                    )
                else:
                    self.app.notify(
                        f"Created and switched to branch '{created_branch}'",
                        severity="success",
                        timeout=2.0
                    )
                
                # Refresh UI to show the new branch
                self.app.refresh_data_fast()
                
                # Update branches pane selection to highlight the new branch
                # Use a timer to ensure branches are reloaded after refresh_data_fast completes
                def update_selection():
                    """Update selection to the newly created branch."""
                    try:
                        # Find the new branch in the list and select it
                        for i, branch in enumerate(self.app.branches):
                            if branch.name == created_branch:
                                self.app.branches_pane.index = i
                                self.app.branches_pane.highlighted = i
                                break
                    except (AttributeError, IndexError):
                        # If branches haven't been loaded yet, try again after a short delay
                        pass
                
                # Schedule selection update after a short delay to ensure refresh completes
                self.app.set_timer(0.1, update_selection)
            else:
                # Show error notification
                error_msg = result.get("error", "Unknown error")
                self.app.notify(
                    f"Failed to create branch '{branch_name}': {error_msg}",
                    severity="error",
                    timeout=5.0
                )
        
        # Show the dialog
        dialog = NewBranchDialog(base_branch=base_branch)
        self.app.push_screen(dialog, callback=on_dialog_result)
    
    def delete(self) -> None:
        """Delete a branch.
        
        This action is triggered when 'd' is pressed while the branches pane has focus.
        Shows a confirmation dialog, then deletes the branch if confirmed.
        """
        # Get the selected branch index
        selected_index = self.app.branches_pane.index
        if selected_index is None or selected_index < 0 or selected_index >= len(self.app.branches):
            self.app.notify("No branch selected", severity="warning")
            return
        
        # Get the branch name
        branch_to_delete = self.app.branches[selected_index].name
        
        # Check if trying to delete the current branch
        current_branch = self.app._get_current_branch_name()
        if branch_to_delete == current_branch:
            self.app.notify(
                "Cannot delete the current branch. Switch to another branch first.",
                severity="error",
                timeout=5.0
            )
            return
        
        # Check if branch has remote tracking
        has_remote, remote_name = self.app.branch_service.has_remote_tracking(branch_to_delete)
        
        # Show confirmation dialog with options
        from ..ui.dialogs import DeleteBranchDialog
        
        def on_dialog_result(action: str | None) -> None:
            """Handle dialog result.
            
            Args:
                action: One of "local", "remote", "both", or None (cancelled).
            """
            if not action:
                # User cancelled
                return
            
            success_messages = []
            errors = []
            
            # Delete local branch
            if action in ("local", "both"):
                result = self.app.branch_service.delete_branch(
                    branch_to_delete,
                    force=False,
                    git_service=self.app.git
                )
                
                if not result["success"]:
                    # If deletion failed, check if it's because branch is not merged
                    # Try force delete as fallback
                    error_msg = result.get("error", "Unknown error")
                    if "not fully merged" in error_msg.lower() or "not merged" in error_msg.lower():
                        # Branch is not merged - try force delete
                        result = self.app.branch_service.delete_branch(
                            branch_to_delete,
                            force=True,
                            git_service=self.app.git
                        )
                
                if result["success"]:
                    success_messages.append("local")
                else:
                    errors.append(f"local: {result.get('error', 'Unknown error')}")
            
            # Delete remote branch
            if action in ("remote", "both") and has_remote:
                # Extract remote name (e.g., "origin/branch" -> "origin")
                remote = "origin"  # Default to origin
                if remote_name and "/" in remote_name:
                    remote = remote_name.split("/")[0]
                
                result = self.app.branch_service.delete_remote_branch(
                    branch_to_delete,
                    remote=remote,
                    git_service=self.app.git
                )
                
                if result["success"]:
                    success_messages.append("remote")
                else:
                    errors.append(f"remote: {result.get('error', 'Unknown error')}")
            
            # Show results
            if errors:
                # Some operations failed
                error_msg = "; ".join(errors)
                self.app.notify(
                    f"Failed to delete branch '{branch_to_delete}': {error_msg}",
                    severity="error",
                    timeout=5.0
                )
            elif success_messages:
                # All operations succeeded
                if action == "both":
                    message = f"Deleted branch '{branch_to_delete}' (local and remote)"
                elif action == "local":
                    message = f"Deleted local branch '{branch_to_delete}'"
                else:
                    message = f"Deleted remote branch '{branch_to_delete}'"
                
                self.app.notify(
                    message,
                    severity="success",
                    timeout=2.0
                )
                
                # Refresh UI to remove the deleted branch
                self.app.refresh_data_fast()
                
                # Update selection - move to previous branch or first branch
                def update_selection():
                    """Update selection after branch deletion."""
                    try:
                        # If we deleted the selected branch, select the previous one or first
                        if selected_index < len(self.app.branches):
                            # Select the same index (which will be the next branch now)
                            new_index = min(selected_index, len(self.app.branches) - 1)
                        else:
                            # If we deleted the last branch, select the new last one
                            new_index = max(0, len(self.app.branches) - 1)
                        
                        if len(self.app.branches) > 0:
                            self.app.branches_pane.index = new_index
                            self.app.branches_pane.highlighted = new_index
                    except (AttributeError, IndexError):
                        pass
                
                # Schedule selection update after refresh completes
                self.app.set_timer(0.1, update_selection)
        
        # Show the dialog
        dialog = DeleteBranchDialog(
            branch_name=branch_to_delete,
            has_remote=has_remote,
            remote_name=remote_name
        )
        self.app.push_screen(dialog, callback=on_dialog_result)
    
    def rename(self) -> None:
        """Rename a branch.
        
        This action is triggered when 'r' is pressed while the branches pane has focus.
        Shows a dialog to get the new branch name, then renames the branch.
        """
        # Get the selected branch index
        selected_index = self.app.branches_pane.index
        if selected_index is None or selected_index < 0 or selected_index >= len(self.app.branches):
            self.app.notify("No branch selected", severity="warning")
            return
        
        # Get the branch name
        branch_to_rename = self.app.branches[selected_index].name
        
        # Show dialog to get new branch name
        from ..ui.dialogs import RenameBranchDialog
        
        def on_dialog_result(new_name: str | None) -> None:
            """Handle dialog result."""
            if not new_name:
                # User cancelled
                return
            
            # Validate branch name (basic check)
            new_name = new_name.strip()
            if not new_name:
                self.app.notify("Branch name cannot be empty", severity="warning")
                return
            
            # Check if new name is same as current name
            if new_name == branch_to_rename:
                self.app.notify(
                    f"New branch name is the same as current name",
                    severity="info",
                    timeout=2.0
                )
                return
            
            # Perform branch rename using branch service
            result = self.app.branch_service.rename_branch(
                branch_to_rename,
                new_name,
                git_service=self.app.git
            )
            
            if result["success"]:
                renamed_branch = result.get("branch", new_name)
                
                # If we renamed the current branch, update active_branch
                current_branch = self.app._get_current_branch_name()
                if branch_to_rename == current_branch:
                    self.app.active_branch = renamed_branch
                
                # Show success notification
                self.app.notify(
                    f"Renamed branch '{branch_to_rename}' to '{renamed_branch}'",
                    severity="success",
                    timeout=2.0
                )
                
                # Refresh UI to show the renamed branch
                self.app.refresh_data_fast()
                
                # Update branches pane selection to highlight the renamed branch
                def update_selection():
                    """Update selection to the renamed branch."""
                    try:
                        # Find the renamed branch in the list and select it
                        for i, branch in enumerate(self.app.branches):
                            if branch.name == renamed_branch:
                                self.app.branches_pane.index = i
                                self.app.branches_pane.highlighted = i
                                break
                    except (AttributeError, IndexError):
                        pass
                
                # Schedule selection update after a short delay to ensure refresh completes
                self.app.set_timer(0.1, update_selection)
            else:
                # Show error notification
                error_msg = result.get("error", "Unknown error")
                self.app.notify(
                    f"Failed to rename branch '{branch_to_rename}': {error_msg}",
                    severity="error",
                    timeout=5.0
                )
        
        # Show the dialog with current branch name as initial value
        dialog = RenameBranchDialog(current_name=branch_to_rename)
        self.app.push_screen(dialog, callback=on_dialog_result)

