import pytest

from orb.hotkey import HotkeyState, parse_hotkey, update_hotkey_state


def test_parse_hotkey():
    chord = parse_hotkey("Ctrl+Space")
    assert chord.key == "space"
    assert chord.modifiers == frozenset({"ctrl"})
    confirm = parse_hotkey("ctrl+shift+y")
    assert confirm.key == "y"
    assert confirm.modifiers == frozenset({"ctrl", "shift"})
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+shift")


def test_push_to_talk_edges():
    state = HotkeyState()
    ptt = parse_hotkey("ctrl+space")
    confirm = parse_hotkey("ctrl+shift+y")
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="ctrl") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="space") == ["ptt_down"]
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="space") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=False, token="space") == ["ptt_up"]


def test_confirm_fires_once_per_press():
    state = HotkeyState()
    ptt = parse_hotkey("ctrl+space")
    confirm = parse_hotkey("ctrl+shift+y")
    actions: list[str] = []
    for token in ("ctrl", "shift", "y"):
        actions.extend(update_hotkey_state(state, ptt, confirm, pressed=True, token=token))
    assert actions == ["confirm"]
    assert update_hotkey_state(state, ptt, confirm, pressed=False, token="y") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="y") == ["confirm"]


def test_releasing_one_ctrl_side_keeps_the_other():
    state = HotkeyState()
    ptt = parse_hotkey("ctrl+alt+space")
    confirm = parse_hotkey("ctrl+shift+y")
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="ctrl_l") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="ctrl_r") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="alt_l") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=True, token="space") == ["ptt_down"]
    assert update_hotkey_state(state, ptt, confirm, pressed=False, token="ctrl_l") == []
    assert update_hotkey_state(state, ptt, confirm, pressed=False, token="ctrl_r") == ["ptt_up"]


def test_extra_modifier_does_not_match_push_to_talk():
    state = HotkeyState()
    ptt = parse_hotkey("ctrl+space")
    confirm = parse_hotkey("ctrl+shift+y")
    actions: list[str] = []
    for token in ("ctrl", "shift", "space"):
        actions.extend(update_hotkey_state(state, ptt, confirm, pressed=True, token=token))
    assert "ptt_down" not in actions
