"""Dialog widgets for pygitzen.

Contains modal dialogs for user input (create branch, rename, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.screen import ModalScreen
from textual.widgets import Input, Label, Button, Static, Link, ListView, ListItem, TextArea
from textual.containers import Container, Horizontal, Vertical
from textual.binding import Binding
from rich.text import Text


class MinimalDialog(ModalScreen[str]):
    """Minimal floating dialog with input field and Enter key submission."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    MinimalDialog {
        align: center middle;
    }

    #dialog {
        width: 60%;
        min-width: 40;
        max-width: 80;
        height: 13;
        background: $surface;
        border: solid $primary;
        layout: vertical;
        padding: 1;
    }

    #title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #input {
        width: 100%;
    }
    """

    def __init__(self, title: str, placeholder: str = "", initial_value: str = "") -> None:
        """Initialize minimal dialog.
        
        Args:
            title: Dialog title.
            placeholder: Placeholder text for input.
            initial_value: Initial input value.
        """
        super().__init__()
        self.title = title
        self.placeholder = placeholder
        self.initial_value = initial_value

    def compose(self):
        """Compose dialog widgets."""
        with Container(id="dialog"):
            yield Label(self.title, id="title")
            yield Input(
                value=self.initial_value,
                placeholder=self.placeholder,
                id="input"
            )

    def on_mount(self) -> None:
        """Focus input when dialog is mounted."""
        self.query_one("#input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key press in input field."""
        value = event.value.strip()
        if value:
            self.dismiss(value)
        else:
            self.dismiss(None)
    
    def action_dismiss(self) -> None:
        """Dismiss the dialog when Escape is pressed."""
        self.dismiss(None)


class NewBranchDialog(MinimalDialog):
    """Dialog for creating a new branch."""

    def __init__(self, base_branch: str | None = None) -> None:
        """Initialize new branch dialog.
        
        Args:
            base_branch: Optional base branch to create from.
        """
        title = f"Create new branch{' from ' + base_branch if base_branch else ''}"
        placeholder = "Branch name"
        super().__init__(title=title, placeholder=placeholder)


class RenameBranchDialog(MinimalDialog):
    """Dialog for renaming a branch."""

    def __init__(self, current_name: str) -> None:
        """Initialize rename branch dialog.
        
        Args:
            current_name: Current branch name.
        """
        title = f"Rename branch: {current_name}"
        placeholder = "New branch name"
        super().__init__(title=title, placeholder=placeholder, initial_value=current_name)


class StashRenameDialog(ModalScreen[str | None]):
    """Dialog for renaming a stash entry with full stash details."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("ctrl+j", "submit", "Rename", show=False),
        Binding("ctrl+enter", "submit", "Rename", show=False),
    ]

    DEFAULT_CSS = """
    StashRenameDialog {
        align: center middle;
    }

    StashRenameDialog #stash-rename-dialog {
        width: 75%;
        min-width: 60;
        max-width: 85;
        height: auto;
        min-height: 12;
        background: $surface;
        border: thick $primary;
        layout: vertical;
        padding: 2;
    }

    StashRenameDialog #stash-rename-header {
        width: 100%;
        height: auto;
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 2;
        padding-bottom: 1;
        border-bottom: solid $primary;
    }

    StashRenameDialog #stash-rename-subtitle {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashRenameDialog #stash-rename-input-section {
        width: 100%;
        height: auto;
        layout: vertical;
        margin-bottom: 2;
    }

    StashRenameDialog #stash-rename-input-label {
        color: $text;
        text-style: bold;
        margin-bottom: 1;
    }

    StashRenameDialog #stash-rename-input {
        width: 100%;
        height: 3;
        border: solid $primary;
        background: $surface;
        margin-bottom: 1;
    }

    StashRenameDialog #stash-rename-input:focus {
        border: solid $accent;
    }

    StashRenameDialog #stash-rename-info {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashRenameDialog #stash-rename-button-container {
        width: 100%;
        layout: horizontal;
        align: center middle;
        height: auto;
    }

    StashRenameDialog #cancel-button {
        margin-right: 1;
    }

    StashRenameDialog #rename-button {
        min-width: 15;
    }
    """

    def __init__(self, stash_details: str, current_message: str) -> None:
        """Initialize stash rename dialog.
        
        Args:
            stash_details: Full stash details (e.g., "stash@{0}: On branch: message").
            current_message: Current stash message only (for fallback).
        """
        super().__init__()
        self.stash_details = stash_details
        self.current_message = current_message
        # Use full stash details as initial value (everything after "stash@{X}: ")
        # Extract the part after "stash@{index}: " for editing
        import re
        match = re.match(r'stash@\{\d+\}:\s*(.+)', stash_details)
        if match:
            self.initial_editable_text = match.group(1).strip()
        else:
            # Fallback to just message if parsing fails
            self.initial_editable_text = current_message

    def compose(self):
        """Compose dialog widgets."""
        with Container(id="stash-rename-dialog"):
            yield Static("Rename stash", id="stash-rename-header")
            yield Static(self.stash_details, id="stash-rename-subtitle")
            
            with Container(id="stash-rename-input-section"):
                yield Label("Edit stash details:", id="stash-rename-input-label")
                yield Input(
                    value=self.initial_editable_text,
                    placeholder="Edit stash details (e.g., 'On branch: message' or just 'message')",
                    id="stash-rename-input"
                )
            
            yield Static("Press Ctrl+Enter to rename, or Escape to cancel", id="stash-rename-info")
            
            with Container(id="stash-rename-button-container"):
                yield Button("Cancel", id="cancel-button", variant="default")
                yield Button("Rename ✓", id="rename-button", variant="primary")

    def on_mount(self) -> None:
        """Focus input when dialog is mounted."""
        input_widget = self.query_one("#stash-rename-input", Input)
        input_widget.focus()
        # Select all text so user can easily replace it
        input_widget.action_select_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key press in input field."""
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "rename-button":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        """Submit rename when Ctrl+J or Ctrl+Enter is pressed or Rename button is clicked."""
        input_widget = self.query_one("#stash-rename-input", Input)
        value = input_widget.value.strip()
        if not value:
            # Don't allow empty message
            return
        self.dismiss(value)
    
    def action_dismiss(self) -> None:
        """Dismiss the dialog when Escape is pressed."""
        self.dismiss(None)


