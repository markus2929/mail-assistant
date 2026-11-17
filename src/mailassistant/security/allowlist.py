"""Strict allowlist of everything the system may do. Anything not listed here cannot happen.

Sending mail, deleting mail, accepting invitations and changing settings are deliberately
ABSENT: they do not exist as actions in this version. Model output can only ever be turned
into one of these proposal types by deterministic code.
"""
from enum import StrEnum


class ActionType(StrEnum):
    SUGGEST_LABEL = "suggest_label"
    MOVE_TO_FOLDER = "move_to_folder"
    PROPOSE_EVENT = "propose_event"
    CREATE_CALENDAR_EVENT = "create_calendar_event"  # requires an approved EventProposal
    PROPOSE_FOLLOWUP = "propose_followup"
    PROPOSE_CLOSE_FOLLOWUP = "propose_close_followup"
    NOTIFY_USER = "notify_user"  # push to the user's OWN Home Assistant (Phase 6); never to anyone else


class AutomationLevel(StrEnum):
    OFF = "off"
    SUGGEST = "suggest"  # default: record proposals only
    APPLY_AFTER_APPROVAL = "apply_after_approval"
    AUTO = "auto"


_ORDER = [AutomationLevel.OFF, AutomationLevel.SUGGEST,
          AutomationLevel.APPLY_AFTER_APPROVAL, AutomationLevel.AUTO]

# Highest automation level each action may ever be configured to.
MAX_LEVEL: dict[ActionType, AutomationLevel] = {
    ActionType.SUGGEST_LABEL: AutomationLevel.AUTO,
    ActionType.MOVE_TO_FOLDER: AutomationLevel.AUTO,
    ActionType.PROPOSE_EVENT: AutomationLevel.AUTO,
    ActionType.CREATE_CALENDAR_EVENT: AutomationLevel.APPLY_AFTER_APPROVAL,
    ActionType.PROPOSE_FOLLOWUP: AutomationLevel.AUTO,
    ActionType.PROPOSE_CLOSE_FOLLOWUP: AutomationLevel.APPLY_AFTER_APPROVAL,
    ActionType.NOTIFY_USER: AutomationLevel.AUTO,
}


class ActionNotAllowed(Exception):
    pass


def effective_level(action: str, configured: AutomationLevel) -> AutomationLevel:
    """Clamp the configured level to the maximum permitted for this action."""
    try:
        a = ActionType(action)
    except ValueError as e:
        raise ActionNotAllowed(f"unknown action {action!r}") from e
    return min(configured, MAX_LEVEL[a], key=_ORDER.index)
