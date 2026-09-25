from __future__ import annotations

from typing import Any, Callable


# 프로필 카드의 상태 줄·지표 표·경로 줄을 Label로 만들면 Codex 카드
# 하나에 네이티브 창이 30개 가까이 생긴다. Windows Tk는 첫 매핑과 창
# 크기 변경 때 네이티브 창마다 비용을 치르므로, 프로필이 늘수록 탭
# 열기와 창 크기 조절이 초 단위로 멈춘다. 이 영역 전체를 Canvas 하나의
# 텍스트 항목으로 그려 카드당 네이티브 창을 1개로 줄인다.
LABEL_FG = "#6B7280"
VALUE_FG = "#111827"
METRIC_FONT = ("Segoe UI", 9)
LINE_FONT = ("Segoe UI", 8)
# 기존 Label 배치와 같은 간격을 유지한다. Label은 글자 둘레에 테두리 2px와
# 안쪽 여백 1px(_TEXT_INSET)를 두었고, grid padx는
# 이름0 (0,6) · 값0 (6,12) · 이름1 (18,6) · 값1 (6,0), 지표 행 pady는 1이었다.
_TEXT_INSET = 3
_LABEL_VALUE_GAP = _TEXT_INSET + 6 + 6 + _TEXT_INSET
_VALUE_TRAILING_GAP = _TEXT_INSET + 12
_SECOND_PAIR_LEAD = 18 + _TEXT_INSET
_ROW_PAD = _TEXT_INSET + 1
# 기존 pady: 상태 줄 (0,1) · 지표 표 (1,3) · 경로 줄 (0,1)
_LINE_TOP = _TEXT_INSET
_LINE_ADVANCE = 2 * _TEXT_INSET + 1
_TABLE_TOP_GAP = 1
_TABLE_BOTTOM_GAP = 3
WIDE_PAIR_MIN_WIDTH = 700


class MetricCell:
    """Row handle that keeps the grid()/grid_remove() visibility contract."""

    def __init__(
        self,
        owner: "ProfileDetailCanvas",
        key: str,
        label_item: int,
        value_item: int,
        wraplength: int,
    ) -> None:
        self.owner = owner
        self.key = key
        self.label_item = label_item
        self.value_item = value_item
        self.wraplength = max(1, int(wraplength))
        self.visible = True
        self.label_width = 0

    def grid(self) -> None:
        self.owner.set_visible(self, True)
        return

    def grid_remove(self) -> None:
        self.owner.set_visible(self, False)
        return


class _TextLine:
    def __init__(self, item: int, wraplength: int) -> None:
        self.item = item
        self.wraplength = max(1, int(wraplength))


