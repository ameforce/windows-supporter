from src.utils.LibConnector import LibConnector


class VirtualDesktop:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__CLSID_VirtualDesktopManager = '{AA509086-5CA9-4C25-8F95-589D3C07B48A}'
        self.__IID_IVirtualDesktopManager = '{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}'
        self.__lib.ctypes.oledll.ole32.CoInitialize(None)
        self.__desktop_manager = self.__lib.CreateObject(self.__CLSID_VirtualDesktopManager,
                                                         interface=self.__IID_IVirtualDesktopManager)
        self.__run_program_list = ['notepad.exe']
        return

    def run_program(self) -> None:
        process_list = []
        for program in self.__run_program_list:
            process_list.append(self.__lib.subprocess.Popen(program))
            self.__lib.time.sleep(2)
        return

    def move_program(self) -> None:

        return
