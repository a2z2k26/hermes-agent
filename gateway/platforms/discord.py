"""Compatibility exports for Discord Inbox Review tests.

The active Discord adapter lives in ``plugins.platforms.discord.adapter`` in
pluginized Hermes builds. Keep this shim narrow so legacy tests and probes can
import the Inbox Review UI symbols without resurrecting the old gateway adapter
module path.
"""

from plugins.platforms.discord.adapter import (  # noqa: F401
    InboxReviewView,
    _is_inbox_review_stop_trigger,
    _is_inbox_review_trigger,
)
