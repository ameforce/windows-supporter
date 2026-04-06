import os
import shutil
import threading

from src.utils.ToolTip import ToolTip


class LiJaMong:
    def __init__(self) -> None:
        self.__source_dir = r"C:\Workspace\Git\Module\LiJaMong\res\Scripts"
        self.__dest_dirs = [
            r"C:\Users\epapyrus\AppData\Roaming\LibreOffice\4\user\basic\Standard",
            r"C:\Program Files\LibreOffice\presets\basic\Standard",
        ]
        self.__poll_interval_sec = 3.0

        self.__root = None
        self.__event_queue = None
        self.__after_id = None

        self.__worker_lock = threading.Lock()
        self.__worker_busy = False
        return

    def attach(self, root, event_queue) -> None:
        self.__root = root
        self.__event_queue = event_queue
        self.__start_tick()
        return

    def __ui_post(self, fn) -> None:
        q = self.__event_queue
        if q is None:
            return
        try:
            q.put(fn)
        except Exception:
            return
        return

    def __start_tick(self) -> None:
        root = self.__root
        if root is None:
            return
        if self.__after_id is not None:
            return
        try:
            self.__after_id = root.after(
                int(max(0.5, self.__poll_interval_sec) * 1000),
                self.__tick,
            )
        except Exception:
            self.__after_id = None
        return

    def __tick(self) -> None:
        root = self.__root
        if root is None:
            return
        self.__after_id = None
        try:
            self.__start_worker()
        except Exception:
            pass
        try:
            self.__after_id = root.after(
                int(max(0.5, self.__poll_interval_sec) * 1000),
                self.__tick,
            )
        except Exception:
            self.__after_id = None
        return

    def __start_worker(self) -> None:
        with self.__worker_lock:
            if self.__worker_busy:
                return
            self.__worker_busy = True
        try:
            threading.Thread(target=self.__worker, daemon=True).start()
        except Exception:
            with self.__worker_lock:
                self.__worker_busy = False
        return

    def __worker(self) -> None:
        try:
            did_sync = self.__check_and_sync()
            if did_sync:
                self.__notify_sync()
        finally:
            with self.__worker_lock:
                self.__worker_busy = False
        return

    def __notify_sync(self) -> None:
        def ui_task() -> None:
            root = self.__root
            if root is None:
                return
            tooltip = ToolTip(root, "LiJaMong 동기화 완료", bind_events=False)
            tooltip.show_tooltip()
            root.after(2000, tooltip.hide_tooltip)
            return

        self.__ui_post(ui_task)
        return

    def __check_and_sync(self) -> bool:
        src_root = self.__source_dir
        if not os.path.isdir(src_root):
            return False
        source_map = self.__snapshot_dir(src_root)
        if not source_map:
            return False

        mismatch_dirs = []
        for dest_root in self.__dest_dirs:
            if self.__needs_sync(source_map, dest_root):
                mismatch_dirs.append(dest_root)

        if not mismatch_dirs:
            return False

        copied = False
        for dest_root in mismatch_dirs:
            if self.__sync_all(src_root, dest_root, source_map):
                copied = True
        return copied

    def __snapshot_dir(self, root: str) -> dict[str, int]:
        out: dict[str, int] = {}
        root_norm = os.path.normpath(root)
        prefix = root_norm + os.sep
        prefix_len = len(prefix)
        stack = [root_norm]

        while stack:
            path = stack.pop()
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            stat = entry.stat()
                        except Exception:
                            continue
                        rel = entry.path[prefix_len:]
                        out[rel] = int(stat.st_mtime_ns)
            except Exception:
                continue
        return out

    def __needs_sync(self, source_map: dict[str, int], dest_root: str) -> bool:
        if not os.path.isdir(dest_root):
            return True
        join = os.path.join
        for rel, src_mtime in source_map.items():
            dest_path = join(dest_root, rel)
            try:
                dest_mtime = int(os.stat(dest_path).st_mtime_ns)
            except Exception:
                return True
            if dest_mtime < int(src_mtime):
                return True
        return False

    def __sync_all(
        self,
        src_root: str,
        dest_root: str,
        source_map: dict[str, int],
    ) -> bool:
        try:
            os.makedirs(dest_root, exist_ok=True)
        except Exception:
            return False

        join = os.path.join
        makedirs = os.makedirs
        copy2 = shutil.copy2
        created_dirs: set[str] = set()
        did_copy = False

        for rel, src_mtime in source_map.items():
            src_path = join(src_root, rel)
            dest_path = join(dest_root, rel)
            dest_dir = os.path.dirname(dest_path)
            if dest_dir and dest_dir not in created_dirs:
                try:
                    makedirs(dest_dir, exist_ok=True)
                    created_dirs.add(dest_dir)
                except Exception:
                    continue
            try:
                dest_mtime = int(os.stat(dest_path).st_mtime_ns)
                if dest_mtime > int(src_mtime):
                    continue
            except Exception:
                pass
            try:
                copy2(src_path, dest_path)
                did_copy = True
            except Exception:
                continue
        return did_copy
