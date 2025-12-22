"""Sync action handlers for push and pull operations.

This module contains the action handlers that respond to user keybindings for
synchronizing branches with remote repositories. It coordinates between the
UI layer and the sync service to provide a complete user experience.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import PygitzenApp


class SyncActionHandler:
    """Handler for sync operations (push/pull)."""

    def __init__(self, app: "PygitzenApp") -> None:
        """Initialize sync action handler.
        
        Args:
            app: The PygitzenApp instance (for accessing UI and services).
        """
        self.app = app

    def push(self) -> None:
        """Push the current branch to its remote repository.
        
        This method handles pushing when the user presses 'P'. It checks if the
        branch has upstream configured. If it does, it pushes directly. If not,
        it prompts the user to set an upstream first, then pushes with the
        --set-upstream flag to configure tracking.
        """
        current_branch = self.app._get_current_branch_name()
        if not current_branch:
            self.app.notify("No branch checked out", severity="warning")
            return

        # Check if this branch has upstream tracking configured
        has_upstream, remote, remote_branch = self.app.sync_service.get_upstream(current_branch)
        
        if has_upstream and remote and remote_branch:
            # Branch has upstream, so we can push directly
            result = self.app.sync_service.push(
                branch_name=current_branch,
                remote=remote,
                remote_branch=remote_branch,
                force=False,
                force_with_lease=False,
                set_upstream=False,
            )
            
            if result["success"]:
                message = f"Pushed {current_branch} to {remote}/{remote_branch}"
                self.app.notify(message, severity="success", timeout=2.0)
                self.app.command_log_pane.update_log(message)
                self.app.refresh_data_fast()
            else:
                error_msg = result.get("error", "Unknown error")
                # Check if the push was rejected, which might indicate the need
                # for a force push if the remote has diverged
                if "Updates were rejected" in error_msg or "non-fast-forward" in error_msg:
                    message = f"Push rejected: {error_msg}"
                    self.app.notify(message, severity="error", timeout=5.0)
                    self.app.command_log_pane.update_log(message)
                else:
                    message = f"Failed to push: {error_msg}"
                    self.app.notify(message, severity="error", timeout=5.0)
                    self.app.command_log_pane.update_log(message)
        else:
            # No upstream configured, so we need to ask the user to set one
            # We pre-fill the dialog with a sensible default
            suggested_remote = self.app.sync_service.get_suggested_remote()
            initial_value = f"{suggested_remote} {current_branch}"
            
            from ..ui.dialogs import SetUpstreamDialog
            
            def on_dialog_result(upstream: str | None) -> None:
                """Handle the result from the upstream configuration dialog.
                
                If the user provides an upstream, we parse it and push with
                --set-upstream to configure tracking for future operations.
                """
                if not upstream:
                    return
                
                try:
                    # Parse the upstream string which should be in format "remote branch-name"
                    remote, remote_branch = self.app.sync_service.parse_upstream(upstream)
                    
                    # Push and set upstream in one operation
                    result = self.app.sync_service.push(
                        branch_name=current_branch,
                        remote=remote,
                        remote_branch=remote_branch,
                        force=False,
                        force_with_lease=False,
                        set_upstream=True,
                    )
                    
                    if result["success"]:
                        message = f"Pushed {current_branch} to {remote}/{remote_branch} (set upstream)"
                        self.app.notify(message, severity="success", timeout=2.0)
                        self.app.command_log_pane.update_log(message)
                        self.app.refresh_data_fast()
                    else:
                        error_msg = result.get("error", "Unknown error")
                        message = f"Failed to push: {error_msg}"
                        self.app.notify(message, severity="error", timeout=5.0)
                        self.app.command_log_pane.update_log(message)
                except ValueError as e:
                    # The upstream string format was invalid
                    message = f"Invalid upstream format: {str(e)}"
                    self.app.notify(message, severity="error", timeout=3.0)
                    self.app.command_log_pane.update_log(message)
            
            dialog = SetUpstreamDialog(current_branch, initial_value=initial_value)
            self.app.push_screen(dialog, callback=on_dialog_result)

    def pull(self) -> None:
        """Pull changes from the remote repository into the current branch.
        
        This method handles pulling changes when the user presses 'p'. It first
        checks if the current branch has an upstream configured. If it does, we
        let git use that configuration directly. If not, we prompt the user to
        set an upstream branch first, then proceed with the pull.
        
        When upstream is configured, we don't pass explicit remote/branch arguments
        to git pull. This ensures git uses its own upstream configuration and
        produces the standard git error messages if something goes wrong, which
        matches the behavior users expect from the command line.
        """
        current_branch = self.app._get_current_branch_name()
        if not current_branch:
            self.app.notify("No branch checked out", severity="warning")
            return

        # Determine if this branch has upstream tracking configured
        has_upstream, remote, remote_branch = self.app.sync_service.get_upstream(current_branch)
        
        if has_upstream and remote and remote_branch:
            # Branch has upstream configured, so we can pull directly
            # By not passing remote/branch explicitly, git will use the upstream
            # config and provide standard error messages if the remote branch
            # doesn't exist or other issues occur
            result = self.app.sync_service.pull(
                branch_name=current_branch,
                remote=None,
                remote_branch=None,
                fast_forward_only=False,
            )
            
            if result["success"]:
                message = f"Pulled {remote}/{remote_branch} into {current_branch}"
                self.app.notify(message, severity="success", timeout=2.0)
                self.app.command_log_pane.update_log(message)
                self.app.refresh_data_fast()
            else:
                # When pull fails, display the exact error message from git
                # without adding any custom prefixes. This matches what users
                # would see if they ran git pull on the command line
                error_msg = result.get("error", "Unknown error")
                self.app.notify(error_msg, severity="error", timeout=8.0)
                self.app.command_log_pane.update_log(error_msg)
        else:
            # No upstream configured, so we need to ask the user to set one
            # We pre-fill the dialog with a sensible default based on available remotes
            suggested_remote = self.app.sync_service.get_suggested_remote()
            initial_value = f"{suggested_remote} {current_branch}"
            
            from ..ui.dialogs import SetUpstreamDialog
            
            def on_dialog_result(upstream: str | None) -> None:
                """Handle the result from the upstream configuration dialog.
                
                If the user provides an upstream, we first set it in git config,
                then attempt to pull. If setting upstream fails because the remote
                branch doesn't exist, we provide helpful guidance about fetching
                or pushing instead.
                """
                if not upstream:
                    return
                
                try:
                    # Parse the upstream string which should be in format "remote branch-name"
                    remote, remote_branch = self.app.sync_service.parse_upstream(upstream)
                    
                    # Configure the upstream tracking in git before attempting to pull
                    upstream_ref = f"{remote}/{remote_branch}"
                    set_result = self.app.branch_service.set_upstream(
                        branch=current_branch,
                        upstream=upstream_ref,
                        git_service=self.app.git
                    )
                    
                    if not set_result["success"]:
                        error_msg = set_result.get("error", "Unknown error")
                        # Check if the error indicates the remote branch doesn't exist
                        if "does not exist" in error_msg or "not found" in error_msg.lower():
                            # Extract a clean error message and provide actionable guidance
                            import re
                            match = re.search(r"upstream branch ['\"]([^'\"]+)['\"]", error_msg)
                            if match:
                                upstream_branch = match.group(1)
                                clean_error = f"upstream branch {upstream_branch} not found."
                            else:
                                clean_error = error_msg.replace("fatal: ", "").strip()
                            
                            # Provide clear next steps for the user
                            full_message = f"{clean_error}\nIf you expect it to exist, you should fetch (with 'f').\nOtherwise, you should push (with 'P')."
                            self.app.notify(full_message, severity="error", timeout=8.0)
                            self.app.command_log_pane.update_log(full_message)
                        else:
                            # For other errors, just show the git error message
                            self.app.notify(error_msg, severity="error", timeout=5.0)
                            self.app.command_log_pane.update_log(f"Failed to set upstream: {error_msg}")
                        return
                    
                    # Upstream is now configured, proceed with the pull
                    result = self.app.sync_service.pull(
                        branch_name=current_branch,
                        remote=remote,
                        remote_branch=remote_branch,
                        fast_forward_only=False,
                    )
                    
                    if result["success"]:
                        message = f"Pulled {remote}/{remote_branch} into {current_branch}"
                        self.app.notify(message, severity="success", timeout=2.0)
                        self.app.command_log_pane.update_log(message)
                        self.app.refresh_data_fast()
                    else:
                        error_msg = result.get("error", "Unknown error")
                        message = f"Failed to pull: {error_msg}"
                        self.app.notify(message, severity="error", timeout=5.0)
                        self.app.command_log_pane.update_log(message)
                except ValueError as e:
                    # The upstream string format was invalid
                    message = f"Invalid upstream format: {str(e)}"
                    self.app.notify(message, severity="error", timeout=3.0)
                    self.app.command_log_pane.update_log(message)
            
            dialog = SetUpstreamDialog(current_branch, initial_value=initial_value)
            self.app.push_screen(dialog, callback=on_dialog_result)

    def fetch(self) -> None:
        """Fetch changes from all configured remotes.
        
        This method fetches the latest changes from remote repositories when
        the user presses 'f'. After a successful fetch, it refreshes the UI
        to show updated branch information, commits, and tags.
        """
        result = self.app.sync_service.fetch(fetch_all=True)
        
        if result["success"]:
            message = "Fetched from remote"
            self.app.notify(message, severity="success", timeout=2.0)
            self.app.command_log_pane.update_log(message)
            # Refresh branches, commits, remotes, and tags
            self.app.refresh_data_fast()
        else:
            error_msg = result.get("error", "Unknown error")
            # Check for authentication errors
            if "exit status 128" in error_msg or "authentication" in error_msg.lower():
                message = f"Fetch failed: Authentication error. Check your credentials."
                self.app.notify(message, severity="error", timeout=5.0)
                self.app.command_log_pane.update_log(message)
            else:
                message = f"Failed to fetch: {error_msg}"
                self.app.notify(message, severity="error", timeout=5.0)
                self.app.command_log_pane.update_log(message)

