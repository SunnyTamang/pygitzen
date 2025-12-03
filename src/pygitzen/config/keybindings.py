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
        """Merge user overrides with default bindings using hybrid approach.
        
        Hybrid Logic:
        - If user's key exists in defaults → Override that key's action (key-based)
        - If user's key is new → Replace default binding with same action (action-based)
        - If user's key and action are both new → Add as new binding
        
        Args:
            defaults: List of default Binding objects.
            user_overrides: Dict mapping key -> action from user config.
        
        Returns:
            Merged list of Binding objects.
        """
        # Create maps for lookup
        action_to_bindings = {}  # action -> list of bindings (multiple can have same action)
        key_to_binding = {binding.key: binding for binding in defaults}
        
        # Build action map (list of bindings per action)
        for binding in defaults:
            if binding.action not in action_to_bindings:
                action_to_bindings[binding.action] = []
            action_to_bindings[binding.action].append(binding)
        
        result = []
        replaced_keys = set()
        
        # Process user overrides
        for user_key, user_action in user_overrides.items():
            # CASE 1: User's key EXISTS in defaults → Override that key's action (key-based)
            if user_key in key_to_binding:
                old_binding = key_to_binding[user_key]
                old_action = old_binding.action
                
                # Create new binding with user's action
                new_binding = Binding(
                    user_key,
                    user_action,
                    user_action.replace("_", " ").title(),  # Format description
                    show=getattr(old_binding, "show", True)
                )
                result.append(new_binding)
                replaced_keys.add(user_key)
                
                # IMPORTANT: If the new action already has a default binding, mark it as replaced too
                # This prevents duplicate bindings for the same action
                # Example: User sets 's' = 'refresh', default has 'r' = 'refresh'
                # We override 's' to 'refresh', but should also remove 'r'
                if user_action in action_to_bindings:
                    # Find all default bindings with this action and mark their keys as replaced
                    for old_binding_with_action in action_to_bindings[user_action]:
                        replaced_keys.add(old_binding_with_action.key)
            
            # CASE 2: User's key is NEW → Find default binding with this action and replace (action-based)
            elif user_action in action_to_bindings:
                # Find the first default binding with this action
                old_binding = action_to_bindings[user_action][0]
                old_key = old_binding.key
                
                # Replace: Create new binding with user's key but same action
                new_binding = Binding(
                    user_key,
                    user_action,
                    getattr(old_binding, "description", user_action),
                    show=getattr(old_binding, "show", True)
                )
                
                result.append(new_binding)
                replaced_keys.add(old_key)
            
            # CASE 3: New key AND new action → Just add it
            else:
                result.append(Binding(user_key, user_action, user_action.replace("_", " ").title()))
        
        # Add defaults that weren't replaced
        for binding in defaults:
            if binding.key not in replaced_keys:
                result.append(binding)
        
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
                    # print("after_merging", self._merge_bindings(defaults, user_overrides))
                    return self._merge_bindings(defaults, user_overrides)

        # No config file or no overrides for this pane - return defaults
        return defaults

