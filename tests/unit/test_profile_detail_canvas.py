import tkinter as tk
import unittest
from types import SimpleNamespace

from src.apps.profile_detail_canvas import WIDE_PAIR_MIN_WIDTH, ProfileDetailCanvas


class ProfileDetailCanvasTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless host
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.detail = ProfileDetailCanvas(tk, self.root, bg="#FFFFFF")
        self.vars = {}
        for key, label in (
            ("captured_at", "최근 확인 시각"),
            ("remaining_credit", "남은 크레딧"),
            ("five_hour_limit", "5시간 사용 한도"),
            ("five_hour_limit_reset_at", "5시간 한도 초기화"),
            ("weekly_limit", "주간 사용 한도"),
            ("weekly_limit_reset_at", "주간 한도 초기화"),
        ):
            var = tk.StringVar(master=self.root, value="-")
            self.vars[key] = var
            self.detail.add(key, label, var, wraplength=200)

    def tearDown(self) -> None:
        root = getattr(self, "root", None)
        if root is not None:
            root.destroy()

    def _resize(self, width: int) -> None:
        self.detail._on_configure(SimpleNamespace(width=width))

    def _x(self, item: int) -> int:
        return int(self.detail.canvas.coords(item)[0])

    def _y(self, item: int) -> int:
        return int(self.detail.canvas.coords(item)[1])

    def _height(self) -> int:
        return int(self.detail.canvas.cget("height"))

    def test_wide_layout_aligns_values_in_two_pair_columns(self) -> None:
        self._resize(WIDE_PAIR_MIN_WIDTH + 100)
        cells = self.detail.cells

        left = cells[0::2]
        right = cells[1::2]
        # 같은 열의 이름 오른쪽 끝과 값 시작이 모든 행에서 같아야 한다.
        self.assertEqual(len({self._x(cell.label_item) for cell in left}), 1)
        self.assertEqual(len({self._x(cell.value_item) for cell in left}), 1)
        self.assertEqual(len({self._x(cell.value_item) for cell in right}), 1)
        self.assertGreater(self._x(right[0].label_item), self._x(left[0].value_item))
        # 한 행의 두 지표는 같은 y에 놓인다.
        for first, second in zip(left, right):
            self.assertEqual(self._y(first.value_item), self._y(second.value_item))

    def test_narrow_layout_stacks_one_pair_per_row(self) -> None:
        self._resize(WIDE_PAIR_MIN_WIDTH - 200)

        cells = self.detail.cells
        self.assertEqual(len({self._x(cell.value_item) for cell in cells}), 1)
        self.assertEqual(len({self._y(cell.value_item) for cell in cells}), len(cells))

    def test_hidden_rows_release_height_without_moving_columns(self) -> None:
        self._resize(WIDE_PAIR_MIN_WIDTH + 100)
        cells = self.detail.cells
        before_height = self._height()
        before_value_x = self._x(cells[0].value_item)

        # 2열 배치에서 한 행을 이루는 두 지표를 숨긴다.
        cells[2].grid_remove()
        cells[3].grid_remove()
        self.detail.layout()

        self.assertLess(self._height(), before_height)
        self.assertEqual(self._x(cells[0].value_item), before_value_x)
        self.assertEqual(self.detail.canvas.itemcget(cells[2].value_item, "state"), "hidden")

        cells[2].grid()
        cells[3].grid()
        self.detail.layout()
        self.assertEqual(self._height(), before_height)

    def test_value_updates_redraw_text_and_grow_wrapped_rows(self) -> None:
        self._resize(WIDE_PAIR_MIN_WIDTH - 200)
        before_height = self._height()
        cell = self.detail.cells[1]

        self.vars["remaining_credit"].set("US$1,234,567,890.12 " * 6)
        self.detail.layout()

        self.assertEqual(
            self.detail.canvas.itemcget(cell.value_item, "text"),
            "US$1,234,567,890.12 " * 6,
        )
        self.assertGreater(self._height(), before_height)
        # 줄바꿈 폭은 할당된 값 열 폭을 넘지 않는다.
        box = self.detail.canvas.bbox(cell.value_item)
        self.assertLessEqual(box[2], WIDE_PAIR_MIN_WIDTH - 200 + 1)

    def test_requested_width_never_exceeds_allotted_width(self) -> None:
        self.vars["remaining_credit"].set("매우 긴 값 " * 20)
        for width in (360, 520, WIDE_PAIR_MIN_WIDTH + 50):
            self._resize(width)
            self.assertLessEqual(int(self.detail.canvas.cget("width")), width)

    def test_requested_width_stays_within_width_when_values_fill_columns(self) -> None:
        # 값이 줄바꿈되어 열 폭을 꽉 채우고 긴 줄이 캔버스 폭을 채워도
        # 요구 폭은 할당 폭을 넘지 않는다.
        self.detail.add_line(section="bottom", text="경로 " * 200, wraplength=300)
        for key in self.vars:
            self.vars[key].set("가나다라 " * 40)
        for width in (400, 500, WIDE_PAIR_MIN_WIDTH + 1, 900):
            self._resize(width)
            self.assertLessEqual(int(self.detail.canvas.cget("width")), width)

    def test_unknown_width_layout_keeps_pairs_apart(self) -> None:
        self.vars["captured_at"].set("2026-09-23 06:12:31 UTC")
        self.detail.layout()

        cells = self.detail.cells
        first_value_box = self.detail.canvas.bbox(cells[0].value_item)
        second_label_box = self.detail.canvas.bbox(cells[1].label_item)
        # 첫 <Configure> 전에도 둘째 쌍의 이름은 첫째 값이 끝난 뒤에 놓인다.
        self.assertGreater(second_label_box[0], first_value_box[2])

    def test_lines_wrap_to_canvas_width_and_bind_clicks(self) -> None:
        clicks = []
        status = tk.StringVar(master=self.root, value="조회 상태: -")
        top = self.detail.add_line(section="top", variable=status, wraplength=260)
        link = self.detail.add_line(
            section="bottom",
            text="설정 파일: C:\\settings.json",
            fill="#2563EB",
            wraplength=300,
            on_click=lambda: clicks.append(True),
        )
        self._resize(480)

        # 상태 줄은 표 위, 경로 줄은 표 아래에 놓인다.
        first_value = self.detail.cells[0].value_item
        self.assertLess(self._y(top), self._y(first_value))
        self.assertGreater(self._y(link), self._y(self.detail.cells[-1].value_item))
        # 줄은 기존 Label과 같은 좌우 안쪽 여백(3px)을 뺀 캔버스 폭에서 줄바꿈한다.
        self.assertEqual(int(self.detail.canvas.itemcget(top, "width")), 480 - 6)

        status.set("조회 상태: 대기 중")
        self.assertEqual(self.detail.canvas.itemcget(top, "text"), "조회 상태: 대기 중")

        self.assertIn("<Button-1>", self.detail.canvas.tag_bind(link))
        self.assertEqual(self.detail.canvas.tag_bind(top), ())
        self.assertTrue(getattr(self.detail.canvas, "_windows_supporter_wraps", False))

    def test_one_native_widget_per_card_detail(self) -> None:
        self.detail.add_line(section="top", text="값 상태: -", wraplength=260)
        self.detail.add_line(section="bottom", text="상태 파일: -", wraplength=300)

        self.assertEqual(self.root.winfo_children(), [self.detail.canvas])


if __name__ == "__main__":
    unittest.main()
