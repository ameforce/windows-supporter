"""Windowless profile regions rendered by one Tk canvas.

Cards keep their own model, geometry and callbacks, not a Windows child HWND.
"""
from __future__ import annotations
from types import SimpleNamespace
from typing import Any


class BoardLabel:
    def __init__(self, board: "ProfileBoard", text: str, *, bold=False):
        self.board = board
        self.item = board.canvas.create_text(0, 0, text=text, anchor="nw",
            fill="#111827" if bold else "#6B7280",
            font=("Segoe UI", 9, "bold") if bold else ("Segoe UI", 8))

    def configure(self, **options):
        self.board.canvas.itemconfigure(self.item, **options)


class BoardGroup:
    def __init__(self, board: "ProfileBoard", side: str, title: str, hint: str):
        self.board, self.side = board, side
        self.master = board.canvas
        self.x = self.y = 0
        self.width, self.height = 700, 44
        self.outline = board.canvas.create_rectangle(0, 0, 1, 1, outline="#E5E7EB", fill="#FFFFFF")
        self.title = BoardLabel(board, title, bold=True)
        self.hint = BoardLabel(board, hint)

    def configure(self, **options):
        if "highlightbackground" in options:
            self.board.canvas.itemconfigure(self.outline, outline=options["highlightbackground"])

    def winfo_rootx(self): return self.board.canvas.winfo_rootx() + self.x
    def winfo_rooty(self): return self.board.canvas.winfo_rooty() + self.y - int(self.board.canvas.canvasy(0))
    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def winfo_reqwidth(self): return 300
    def winfo_children(self): return [r for r in self.board.regions if r.group is self]
    def cget(self, name): return 1 if name == "highlightthickness" else 0
    def grid_info(self): return {}
    def pack_info(self): return {}


class BoardIndicator:
    def __init__(self, group: BoardGroup):
        self.group = group
        self.item = group.board.canvas.create_rectangle(0, 0, 1, 1,
            fill="#2563EB", outline="", state="hidden")

    def place(self, *, x=0, y=0, height=3, **_kwargs):
        group = self.group
        group.board.canvas.coords(self.item, group.x+x, group.y+y,
                                  group.x+group.width, group.y+y+height)
        group.board.canvas.itemconfigure(self.item, state="normal")

    def place_forget(self): self.group.board.canvas.itemconfigure(self.item, state="hidden")
    def grid_remove(self): self.place_forget()
    def lift(self): self.group.board.canvas.tag_raise(self.item)