class ProfileDetailCanvas:
    def __init__(self, tk: Any, parent: Any, *, bg: str, canvas: Any = None) -> None:
        self._cells: list[MetricCell] = []
        self._top_lines: list[_TextLine] = []
        self._bottom_lines: list[_TextLine] = []
        self._width = 0
        self._layout_after_id: Any = None
        self._destroyed = False
        self._requested_size: Any = None
        self.canvas = canvas if canvas is not None else tk.Canvas(
            parent,
            bg=bg,
            highlightthickness=0,
            bd=0,
            width=1,
            height=1,
        )
        try:
            # 창 폭 측정(_pane_box_unwrapped_width)은 wraplength 라벨을
            # 줄일 수 있는 콘텐츠로 보고 제외한다. 이 영역도 좁아지면
            # 줄바꿈되므로 같은 취급을 받도록 표시한다.
            self.canvas._windows_supporter_wraps = True
        except Exception:
            pass
        try:
            self.canvas.bind("<Configure>", self._on_configure)
            self.canvas.bind("<Destroy>", self._on_destroy)
        except Exception:
            pass

    @property
    def cells(self) -> list[MetricCell]:
        return list(self._cells)

    def add_line(
        self,
        *,
        section: str,
        wraplength: int,
        text: str = "",
        variable: Any = None,
        fill: str = LABEL_FG,
        on_click: Callable[[], None] | None = None,
        font: Any = LINE_FONT,
    ) -> int:
        """Add a full-width text line above ("top") or below ("bottom") the table."""
        canvas = self.canvas
        item = canvas.create_text(
            0,
            0,
            text=self._var_text(variable) if variable is not None else str(text),
            anchor="nw",
            justify="left",
            fill=fill,
            font=font,
            width=max(1, int(wraplength)),
        )
        line = _TextLine(item, wraplength)
        (self._top_lines if section == "top" else self._bottom_lines).append(line)
        if variable is not None:
            self._trace_text(variable, item)
        if on_click is not None:
            self._bind_click(item, on_click)
        self.request_layout()
        return item

    def add(self, key: str, label: str, display_var: Any, *, wraplength: int) -> MetricCell:
        canvas = self.canvas
        label_item = canvas.create_text(
            0,
            0,
            text=str(label),
            anchor="ne",
            justify="right",
            fill=LABEL_FG,
            font=METRIC_FONT,
            width=max(90, int(wraplength) // 2),
        )
        value_item = canvas.create_text(
            0,
            0,
            text=self._var_text(display_var),
            anchor="nw",
            justify="left",
            fill=VALUE_FG,
            font=METRIC_FONT,
        )
        cell = MetricCell(self, key, label_item, value_item, wraplength)
        # 숨긴 캔버스 항목은 bbox가 비므로 이름 폭은 보이는 상태에서 미리 잰다.
        cell.label_width = self._item_size(label_item)[0]
        self._cells.append(cell)
        self._trace_text(display_var, value_item)
        self.request_layout()
        return cell

    def set_visible(self, cell: MetricCell, visible: bool) -> None:
        if cell.visible is bool(visible):
            return
        cell.visible = bool(visible)
        self.request_layout()
        return

    def _on_destroy(self, event: Any = None) -> None:
        if event is not None and getattr(event, "widget", self.canvas) is not self.canvas:
            return
        self._destroyed = True
        after_id, self._layout_after_id = self._layout_after_id, None
        if after_id is not None:
            try:
                self.canvas.after_cancel(after_id)
            except Exception:
                pass

    def request_layout(self) -> None:
        if self._destroyed:
            return
        # 같은 idle 주기의 값 변경·표시 전환을 한 번의 배치로 합친다.
        if self._layout_after_id is not None:
            return
        try:
            self._layout_after_id = self.canvas.after_idle(self._run_layout)
        except Exception:
            self._layout_after_id = None
            self.layout()
        return

    def _run_layout(self) -> None:
        self._layout_after_id = None
        self.layout()
        return

    def _on_configure(self, event: Any = None) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width <= 1 or width == self._width:
            return
        self._width = width
        self.request_layout()
        return

    def _trace_text(self, variable: Any, item: int) -> None:
        try:
            variable.trace_add(
                "write",
                lambda *_args, var=variable, target=item: self._on_text(target, var),
            )
        except Exception:
            pass
        return

    def _bind_click(self, item: int, on_click: Callable[[], None]) -> None:
        canvas = self.canvas
        try:
            canvas.tag_bind(item, "<Button-1>", lambda _event: on_click())
            canvas.tag_bind(item, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind(item, "<Leave>", lambda _event: canvas.configure(cursor=""))
        except Exception:
            pass
        return

    def _on_text(self, item: int, variable: Any) -> None:
        text = self._var_text(variable)
        try:
            if str(self.canvas.itemcget(item, "text")) == text:
                return
        except Exception:
            pass
        try:
            self.canvas.itemconfigure(item, text=text)
        except Exception:
            return
        self.request_layout()
        return

    @staticmethod
    def _var_text(variable: Any) -> str:
        try:
            return str(variable.get())
        except Exception:
            return "-"

    def _item_size(self, item: int) -> tuple[int, int]:
        try:
            box = self.canvas.bbox(item)
        except Exception:
            box = None
        if not box:
            return (0, 0)
        return (max(0, box[2] - box[0]), max(0, box[3] - box[1]))

    def label_reserve(self) -> int:
        # 숨긴 행도 포함한 모든 이름의 최대 폭을 예약해, 선택 행의 표시
        # 여부나 값 길이가 바뀌어도 열 경계가 움직이지 않게 한다.
        reserve = 0
        for cell in self._cells:
            reserve = max(reserve, cell.label_width)
        return reserve

    @staticmethod
    def pair_columns(width: int) -> int:
        return 2 if width <= 1 or width >= WIDE_PAIR_MIN_WIDTH else 1

    def _place_lines(self, lines: list[_TextLine], y: int) -> tuple[int, int]:
        natural = 0
        for line in lines:
            wrap = (
                line.wraplength
                if self._width <= 1
                else self._width - 2 * _TEXT_INSET
            )
            try:
                self.canvas.itemconfigure(line.item, width=max(1, wrap))
                self.canvas.coords(line.item, _TEXT_INSET, y + _LINE_TOP)
            except Exception:
                pass
            line_width, line_height = self._item_size(line.item)
            natural = max(natural, line_width + 2 * _TEXT_INSET)
            y += line_height + _LINE_ADVANCE
        return y, natural

    def _place_table(self, y: int) -> tuple[int, int]:
        canvas = self.canvas
        width = int(self._width or 0)
        pair_columns = self.pair_columns(width)
        label_width = self.label_reserve() + _TEXT_INSET
        fixed = label_width + _LABEL_VALUE_GAP + _VALUE_TRAILING_GAP
        if pair_columns > 1:
            fixed = (
                2 * (label_width + _LABEL_VALUE_GAP)
                + _VALUE_TRAILING_GAP
                + _SECOND_PAIR_LEAD
            )
        value_width = 0
        if width > 1:
            value_width = max(1, (width - fixed) // pair_columns)
        rows: dict[int, list[tuple[int, MetricCell]]] = {}
        for index, cell in enumerate(self._cells):
            state = "normal" if cell.visible else "hidden"
            wrap = cell.wraplength if value_width <= 0 else min(cell.wraplength, value_width)
            try:
                canvas.itemconfigure(cell.label_item, state=state)
                canvas.itemconfigure(cell.value_item, state=state, width=max(1, wrap))
            except Exception:
                pass
            rows.setdefault(index // pair_columns, []).append((index, cell))
        if value_width <= 0:
            # 첫 <Configure> 전에는 폭을 모른다. 값 열을 0으로 두면 둘째
            # 쌍이 첫째 쌍의 값 위에 겹치므로, 보이는 값의 실제 폭(줄바꿈
            # 상한 적용)으로 값 열을 잡는다.
            value_width = max(
                (
                    self._item_size(cell.value_item)[0]
                    for cell in self._cells
                    if cell.visible
                ),
                default=0,
            )
        second_origin = (
            label_width
            + _LABEL_VALUE_GAP
            + max(value_width, 0)
            + _VALUE_TRAILING_GAP
            + _SECOND_PAIR_LEAD
        )
        natural_value = 0
        for row_index in sorted(rows):
            row_cells = rows[row_index]
            height = 0
            for _index, cell in row_cells:
                if not cell.visible:
                    continue
                label_height = self._item_size(cell.label_item)[1]
                value_width_now, value_height = self._item_size(cell.value_item)
                height = max(height, label_height, value_height)
                natural_value = max(natural_value, value_width_now)
            if height <= 0:
                continue
            for index, cell in row_cells:
                origin = second_origin if index % pair_columns == 1 else 0
                try:
                    canvas.coords(cell.label_item, origin + label_width, y + _ROW_PAD)
                    canvas.coords(
                        cell.value_item,
                        origin + label_width + _LABEL_VALUE_GAP,
                        y + _ROW_PAD,
                    )
                except Exception:
                    pass
            y += height + 2 * _ROW_PAD
        natural = label_width + _LABEL_VALUE_GAP + natural_value + _VALUE_TRAILING_GAP
        return y, natural

    def layout(self) -> None:
        if self.canvas is None or self._destroyed:
            return
        y, _ = self._place_lines(self._top_lines, 0)
        if self._cells:
            y, _ = self._place_table(y + _TABLE_TOP_GAP)
            y += _TABLE_BOTTOM_GAP
        y, _ = self._place_lines(self._bottom_lines, y)
        # The grid supplies horizontal space. Wrapped text must not feed its
        # allocated width back into the requested width and trigger reflow.
        requested = (1, max(1, y))
        if requested != self._requested_size:
            self._requested_size = requested
            try:
                self.canvas.configure(height=requested[1])
            except Exception:
                pass
        return
