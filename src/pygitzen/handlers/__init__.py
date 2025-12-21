"""Action handlers module for pygitzen.

This module contains action handlers that coordinate between UI and services.
Action handlers are responsible for:
- Checking UI state (focus, selection)
- Calling appropriate services
- Updating UI (notifications, refresh)
- Handling user feedback

Unlike services, handlers have UI dependencies and are aware of the app context.
"""

from .branch_actions import BranchActionHandler
from .commit_actions import CommitActionHandler
from .file_actions import FileActionHandler
from .stash_actions import StashActionHandler
from .sync_actions import SyncActionHandler

__all__ = [
    "BranchActionHandler",
    "CommitActionHandler",
    "FileActionHandler",
    "StashActionHandler",
    "SyncActionHandler",
]

