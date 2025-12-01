"""Dialog widgets for pygitzen.

Contains modal dialogs for user input (create branch, rename, etc.).
"""

from __future__ import annotations

from textual.screen import ModalScreen
from textual.widgets import Input, Label, Button
from textual.containers import Container


class MinimalDialog(ModalScreen[str]):
    """Minimal floating dialog with input field and Enter key submission."""

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
        grid-rows: auto auto;
        grid-gaps: 1;
        padding: 1;
    }

    #title {
        text-align: center;
        text-style: bold;
        color: $text;
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


class DeleteBranchDialog(ModalScreen[bool]):
    """Confirmation dialog for deleting a branch."""

    DEFAULT_CSS = """
    DeleteBranchDialog {
        align: center middle;
    }

    #dialog {
        width: 50;
        height: 8;
        background: $surface;
        border: solid $error;
        grid-rows: auto auto auto;
        grid-gaps: 1;
        padding: 1;
    }

    #message {
        text-align: center;
        color: $text;
    }

    #button-container {
        width: 100%;
        grid-columns: 1fr 1fr;
        grid-gaps: 1;
    }
    """

    def __init__(self, branch_name: str) -> None:
        """Initialize delete branch dialog.
        
        Args:
            branch_name: Name of branch to delete.
        """
        super().__init__()
        self.branch_name = branch_name

    def compose(self):
        """Compose dialog widgets."""
        with Container(id="dialog"):
            yield Label(f"Delete branch '{self.branch_name}'?", id="message")
            with Container(id="button-container"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Delete", id="delete", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "delete":
            self.dismiss(True)
        else:
            self.dismiss(False)


class SetUpstreamDialog(MinimalDialog):
    """Dialog for setting upstream branch."""

    def __init__(self, branch_name: str) -> None:
        """Initialize set upstream dialog.
        
        Args:
            branch_name: Local branch name.
        """
        title = f"Set upstream for '{branch_name}'"
        placeholder = "Upstream branch (e.g., origin/main)"
        super().__init__(title=title, placeholder=placeholder)


class ConfirmDialog(ModalScreen[bool]):
    """Generic confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }

    #dialog {
        width: 50;
        height: 8;
        background: $surface;
        border: solid $primary;
        grid-rows: auto auto auto;
        grid-gaps: 1;
        padding: 1;
    }

    #message {
        text-align: center;
        color: $text;
    }

    #button-container {
        width: 100%;
        grid-columns: 1fr 1fr;
        grid-gaps: 1;
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