class DeleteBranchDialog(ModalScreen[str | None]):
    """Confirmation dialog for deleting a branch with multiple options (like lazygit)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        # Binding("c", "delete_local", "Delete Local", show=False),
        # Binding("r", "delete_remote", "Delete Remote", show=False),
        # Binding("b", "delete_both", "Delete Both", show=False),
        # Binding("enter", "confirm_selection", "Confirm", show=False),
    ]

    DEFAULT_CSS = """
    DeleteBranchDialog {
        align: center middle;
    }

    /*width: 60;
        min-width: 50;
        max-width: 70;
        height: auto;
        min-height: 30;
        background: $surface;
        border: solid $error;
        layout: vertical;
        padding: 1;*/

    DeleteBranchDialog #dialog {
        width: 60;
        min-width: 50;
        max-width: 70;
        height: auto;
        /*min-height: 12;*/
        background: $surface;
        border: solid $error;
        layout: vertical;
        padding: 1;
    }

    DeleteBranchDialog #title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    DeleteBranchDialog #options-list {
        width: 100%;
        height: auto;
        min-height: 5;
        border: none;
        background: $surface;
    }

    #delete-branch-dialog ListItem {
        padding: 0 1;
        height: 1;
    }

    #delete-branch-dialog #options-list ListItem.--highlight {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }

    #delete-branch-dialog #options-list ListItem.--highlight:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
    }

    #delete-branch-dialog #options-list ListItem.highlighted-option {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
    }

    #delete-branch-dialog #options-list ListItem.highlighted-option:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
    }

    #delete-branch-dialog .option-disabled {
        color: $text-muted;
        opacity: 0.5;
    }

    #delete-branch-dialog ListItem.option-disabled.--highlight {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
        border: dashed $error;
    }

    #delete-branch-dialog ListItem.option-disabled.--highlight:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
        border: dashed $error;
    }

    #delete-branch-dialog #options-list ListItem.option-disabled.highlighted-option {
        background: #357ABD;
        color: #ffffff;
        text-style: bold;
        border: dashed $error;
    }

    #delete-branch-dialog #options-list ListItem.option-disabled.highlighted-option:focus {
        background: #2f6aa3;
        color: #ffffff;
        text-style: bold;
        border: dashed $error;
    }

    #delete-branch-dialog #options-list ListItem.--highlight > Static {
        background: transparent;
        color: #ffffff;
    }

    #delete-branch-dialog #options-list ListItem.highlighted-option > Static {
        background: transparent;
        color: #ffffff;
    }

    #delete-branch-dialog #disabled-message {
        width: 100%;
        margin-top: 1;
        padding: 1;
        border: solid $warning;
        background: $surface-lighten-1;
        color: $error;
        text-align: center;
        display: none;
    }

    #delete-branch-dialog #disabled-message.visible {
        display: block;
    }
    """

    def __init__(self, branch_name: str, has_remote: bool = False, remote_name: str | None = None) -> None:
        """Initialize delete branch dialog.
        
        Args:
            branch_name: Name of branch to delete.
            has_remote: Whether the branch has a remote tracking branch.
            remote_name: Remote branch name (e.g., "origin/branch-name") if exists.
        """
        super().__init__(id="delete-branch-dialog")
        self.branch_name = branch_name
        self.has_remote = has_remote
        self.remote_name = remote_name
        self.selected_option: str | None = None
        self._last_highlighted: int | None = None

    def compose(self):
        """Compose dialog widgets."""
        from rich.text import Text as RichText
        
        with Container(id="dialog"):
            yield Label(f"Delete branch '{self.branch_name}'?", id="title")
            
            # Options list - yield ListView with ListItems directly
            with ListView(id="options-list") as options_list:
                options_list.can_focus = True
                
                # Option 1: Delete local branch (always available)
                local_text = RichText()
                # local_text.append("c ", style="cyan bold")
                local_text.append("Delete local branch", style="white")
                yield ListItem(Static(local_text), id="local")
                
                # Option 2: Delete remote branch (only if has remote)
                remote_text = RichText()
                if self.has_remote:
                    # remote_text.append("r ", style="cyan bold")
                    remote_text.append("Delete remote branch", style="white")
                    yield ListItem(Static(remote_text), id="remote")
                else:
                    # remote_text.append("r ", style="dim")
                    remote_text.append("[Disabled] ", style="red bold")
                    remote_text.append("Delete remote branch", style="red dim")
                    remote_item = ListItem(Static(remote_text), id="remote")
                    remote_item.add_class("option-disabled")
                    yield remote_item
                
                # Option 3: Delete both (only if has remote)
                both_text = RichText()
                if self.has_remote:
                    # both_text.append("b ", style="cyan bold")
                    both_text.append("Delete local and remote branch", style="white")
                    yield ListItem(Static(both_text), id="both")
                else:
                    # both_text.append("b ", style="dim")
                    both_text.append("[Disabled] ", style="red bold")
                    both_text.append("Delete local and remote branch", style="red dim")
                    both_item = ListItem(Static(both_text), id="both")
                    both_item.add_class("option-disabled")
                    yield both_item
                
                # Option 4: Cancel
                cancel_text = RichText()
                cancel_text.append("Cancel", style="white")
                yield ListItem(Static(cancel_text), id="cancel")
            
            # Show disabled message only if no remote (will be shown/hidden reactively)
            # Always create it so we can show it when a disabled option is highlighted
            # yield Static(
            #     "Disabled: The selected branch has no upstream (or the upstream is not stored locally)",
            #     id="disabled-message"
            # )

            # the above static is not working, so we need to use the below static as the above Static will be creaeted all the time
            # and the below static will be created only when the has_remote is False
            if not self.has_remote:
                yield Static(
                    "Disabled: The selected branch has no upstream (or the upstream is not stored locally)",
                    id="disabled-message"
                )

    def on_mount(self) -> None:
        """Focus the options list when dialog is mounted."""
        options_list = self.query_one("#options-list", ListView)
        options_list.focus()
        options_list.index = 0
        options_list.highlighted = 0
        # Watch for both index and highlighted changes
        self.watch(options_list, "index", self._on_index_changed)
        self.watch(options_list, "highlighted", self._on_highlight_changed)
        # Initial highlight - use a timer to ensure ListView is fully mounted
        self.set_timer(0.05, lambda: self._update_highlighting(0))
    
    def _update_highlighting(self, index: int | None) -> None:
        """Update visual highlighting by adding/removing classes."""
        try:
            options_list = self.query_one("#options-list", ListView)
        except:
            return
        
        # Remove highlight from previous item
        if self._last_highlighted is not None and self._last_highlighted < len(options_list.children):
            try:
                item = options_list.children[self._last_highlighted]
                if isinstance(item, ListItem):
                    item.remove_class("highlighted-option")
            except:
                pass
        
        # Add highlight to current item
        if index is not None and index < len(options_list.children):
            try:
                item = options_list.children[index]
                if isinstance(item, ListItem):
                    item.add_class("highlighted-option")
                    self._last_highlighted = index
            except:
                pass
    
    def _on_index_changed(self, index: int | None) -> None:
        """Handle index changes (mouse clicks, etc.)."""
        self._update_highlighting(index)
    
    def _on_highlight_changed(self, highlighted: int | None) -> None:
        """Show/hide disabled message based on highlighted option and update highlighting."""
        # Update visual highlighting
        self._update_highlighting(highlighted)
        
        # Check if disabled message exists (it only exists if has_remote is False)
        try:
            disabled_message = self.query_one("#disabled-message", Static)
        except Exception:
            # Widget doesn't exist, which is fine
            return
        
        if highlighted is None:
            disabled_message.remove_class("visible")
            return
        
        options_list = self.query_one("#options-list", ListView)
        try:
            if highlighted < len(options_list.children):
                item = options_list.children[highlighted]
                if isinstance(item, ListItem) and "option-disabled" in item.classes:
                    # Highlighted item is disabled - show message
                    disabled_message.add_class("visible")
                else:
                    # Highlighted item is not disabled - hide message
                    disabled_message.remove_class("visible")
        except (IndexError, AttributeError):
            disabled_message.remove_class("visible")

    def on_list_view_selected(self, event) -> None:
        """Handle selection in the options list when Enter is pressed."""
        if event.list_view.id == "options-list":
            selected_item = event.item
            if selected_item:
                # Check if the selected item is disabled
                if "option-disabled" in selected_item.classes:
                    self.app.notify(
                        "This option is disabled: The selected branch has no upstream (or the upstream is not stored locally)",
                        severity="warning",
                        timeout=3.0
                    )
                    return
                
                item_id = selected_item.id
                if item_id == "local":
                    self.action_delete_local()
                elif item_id == "remote" and self.has_remote:
                    self.action_delete_remote()
                elif item_id == "both" and self.has_remote:
                    self.action_delete_both()
                elif item_id == "cancel":
                    self.action_cancel()
    
    def action_confirm_selection(self) -> None:
        """Confirm the currently selected option when Enter is pressed."""
        options_list = self.query_one("#options-list", ListView)
        current_index = options_list.index
        if current_index is not None:
            # Get the item at current index
            try:
                item = options_list.children[current_index]
                if isinstance(item, ListItem):
                    # Check if the selected item is disabled
                    if "option-disabled" in item.classes:
                        self.app.notify(
                            "This option is disabled: The selected branch has no upstream (or the upstream is not stored locally)",
                            severity="warning",
                            timeout=3.0
                        )
                        return
                    
                    item_id = item.id
                    if item_id == "local":
                        self.action_delete_local()
                    elif item_id == "remote" and self.has_remote:
                        self.action_delete_remote()
                    elif item_id == "both" and self.has_remote:
                        self.action_delete_both()
                    elif item_id == "cancel":
                        self.action_cancel()
            except (IndexError, AttributeError):
                pass
    
    def action_delete_local(self) -> None:
        """Delete local branch only."""
        self.dismiss("local")
    
    def action_delete_remote(self) -> None:
        """Delete remote branch only."""
        if not self.has_remote:
            # Option is disabled, don't do anything
            return
        self.dismiss("remote")
    
    def action_delete_both(self) -> None:
        """Delete both local and remote branch."""
        if not self.has_remote:
            # Option is disabled, don't do anything
            return
        self.dismiss("both")
    
    def action_cancel(self) -> None:
        """Cancel deletion when Escape is pressed."""
        self.dismiss(None)


class SetUpstreamDialog(MinimalDialog):
    """Dialog for setting upstream branch.
    
    Format: "remote branch-name" (e.g., "origin main")
    Matches Lazygit's upstream prompt format.
    """

    def __init__(self, branch_name: str, initial_value: str = "") -> None:
        """Initialize set upstream dialog.
        
        Args:
            branch_name: Local branch name.
            initial_value: Initial upstream value (e.g., "origin branch-name").
        """
        title = "Enter upstream"
        placeholder = "Upstream (e.g., origin main)"
        super().__init__(title=title, placeholder=placeholder, initial_value=initial_value)


class CommitDialog(ModalScreen[tuple[str, str] | None]):
    """Dialog for creating a commit with summary and description."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
        Binding("ctrl+j", "submit", "Commit", show=False),
        Binding("ctrl+enter", "submit", "Commit", show=False),
    ]

    DEFAULT_CSS = """
    CommitDialog {
        align: center middle;
    }
    
    CommitDialog #new-commit-dialog {
        width: 75%;
        min-width: 60;
        max-width: 85;
        height: auto;
        min-height: 20;
        background: $surface;
        border: thick $primary;
        layout: vertical;
        padding: 2;
    }
    
    CommitDialog #new-commit-header {
        width: 100%;
        height: auto;
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 2;
        padding-bottom: 1;
        border-bottom: solid $primary;
    }
    
    CommitDialog #new-commit-subtitle {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }
    
    CommitDialog #summary-section {
        width: 100%;
        height: auto;
        layout: vertical;
        margin-bottom: 2;
    }
    
    CommitDialog #summary-label-container {
        width: 100%;
        height: auto;
        layout: horizontal;
        margin-bottom: 1;
    }
    
    CommitDialog #summary-label {
        color: $text;
        text-style: bold;
    }
    
    CommitDialog #summary-required {
        color: $error;
        margin-left: 1;
    }
    
    CommitDialog #summary-input {
        width: 100%;
        height: 3;
        border: solid $primary;
        background: $surface;
    }
    
    CommitDialog #summary-input:focus {
        border: solid $accent;
    }
    
    CommitDialog #description-section {
        width: 100%;
        height: auto;
        layout: vertical;
        margin-bottom: 2;
    }
    
    CommitDialog #description-label-container {
        width: 100%;
        height: auto;
        layout: horizontal;
        margin-bottom: 1;
    }
    
    CommitDialog #description-label {
        color: $text;
        text-style: bold;
    }
    
    CommitDialog #description-optional {
        color: $text-muted;
        margin-left: 1;
    }
    
    CommitDialog #description-input {
        width: 100%;
        height: 10;
        border: solid $primary;
        background: $surface;
    }
    
    CommitDialog #description-input:focus {
        border: solid $accent;
    }
    
    CommitDialog #info-bar {
        width: 100%;
        height: auto;
        padding: 1;
        margin-bottom: 2;
        border: solid $primary 30%;
        background: $surface-lighten-1;
        text-align: center;
        color: $text-muted;
    }
    
    CommitDialog #button-container {
        width: 100%;
        layout: horizontal;
        align: right middle;
        height: auto;
    }
    
    CommitDialog #cancel-button {
        margin-right: 1;
    }
    
    CommitDialog #commit-button {
        min-width: 15;
    }
    """

    def __init__(self, initial_summary: str = "", initial_description: str = "", staged_files_count: int = 0) -> None:
        """Initialize commit dialog.
        
        Args:
            initial_summary: Initial commit summary text.
            initial_description: Initial commit description text.
            staged_files_count: Number of staged files to display.
        """
        super().__init__()
        self.initial_summary = initial_summary
        self.initial_description = initial_description
        self.staged_files_count = staged_files_count

    def compose(self):
        """Compose dialog widgets."""
        from rich.text import Text
        
        with Container(id="new-commit-dialog"):
            # Header
            yield Label("Commit Changes", id="new-commit-header")
            yield Label("Create a new commit with staged files", id="new-commit-subtitle")
            
            # Summary section
            with Container(id="summary-section"):
                with Container(id="summary-label-container"):
                    yield Label("Summary", id="summary-label")
                    yield Label("*", id="summary-required")
                yield Input(
                    value=self.initial_summary,
                    placeholder="Enter commit message (e.g., Add feature: user authentication)",
                    id="summary-input"
                )
            
            # Description section
            with Container(id="description-section"):
                with Container(id="description-label-container"):
                    yield Label("Description", id="description-label")
                    yield Label("(optional)", id="description-optional")
                yield TextArea(
                    text=self.initial_description,
                    placeholder="Provide additional details about this commit...\n\nExample:\n- Implemented login/logout functionality\n- Added password validation\n- Updated user model",
                    id="description-input"
                )
            
            # Info bar
            info_text = Text()
            # info_text.append("ℹ️  ", style="cyan")
            info_text.append(f"{self.staged_files_count} file", style="white")
            if self.staged_files_count != 1:
                info_text.append("s", style="white")
            info_text.append(" staged", style="white")
            info_text.append(" • Press ", style="dim")
            info_text.append("Ctrl+Enter", style="cyan bold")
            info_text.append(" to commit", style="dim")
            
            yield Static(info_text, id="info-bar")
            
            # Buttons
            with Container(id="button-container"):
                yield Button("Cancel", id="cancel-button", variant="default")
                yield Button("Commit ✓", id="commit-button", variant="primary")

    def on_mount(self) -> None:
        """Focus summary input when dialog is mounted."""
        self.query_one("#summary-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "commit-button":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        """Submit commit when Ctrl+J or Ctrl+Enter is pressed or Commit button is clicked."""
        summary_input = self.query_one("#summary-input", Input)
        description_input = self.query_one("#description-input", TextArea)
        
        summary = summary_input.value.strip()
        description = description_input.text.strip()
        
        if not summary:
            # Show warning but don't dismiss
            self.app.notify(
                "Commit message cannot be empty",
                severity="warning",
                timeout=2.0
            )
            return
        
        self.dismiss((summary, description))
    
    def action_dismiss(self) -> None:
        """Dismiss the dialog when Escape is pressed."""
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    """Generic confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }

    ConfirmDialog #dialog {
        width: 50;
        height: 8;
        background: $surface;
        border: solid $primary;
        layout: vertical;
        padding: 1;
    }

    ConfirmDialog #message {
        text-align: center;
        color: $text;
        margin-bottom: 1;
    }

    ConfirmDialog #button-container {
        width: 100%;
        layout: horizontal;
        margin-top: 1;
    }
    """

    def __init__(self, message: str, confirm_text: str = "Confirm", cancel_text: str = "Cancel") -> None:
        """Initialize confirmation dialog.
        
        Args:
            message: Confirmation message.
            confirm_text: Text for confirm button.
            cancel_text: Text for cancel button.
        """
        super().__init__()
        self.message = message
        self.confirm_text = confirm_text
        self.cancel_text = cancel_text

    def compose(self):
        """Compose dialog widgets."""
        with Container(id="dialog"):
            yield Label(self.message, id="message")
            with Container(id="button-container"):
                yield Button(self.cancel_text, id="cancel", variant="default")
                yield Button(self.confirm_text, id="confirm", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)


class UnboundActionsModal(ModalScreen[None]):
    """Floating modal window for displaying unbound actions details."""

    DEFAULT_CSS = """
    UnboundActionsModal {
        align: center middle;
    }
    
    #dialog {
        width: 70%;
        min-width: 50;
        max-width: 90;
        height: auto;
        max-height: 80%;
        padding: 0;
        border: thick $accent 60%;
        background: #1e1e1e;
    }
    
    #unbound-content {
        width: 100%;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
    ]

    def __init__(self, unbound_actions: list[dict], config_path: Optional[Path] = None) -> None:
        """Initialize the modal with unbound actions data.

        Args:
            unbound_actions: List of dicts with keys:
                - 'action': str - Action name that is unbound
                - 'was_key': str - Key that was originally bound to this action
                - 'pane': str - Pane name
                - 'description': str - Human-readable description
            config_path: Optional path to config file for display
        """
        super().__init__()
        self._unbound_actions = unbound_actions
        self._config_path = config_path

    def compose(self):
        """Compose the modal dialog."""
        with Container(id="dialog"):
            content_widget = Static(self._build_content(), id="unbound-content")
            yield content_widget

    def _build_content(self) -> Text:
        """Build the content text for the modal."""
        text = Text()

        if not self._unbound_actions:
            text.append("No unbound actions found.\n\n", style="green bold")
            text.append("All keybindings are properly configured.", style="white")
            return text

        text.append("Unbound Actions Detected\n", style="yellow bold")
        text.append("=" * 60 + "\n", style="dim")
        text.append(
            f"\nFound {len(self._unbound_actions)} action(s) that lost their keybindings:\n\n",
            style="white"
        )

        for i, action_info in enumerate(self._unbound_actions, 1):
            action = action_info.get("action", "unknown")
            was_key = action_info.get("was_key", "?")
            description = action_info.get("description", action.replace("_", " ").title())
            pane = action_info.get("pane", "app")

            text.append(f"{i}. ", style="cyan")
            text.append(f"{description}", style="yellow")
            text.append(f" ({action})", style="dim")
            text.append("\n   ", style="white")
            text.append("Was bound to: ", style="dim")
            text.append(f"'{was_key}'", style="cyan")
            if pane != "app":
                text.append(f" (in {pane} pane)", style="dim")
            text.append("\n\n", style="white")

        text.append("To fix this, edit your keybindings config file:\n", style="white")
        if self._config_path:
            text.append(f"  ", style="white")
            text.append(str(self._config_path), style="cyan")
        else:
            text.append("  ~/.config/pygitzen/keybindings.toml", style="cyan")
            text.append(
                " (or %APPDATA%\\pygitzen\\keybindings.toml on Windows)", style="dim"
            )
        text.append("\n\n", style="white")
        text.append("Example configuration:\n", style="white")
        text.append("  [app]\n", style="dim")
        text.append(
            f"  \"{self._unbound_actions[0].get('was_key', 'x')}\" = \"{self._unbound_actions[0].get('action', 'example_action')}\"\n",
            style="cyan"
        )
        text.append("\n", style="white")
        text.append("Press ESC to close this window.", style="dim")

        return text

    def action_dismiss(self) -> None:
        """Dismiss the modal."""
        self.dismiss(None)


