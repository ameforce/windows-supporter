from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip


class Skype:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        return

    def is_skype_active(self):
        one_note_windows = [win for win in self.__lib.gw.getWindowsWithTitle('Skype') if win.isActive]
        return bool(one_note_windows)

    def action(self, root) -> None:
        self.__lib.pyautogui.hotkey('enter')
        tooltip = ToolTip(root, f"성공적으로 보조됨: Enter", bind_events=False)
        tooltip.show_tooltip()
        root.after(100, tooltip.hide_tooltip)
        return