class CardRegion:
    def __init__(self, board: "ProfileBoard", profile_id: str, group: BoardGroup, row: int):
        self.board, self.profile_id, self.group, self.row = board, profile_id, group, row
        self.master = board.canvas
        self.x = self.y = 0
        self.width, self.height = 300, 1
        self.local_coords = {}
        self.rendered_coords = {}
        self.item_options = {}
        self.item_bounds = {}
        self.outline_bounds = None
        self.bindings = {}
        self.detail = None
        self.selected = False
        self.layout_callback = None
        self.layout_serial = 0
        self.layout_token = None
        self.tag = "profile-region-" + profile_id
        self._windows_supporter_unwrapped_reqwidth = 280
        self.outline = board.canvas.create_rectangle(0, 0, 1, 1,
            fill="#FFFFFF", outline="#E5E7EB", tags=(self.tag,))

    def create_text(self, x, y, **options):
        options["tags"] = (self.tag,)
        item = self.board.canvas.create_text(self.x+x, self.y+y, **options)
        self.local_coords[item] = (x, y)
        self.rendered_coords[item] = (self.x+x, self.y+y)
        self.item_options[item] = dict(options)
        return item

    def coords(self, item, *values):
        if not values:
            return self.local_coords.get(item, ())
        self.local_coords[item] = tuple(values)
        translated = tuple(v+(self.x if i % 2 == 0 else self.y) for i, v in enumerate(values))
        previous = self.rendered_coords.get(item)
        if translated == previous:
            return
        self.board.canvas.coords(item, *translated)
        self.rendered_coords[item] = translated
        box = self.item_bounds.get(item)
        if box is not None and previous is not None and len(values) == 2:
            dx, dy = translated[0]-previous[0], translated[1]-previous[1]
            self.item_bounds[item] = (box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy)
        else:
            self.item_bounds.pop(item, None)

    def itemconfigure(self, item, **options):
        previous = self.item_options.setdefault(item, {})
        changed = {key: value for key, value in options.items() if previous.get(key) != value}
        if not changed:
            return
        self.board.canvas.itemconfigure(item, **changed)
        previous.update(changed)
        if set(changed) & {"text", "width", "font", "state", "anchor", "justify"}:
            self.item_bounds.pop(item, None)

    def itemcget(self, item, option):
        if option == "text" and "text" in self.item_options.get(item, {}):
            return self.item_options[item]["text"]
        return self.board.canvas.itemcget(item, option)

    def bbox(self, item):
        if item not in self.item_bounds:
            self.item_bounds[item] = self.board.canvas.bbox(item)
        box = self.item_bounds[item]
        if box is None:
            return None
        return (box[0]-self.x, box[1]-self.y, box[2]-self.x, box[3]-self.y)

    def configure(self, **options):
        height = options.get("height")
        if height is not None and int(height) != self.height:
            self.height = max(1, int(height))
            self.board.request_layout()
        if "highlightbackground" in options:
            color = options["highlightbackground"]
            if self.selected and color == "#E5E7EB":
                color = "#2563EB"
            self.board.canvas.itemconfigure(self.outline, outline=color)
        if "cursor" in options:
            self.board.canvas.configure(cursor=options["cursor"])

    def cget(self, option):
        return {"height":self.height,"width":1,"highlightthickness":1,"wraplength":0}.get(option, "")

    def bind(self, sequence, callback, add=None):
        self.bindings[sequence] = callback

    def tag_bind(self, item, sequence, callback=None):
        if callback is None:
            return self.board.canvas.tag_bind(item, sequence)
        self.board.canvas.tag_bind(item, sequence, callback)

    def after_idle(self, callback):
        self.layout_serial += 1
        self.layout_token = f"region-{self.profile_id}-{self.layout_serial}"
        self.layout_callback = callback
        self.board.request_layout()
        return self.layout_token

    def after_cancel(self, identifier):
        if identifier == self.layout_token:
            self.layout_callback = None
            self.layout_token = None

    def grid(self, *, in_=None, row=None, **_options):
        if in_ is not None:
            self.group = in_
        if row is not None:
            self.row = int(row)
        self.board.request_layout()

    def grid_info(self): return {"in":self.group,"row":self.row,"padx":0}
    def pack_info(self): return {}
    def winfo_rootx(self): return self.board.canvas.winfo_rootx()+self.x
    def winfo_rooty(self): return self.board.canvas.winfo_rooty()+self.y-int(self.board.canvas.canvasy(0))
    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def winfo_reqwidth(self): return 280
    def winfo_children(self): return []
    def winfo_id(self): return self.board.canvas.winfo_id()
    def winfo_exists(self): return self.board.canvas.winfo_exists()
    def lift(self): self.board.canvas.tag_raise(self.tag)
    def grab_set(self): self.board.canvas.grab_set()
    def grab_release(self): self.board.canvas.grab_release()
    def focus_set(self): self.board.canvas.focus_set()

    def place_at(self, x: int, y: int, width: int):
        previous = (self.x, self.y, self.width)
        dx, dy = x-self.x, y-self.y
        if dx or dy:
            # One retained-scene translation replaces a native call per text item.
            self.board.canvas.move(self.tag, dx, dy)
            for item, coords in list(self.rendered_coords.items()):
                self.rendered_coords[item] = tuple(v+(dx if i % 2 == 0 else dy) for i, v in enumerate(coords))
            for item, box in list(self.item_bounds.items()):
                if box is not None:
                    self.item_bounds[item] = (box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy)
        self.x, self.y, self.width = x, y, width
        callback, self.layout_callback = self.layout_callback, None
        self.layout_token = None
        if self.detail is not None:
            self.detail._width = width
            if callback is not None:
                callback()
            elif width != previous[2]:
                self.detail.layout()
        bounds = (x, y, x+width, y+self.height)
        if bounds != self.outline_bounds:
            self.board.canvas.coords(self.outline, *bounds)
            self.outline_bounds = bounds


