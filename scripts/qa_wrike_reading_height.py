"""Render owned native Tk windows to verify the timelog reading-height contract.

Uses synthetic records and real widgets. Work-area limits are injected so the
same screen can also exercise short displays without changing monitor settings.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def renderer(source_ref: str | None):
    name = "src.apps.wrike_worktime_panel"
    if source_ref:
        result = subprocess.run(
            ["git", "show", f"{source_ref}:src/apps/wrike_worktime_panel.py"],
            cwd=ROOT, capture_output=True, check=True,
        )
        source = result.stdout.decode("utf-8", "strict")
        result.stderr.decode("utf-8", "strict")
        module = types.ModuleType(name)
        module.__file__ = f"git:{source_ref}:src/apps/wrike_worktime_panel.py"
        sys.modules[name] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    else:
        source = (ROOT / "src/apps/wrike_worktime_panel.py").read_text(encoding="utf-8")
        module = importlib.import_module(name)
    return module, hashlib.sha256(source.encode("utf-8")).hexdigest()


def make_model(api):
    from src.apps.wrike_timelog_details import TimelogDayDetails, TimelogDetailRow

    monday = date(2026, 10, 12)
    rows = tuple(api.WorktimePanelDayRow(
        day, (monday + timedelta(days=i)).strftime("%m/%d"),
        (monday + timedelta(days=i)).isoformat(), 540 if i < 5 else 0,
        "Wrike 1시간 15분 · 현재 기대 1시간 46분 · 부족 31분" if i == 0
        else "목표 9시간" if i < 5 else "휴무", i == 0,
        "#DC2626" if i == 0 else "#6B7280",
    ) for i, day in enumerate(("월", "화", "수", "목", "금", "토", "일")))
    titles = ("문서 변환 결과의 글자 간격과 회귀 검증", "주간 작업 기록과 변경 사항 정리", "설치 패키지 실행 및 복구 확인", "타임로그 화면 읽기 영역 검증")
    records = tuple(TimelogDetailRow(
        monday.isoformat(), f"log-{i}", f"task-{i // 2}", 15,
        f"검증 기록 {i + 1}: 추가 상태에서도 타임로그의 내용을 안정적으로 읽을 수 있는지 확인",
        titles[i // 2], "ready",
    ) for i in range(8))
    return api.WorktimePanelModel(
        "2026-10-12 - 2026-10-18", "동기화 · 2026-10-12 10:16:12 · 방금 · fresh · Flex fresh", "fresh",
        (api.WorktimePanelLine("Flex 근무 9시간 · Flex 배정 퇴근 18:00 · Wrike 기록 1시간 15분", "#2563EB"),
         api.WorktimePanelLine("출근 08:30 · 예상 퇴근 18:30", "#111827"),
         api.WorktimePanelLine("추가 상태 3건", "#6B7280")),
        540, "08:30", False, rows,
        day_details=tuple(TimelogDayDetails(row.date_key, "available",
            120 if row.today else 0, records if row.today else ()) for row in rows),
    )


def pump(root):
    for _ in range(8):
        root.update()
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-ref")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    api, source_hash = renderer(args.source_ref)
    import tkinter as tk
    import tkinter.font as tkfont
    from scripts.native_window_capture import capture_window

    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda kind, error, tb: errors.append(f"{kind.__name__}: {error}")
    baseline = make_model(api)
    scenarios = {
        "normal": baseline,
        "clock-in": replace(baseline, prompt=api.WorktimeActivityPrompt("08:30")),
        "overtime-prompt": replace(baseline, overtime_prompt=api.WorktimeOvertimePrompt("18:30", "18:30", 120)),
        "overtime-active": replace(baseline, overtime_state=api.WorktimeOvertimeState("active", "18:30", 45, "18:30", 120)),
        "combined": replace(baseline, prompt=api.WorktimeActivityPrompt("08:30"),
            overtime_state=api.WorktimeOvertimeState("active", "18:30", 45, "18:30", 120),
            flex_pending=(api.WorktimeFlexPendingRecord("2026-10-11", "전일 초과근무", "18:00", "20:00", 120),)),
    }
    results = []
    interactions = []
    panel = None
    try:
        for width, height, scale in ((1280, 960, 100), (800, 640, 100), (608, 535, 100), (800, 640, 150)):
            root.tk.call("tk", "scaling", (96 * scale / 100) / 72)
            area = (0, 0, width, height)
            state = [baseline]
            panel = api.WorktimeQuickPanel(root, lambda: state[0], lambda: None,
                lambda: None, lambda value: True, lambda key, value: True,
                lambda: None, lambda: None, lambda value: None,
                lambda old, new: True, lambda: None, lambda: None,
                tk_module=tk, idle_timeout_ms=60_000)
            group = []
            with patch.object(api, "_work_area_for_point", return_value=area), patch.object(api, "_work_area_for_window", return_value=area):
                for name, model in scenarios.items():
                    state[0] = model
                    panel.show(activate=False)
                    pump(root)
                    window = panel._window
                    detail = panel._widgets["detail_text"]
                    footer = panel._widgets["footer"]
                    line_height = tkfont.Font(root=root, font=detail.cget("font")).metrics("linespace")
                    padding = 2 * (int(detail.cget("pady")) + int(detail.cget("highlightthickness")) + int(detail.cget("borderwidth")))
                    entry = {
                        "scenario": name, "work_area": area, "scale_percent": scale,
                        "window": [window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()],
                        "detail_height_px": detail.winfo_height(), "detail_configured_rows": int(detail.cget("height")),
                        "body_font_line_px": line_height, "visible_body_lines": (detail.winfo_height() - padding) // line_height,
                        "footer_visible": bool(footer.winfo_ismapped() and footer.winfo_height() >= footer.winfo_reqheight()
                            and footer.winfo_rooty() + footer.winfo_height() <= window.winfo_rooty() + window.winfo_height()),
                    }
                    filename = f"{width}x{height}-{scale}-{name}.png"
                    entry["capture_method"] = capture_window(window, args.output / filename)
                    entry["screenshot"] = filename
                    results.append(entry)
                    group.append(entry)
                if args.verify:
                    assert len({x["detail_height_px"] for x in group}) == 1, group
                    assert all(x["visible_body_lines"] >= 8 for x in group), group
                    assert all(x["footer_visible"] for x in group), group
                    assert all(0 <= x["window"][0] and 0 <= x["window"][1]
                        and x["window"][0] + x["window"][2] <= width
                        and x["window"][1] + x["window"][3] <= height for x in group), group
                canvas = panel._widgets.get("auxiliary_canvas")
                if canvas is not None:
                    protected_height = detail.winfo_height()
                    canvas.yview_moveto(1.0)
                    pump(root)
                    bottom_capture = f"{width}x{height}-{scale}-summary-bottom.png"
                    capture_window(window, args.output / bottom_capture)
                    bottom_view = canvas.yview()
                    # Activate only this owned test window to deliver real Tk
                    # focus events, including ancestor FocusIn notifications.
                    window.focus_force()
                    panel._widgets["clock_button"].invoke()
                    pump(root)
                    editor = panel._widgets["inline_editor"]
                    editor_top = editor.winfo_rooty() - canvas.winfo_rooty()
                    interaction = {
                        "work_area": area, "scale_percent": scale,
                        "summary_bottom_yview": bottom_view,
                        "editor_top_in_viewport": editor_top,
                        "editor_height": editor.winfo_height(),
                        "auxiliary_height": canvas.winfo_height(),
                        "detail_height_unchanged": detail.winfo_height() == protected_height,
                        "summary_screenshot": bottom_capture,
                        "editor_screenshot": f"{width}x{height}-{scale}-clock-editor.png",
                    }
                    capture_window(window, args.output / interaction["editor_screenshot"])
                    interactions.append(interaction)
                    if args.verify:
                        assert bottom_view[1] >= 0.999, interaction
                        assert editor_top >= 0, interaction
                        assert editor_top + editor.winfo_height() <= canvas.winfo_height(), interaction
                        assert interaction["detail_height_unchanged"], interaction
            panel.destroy()
            panel = None
        assert not errors, errors
    finally:
        if panel is not None:
            panel.destroy()
        root.destroy()
        report = {"source_ref": args.source_ref, "source_sha256": source_hash,
            "synthetic_data": True, "native_tk": True, "results": results,
            "interaction_checks": interactions, "callback_errors": errors}
        (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
