"""Hold-to-talk and confirm chords. pynput is imported only when listening."""

from __future__ import annotations

from dataclasses import dataclass, field


class HotkeyDependencyError(RuntimeError):
    """pynput is not installed, so global hotkeys are unavailable."""


_MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "meta": "cmd",
}


@dataclass(frozen=True)
class HotkeyChord:
    modifiers: frozenset[str]
    key: str


def parse_hotkey(spec: str) -> HotkeyChord:
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    modifiers: set[str] = set()
    key: str | None = None
    for part in parts:
        if part in _MODIFIERS:
            modifiers.add(_MODIFIERS[part])
            continue
        if key is not None:
            raise ValueError(f"hotkey has more than one key: {spec}")
        key = part
    if key is None:
        raise ValueError(f"hotkey needs a non-modifier key: {spec}")
    return HotkeyChord(modifiers=frozenset(modifiers), key=key)


@dataclass
class HotkeyState:
    pressed: set[str] = field(default_factory=set)
    ptt_down: bool = False
    confirm_down: bool = False


def update_hotkey_state(state: HotkeyState, ptt: HotkeyChord, confirm: HotkeyChord, *, pressed: bool, token: str | None) -> list[str]:
    """Update held keys and return actions: ptt_down, ptt_up, confirm."""
    if token:
        if pressed:
            state.pressed.add(token)
        else:
            state.pressed.discard(token)
    actions: list[str] = []
    ptt_now = _matches(state, ptt)
    confirm_now = _matches(state, confirm)
    if ptt_now and not state.ptt_down:
        state.ptt_down = True
        actions.append("ptt_down")
    elif state.ptt_down and not ptt_now:
        state.ptt_down = False
        actions.append("ptt_up")
    if confirm_now and not state.confirm_down:
        state.confirm_down = True
        actions.append("confirm")
    elif state.confirm_down and not confirm_now:
        state.confirm_down = False
    return actions


def _matches(state: HotkeyState, chord: HotkeyChord) -> bool:
    if chord.key not in state.pressed:
        return False
    held_mods = state.pressed & {"ctrl", "alt", "shift", "cmd"}
    return held_mods == set(chord.modifiers)


class HotkeyListener:
    def __init__(self, ptt: str, confirm: str, on_ptt, on_confirm):
        self.ptt = parse_hotkey(ptt)
        self.confirm = parse_hotkey(confirm)
        self.on_ptt = on_ptt
        self.on_confirm = on_confirm
        self.state = HotkeyState()
        self._listener = None
        self._keyboard = None

    def start(self) -> None:
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise HotkeyDependencyError(
                "pynput is not installed. On the Mac run: pip install -e '.[hotkey]'. "
                "macOS also needs Input Monitoring permission for the terminal."
            ) from exc
        self._keyboard = keyboard
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key) -> None:
        self._dispatch(True, self._token(key))

    def _on_release(self, key) -> None:
        self._dispatch(False, self._token(key))

    def _dispatch(self, pressed: bool, token: str | None) -> None:
        actions = update_hotkey_state(self.state, self.ptt, self.confirm, pressed=pressed, token=token)
        if "ptt_down" in actions:
            self.on_ptt(True)
        if "ptt_up" in actions:
            self.on_ptt(False)
        if "confirm" in actions:
            self.on_confirm()

    def _token(self, key) -> str | None:
        keyboard = self._keyboard
        if keyboard is None:
            return None
        mapping = {
            keyboard.Key.ctrl: "ctrl",
            keyboard.Key.ctrl_l: "ctrl",
            keyboard.Key.ctrl_r: "ctrl",
            keyboard.Key.alt: "alt",
            keyboard.Key.alt_l: "alt",
            keyboard.Key.alt_r: "alt",
            keyboard.Key.shift: "shift",
            keyboard.Key.shift_l: "shift",
            keyboard.Key.shift_r: "shift",
            keyboard.Key.cmd: "cmd",
            keyboard.Key.cmd_l: "cmd",
            keyboard.Key.cmd_r: "cmd",
        }
        if key in mapping:
            return mapping[key]
        character = getattr(key, "char", None)
        if isinstance(character, str) and character:
            return character.lower()
        name = getattr(key, "name", None)
        if isinstance(name, str) and name:
            return name.lower()
        return None
