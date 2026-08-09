import logging
from dataclasses import dataclass

from Xlib import XK, X
from Xlib import display as xdisplay
from Xlib.ext import xtest

log = logging.getLogger(__name__)

LEFT = 1
MIDDLE = 2
RIGHT = 3
WHEEL_UP = 4
WHEEL_DOWN = 5

BUTTONS = {"left": LEFT, "middle": MIDDLE, "right": RIGHT}

SPECIAL_KEYS = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "insert": "Insert",
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "shift": "Shift_L",
    "meta": "Super_L",
    "super": "Super_L",
    "win": "Super_L",
    "capslock": "Caps_Lock",
}


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int


class XtestError(Exception):
    pass


class Xtest:
    def __init__(self, display_name: str) -> None:
        self._display = xdisplay.Display(display_name)
        if not self._display.query_extension("XTEST"):
            raise XtestError("XTEST extension is unavailable")
        self._root = self._display.screen().root
        self._spare = self._find_spare_keycode()

    def close(self) -> None:
        self._display.close()

    def pointer(self) -> Point:
        data = self._root.query_pointer()
        return Point(int(data.root_x), int(data.root_y))

    def move(self, x: int, y: int) -> None:
        xtest.fake_input(self._display, X.MotionNotify, x=int(x), y=int(y))
        self._display.sync()

    def button(self, button: int, press: bool) -> None:
        event = X.ButtonPress if press else X.ButtonRelease
        xtest.fake_input(self._display, event, button)
        self._display.sync()

    def wheel(self, up: bool) -> None:
        button = WHEEL_UP if up else WHEEL_DOWN
        self.button(button, True)
        self.button(button, False)

    def key(self, keysym_name: str, press: bool) -> None:
        keycode, shifted = self._resolve(keysym_name)
        if press and shifted:
            self._raw_key(self._keycode_of("Shift_L"), True)
        self._raw_key(keycode, press)
        if not press and shifted:
            self._raw_key(self._keycode_of("Shift_L"), False)

    def tap(self, keysym_name: str) -> None:
        self.key(keysym_name, True)
        self.key(keysym_name, False)

    def char(self, character: str) -> None:
        name = self._keysym_name(character)
        if name is not None:
            self.tap(name)
            return
        self._tap_remapped(character)

    def _raw_key(self, keycode: int, press: bool) -> None:
        event = X.KeyPress if press else X.KeyRelease
        xtest.fake_input(self._display, event, keycode)
        self._display.sync()

    def _keysym_name(self, character: str) -> str | None:
        name = XK.keysym_to_string(ord(character))
        if name is None:
            return None
        if self._display.keysym_to_keycode(XK.string_to_keysym(name)) == 0:
            return None
        return name

    def _resolve(self, keysym_name: str) -> tuple[int, bool]:
        keysym = XK.string_to_keysym(keysym_name)
        if keysym == 0:
            raise XtestError(f"unknown key: {keysym_name}")
        mapping = list(self._display.keysym_to_keycodes(keysym))
        if not mapping:
            raise XtestError(f"key is not mapped: {keysym_name}")
        keycode, index = mapping[0]
        return int(keycode), int(index) % 2 == 1

    def _keycode_of(self, keysym_name: str) -> int:
        return int(self._display.keysym_to_keycode(XK.string_to_keysym(keysym_name)))

    def _tap_remapped(self, character: str) -> None:
        keysym = ord(character)
        if keysym >= 0x100:
            keysym = 0x01000000 + ord(character)
        self._display.change_keyboard_mapping(self._spare, [[keysym] * 2])
        self._display.sync()
        try:
            self._raw_key(self._spare, True)
            self._raw_key(self._spare, False)
        finally:
            self._display.change_keyboard_mapping(self._spare, [[X.NoSymbol] * 2])
            self._display.sync()

    def _find_spare_keycode(self) -> int:
        first = int(self._display.display.info.min_keycode)
        count = int(self._display.display.info.max_keycode) - first + 1
        mapping = list(self._display.get_keyboard_mapping(first, count))
        for offset, keysyms in enumerate(mapping):
            if not any(keysyms):
                return first + offset
        raise XtestError("no spare keycode for remapping")
