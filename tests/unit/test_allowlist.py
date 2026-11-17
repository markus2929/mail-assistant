import pytest

from mailassistant.security.allowlist import (ActionNotAllowed, ActionType, AutomationLevel,
                                              effective_level)


def test_calendar_creation_never_exceeds_approval():
    lvl = effective_level("create_calendar_event", AutomationLevel.AUTO)
    assert lvl == AutomationLevel.APPLY_AFTER_APPROVAL


def test_low_risk_action_can_be_auto():
    assert effective_level("move_to_folder", AutomationLevel.AUTO) == AutomationLevel.AUTO


@pytest.mark.parametrize("bad", ["send_email", "delete_email", "accept_invitation",
                                 "change_settings", "run_command"])
def test_dangerous_actions_do_not_exist(bad):
    assert bad not in {a.value for a in ActionType}
    with pytest.raises(ActionNotAllowed):
        effective_level(bad, AutomationLevel.SUGGEST)