class AboutModal(ModalScreen[None]):
    """Floating modal window for displaying About information."""

    DEFAULT_CSS = """
    AboutModal {
        align: center middle;
    }
    
    #dialog {
        width: 90%;
        min-width: 80;
        max-width: 95;
        height: auto;
        max-height: 90%;
        padding: 0;
        border: thick $accent 60%;
        background: #1e1e1e;
    }
    
    #about-content {
        width: 100%;
        padding: 1 2;
        layout: vertical;
        text-align: center;
        content-align: center middle;
        overflow-x: auto;
    }
    
    #art-display {
        width: 100%;
        height: auto;
        padding: 1;
        text-align: center;
        content-align: center middle;
        align: center middle;
        margin-bottom: 1;
    }
    
    #about-header {
        width: 100%;
        height: auto;
        padding: 1;
        /*margin-top: 1;*/
        text-align: center;
        content-align: center middle;
        align: center middle;
    }
    #copyright-text{

        width: 100%;
        height: auto;
        padding: 1 0 0 0;
        margin-top: 1;
        text-align: center;
        content-align: center middle;
        align: center middle;
    }
    #app-name{
        width:100%;
        height: auto;
        padding: 1 0 0 0;
        text-align: center;
        content-align: center middle;
        align: center middle;
    }
    #urls-container {
        width: 100%;
        height: auto;
        padding: 1 2;
        margin-top: 1;
        align: center middle;
        content-align: center middle;
        text-align: center;
        layout: vertical;
    }

    .url-row {
        width: auto;
        height: auto;
        layout: horizontal;
        align: center middle;
        padding: 0 1;
    }

    .label {
        width: auto;
        color: white;
        text-align: right;
        margin-right: 1;
    }

    Link {
        text-align: left;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
    ]

    def compose(self):
        """Compose the modal dialog."""
 
        with Container(id="dialog"):
            with Container(id="about-content"):
                yield Static("", id="art-display")
                yield Static("", id="copyright-text")
                yield Static("", id="app-name")
                yield Static("", id="about-header")
                yield Container(id="urls-container")

    def on_mount(self) -> None:
        """Build and display the about content when the modal is mounted."""
        # Create styled About Us text with colors
        art = """
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠿⠿⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⣩⣶⣿⣿⣿⣦⡹⣿⣿⣿⣿⣿⡿⣫⣵⣶⣭⣝⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠏⣼⣿⣿⣿⣿⣿⣿⣷⡸⣿⣿⣿⠏⣼⣿⣿⣿⣿⣿⣷⡹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠏⠸⠿⢿⣿⣿⣿⣿⣿⣿⡇⢻⣿⡏⣼⣿⣿⣿⣿⣿⣿⣿⣷⠹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⣸⣿⣏⢳⣶⣶⣶⡶⢲⣲⣶⢸⡽⢠⣭⣭⢝⣛⣛⣛⣛⡫⣭⣅⢹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⣿⡟⢛⢤⣿⣿⣶⣂⡻⢿⣿⢠⡇⣼⣿⠿⢎⠿⠿⣿⡏⡼⣿⣿⡌⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢸⣿⠗⠂⠀⢐⣀⣐⠲⠒⢤⣿⡆⠀⣿⣧⠼⢉⣍⡃⠤⣄⠠⢼⣿⡇⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⣿⣿⠖⠢⢤⣿⣛⡻⠇⠲⣄⣹⠀⠀⣿⣇⠬⢀⡒⠒⠓⢀⠠⢨⣿⣿⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣇⣿⡧⠂⢬⣭⣭⣛⡻⢅⠢⢨⣏⠀⠀⣿⢅⡬⣰⡞⢛⣛⠣⠈⢄⣻⣿⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣛⠻⠿⠛⣩⠴⣶⡚⠫⢍⡒⢧⣤⣀⠈⡇⠀⠀⢻⠋⡀⢐⣚⣛⣛⣿⣆⡄⠙⠛⢾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣇⡀⠀⠀⠙⠛⠷⠀⠈⠑⠀⠉⠀⠀⠀⠀⣾⠀⠊⠑⣋⠩⠉⠁⢀⠀⢀⠀⠄⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⢿⣿⣿⣿⣿⠿⠿⠃⣡⠶⠿⠒⠶⢂⠀⠀⠐⡆⠀⠀⠀⠋⠈⠀⣀⣶⣾⣿⣿⣿⣿⡈⡎⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⠉⡛⢿⣿⠨⠎⠀⠀⠀⠀⠀⠀⠀⠈⠂⠀⠀⠀⠀⠀⢀⡜⢸⠿⠛⠉⠉⠉⠉⠁⠂⠐⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡷⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡀⠀⠀⠰⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⠀⠀⠠⠄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⣿⣿⡿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠛⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⣿⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⢠⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣾⣿⣷⡀⢶⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢻⣿⣿⣿⣿⣿⣿⣿⣿
⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⠀⢰⣿⣿⣿⣿⣷⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠿⣿⣿⣿⣿⣿⣿
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⡏⠛⠀⢸⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢿⣿⣿⣿⣿
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⣿⣿⣦⡀⣼⣿⣿⣿⣿⣿⣿⡆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿⣿
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀

        """

        art_centered = art.strip()
        art_display = self.query_one("#art-display", Static)
        art_display.update(art_centered)

        copyright_text = Text()
        copyright_text.append("Copyright 2025 ", style="white")
        copyright_text.append("Sunny Tamang", style="bold cyan")
        #copyright_text.append("\n\n")
#         copyright_text.append("""
#                   o               
#                o  |               
# o-o  o  o o--o   -o- o-o o-o o-o  
# |  | |  | |  | |  |   /  |-' |  | 
# O-o  o--O o--O |  o  o-o o-o o  o 
# |       |    |                    
# o    o--o o--o                    
#         """)
        app_name_text = Text()
        app_name_text.append("pygitzen", style="bold green underline")

        copyright_texts = self.query_one("#copyright-text", Static)
        copyright_texts.update(copyright_text)

        app_name = self.query_one("#app-name", Static)
        app_name.update(app_name_text)

        about_us_text = Text()
        # about_us_text.append("Copyright 2025 ", style="white")
        # about_us_text.append("Sunny Tamang", style="bold cyan")
        # about_us_text.append("\n\n")
