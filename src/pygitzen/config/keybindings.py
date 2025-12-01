"""Keybinding configuration management for pygitzen.

Handles loading user keybinding configurations from TOML files,
merging with defaults, and providing bindings for app and panes.
"""

import os
import platform
from pathlib import Path
from typing import Optional

from textual.binding import Binding

try:
    import tomli as tomllib  # Python < 3.11
except ImportError:
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        # Fallback: if tomli not available, config loading will fail gracefully
        tomllib = None


class KeybindingConfig:
    """Manages keybinding configuration with user overrides and defaults."""

    def __init__(self) -> None:
        """Initialize keybinding config."""
        self.config_path: Optional[Path] = self._get_config_path()

    def _get_config_path(self) -> Optional[Path]:
        """Get platform-specific config file path.
        
        Returns:
            Path to config file, or None if path cannot be determined.
        """
        system = platform.system()

        if system == "Windows":
            # Windows: %APPDATA%\pygitzen\keybindings.toml
            appdata = os.getenv("APPDATA")
            if appdata:
                return Path(appdata) / "pygitzen" / "keybindings.toml"
            return None

        # macOS/Linux: ~/.config/pygitzen/keybindings.toml
        return Path.home() / ".config" / "pygitzen" / "keybindings.toml"

    def _get_default_bindings(self, pane: str) -> list[Binding]:
        """Get hardcoded default bindings for a pane.
        
        Args:
            pane: Pane name ("app", "branches", "commits", etc.)
        
        Returns:
            List of default Binding objects for the pane.
        """
        defaults: dict[str, list[Binding]] = {
            "app": [
                Binding("q", "quit", "Quit"),
                Binding("r", "refresh", "Refresh"),
                Binding("j", "down", "Down", show=False),
                Binding("k", "up", "Up", show=False),
                Binding("h", "left", "Left", show=False),
                Binding("l", "right", "Right", show=False),
                Binding("@", "toggle_command_log", "Toggle Command Log"),
                Binding("space", "select", "Select"),
                Binding("enter", "select", "Select"),
                Binding("c", "checkout", "Checkout"),
                Binding("b", "branch", "Branch"),
                Binding("s", "stash", "Stash"),
                Binding("+", "load_more", "More"),
                Binding("g", "toggle_graph_style", "Toggle Graph Style"),
            ],
            "branches": [
                Binding("c", "checkout", "Checkout"),
                Binding("space", "select", "Select"),
                Binding("enter", "select", "Select"),
                Binding("n", "new_branch", "New"),
                Binding("d", "delete_branch", "Delete"),
                Binding("r", "rename_branch", "Rename"),
                Binding("m", "merge_branch", "Merge"),
                Binding("p", "push_branch", "Push"),
                Binding("u", "set_upstream", "Upstream"),
            ],
            # Add more panes as needed
            "commits": [
                Binding("c", "checkout", "Checkout"),
                Binding("space", "select", "Select"),
                Binding("enter", "select", "Select"),
            ],
            "stash": [
                Binding("space", "select", "Select"),
                Binding("enter", "select", "Select"),
            ],
            "tags": [
                Binding("space", "select", "Select"),
                Binding("enter", "select", "Select"),
            ],
            "remotes": [
                Binding("space", "select", "Select"),
                Binding("enter", "select", "Select"),
            ],
        }

        return defaults.get(pane, [])

    def _load_config_file(self, path: Path) -> dict:
        """Load TOML config file.
        
        Args:
            path: Path to config file.
        
        Returns:
            Dict with config data, or empty dict if file can't be loaded.
        """
        if tomllib is None:
            # tomli not available, can't load config
            return {}

        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            # File doesn't exist, is invalid, or can't be read
            return {}

    def _merge_bindings(
        self, defaults: list[Binding], user_overrides: dict[str, str]
    ) -> list[Binding]:
        """Merge user overrides with default bindings.
        
        Args:
            defaults: List of default Binding objects.
            user_overrides: Dict mapping key -> action from user config.
        
        Returns:
            Merged list of Binding objects.
        """
        # Create a map of existing keys for quick lookup
        key_to_binding = {binding.key: binding for binding in defaults}
        key_to_index = {binding.key: i for i, binding in enumerate(defaults)}

        # Apply user overrides
        for key, action in user_overrides.items():
            if key in key_to_binding:
                # Replace existing binding
                old_binding = key_to_binding[key]
                # Try to get description from old binding, or use action
                description = getattr(old_binding, "description", action)
                show = getattr(old_binding, "show", True)
                key_to_binding[key] = Binding(key, action, description, show=show)
            else:
                # New binding (not in defaults)
                key_to_binding[key] = Binding(key, action, action)

        # Return merged bindings, preserving order where possible
        result = []
        seen_keys = set()

        # First, add defaults in original order (if not overridden)
        for binding in defaults:
            if binding.key not in user_overrides:
                result.append(binding)
                seen_keys.add(binding.key)

        # Then add overrides and new bindings
        for key, action in user_overrides.items():
            if key not in seen_keys:
                result.append(key_to_binding[key])
                seen_keys.add(key)

        return result

    def get_bindings(self, pane: str = "app") -> list[Binding]:
        """Get bindings for a pane, merging user config with defaults.
        
        Args:
            pane: Pane name ("app", "branches", "commits", etc.)
        
        Returns:
            List of Binding objects for the pane.
        """
        defaults = self._get_default_bindings(pane)

        # Check if config file exists
        if self.config_path and self.config_path.exists():
            config_data = self._load_config_file(self.config_path)
            if config_data:
                # Get user overrides for this pane
                if pane == "app":
                    user_overrides = config_data.get("app", {})
                else:
                    panes_config = config_data.get("panes", {})
                    user_overrides = panes_config.get(pane, {})

                if user_overrides:
                    return self._merge_bindings(defaults, user_overrides)

        # No config file or no overrides for this pane - return defaults
        return defaults

