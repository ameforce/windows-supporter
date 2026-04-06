from src.utils.LibConnector import LibConnector


class ToolTip(object):
    def __init__(
        self,
        widget,
        text: str = 'Windows Supporter',
        bind_events: bool = True,
        auto_hide_ms: int | None = None,
        keep_on_hover: bool = False,
        lines: list[tuple[str, str | None]] | None = None,
    ) -> None:
        self.__lib = LibConnector()
        self.__wait_time = 500     # MilliSecond
        self.__wrap_length = 1800  # Pixel
        self.__widget = widget
        self.__text = text
        self.__lines = lines
        self.__bind_events = bool(bind_events)
        self.__auto_hide_ms = auto_hide_ms
        self.__keep_on_hover = bool(keep_on_hover)
        self.__border_normal = "#E5E7EB"
        self.__border_hover = "#2563EB"
        if self.__bind_events:
            try:
                self.__widget.bind("<Enter>", self.on_enter, add=True)
                self.__widget.bind("<Leave>", self.on_leave, add=True)
            except Exception:
                self.__widget.bind("<Enter>", self.on_enter)
                self.__widget.bind("<Leave>", self.on_leave)
        self.__widget_id = None
        self.__tw = None
        self.__hide_after_id = None
        self.__hovering = False
        self.__container = None
        self.__countdown_label = None
        self.__countdown_after_id = None
        self.__deadline_ts = None
        self.__expired_while_hovering = False
        return

    def on_enter(self, event=None) -> None:
        self.schedule()
        return

    def on_leave(self, event=None) -> None:
        self.unschedule()
        self.hide_tooltip()
        return

    def schedule(self) -> None:
        self.unschedule()
        self.__widget_id = self.__widget.after(self.__wait_time, self.show_tooltip)
        return

    def unschedule(self):
        widget_id = self.__widget_id
        self.__widget_id = None
        if widget_id:
            self.__widget.after_cancel(widget_id)
        return

    def show_tooltip(self, event=None) -> None:
        try:
            x, y = self.__lib.win32gui.GetCursorPos()
        except Exception:
            x, y = self.__lib.pyautogui.position()
        x = int(x) + 16
        y = int(y) + 16
        self.__tw = self.__lib.tk.Toplevel(self.__widget)
        self.__tw.wm_overrideredirect(True)
        self.__tw.wm_geometry("+%d+%d" % (x, y))
        try:
            self.__tw.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.__tw.lift()
        except Exception:
            pass
        bg = "#ffffff"
        container = self.__lib.tk.Frame(
            self.__tw,
            background=bg,
            relief='solid',
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=self.__border_normal,
        )
        self.__container = container
        container.pack()

        if self.__lines:
            for line, color in self.__lines:
                fg = color if color else "#111111"
                self.__lib.tk.Label(
                    container,
                    text=str(line),
                    justify='left',
                    background=bg,
                    foreground=fg,
                    wraplength=self.__wrap_length,
                    anchor="w",
                ).pack(fill="x", padx=4, pady=1)
        else:
            self.__lib.tk.Label(
                container,
                text=self.__text,
                justify='left',
                background=bg,
                foreground="#111111",
                wraplength=self.__wrap_length,
            ).pack(ipadx=1, padx=4, pady=2)

        if self.__auto_hide_ms is not None and self.__auto_hide_ms > 0:
            self.__countdown_label = self.__lib.tk.Label(
                container,
                text="",
                justify='left',
                background=bg,
                foreground="#6B7280",
                wraplength=self.__wrap_length,
                anchor="w",
            )
            self.__countdown_label.pack(fill="x", padx=4, pady=(2, 2))

        self.__adjust_position(x, y)

        if self.__keep_on_hover:
            try:
                self.__tw.bind("<Enter>", self.__on_tooltip_enter)
                self.__tw.bind("<Leave>", self.__on_tooltip_leave)
            except Exception:
                pass

        self.__start_countdown()
        return

    def hide_tooltip(self) -> None:
        tw = self.__tw
        self.__tw = None
        self.__cancel_auto_hide()
        if tw:
            tw.destroy()
        return

    def __schedule_auto_hide(self) -> None:
        if self.__auto_hide_ms is None or self.__auto_hide_ms <= 0:
            return
        if self.__tw is None:
            return
        self.__cancel_auto_hide()
        try:
            self.__hide_after_id = self.__tw.after(250, self.__countdown_tick)
        except Exception:
            self.__hide_after_id = None
        return

    def __cancel_auto_hide(self) -> None:
        if self.__tw is None or self.__hide_after_id is None:
            return
        try:
            self.__tw.after_cancel(self.__hide_after_id)
        except Exception:
            pass
        self.__hide_after_id = None
        return

    def __on_tooltip_enter(self, _event=None) -> None:
        self.__hovering = True
        self.__update_border(True)
        return

    def __on_tooltip_leave(self, _event=None) -> None:
        if self.__pointer_inside():
            return
        self.__hovering = False
        self.__update_border(False)
        if self.__expired_while_hovering:
            self.hide_tooltip()
            return
        self.__schedule_auto_hide()
        return

    def __update_border(self, hovering: bool) -> None:
        container = self.__container
        if container is None:
            return
        try:
            color = self.__border_hover if hovering else self.__border_normal
            container.configure(highlightbackground=color)
        except Exception:
            pass
        return

    def __pointer_inside(self) -> bool:
        tw = self.__tw
        if tw is None:
            return False
        try:
            px = int(tw.winfo_pointerx())
            py = int(tw.winfo_pointery())
        except Exception:
            try:
                px, py = self.__lib.win32gui.GetCursorPos()
                px = int(px)
                py = int(py)
            except Exception:
                return False
        try:
            x = int(tw.winfo_rootx())
            y = int(tw.winfo_rooty())
            w = int(tw.winfo_width())
            h = int(tw.winfo_height())
        except Exception:
            return False
        if w <= 0 or h <= 0:
            return False
        return x <= px <= (x + w) and y <= py <= (y + h)

    def __get_monitor_work_area(self, x: int, y: int) -> tuple[int, int, int, int] | None:
        try:
            import ctypes
            import ctypes.wintypes

            MONITOR_DEFAULTTONEAREST = 2

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', ctypes.wintypes.DWORD),
                    ('rcMonitor', ctypes.wintypes.RECT),
                    ('rcWork', ctypes.wintypes.RECT),
                    ('dwFlags', ctypes.wintypes.DWORD),
                ]

            user32 = ctypes.windll.user32
            _MonitorFromRect = user32.MonitorFromRect
            _MonitorFromRect.restype = ctypes.c_void_p
            rect = ctypes.wintypes.RECT(int(x), int(y), int(x) + 1, int(y) + 1)
            hMonitor = _MonitorFromRect(ctypes.byref(rect), MONITOR_DEFAULTTONEAREST)
            if not hMonitor:
                return None
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            _GetMonitorInfoW = user32.GetMonitorInfoW
            _GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            _GetMonitorInfoW.restype = ctypes.c_int
            if not _GetMonitorInfoW(hMonitor, ctypes.byref(mi)):
                return None
            work = mi.rcWork
            return (int(work.left), int(work.top), int(work.right), int(work.bottom))
        except Exception:
            return None

    def __adjust_position(self, x: int, y: int) -> None:
        tw = self.__tw
        if tw is None:
            return
        try:
            tw.update_idletasks()
        except Exception:
            pass
        try:
            w = int(tw.winfo_width())
            h = int(tw.winfo_height())
        except Exception:
            return
        work_area = self.__get_monitor_work_area(x, y)
        if work_area is not None:
            ml, mt, mr, mb = work_area
        else:
            ml, mt = 0, 0
            try:
                mr = int(tw.winfo_screenwidth())
                mb = int(tw.winfo_screenheight())
            except Exception:
                return
        nx = int(x)
        ny = int(y)
        if nx + w > mr:
            nx = int(x - w - 16)
        if ny + h > mb:
            ny = int(y - h - 16)
        if nx < ml:
            nx = ml
        if ny < mt:
            ny = mt
        if nx + w > mr:
            nx = max(ml, mr - w)
        if ny + h > mb:
            ny = max(mt, mb - h)
        try:
            tw.wm_geometry("+%d+%d" % (nx, ny))
        except Exception:
            return

    def __start_countdown(self) -> None:
        if self.__auto_hide_ms is None or self.__auto_hide_ms <= 0:
            return
        try:
            self.__deadline_ts = self.__lib.time.monotonic() + (int(self.__auto_hide_ms) / 1000.0)
        except Exception:
            self.__deadline_ts = None
        self.__expired_while_hovering = False
        self.__schedule_auto_hide()
        return

    def __set_countdown_text(self, text: str) -> None:
        label = self.__countdown_label
        if label is None:
            return
        try:
            label.configure(text=str(text))
        except Exception:
            return
        return

    def __countdown_tick(self) -> None:
        if self.__tw is None:
            return
        if self.__auto_hide_ms is None or self.__auto_hide_ms <= 0:
            return
        if self.__deadline_ts is None:
            return

        try:
            now = self.__lib.time.monotonic()
        except Exception:
            now = 0.0
        remain = float(self.__deadline_ts) - float(now)
        if remain <= 0:
            if self.__hovering:
                self.__expired_while_hovering = True
                self.__set_countdown_text("호버링 중...")
                self.__hide_after_id = None
                return
            self.hide_tooltip()
            return

        seconds = int(remain)
        if remain - float(seconds) > 1e-6:
            seconds += 1
        self.__set_countdown_text(f"{seconds}초 후 닫힘")
        self.__schedule_auto_hide()
        return