#         about_us_text.append(
#             """
#                   o               
#                o  |               
# o-o  o  o o--o   -o- o-o o-o o-o  
# ||  | |  | |  | |  |   /  |-' |  | 
# O-o  o--O o--O |  o  o-o o-o o  o 
# ||       |    |                    
# o    o--o o--o                    
#
# """
#         )
        about_us_text.append("This is inspired by ", style="white")
        about_us_text.append("lazygit", style="bold")
        about_us_text.append(" but with ", style="white")
        about_us_text.append("Python", style="bold")
        about_us_text.append(" implementation.\n", style="white")
        about_us_text.append(
            "A modern, fast, and intuitive terminal UI for Git operations.",
            style="dim white"
        )

        # Update About Us header
        about_header = self.query_one("#about-header", Static)
        about_header.update(about_us_text)

        # Create clickable Link widgets with labels
        urls = [
            ("Raise an Issue: ", "https://github.com/SunnyTamang/pygitzen/issues"),
            ("Release Notes: ", "https://github.com/SunnyTamang/pygitzen/releases"),
            ("Become a sponsor: ", "https://github.com/sponsors/SunnyTamang"),
        ]

        url_container = self.query_one("#urls-container")
        url_container.remove_children()

        for label, url in urls:
            url_row = Horizontal(
                Static(f"{label}:", classes="label"),
                Link(url, url=url),
                classes="url-row",
            )
            url_container.mount(url_row)

    def action_dismiss(self) -> None:
        """Dismiss the modal."""
        self.dismiss(None)


