from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip
import win32clipboard
import win32con
import math

WRIKE_CLIPBOARD_VARIANTS = (
    "plain_only_url",
    "plain_bulleted_url",
    "plain+html_text_url",
    "plain+html_anchor_url",
    "plain+html_anchor_url_flat_ql_indent",
    "plain+html_anchor_url_flat_stringify",
    "plain+html_anchor_url_nested_stringify",
    "control_with_trailing_space_or_newline",
)

DEFAULT_WRIKE_CLIPBOARD_VARIANT = "plain+html_anchor_url_flat_ql_indent"

WRIKE_CLIPBOARD_VARIANT_OPTIONS = {
    "plain_only_url": {
        "plain_url_mode": "standalone",
        "html_mode": "omit",
        "html_list_mode": "nested_simple",
        "force_trailing_space": False,
    },
    "plain_bulleted_url": {
        "plain_url_mode": "bulleted",
        "html_mode": "omit",
        "html_list_mode": "nested_simple",
        "force_trailing_space": False,
    },
    "plain+html_text_url": {
        "plain_url_mode": "standalone",
        "html_mode": "text",
        "html_list_mode": "nested_simple",
        "force_trailing_space": False,
    },
    "plain+html_anchor_url": {
        "plain_url_mode": "standalone",
        "html_mode": "anchor",
        "html_list_mode": "nested_simple",
        "force_trailing_space": False,
    },
    "plain+html_anchor_url_flat_ql_indent": {
        "plain_url_mode": "standalone",
        "html_mode": "anchor",
        "html_list_mode": "flat_ql_indent",
        "force_trailing_space": False,
    },
    "plain+html_anchor_url_flat_stringify": {
        "plain_url_mode": "standalone",
        "html_mode": "anchor",
        "html_list_mode": "flat_stringify",
        "force_trailing_space": False,
    },
    "plain+html_anchor_url_nested_stringify": {
        "plain_url_mode": "standalone",
        "html_mode": "anchor",
        "html_list_mode": "nested_stringify",
        "force_trailing_space": False,
    },
    "control_with_trailing_space_or_newline": {
        "plain_url_mode": "standalone",
        "html_mode": "omit",
        "html_list_mode": "nested_simple",
        "force_trailing_space": True,
    },
}


