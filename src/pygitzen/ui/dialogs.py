"""Dialog widgets for pygitzen.

Contains modal dialogs for user input (create branch, rename, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.screen import ModalScreen
from textual.widgets import Input, Label, Button, Static, Link
from textual.containers import Container, Horizontal
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