class StashMessageDialog(ModalScreen[str | None]):
    """Dialog for entering stash message."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("ctrl+j", "submit", "Stash", show=False),
        Binding("ctrl+enter", "submit", "Stash", show=False),
    ]

    DEFAULT_CSS = """
    StashMessageDialog {
        align: center middle;
    }

    StashMessageDialog #stash-dialog {
        width: 75%;
        min-width: 60;
        max-width: 85;
        height: auto;
        min-height: 15;
        background: $surface;
        border: thick $primary;
        layout: vertical;
        padding: 2;
    }

    StashMessageDialog #stash-header {
        width: 100%;
        height: auto;
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 2;
        padding-bottom: 1;
        border-bottom: solid $primary;
    }

    StashMessageDialog #stash-subtitle {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashMessageDialog #stash-input-section {
        width: 100%;
        height: auto;
        layout: vertical;
        margin-bottom: 2;
    }

    StashMessageDialog #stash-input-label {
        color: $text;
        text-style: bold;
        margin-bottom: 1;
    }

    StashMessageDialog #stash-input {
        width: 100%;
        height: 3;
        border: solid $primary;
        background: $surface;
        margin-bottom: 1;
    }

    StashMessageDialog #stash-input:focus {
        border: solid $accent;
    }

    StashMessageDialog #stash-info {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashMessageDialog #stash-button-container {
        width: 100%;
        layout: horizontal;
        align: center middle;
        height: auto;
    }

    StashMessageDialog #cancel-button {
        margin-right: 1;
    }

    StashMessageDialog #stash-button {
        min-width: 15;
    }
    """

    def __init__(self, title: str = "Stash changes", placeholder: str = "Enter stash message (optional)") -> None:
        """Initialize stash message dialog.
        
        Args:
            title: Dialog title.
            placeholder: Placeholder text for input.
        """
        super().__init__()
        self.title_text = title
        self.placeholder_text = placeholder

    def compose(self):
        """Compose dialog widgets."""
        with Container(id="stash-dialog"):
            yield Static(self.title_text, id="stash-header")
            yield Static("Enter a message to describe your stashed changes", id="stash-subtitle")
            
            with Container(id="stash-input-section"):
                yield Label("Stash message:", id="stash-input-label")
                yield Input(
                    placeholder=self.placeholder_text,
                    id="stash-input"
                )
            
            yield Static("Press Ctrl+Enter to stash, or Escape to cancel", id="stash-info")
            
            with Container(id="stash-button-container"):
                yield Button("Cancel", id="cancel-button", variant="default")
                yield Button("Stash ✓", id="stash-button", variant="primary")

    def on_mount(self) -> None:
        """Focus input when dialog is mounted."""
        self.query_one("#stash-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key press in input field."""
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "stash-button":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        """Submit stash when Ctrl+J or Ctrl+Enter is pressed or Stash button is clicked."""
        input_widget = self.query_one("#stash-input", Input)
        value = input_widget.value.strip()
        # Allow empty input (stash message is optional)
        self.dismiss(value)
    
    def action_dismiss(self) -> None:
        """Dismiss the dialog when Escape is pressed."""
        self.dismiss(None)


