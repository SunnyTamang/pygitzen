"""Dialog widgets for pygitzen.

Contains modal dialogs for user input (create branch, rename, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.screen import ModalScreen
from textual.widgets import Input, Label, Button, Static
from textual.containers import Container
from textual.binding import Binding
from rich.text import Text


class MinimalDialog(ModalScreen[str]):
    """Minimal floating dialog with input field and Enter key submission."""

    CSS_PATH = "../styles/dialogs.tcss"

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

    CSS_PATH = "../styles/dialogs.tcss"

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

    CSS_PATH = "../styles/dialogs.tcss"

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