class ProfileBoard:
    def __init__(self, tk: Any, parent: Any, view: Any, assignment: dict, priority: str):
        self.view = view
        self.regions: list[CardRegion] = []
        self.width = 720
        self.pending = None
        self.laying_out = False
        self.closed = False
        self.requested_height = 0
        self.autoscroll = None
        self.pointer = None
        self.scrollregion = None
        self.selected_region = None
        self.canvas = view._scroll_canvas
        self.body = parent
        parent.bind("<Configure>", lambda event:self.request_layout(), add="+")
        self.groups = {}
        for side, title in (("left","왼쪽 영역"),("right","오른쪽 영역"),("pool","표시 안 함 (보관함)")):
            count = len(assignment.get(side, []))
            caption = f"{title} ({count}/2)" if side != "pool" else f"{title} ({count})"
            slots = "1·2번" if side == priority else "3·4번"
            hint = f"작업표시줄 {slots} 슬롯 · 제목을 끌어 교환" if side != "pool" else "끌어다 놓으면 작업표시줄에서 제외됩니다."
            self.groups[side] = BoardGroup(self, side, caption, hint)
        self.indicators = {side:BoardIndicator(group) for side,group in self.groups.items()}
        self.canvas.bind("<Configure>", self._configure, add="+")
        self.canvas.bind("<Destroy>", self._destroy, add="+")
        self.canvas.bind("<B1-Motion>", self._drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._drag_release)
        self.canvas.bind("<Escape>", self._cancel_drag)
        self.canvas.bind("<Unmap>", self._cancel_drag)
        self.canvas.bind("<Control-Up>", lambda event:self._keyboard_select(-1))
        self.canvas.bind("<Control-Down>", lambda event:self._keyboard_select(1))
        # Keyboard route to the selected card's shared action menu.
        self.canvas.bind("<Shift-F10>", lambda event:self.view._open_profile_menu_for_selection())
        self.canvas.bind("<App>", lambda event:self.view._open_profile_menu_for_selection())

    def create_region(self, profile_id: str, side: str, row: int) -> CardRegion:
        region = CardRegion(self, profile_id, self.groups[side], row)
        self.regions.append(region)
        self.request_layout()
        return region

    def _configure(self, event):
        width = int(event.width)
        if width > 1 and width != self.width:
            self.width = width
            self.request_layout()

    def request_layout(self):
        if self.closed or self.laying_out or self.pending is not None:
            return
        self.pending = self.canvas.after_idle(self.layout)

    def _layout_group(self, group: BoardGroup, x: int, y: int, width: int) -> int:
        group.x, group.y, group.width = x, y, width
        self.canvas.coords(group.title.item, x+8, y+7)
        self.canvas.coords(group.hint.item, x+8, y+25)
        self.canvas.itemconfigure(group.hint.item, width=max(1,width-16))
        hint_box = self.canvas.bbox(group.hint.item)
        row_y = max(y+47, int(hint_box[3])+8 if hint_box else y+47)
        for region in sorted((r for r in self.regions if r.group is group), key=lambda r:r.row):
            region.place_at(x+6, row_y, max(1,width-12))
            row_y += region.height+7
        group.height = max(47, row_y-y)
        self.canvas.coords(group.outline, x, y, x+width, y+group.height)
        return group.height

    def layout(self):
        self.pending = None
        if self.closed:
            return
        self.laying_out = True
        try:
            width = max(1, self.width-2)
            origin = int(self.body.winfo_reqheight())+8
            if width >= 620:
                column = (width-10)//2
                left = self._layout_group(self.groups["left"], 1, origin, column)
                right = self._layout_group(self.groups["right"], column+11, origin, width-column-10)
                next_y = origin+max(left,right)+10
            else:
                next_y = origin+self._layout_group(self.groups["left"], 1, origin, width)+10
                next_y += self._layout_group(self.groups["right"], 1, next_y, width)+8
            total = next_y+self._layout_group(self.groups["pool"], 1, next_y, width)+2
            self.requested_height = total
            region = self.canvas.bbox("all")
            if region != self.scrollregion:
                self.scrollregion = region
                self.canvas.configure(scrollregion=region)
        finally:
            self.laying_out = False

    def _keyboard_select(self, direction: int):
        order = self.view._account_order
        if not order:
            return "break"
        current = self.view._active_account_id
        index = order.index(current) if current in order else 0
        target = order[max(0,min(len(order)-1,index+direction))]
        self.view._select_profile(target)
        self.ensure_visible(target)
        return "break"

    def _destroy(self, event):
        if event.widget is not self.canvas:
            return
        self.closed = True
        self._stop_autoscroll()
        if self.pending is not None:
            try:
                self.canvas.after_cancel(self.pending)
            except Exception:
                pass
            self.pending = None
        for region in self.regions:
            callback = region.bindings.get("<Destroy>")
            if callback is not None:
                callback(SimpleNamespace(widget=region))

    def _drag_motion(self, event):
        self.pointer = (int(event.x_root), int(event.y_root))
        self.view._on_pane_drag_motion(event)
        active = self.view._drag_state or {}
        if self.autoscroll is None and active.get("active"):
            self.autoscroll = self.canvas.after(50, self._autoscroll_step)

    def _autoscroll_step(self):
        self.autoscroll = None
        if self.closed or self.view._drag_state is None or self.pointer is None:
            return
        x, y = self.pointer
        left, top = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if not left <= x <= left+width or not top-24 <= y <= top+height+24:
            return
        direction = -1 if y < top+24 else 1 if y > top+height-24 else 0
        if not direction:
            return
        previous = self.canvas.yview()
        self.canvas.yview_scroll(direction, "units")
        if self.canvas.yview() == previous:
            return
        self.view._on_pane_drag_motion(SimpleNamespace(x_root=x, y_root=y))
        self.autoscroll = self.canvas.after(50, self._autoscroll_step)

    def _stop_autoscroll(self):
        pending, self.autoscroll = self.autoscroll, None
        self.pointer = None
        if pending is not None:
            try:
                self.canvas.after_cancel(pending)
            except Exception:
                pass

    def _drag_release(self, event):
        self._stop_autoscroll()
        self.view._on_pane_drag_release(event)

    def _cancel_drag(self, event):
        self._stop_autoscroll()
        return self.view._cancel_pane_drag(event)

    def select(self, profile_id: str) -> None:
        region = next((r for r in self.regions if r.profile_id == profile_id), None)
        if region is self.selected_region:
            return
        if self.selected_region is not None:
            self.selected_region.selected = False
            self.canvas.itemconfigure(self.selected_region.outline, outline="#E5E7EB", width=1)
        self.selected_region = region
        if region is not None:
            region.selected = True
            self.canvas.itemconfigure(region.outline, outline="#2563EB", width=2)

    def ensure_visible(self, profile_id: str) -> None:
        region = next((r for r in self.regions if r.profile_id == profile_id), None)
        bounds = self.canvas.bbox("all")
        if region is None or not bounds:
            return
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        if region.y < top or region.y + min(region.height, 40) > bottom:
            fraction = (region.y - bounds[1]) / max(1, bounds[3] - bounds[1])
            self.canvas.yview_moveto(max(0.0, min(1.0, fraction)))
