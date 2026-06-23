from __future__ import annotations

import queue
import subprocess
import threading
import time
from typing import Any, Callable

from src.utils.subprocess_utils import build_no_window_subprocess_kwargs, run_no_window


def run_no_window_with_progress(
    argv: list[str],
    *,
    subprocess_module=subprocess,
    progress_ui: Any | None = None,
    progress_line_callback: Callable[[str], None] | None = None,
    pump_interval_seconds: float = 0.1,
    **kwargs: Any,
) -> Any:
    popen_factory = getattr(subprocess_module, "Popen", None)
    if callable(popen_factory) and bool(kwargs.get("capture_output")) and bool(kwargs.get("text")):
        return _run_popen_with_output_progress(
            argv,
            subprocess_module=subprocess_module,
            progress_ui=progress_ui,
            progress_line_callback=progress_line_callback,
            pump_interval_seconds=pump_interval_seconds,
            kwargs=kwargs,
        )
    return _run_threaded_with_progress(
        argv,
        subprocess_module=subprocess_module,
        progress_ui=progress_ui,
        pump_interval_seconds=pump_interval_seconds,
        kwargs=kwargs,
    )


def _run_popen_with_output_progress(
    argv: list[str],
    *,
    subprocess_module: Any,
    progress_ui: Any | None,
    progress_line_callback: Callable[[str], None] | None,
    pump_interval_seconds: float,
    kwargs: dict[str, Any],
) -> Any:
    run_kwargs = dict(kwargs)
    timeout = run_kwargs.pop("timeout", None)
    check = bool(run_kwargs.pop("check", False))
    run_kwargs.pop("capture_output", None)
    run_kwargs.setdefault("stdout", getattr(subprocess_module, "PIPE", subprocess.PIPE))
    run_kwargs.setdefault("stderr", getattr(subprocess_module, "STDOUT", subprocess.STDOUT))
    for key, value in build_no_window_subprocess_kwargs(subprocess_module).items():
        run_kwargs.setdefault(key, value)

    process = subprocess_module.Popen(argv, **run_kwargs)
    output_parts: list[str] = []
    deadline = time.monotonic() + float(timeout) if timeout is not None and float(timeout) > 0 else None
    line_queue: queue.Queue[Any] = queue.Queue()
    reader_done = object()
    _start_output_reader(getattr(process, "stdout", None), line_queue, reader_done)

    output_complete = False
    while True:
        if deadline is not None and time.monotonic() > deadline:
            killer = getattr(process, "kill", None)
            if callable(killer):
                killer()
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        output_complete = _drain_output_queue(
            line_queue,
            reader_done,
            output_parts=output_parts,
            output_complete=output_complete,
            progress_ui=progress_ui,
            progress_line_callback=progress_line_callback,
        )
        poll = getattr(process, "poll", None)
        returncode = poll() if callable(poll) else getattr(process, "returncode", None)
        if returncode is not None and output_complete:
            break
        _pump(progress_ui)
        time.sleep(max(0.01, float(pump_interval_seconds)))

    wait = getattr(process, "wait", None)
    returncode = getattr(process, "returncode", None)
    if callable(wait) and returncode is None:
        returncode = wait(timeout=0)
    returncode = int(returncode if returncode is not None else 0)
    stdout_text = "".join(output_parts)
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, argv, output=stdout_text)
    return subprocess.CompletedProcess(argv, returncode, stdout_text, "")


def _start_output_reader(stdout: Any, line_queue: queue.Queue[Any], reader_done: object) -> None:
    def read_output() -> None:
        if stdout is None:
            line_queue.put(reader_done)
            return
        try:
            while True:
                line = stdout.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode(errors="replace")
                line_queue.put(str(line))
        except Exception:
            pass
        finally:
            line_queue.put(reader_done)

    threading.Thread(target=read_output, daemon=True).start()


def _drain_output_queue(
    line_queue: queue.Queue[Any],
    reader_done: object,
    *,
    output_parts: list[str],
    output_complete: bool,
    progress_ui: Any | None,
    progress_line_callback: Callable[[str], None] | None,
) -> bool:
    while True:
        try:
            item = line_queue.get_nowait()
        except queue.Empty:
            return output_complete
        if item is reader_done:
            output_complete = True
            continue
        output_parts.append(str(item))
        if callable(progress_line_callback):
            progress_line_callback(str(item))
        _pump(progress_ui)


def _run_threaded_with_progress(
    argv: list[str],
    *,
    subprocess_module: Any,
    progress_ui: Any | None,
    pump_interval_seconds: float,
    kwargs: dict[str, Any],
) -> Any:
    holder: dict[str, Any] = {}

    def worker() -> None:
        try:
            holder["result"] = run_no_window(
                argv,
                subprocess_module=subprocess_module,
                **kwargs,
            )
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        _pump(progress_ui)
        thread.join(max(0.01, float(pump_interval_seconds)))
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def _pump(progress_ui: Any | None) -> None:
    if progress_ui is None:
        return
    try:
        progress_ui.pump()
    except Exception:
        pass
