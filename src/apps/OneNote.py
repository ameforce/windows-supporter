from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip


class OneNote:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        return

    def is_one_note_active(self):
        one_note_windows = [win for win in self.__lib.gw.getWindowsWithTitle('OneNote') if win.isActive]
        return bool(one_note_windows)

    def get_date(self) -> str:
        now = self.__lib.datetime.now()
        formatted_date = now.strftime("[%m/%d] ")
        return formatted_date

    def action(self, root) -> None:
        transformed_text = self.get_date()
        backup_data = self.__lib.pyperclip.paste()
        self.__lib.pyperclip.copy(transformed_text)
        self.__lib.pyautogui.hotkey('ctrl', 'v')
        self.__lib.pyperclip.copy(backup_data)
        tooltip = ToolTip(root,
                          f"성공적으로 삽입됨: {transformed_text}\n"
                          f"성공적으로 복구됨: {backup_data}",
                          bind_events=False)
        tooltip.show_tooltip()
        root.after(1500, tooltip.hide_tooltip)
        return