class Notion:
    def __init__(self) -> None:
        self.__lib = LibConnector()
        self.__last_wrike_payload_key = None
        self.__last_wrike_payload_bundle = None
        return

    def is_notion_running(self):
        notion_pid_list = []
        for process in self.__lib.psutil.process_iter(attrs=["pid", "name"]):
            if process.info["name"] and "Notion" in process.info["name"]:
                notion_pid_list.append(process.info["pid"])
        if len(notion_pid_list) == 0:
            return None
        return notion_pid_list

    def get_active_window_pid(self):
        hwnd = self.__lib.win32gui.GetForegroundWindow()
        if hwnd:
            _, pid = self.__lib.win32process.GetWindowThreadProcessId(hwnd)
            return pid
        return None

    def is_notion_active(self) -> bool:
        active_pid = self.get_active_window_pid()
        if not active_pid:
            return False
        try:
            process_name = self.__lib.psutil.Process(active_pid).name()
        except Exception:
            return False
        return bool(process_name and "Notion" in process_name)

    def get_date(self) -> str:
        now = self.__lib.datetime.now()
        formatted_date = now.strftime("[%m/%d] ")
        return formatted_date

    def action(self, root) -> None:
        transformed_text = self.get_date()
        backup_data = self.__lib.pyperclip.paste()
        self.__lib.pyperclip.copy(transformed_text)
        self.__lib.pyautogui.keyUp('ctrl')
        self.__lib.pyautogui.hotkey('ctrl', 'v')
        self.__lib.pyperclip.copy(backup_data)
        tooltip = ToolTip(root,
                          f"성공적으로 삽입됨: {transformed_text}\n"
                          f"성공적으로 복구됨: {backup_data}",
                          bind_events=False)
        tooltip.show_tooltip()
        root.after(1500, tooltip.hide_tooltip)
        return

    def rewrite_clipboard_for_slack(self, root) -> None:
        self.rewrite_clipboard_for_wrike(root)
        return

    def rewrite_clipboard_for_wrike(self, root) -> None:
        try:
            raw = self.__lib.pyperclip.paste()
        except Exception:
            return
        payload = self.build_wrike_clipboard_payload(str(raw or ""))
        if payload is None:
            return
        plain_text, cf_html, html_bytes = payload

        did_write = False
        try:
            self._set_clipboard_text_and_html(plain_text, cf_html, html_bytes)
            did_write = True
        except Exception:
            did_write = False

        if did_write:
            try:
                tooltip = ToolTip(root, "Wrike용 치환 완료", bind_events=False)
                tooltip.show_tooltip()
                root.after(800, tooltip.hide_tooltip)
            except Exception:
                pass
        return

    def get_wrike_clipboard_variants(self) -> tuple[str, ...]:
        return WRIKE_CLIPBOARD_VARIANTS

    def get_default_wrike_clipboard_variant(self) -> str:
        return DEFAULT_WRIKE_CLIPBOARD_VARIANT

    def get_wrike_clipboard_variant_options(self, variant: str | None = None) -> dict[str, object]:
        key = self._resolve_wrike_clipboard_variant(variant)
        return dict(WRIKE_CLIPBOARD_VARIANT_OPTIONS[key])

    def _resolve_wrike_clipboard_variant(self, variant: str | None = None) -> str:
        key = str(variant or DEFAULT_WRIKE_CLIPBOARD_VARIANT).strip()
        if key not in WRIKE_CLIPBOARD_VARIANT_OPTIONS:
            raise ValueError(f"unknown wrike clipboard variant: {key}")
        return key

    def build_wrike_clipboard_payload(
        self,
        raw_text: str,
        variant: str | None = None,
    ) -> tuple[str, bytes | None, bytes | None] | None:
        bundle = self.build_wrike_clipboard_payload_bundle(raw_text, variant=variant)
        if bundle is None:
            return None
        return bundle["plain_text"], bundle["cf_html"], bundle["html_bytes"]

    def build_wrike_clipboard_payload_bundle(
        self,
        raw_text: str,
        variant: str | None = None,
    ) -> dict[str, object] | None:
        raw = self._normalize_newlines(str(raw_text or ""))
        if not raw.strip():
            return None
        variant_key = self._resolve_wrike_clipboard_variant(variant)
        cache_key = (raw, variant_key)

        if (
            cache_key == self.__last_wrike_payload_key
            and self.__last_wrike_payload_bundle is not None
        ):
            return dict(self.__last_wrike_payload_bundle)

        options = self.get_wrike_clipboard_variant_options(variant_key)
        plain_url_mode = str(options.get("plain_url_mode", "standalone") or "standalone")
        html_mode = str(options.get("html_mode", "omit") or "omit")
        html_list_mode = str(options.get("html_list_mode", "nested_simple") or "nested_simple")
        force_trailing_space = bool(options.get("force_trailing_space", False))

        is_list = False
        try:
            is_list = bool(self.__lib.re.search(r"(?m)^\s*(?:-|\*|\u2022)\s+", raw))
        except Exception:
            is_list = False

        if is_list:
            nodes = self._parse_bullet_tree(raw)
            if not nodes:
                return None
            groups = self._build_wrike_product_groups(nodes)
            if not groups:
                return None
            plain_text = self._render_wrike_plain_sections(
                groups,
                plain_url_mode=plain_url_mode,
                force_trailing_space=force_trailing_space,
            )
            html_fragment = ""
            if html_mode != "omit":
                html_fragment = self._render_wrike_html_sections(
                    groups,
                    html_url_mode=html_mode,
                    html_list_mode=html_list_mode,
                )
        else:
            plain_lines = self._split_wrike_plain_lines(
                self._inline_to_plain_wrike(raw),
                force_trailing_space=force_trailing_space,
            )
            if not plain_lines:
                return None
            plain_text = "\r\n".join(plain_lines) + "\r\n"
            html_fragment = ""
            if html_mode != "omit":
                html_fragment = f"<div>{self._inline_to_html_wrike(raw, html_url_mode=html_mode)}</div>"

        cf_html = None
        html_bytes = None
        if html_mode != "omit":
            cf_html, html_bytes = self._build_cf_html_payload(
                html_fragment,
                source_url="https://www.wrike.com/",
            )
        bundle = {
            "variant": variant_key,
            "plain_text": plain_text,
            "html_fragment": html_fragment,
            "cf_html": cf_html,
            "html_bytes": html_bytes,
            "plain_url_mode": plain_url_mode,
            "html_mode": html_mode,
            "html_list_mode": html_list_mode,
            "force_trailing_space": force_trailing_space,
        }
        self.__last_wrike_payload_key = cache_key
        self.__last_wrike_payload_bundle = dict(bundle)
        return bundle

    def _normalize_newlines(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _normalize_heading_label(self, text: str) -> str:
        try:
            return self.__lib.re.sub(r"\s+", " ", str(text or "")).strip()
        except Exception:
            return str(text or "").strip()

    def _extract_bold_heading_text(self, text: str) -> str | None:
        t = str(text or "").strip()
        if not t.startswith("**") or not t.endswith("**"):
            return None
        inner = t[2:-2].strip()
        if not inner:
            return None
        return inner

    def _is_status_heading_text(self, text: str) -> bool:
        label = self._normalize_heading_label(text).replace(" ", "")
        return label in {"진행중", "진행완료"}

    def _is_meeting_heading_text(self, text: str) -> bool:
        label = self._normalize_heading_label(text).replace(" ", "")
        return label == "회의" or label.startswith("회의내용")

    def _is_overall_title_text(self, text: str) -> bool:
        label = self._normalize_heading_label(text).replace(" ", "")
        return label == "진행업무현황"

    def _normalize_product_name(self, text: str) -> str:
        label = self._normalize_heading_label(text)
        if not label:
            return ""
        lower = label.lower()
        if "webrender" in lower:
            return "WebRender"
        if "pdfio" in lower:
            return "pdfio"
        if "pdfcmd" in lower:
            return "pdfcmd"
        if "securereader" in lower:
            return "SecureReader"
        if "국세청" in label:
            return "국세청 - 전자서고"
        if "인프라" in label:
            return "인프라"
        if "내부업무" in label:
            return "내부업무"
        if "cas" in lower:
            return "CAS"
        return label

    def _infer_product_from_text(self, text: str) -> str:
        label = self._normalize_heading_label(text)
        if not label:
            return ""
        lower = label.lower()
        if "webrender" in lower:
            return "WebRender"
        if "pdfio" in lower:
            return "pdfio"
        if "pdfcmd" in lower:
            return "pdfcmd"
        if "securereader" in lower:
            return "SecureReader"
        if "국세청" in label:
            return "국세청 - 전자서고"
        if "인프라" in label:
            return "인프라"
        if "내부업무" in label:
            return "내부업무"
        if "cas" in lower:
            return "CAS"
        return ""

    def _clone_node(self, node):
        if not node:
            return ["", []]
        text = str(node[0]) if len(node) > 0 else ""
        children = node[1] if len(node) > 1 and isinstance(node[1], list) else []
        copied_children = [self._clone_node(child) for child in children]
        return [text, copied_children]

    def _build_wrike_product_groups(self, nodes) -> dict[str, list]:
        groups: dict[str, list] = {}

        def _push(product: str, node) -> None:
            key = self._normalize_product_name(product) or "기타"
            if key not in groups:
                groups[key] = []
            groups[key].append(self._clone_node(node))

        def _walk(cur_nodes, current_product: str | None) -> None:
            for node in cur_nodes:
                text = str(node[0]) if node and len(node) > 0 else ""
                children = node[1] if node and len(node) > 1 and isinstance(node[1], list) else []
                source = text.strip()
                if not source and not children:
                    continue

                heading_text = self._extract_bold_heading_text(source)
                check_text = heading_text if heading_text is not None else source

                if self._is_overall_title_text(check_text):
                    continue
                if self._is_meeting_heading_text(check_text):
                    continue
                if self._is_status_heading_text(check_text):
                    if children:
                        _walk(children, current_product)
                    continue
                if heading_text is not None and children:
                    next_product = self._normalize_product_name(heading_text)
                    _walk(children, next_product or current_product)
                    continue

                product = current_product or self._infer_product_from_text(source)
                if children and current_product is None:
                    try:
                        has_url = bool(self.__lib.re.search(r"https?://", source))
                    except Exception:
                        has_url = False
                    if not product:
                        _walk(children, None)
                        continue
                    if not has_url:
                        _walk(children, product)
                        continue
                _push(product or "기타", node)

        _walk(nodes, None)
        return groups

    def _try_parse_bullet_line(self, line: str):
        indent = 0
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "\t":
                indent += 4
                i += 1
                continue
            if ch == " " or ch == "\u00A0":
                indent += 1
                i += 1
                continue
            try:
                if ch.isspace():
                    indent += 1
                    i += 1
                    continue
            except Exception:
                pass
            break
        rest = line[i:]
        if rest.startswith("- "):
            return indent, rest[2:].rstrip()
        if rest.startswith("\u2022 "):
            return indent, rest[2:].rstrip()
        if rest.startswith("* "):
            return indent, rest[2:].rstrip()
        return None, None

    def _parse_bullet_tree(self, raw: str):
        lines = raw.split("\n")
        indent_unit = self._detect_indent_unit(lines)
        roots = []
        stack = []
        last_node = None

        for line in lines:
            if not line.strip():
                if last_node is not None and last_node[0] and not last_node[0].endswith("\n"):
                    last_node[0] += "\n"
                continue

            indent, content = self._try_parse_bullet_line(line)
            if indent is None:
                if last_node is not None:
                    last_node[0] += "\n" + line.strip()
                else:
                    node = [line.strip(), []]
                    roots.append(node)
                    stack = [node]
                    last_node = node
                continue

            level = 0
            try:
                if indent_unit > 0:
                    level = int(indent // indent_unit)
            except Exception:
                level = 0

            node = [content.strip(), []]
            if level <= 0 or not stack:
                roots.append(node)
                stack = [node]
                last_node = node
                continue

            if level > len(stack):
                level = len(stack)
            parent = stack[level - 1]
            parent[1].append(node)
            stack = stack[:level] + [node]
            last_node = node

        return roots

    def _detect_indent_unit(self, lines) -> int:
        indents = []
        for line in lines:
            indent, _ = self._try_parse_bullet_line(line)
            if indent is not None and indent > 0:
                indents.append(indent)

        if not indents:
            return 4

        g = 0
        for v in indents:
            try:
                g = math.gcd(g, int(v))
            except Exception:
                pass

        if g >= 2:
            return g
        return min(indents) if min(indents) > 0 else 4

    def _strip_md_bold_markers(self, text: str) -> str:
        return text.replace("**", "")

    def _escape_html(self, s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _md_bold_to_html(self, escaped_text: str) -> str:
        try:
            return self.__lib.re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped_text)
        except Exception:
            return escaped_text

    def _convert_markdown_links_to_url_only(self, text: str) -> str:
        out = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "[":
                j = i + 1
                depth = 1
                while j < n and depth > 0:
                    c = text[j]
                    if c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                    j += 1

                if depth == 0 and j < n and text[j] == "(":
                    k = j + 1
                    while k < n and text[k] != ")":
                        k += 1
                    if k < n:
                        url = text[j + 1 : k].strip()
                        if url:
                            out.append(url)
                            i = k + 1
                            continue

            out.append(text[i])
            i += 1
        return "".join(out)

    def _extract_urls_from_text(self, text: str) -> list[str]:
        urls: list[str] = []
        try:
            for match in self.__lib.re.finditer(r"https?://[^\s<>()]+", str(text or "")):
                token = str(match.group(0) or "").rstrip(".,);")
                if token:
                    urls.append(token)
        except Exception:
            return []
        return urls

    def _is_wrike_task_url(self, url: str) -> bool:
        token = str(url or "").strip()
        if not token:
            return False
        try:
            if self.__lib.re.search(r"^https?://(?:www\.)?wrike\.com/open\.htm\?id=\d+", token):
                return True
            if self.__lib.re.search(r"^https?://(?:www\.)?wrike\.com/workspace\.htm", token):
                return True
            if self.__lib.re.search(r"^https?://(?:www\.)?wrike\.com/.*/task/[\w-]+", token):
                return True
        except Exception:
            return False
        return False

    def _extract_first_wrike_task_url(self, text: str) -> str:
        converted = self._convert_markdown_links_to_url_only(str(text or ""))
        for token in self._extract_urls_from_text(converted):
            if self._is_wrike_task_url(token):
                return token
        return ""

    def _split_wrike_plain_lines(self, text: str, force_trailing_space: bool = False) -> list[str]:
        lines: list[str] = []
        normalized = self._normalize_newlines(str(text or ""))
        for part in normalized.split("\n"):
            stripped = str(part or "").strip()
            if not stripped:
                continue
            if force_trailing_space and self._is_wrike_task_url(stripped):
                stripped = stripped + " "
            lines.append(stripped)
        return lines

    def _inline_to_plain_wrike(self, text: str) -> str:
        wrike_url = self._extract_first_wrike_task_url(text)
        if wrike_url:
            # Wrike auto-renders task metadata when a line is only a task URL.
            return wrike_url
        converted = self._convert_markdown_links_to_url_only(str(text or ""))
        return self._strip_md_bold_markers(converted)

    def _build_wrike_anchor_html(self, url: str) -> str:
        url_attr = self._escape_html(url)
        label_html = self._escape_html(url)
        return (
            f'<a target="_blank" class="c-link c-link--underline" '
            f'data-stringify-link="{url_attr}" url="{url_attr}" data-sk="tooltip_parent" '
            f'href="{url_attr}" rel="noopener noreferrer">{label_html}</a>'
        )

    def _inline_to_html_wrike(self, text: str, html_url_mode: str = "text") -> str:
        wrike_url = self._extract_first_wrike_task_url(text)
        if wrike_url:
            if html_url_mode == "anchor":
                return self._build_wrike_anchor_html(wrike_url)
            return self._escape_html(wrike_url)
        plain = self._inline_to_plain_wrike(text)
        converted = self._md_bold_to_html(self._escape_html(plain))
        return converted.replace("\n", "<br/>")

    def _render_wrike_plain_tree(
        self,
        nodes,
        depth: int,
        out_lines: list[str],
        plain_url_mode: str = "standalone",
        force_trailing_space: bool = False,
    ) -> None:
        for node in nodes:
            text = self._inline_to_plain_wrike(node[0] if node else "")
            lines = self._split_wrike_plain_lines(text, force_trailing_space=force_trailing_space)
            indent = "  " * max(0, int(depth))
            if lines:
                first_line = lines[0]
                if plain_url_mode == "standalone" and self._is_wrike_task_url(first_line.strip()):
                    out_lines.append(f"{indent}{first_line}")
                else:
                    out_lines.append(f"{indent}- {first_line}")
                for extra in lines[1:]:
                    out_lines.append(f"{indent}  {extra}")
            children = node[1] if node and len(node) > 1 and isinstance(node[1], list) else []
            if children:
                self._render_wrike_plain_tree(
                    children,
                    depth + 1,
                    out_lines,
                    plain_url_mode=plain_url_mode,
                    force_trailing_space=force_trailing_space,
                )

    def _render_wrike_plain_sections(
        self,
        groups: dict[str, list],
        plain_url_mode: str = "standalone",
        force_trailing_space: bool = False,
    ) -> str:
        out_lines = []
        for product, nodes in groups.items():
            heading = self._normalize_heading_label(product)
            if heading:
                out_lines.append(heading)
            self._render_wrike_plain_tree(
                nodes,
                0,
                out_lines,
                plain_url_mode=plain_url_mode,
                force_trailing_space=force_trailing_space,
            )
            out_lines.append("")
        if not out_lines:
            return ""
        return "\r\n".join(out_lines).rstrip() + "\r\n"

    def _iter_wrike_html_items(self, nodes, depth: int = 0):
        for node in nodes:
            text = ""
            if node and len(node) > 0:
                text = str(node[0]).rstrip("\n")
            yield depth, text
            children = node[1] if node and len(node) > 1 and isinstance(node[1], list) else []
            if children:
                yield from self._iter_wrike_html_items(children, depth + 1)

    def _render_wrike_html_flat_list(
        self,
        nodes,
        html_url_mode: str = "text",
        stringify: bool = False,
    ) -> str:
        if not nodes:
            return ""
        ul_attrs = []
        if stringify:
            ul_attrs.extend(
                [
                    'data-stringify-type="unordered-list"',
                    'data-list-tree="true"',
                    'class="p-rich_text_list p-rich_text_list__bullet p-rich_text_list--nested"',
                    'data-indent="0"',
                    'data-border="0"',
                ]
            )
        parts = [f"<ul{' ' + ' '.join(ul_attrs) if ul_attrs else ''}>"]
        for depth, text in self._iter_wrike_html_items(nodes, depth=0):
            wrike_depth = max(1, int(depth) + 1)
            li_attrs = [f'class="ql-indent-{wrike_depth}"']
            if stringify:
                li_attrs.extend(
                    [
                        f'data-stringify-indent="{wrike_depth}"',
                        'data-stringify-border="0"',
                    ]
                )
            parts.append(f"<li {' '.join(li_attrs)}>")
            parts.append(self._inline_to_html_wrike(text, html_url_mode=html_url_mode))
            parts.append("</li>")
        parts.append("</ul>")
        return "".join(parts)

    def _render_wrike_html_nested_stringify_list(self, nodes, html_url_mode: str = "text") -> str:
        list_class = "p-rich_text_list p-rich_text_list__bullet p-rich_text_list--nested"

        def _emit_list(cur_nodes, depth: int) -> str:
            if not cur_nodes:
                return ""
            d = int(depth)
            wrike_depth = max(0, d)
            parts = [
                f'<ul data-stringify-type="unordered-list" data-list-tree="true" '
                f'class="{list_class}" data-indent="{wrike_depth}" data-border="0">'
            ]
            for node in cur_nodes:
                text = ""
                if node and len(node) > 0:
                    text = str(node[0]).rstrip("\n")
                li_depth = max(1, d + 1)
                parts.append(
                    f'<li class="ql-indent-{li_depth}" '
                    f'data-stringify-indent="{li_depth}" data-stringify-border="0">'
                )
                parts.append(self._inline_to_html_wrike(text, html_url_mode=html_url_mode))
                children = node[1] if node and len(node) > 1 and isinstance(node[1], list) else []
                if children:
                    parts.append(_emit_list(children, d + 1))
                parts.append("</li>")
            parts.append("</ul>")
            return "".join(parts)

        return _emit_list(nodes, 0)

    def _render_wrike_html_list(
        self,
        nodes,
        html_url_mode: str = "text",
        html_list_mode: str = "nested_simple",
    ) -> str:
        if not nodes:
            return ""
        if html_list_mode == "flat_ql_indent":
            return self._render_wrike_html_flat_list(nodes, html_url_mode=html_url_mode, stringify=False)
        if html_list_mode == "flat_stringify":
            return self._render_wrike_html_flat_list(nodes, html_url_mode=html_url_mode, stringify=True)
        if html_list_mode == "nested_stringify":
            return self._render_wrike_html_nested_stringify_list(nodes, html_url_mode=html_url_mode)
        parts = ["<ul>"]
        for node in nodes:
            text = ""
            if node and len(node) > 0:
                text = str(node[0]).rstrip("\n")
            parts.append("<li>")
            parts.append(self._inline_to_html_wrike(text, html_url_mode=html_url_mode))
            children = node[1] if node and len(node) > 1 and isinstance(node[1], list) else []
            if children:
                parts.append(self._render_wrike_html_list(children, html_url_mode=html_url_mode))
            parts.append("</li>")
        parts.append("</ul>")
        return "".join(parts)

    def _render_wrike_html_sections(
        self,
        groups: dict[str, list],
        html_url_mode: str = "text",
        html_list_mode: str = "nested_simple",
    ) -> str:
        parts = []
        for product, nodes in groups.items():
            heading = self._escape_html(self._normalize_heading_label(product))
            if heading:
                parts.append(f"<div><b>{heading}</b></div>")
            parts.append(
                self._render_wrike_html_list(
                    nodes,
                    html_url_mode=html_url_mode,
                    html_list_mode=html_list_mode,
                )
            )
            parts.append("<div><br/></div>")
        return "".join(parts).rstrip()

    def _convert_markdown_links_to_plain(self, text: str) -> str:
        out = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "[":
                j = i + 1
                depth = 1
                while j < n and depth > 0:
                    c = text[j]
                    if c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                    j += 1

                if depth == 0 and j < n and text[j] == "(":
                    k = j + 1
                    while k < n and text[k] != ")":
                        k += 1
                    if k < n:
                        label = text[i + 1 : j - 1]
                        url = text[j + 1 : k].strip()
                        if url:
                            safe_label = self._strip_md_bold_markers(label).replace("|", " ")
                            out.append(f"{safe_label} ({url})")
                            i = k + 1
                            continue

            out.append(text[i])
            i += 1
        return "".join(out)

    def _convert_markdown_links_to_html(self, text: str) -> str:
        out = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "[":
                j = i + 1
                depth = 1
                while j < n and depth > 0:
                    c = text[j]
                    if c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                    j += 1

                if depth == 0 and j < n and text[j] == "(":
                    k = j + 1
                    while k < n and text[k] != ")":
                        k += 1
                    if k < n:
                        label = text[i + 1 : j - 1]
                        url = text[j + 1 : k].strip()
                        if url:
                            label_html = self._md_bold_to_html(self._escape_html(label))
                            url_attr = self._escape_html(url)
                            out.append(
                                f'<a target="_blank" class="c-link c-link--underline" '
                                f'data-stringify-link="{url_attr}" url="{url_attr}" data-sk="tooltip_parent" '
                                f'href="{url_attr}" rel="noopener noreferrer">{label_html}</a>'
                            )
                            i = k + 1
                            continue

            out.append(text[i])
            i += 1
        return "".join(out)

    def _inline_to_plain(self, text: str) -> str:
        converted = self._convert_markdown_links_to_plain(text)
        return self._strip_md_bold_markers(converted)

    def _inline_to_html(self, text: str) -> str:
        converted = self._convert_markdown_links_to_html(text)
        if "<a " in converted:
            parts = []
            i = 0
            n = len(converted)
            while i < n:
                a_idx = converted.find("<a ", i)
                if a_idx < 0:
                    tail = converted[i:]
                    parts.append(self._md_bold_to_html(self._escape_html(tail)))
                    break
                head = converted[i:a_idx]
                if head:
                    parts.append(self._md_bold_to_html(self._escape_html(head)))
                end_a = converted.find("</a>", a_idx)
                if end_a < 0:
                    tail = converted[a_idx:]
                    parts.append(self._escape_html(tail))
                    break
                parts.append(converted[a_idx : end_a + 4])
                i = end_a + 4
            converted = "".join(parts)
        else:
            converted = self._md_bold_to_html(self._escape_html(converted))
        return converted.replace("\n", "<br/>")

    def _render_plain_sections(self, nodes) -> str:
        out_lines = []
        buffered = []

        def _emit_tree(tree_nodes, depth: int) -> None:
            for node in tree_nodes:
                text = self._inline_to_plain(node[0])
                text = self._normalize_newlines(text)
                lines = [p.strip() for p in text.split("\n") if p.strip()]
                if lines:
                    indent = "\t" * int(depth)
                    out_lines.append(f"{indent}- {lines[0]}")
                    for extra in lines[1:]:
                        out_lines.append(f"{indent}  {extra}")
                if node[1]:
                    _emit_tree(node[1], depth + 1)

        def _flush_buffer():
            nonlocal buffered
            if buffered:
                _emit_tree(buffered, 0)
                buffered = []

        for node in nodes:
            if self._is_root_section_heading_node(node):
                _flush_buffer()
                heading = self._inline_to_plain(node[0]).strip()
                if heading:
                    out_lines.append(heading)
                if node[1]:
                    _emit_tree(node[1], 0)
                out_lines.append("")
            else:
                buffered.append(node)

        _flush_buffer()
        return "\r\n".join(out_lines).rstrip() + "\r\n"

    def _render_html_list(self, nodes) -> str:
        list_class = "p-rich_text_list p-rich_text_list__bullet p-rich_text_list--nested"

        def _emit_list(cur_nodes, depth: int) -> str:
            if not cur_nodes:
                return ""
            d = int(depth)
            parts = [
                f'<ul data-stringify-type="unordered-list" data-list-tree="true" '
                f'class="{list_class}" data-indent="{d}" data-border="0">'
            ]
            for node in cur_nodes:
                text = node[0].rstrip("\n")
                parts.append(f'<li data-stringify-indent="{d}" data-stringify-border="0">')
                parts.append(self._inline_to_html(text))
                if node[1]:
                    parts.append(_emit_list(node[1], d + 1))
                parts.append("</li>")
            parts.append("</ul>")
            return "".join(parts)

        return _emit_list(nodes, 0)

    def _render_html_sections(self, nodes) -> str:
        parts = []
        buffered = []

        def _flush_buffer():
            nonlocal buffered
            if buffered:
                parts.append(self._render_html_list(buffered))
                buffered = []

        for node in nodes:
            if self._is_root_section_heading_node(node):
                _flush_buffer()
                raw = node[0].strip()
                if raw.startswith("**") and raw.endswith("**"):
                    raw = raw[2:-2].strip()
                heading_text = self._escape_html(raw)
                if heading_text:
                    parts.append(
                        '<div class="p-rich_text_section">'
                        f'<b data-stringify-type="bold">{heading_text}</b>'
                        '<br aria-hidden="true"></div>'
                    )
                if node[1]:
                    parts.append(self._render_html_list(node[1]))
            else:
                buffered.append(node)

        _flush_buffer()
        return "".join(parts).rstrip()

    def _build_cf_html_payload(self, fragment_html: str, source_url: str = "https://www.wrike.com/"):
        start_frag = "<!--StartFragment-->"
        end_frag = "<!--EndFragment-->"
        fragment_bytes = fragment_html.encode("utf-8")
        html = (
            "<!DOCTYPE html>"
            f"<html><head><meta charset=\"utf-8\"></head><body>{start_frag}{fragment_html}{end_frag}</body></html>"
        )
        html_bytes = html.encode("utf-8")

        header_template = (
            "Version:0.9\r\n"
            "StartHTML:{:08d}\r\n"
            "EndHTML:{:08d}\r\n"
            "StartFragment:{:08d}\r\n"
            "EndFragment:{:08d}\r\n"
            f"SourceURL:{source_url}\r\n"
        )
        header_probe = header_template.format(0, 0, 0, 0).encode("ascii")
        start_html = len(header_probe)

        start_marker_idx = html_bytes.find(start_frag.encode("ascii"))
        end_marker_idx = html_bytes.find(end_frag.encode("ascii"))
        if start_marker_idx < 0 or end_marker_idx < 0 or end_marker_idx < start_marker_idx:
            start_fragment = start_html
            end_fragment = start_html + len(html_bytes)
        else:
            start_fragment = start_html + start_marker_idx + len(start_frag)
            end_fragment = start_html + end_marker_idx
        end_html = start_html + len(html_bytes)

        header = header_template.format(start_html, end_html, start_fragment, end_fragment).encode("ascii")
        return header + html_bytes, fragment_bytes

    def _set_clipboard_text_and_html(
        self,
        text: str,
        cf_html: bytes | None,
        html_bytes: bytes | None,
    ) -> None:
        last_err = None
        for _ in range(12):
            try:
                win32clipboard.OpenClipboard()
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    self.__lib.time.sleep(0.01)
                except Exception:
                    pass
        if last_err is not None:
            raise last_err

        try:
            win32clipboard.EmptyClipboard()
            text_crlf = text.replace("\n", "\r\n")
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text_crlf)
            if cf_html:
                fmt_html = win32clipboard.RegisterClipboardFormat("HTML Format")
                win32clipboard.SetClipboardData(fmt_html, cf_html + b"\x00")
            if html_bytes:
                try:
                    fmt_text_html = win32clipboard.RegisterClipboardFormat("text/html")
                    win32clipboard.SetClipboardData(fmt_text_html, html_bytes + b"\x00")
                except Exception:
                    pass
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

    def _is_section_heading_node(self, node) -> bool:
        if not node or not node[1]:
            return False
        return self._is_section_heading_text(node[0])

    def _is_root_section_heading_node(self, node) -> bool:
        if not node:
            return False
        return self._is_section_heading_text(node[0])

    def _is_section_heading_text(self, text: str) -> bool:
        t = text.strip()
        if not t.startswith("**") or not t.endswith("**"):
            return False
        inner = t[2:-2].strip()
        return bool(inner)