class StashOptionsMenuDialog(ModalScreen[str | None]):
    """Menu dialog for stash options with radio button interface."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
    ]

    DEFAULT_CSS = """
    StashOptionsMenuDialog {
        align: center middle;
    }

    StashOptionsMenuDialog #stash-options-dialog {
        width: 70%;
        min-width: 60;
        max-width: 80;
        height: auto;
        min-height: 16;
        background: $surface;
        border: thick $primary;
        layout: vertical;
        padding: 2;
    }

    StashOptionsMenuDialog #stash-options-header {
        width: 100%;
        height: auto;
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 2;
        padding-bottom: 1;
        border-bottom: solid $primary;
    }

    StashOptionsMenuDialog #stash-options-subtitle {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashOptionsMenuDialog #radio-container {
        width: 100%;
        height: auto;
        layout: vertical;
        margin-bottom: 2;
        border: solid $primary 30%;
    }

    StashOptionsMenuDialog .radio-option {
        width: 100%;
        height: auto;
        padding: 0 1;
        margin-bottom: 0;
        border: none;
        background: $surface;
        layout: horizontal;
        align: left middle;
    }

    StashOptionsMenuDialog .radio-option:focus {
        background: $primary 10%;
    }

    StashOptionsMenuDialog .radio-option:hover {
        background: $primary 10%;
    }

    StashOptionsMenuDialog .radio-option.selected {
        background: $primary 15%;
    }

    StashOptionsMenuDialog .radio-option.selected:focus {
        background: $primary 20%;
    }

    StashOptionsMenuDialog .radio-option-row {
        width: 100%;
        height: auto;
        layout: horizontal;
        align: left middle;
    }

    StashOptionsMenuDialog .radio-indicator {
        width: 3;
        height: 1;
        margin-right: 1;
        text-align: center;
        content-align: center middle;
    }

    StashOptionsMenuDialog .radio-indicator.selected {
        color: $primary;
    }

    StashOptionsMenuDialog .radio-label {
        width: 1fr;
        color: $text;
        text-style: bold;
    }

    StashOptionsMenuDialog .radio-description {
        width: 1fr;
        color: $text-muted;
        margin-left: 1;
    }

    StashOptionsMenuDialog #stash-options-info {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashOptionsMenuDialog #stash-options-button-container {
        width: 100%;
        layout: horizontal;
        align: center middle;
        height: auto;
    }

    StashOptionsMenuDialog #cancel-button {
        margin-right: 1;
    }

    StashOptionsMenuDialog #confirm-button {
        min-width: 15;
    }
    """

    def __init__(self) -> None:
        """Initialize stash options menu dialog."""
        super().__init__()
        self.selected_option: str | None = None
        self.options = [
            ("all", "Stash all changes", "Stash all modified and staged files"),
            ("keep-index", "Stash all changes (keep index)", "Stash changes but keep staged files staged"),
            ("untracked", "Stash including untracked files", "Stash all changes including untracked files"),
            ("staged", "Stash staged changes only", "Stash only files that are currently staged"),
            ("unstaged", "Stash unstaged changes only", "Stash only files with unstaged changes"),
        ]

    def compose(self):
        """Compose the menu."""
        with Container(id="stash-options-dialog"):
            yield Static("Stash Options", id="stash-options-header")
            yield Static("Select a stash option", id="stash-options-subtitle")
            
            with Container(id="radio-container"):
                for option_id, label, description in self.options:
                    with Container(classes="radio-option", id=f"option-{option_id}") as option_container:
                        option_container.can_focus = True
                        yield Static("○", classes="radio-indicator", id=f"indicator-{option_id}")
                        yield Static(label, classes="radio-label", id=f"label-{option_id}")
                        yield Static(f"  •  {description}", classes="radio-description")
            
            yield Static("Use ↑↓ to navigate, Enter to confirm, or Escape to cancel", id="stash-options-info")
            
            with Container(id="stash-options-button-container"):
                yield Button("Cancel", id="cancel-button", variant="default")
                yield Button("Confirm", id="confirm-button", variant="primary")

    def on_mount(self) -> None:
        """Focus the first option when mounted."""
        # Select first option by default
        self._select_option("all")
        # Focus the first option container
        try:
            first_option = self.query_one("#option-all")
            first_option.focus()
        except Exception:
            pass

    def on_key(self, event) -> None:
        """Handle keyboard navigation."""
        if event.key == "up":
            self._navigate(-1)
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            self._navigate(1)
            event.prevent_default()
            event.stop()
        elif event.key == "enter" or event.key == " ":
            if self.selected_option:
                self.dismiss(self.selected_option)
            event.prevent_default()
            event.stop()

    def _navigate(self, direction: int) -> None:
        """Navigate between options."""
        current_index = 0
        if self.selected_option:
            for i, (option_id, _, _) in enumerate(self.options):
                if option_id == self.selected_option:
                    current_index = i
                    break
        
        new_index = (current_index + direction) % len(self.options)
        new_option_id = self.options[new_index][0]
        self._select_option(new_option_id)
        try:
            new_option = self.query_one(f"#option-{new_option_id}")
            new_option.focus()
        except Exception:
            pass

    def _select_option(self, option_id: str) -> None:
        """Select an option."""
        # Deselect previous
        if self.selected_option:
            try:
                prev_option = self.query_one(f"#option-{self.selected_option}")
                prev_indicator = self.query_one(f"#indicator-{self.selected_option}")
                prev_option.remove_class("selected")
                prev_indicator.remove_class("selected")
                prev_indicator.update("○")
            except Exception:
                pass
        
        # Select new
        self.selected_option = option_id
        try:
            option = self.query_one(f"#option-{option_id}")
            indicator = self.query_one(f"#indicator-{option_id}")
            option.add_class("selected")
            indicator.add_class("selected")
            indicator.update("●")
        except Exception:
            pass

    def on_click(self, event) -> None:
        """Handle click on option."""
        # Check if click is within the dialog
        try:
            dialog = self.query_one("#stash-options-dialog")
            dialog_region = dialog.region
            
            # If click is outside dialog, just ignore it
            if not (dialog_region.x <= event.x < dialog_region.x + dialog_region.width and
                    dialog_region.y <= event.y < dialog_region.y + dialog_region.height):
                return
            
            # Get the widget at the click coordinates
            clicked_widget = self.screen.get_widget_at(event.x, event.y)
            if clicked_widget is None:
                return
            
            # Find the option container by traversing up the DOM
            current = clicked_widget
            while current and current != self:
                if hasattr(current, 'has_class') and current.has_class("radio-option"):
                    if hasattr(current, 'id') and current.id:
                        option_id = current.id.replace("option-", "")
                        self._select_option(option_id)
                        current.focus()
                        return
                current = getattr(current, 'parent', None)
        except Exception:
            # If we can't determine what was clicked, just ignore
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "confirm-button":
            if self.selected_option:
                self.dismiss(self.selected_option)
        else:
            self.dismiss(None)

    def action_confirm(self) -> None:
        """Confirm selection."""
        if self.selected_option:
            self.dismiss(self.selected_option)

    def action_dismiss(self) -> None:
        """Dismiss the menu."""
        self.dismiss(None)


class StashConfirmDialog(ModalScreen[bool]):
    """Stash-specific confirmation dialog with professional styling."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    StashConfirmDialog {
        align: center middle;
    }

    StashConfirmDialog #stash-confirm-dialog {
        width: 70%;
        min-width: 55;
        max-width: 80;
        height: auto;
        min-height: 12;
        background: $surface;
        border: thick $primary;
        layout: vertical;
        padding: 2;
    }

    StashConfirmDialog #stash-confirm-header {
        width: 100%;
        height: auto;
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 2;
        padding-bottom: 1;
        border-bottom: solid $primary;
    }

    StashConfirmDialog #stash-confirm-subtitle {
        width: 100%;
        height: auto;
        text-align: center;
        color: $text-muted;
        margin-bottom: 2;
    }

    StashConfirmDialog #stash-confirm-message-container {
        width: 100%;
        height: auto;
        layout: vertical;
        margin-bottom: 2;
        padding: 1;
        border: solid $primary;
        background: $panel;
    }

    StashConfirmDialog #stash-confirm-message {
        width: 100%;
        height: auto;
        color: $text;
        text-align: center;
    }

    StashConfirmDialog #stash-confirm-stash-info {
        width: 100%;
        height: auto;
        margin-top: 1;
        padding: 1;
        background: $background;
        border: solid $primary;
        border-title-align: left;
        border-title-color: $accent;
    }

    StashConfirmDialog #stash-confirm-stash-name {
        width: 100%;
        height: auto;
        color: $accent;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    StashConfirmDialog #stash-confirm-warning {
        width: 100%;
        height: auto;
        color: $warning;
        text-align: center;
        margin-top: 1;
        text-style: italic;
    }

    StashConfirmDialog #stash-confirm-button-container {
        width: 100%;
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
    }

    StashConfirmDialog #cancel-button {
        margin-right: 1;
        min-width: 12;
    }

    StashConfirmDialog #confirm-button {
        min-width: 12;
    }
    """

    def __init__(
        self, 
        title: str, 
        message: str, 
        stash_name: str | None = None,
        warning: str | None = None,
        confirm_text: str = "Confirm", 
        cancel_text: str = "Cancel"
    ) -> None:
        """Initialize stash confirmation dialog.
        
        Args:
            title: Dialog title.
            message: Confirmation message.
            stash_name: Optional stash name/details to display prominently.
            warning: Optional warning message to display.
            confirm_text: Text for confirm button.
            cancel_text: Text for cancel button.
        """
        super().__init__()
        self.title_text = title
        self.message_text = message
        self.stash_name = stash_name
        self.warning = warning
        self.confirm_text = confirm_text
        self.cancel_text = cancel_text

    def compose(self):
        """Compose dialog widgets."""
        with Container(id="stash-confirm-dialog"):
            yield Static(self.title_text, id="stash-confirm-header")
            yield Static("Please confirm this action", id="stash-confirm-subtitle")
            
            with Container(id="stash-confirm-message-container"):
                yield Static(self.message_text, id="stash-confirm-message")
                
                if self.stash_name:
                    with Container(id="stash-confirm-stash-info"):
                        yield Static(self.stash_name, id="stash-confirm-stash-name")
                
                if self.warning:
                    yield Static(self.warning, id="stash-confirm-warning")
            
            with Container(id="stash-confirm-button-container"):
                yield Button(self.cancel_text, id="cancel-button", variant="default")
                yield Button(self.confirm_text, id="confirm-button", variant="primary")

    def on_mount(self) -> None:
        """Focus confirm button when mounted."""
        try:
            self.query_one("#confirm-button", Button).focus()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "confirm-button":
            self.dismiss(True)
        else:
            self.dismiss(False)
    
    def action_dismiss(self) -> None:
        """Dismiss the dialog when Escape is pressed."""
        self.dismiss(False)

