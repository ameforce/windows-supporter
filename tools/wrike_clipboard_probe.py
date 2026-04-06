import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.apps.Notion import Notion


DEFAULT_TASK_URL = "https://www.wrike.com/open.htm?id=4390203945"
DEFAULT_WAIT_AFTER_PASTE_MS = 1200
DEFAULT_WAIT_AFTER_TRIGGER_MS = 1200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Wrike clipboard payload variants")
    parser.add_argument(
        "--fixture",
        default=str(Path("tests") / "e2e" / "fixtures" / "notion-copy-raw.txt"),
        help="raw Notion copy fixture path",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        help="specific variant to probe (repeatable)",
    )
    parser.add_argument(
        "--task-url",
        default=DEFAULT_TASK_URL,
        help="Wrike task url used for live validation",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="output root directory; defaults to %%TEMP%%\\windows-supporter\\artifacts\\e2e\\wrike-clipboard\\<timestamp>",
    )
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="dump payloads without opening Wrike",
    )
    parser.add_argument(
        "--trigger-key",
        default="Space",
        help="key pressed once after paste for trigger comparison",
    )
    parser.add_argument(
        "--login-timeout-sec",
        type=int,
        default=180,
        help="seconds to wait for manual Wrike login",
    )
    parser.add_argument(
        "--wait-after-paste-ms",
        type=int,
        default=DEFAULT_WAIT_AFTER_PASTE_MS,
        help="wait after Ctrl+V before collecting state",
    )
    parser.add_argument(
        "--wait-after-trigger-ms",
        type=int,
        default=DEFAULT_WAIT_AFTER_TRIGGER_MS,
        help="wait after trigger key before collecting state",
    )
    parser.add_argument(
        "--google-email-env",
        default="WRIKE_GOOGLE_EMAIL",
        help="environment variable name for Google login email",
    )
    parser.add_argument(
        "--google-password-env",
        default="WRIKE_GOOGLE_PASSWORD",
        help="environment variable name for Google login password",
    )
    return parser.parse_args()


def resolve_output_root(args: argparse.Namespace) -> Path:
    if args.output_dir:
        root = Path(args.output_dir)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = (
            Path(tempfile.gettempdir())
            / "windows-supporter"
            / "artifacts"
            / "e2e"
            / "wrike-clipboard"
            / stamp
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_variants(notion: Notion, args: argparse.Namespace) -> list[str]:
    if args.variant:
        return [str(item).strip() for item in args.variant]
    return list(notion.get_wrike_clipboard_variants())


def load_fixture_text(path: str) -> str:
    fixture_path = Path(path)
    return fixture_path.read_text(encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def write_bytes(path: Path, value: bytes) -> None:
    path.write_bytes(value)


def dump_variant_bundle(variant_dir: Path, bundle: dict[str, object]) -> None:
    write_text(variant_dir / "plain_text.txt", str(bundle["plain_text"]))
    write_text(variant_dir / "html_fragment.html", str(bundle["html_fragment"] or ""))
    cf_html = bundle.get("cf_html")
    html_bytes = bundle.get("html_bytes")
    if isinstance(cf_html, bytes):
        write_bytes(variant_dir / "html_format.bin", cf_html)
    if isinstance(html_bytes, bytes):
        write_bytes(variant_dir / "text_html.bin", html_bytes)
    summary = {
        "variant": bundle.get("variant"),
        "plain_url_mode": bundle.get("plain_url_mode"),
        "html_mode": bundle.get("html_mode"),
        "html_list_mode": bundle.get("html_list_mode"),
        "force_trailing_space": bundle.get("force_trailing_space"),
        "plain_text_length": len(str(bundle.get("plain_text") or "")),
        "has_cf_html": isinstance(cf_html, bytes),
        "has_text_html": isinstance(html_bytes, bytes),
    }
    write_text(
        variant_dir / "payload_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2),
    )


def has_visible_locator(locator, limit: int = 10) -> bool:
    try:
        count = min(locator.count(), int(limit))
    except Exception:
        return False
    for index in range(count):
        try:
            if locator.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


def safe_page_url(page) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def safe_page_title(page) -> str:
    try:
        return str(page.title() or "")
    except Exception:
        return ""


def read_secret_env(name: str) -> str:
    key = str(name or "").strip()
    if not key:
        return ""
    return str(os.getenv(key) or "").strip()


def is_login_page(page) -> bool:
    current_url = safe_page_url(page).lower()
    if any(token in current_url for token in ("login", "signin", "sso", "auth")):
        return True
    try:
        if has_visible_locator(page.locator("input[type='password']")):
            return True
    except Exception:
        return True
    for label in ("Log in", "Sign in", "로그인", "SSO"):
        try:
            if has_visible_locator(page.get_by_text(label, exact=False)):
                return True
        except Exception:
            continue
    return False


def is_wrike_authenticated_page(page) -> bool:
    if is_login_page(page):
        return False
    current_url = safe_page_url(page).lower()
    if not current_url:
        return False
    if "wrike.com" not in current_url:
        return False
    if any(token in current_url for token in ("login.wrike.com/login", "signin", "/auth", "/sso")):
        return False
    return True


def find_wrike_authenticated_page(context, preferred_task_url: str = ""):
    preferred = str(preferred_task_url or "").strip().lower()
    fallback = None
    for page in list(getattr(context, "pages", []) or []):
        if not is_wrike_authenticated_page(page):
            continue
        url = safe_page_url(page).lower()
        if preferred and preferred in url:
            return page
        if fallback is None:
            fallback = page
    return fallback


def handle_wrike_login_continue(page, output_root: Path) -> bool:
    current_url = safe_page_url(page).lower()
    has_remember_ui = False
    try:
        if has_visible_locator(page.get_by_role("button", name="Remember", exact=False)):
            has_remember_ui = True
    except Exception:
        pass
    if "login_continue" not in current_url and not has_remember_ui:
        return False
    if click_first_visible_by_names(page, ("Remember", "Forget", "기억", "계속")):
        time.sleep(2.0)
        dump_page_debug(output_root, "wrike-after-login-continue", page)
        return True
    return False


def dump_login_debug(output_root: Path, phase: str, page) -> None:
    debug_dir = output_root / "_login_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug = {
        "phase": phase,
        "url": safe_page_url(page),
        "title": safe_page_title(page),
    }
    write_text(
        debug_dir / f"{phase}.json",
        json.dumps(debug, ensure_ascii=False, indent=2),
    )
    try:
        page.screenshot(path=str(debug_dir / f"{phase}.png"), full_page=True)
    except Exception:
        pass


def dump_context_pages(output_root: Path, phase: str, context) -> None:
    debug_dir = output_root / "_login_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for index, item in enumerate(list(getattr(context, "pages", []) or [])):
        pages.append(
            {
                "index": index,
                "url": safe_page_url(item),
                "title": safe_page_title(item),
                "authenticated": is_wrike_authenticated_page(item),
                "login_page": is_login_page(item),
            }
        )
    write_text(
        debug_dir / f"{phase}-pages.json",
        json.dumps(pages, ensure_ascii=False, indent=2),
    )


def dump_page_debug(output_root: Path, phase: str, page) -> None:
    debug_dir = output_root / "_login_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase,
        "url": safe_page_url(page),
        "title": safe_page_title(page),
    }
    write_text(
        debug_dir / f"{phase}.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    try:
        page.screenshot(path=str(debug_dir / f"{phase}.png"), full_page=True)
    except Exception:
        pass


def click_google_and_get_page(page, context):
    try:
        with context.expect_page(timeout=15000) as next_page_info:
            page.get_by_role("button", name="Google").click(timeout=5000)
        google_page = next_page_info.value
        print(f"google popup detected: {safe_page_url(google_page)}")
        return google_page
    except Exception as exc:
        print(f"google popup wait failed: {exc}")
        try:
            with context.expect_page(timeout=15000) as next_page_info:
                page.locator("button:has-text('Google')").first.click(timeout=5000)
            google_page = next_page_info.value
            print(f"google popup detected via fallback: {safe_page_url(google_page)}")
            return google_page
        except Exception as inner_exc:
            print(f"google popup fallback failed: {inner_exc}")
            return None


def click_first_visible_by_names(page, names: tuple[str, ...]) -> bool:
    for name in names:
        try:
            locator = page.get_by_role("button", name=name, exact=False)
            if has_visible_locator(locator):
                locator.first.click(timeout=5000)
                return True
        except Exception:
            pass
        try:
            locator = page.locator(f"button:has-text('{name}')")
            if has_visible_locator(locator):
                locator.first.click(timeout=5000)
                return True
        except Exception:
            pass
    return False


def wait_for_visible_locator(locator, timeout_ms: int = 15000) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if has_visible_locator(locator):
            return True
        time.sleep(0.2)
    return False


def try_google_login(page, context, args: argparse.Namespace, output_root: Path) -> bool:
    email = read_secret_env(args.google_email_env)
    password = read_secret_env(args.google_password_env)
    if not email or not password:
        return False
    if not is_login_page(page):
        return False

    google_page = click_google_and_get_page(page, context)
    if google_page is None:
        return False

    try:
        google_page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        google_page.bring_to_front()
    except Exception:
        pass

    dump_page_debug(output_root, "google-popup-opened", google_page)

    try:
        email_box = google_page.locator("input[type='email']")
        if not wait_for_visible_locator(email_box, timeout_ms=15000):
            use_another = google_page.get_by_text("Use another account", exact=False)
            if has_visible_locator(use_another):
                use_another.first.click(timeout=5000)
                if not wait_for_visible_locator(email_box, timeout_ms=10000):
                    dump_page_debug(output_root, "google-email-not-visible", google_page)
                    return False
            else:
                account_option = google_page.get_by_text(email, exact=False)
                if has_visible_locator(account_option):
                    account_option.first.click(timeout=5000)
                else:
                    dump_page_debug(output_root, "google-email-not-visible", google_page)
                    return False

        if has_visible_locator(email_box):
            email_box.first.fill(email)
            if not click_first_visible_by_names(google_page, ("Next", "다음")):
                dump_page_debug(output_root, "google-email-next-missing", google_page)
                return False
            try:
                google_page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(1.0)
            dump_page_debug(output_root, "google-after-email", google_page)

        password_box = google_page.locator("input[type='password']")
        if not wait_for_visible_locator(password_box, timeout_ms=20000):
            dump_page_debug(output_root, "google-password-not-visible", google_page)
            return False

        password_box.first.fill(password)
        if not click_first_visible_by_names(google_page, ("Next", "다음")):
            dump_page_debug(output_root, "google-password-next-missing", google_page)
            return False
        time.sleep(2.0)
        dump_page_debug(output_root, "google-after-password", google_page)

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if click_first_visible_by_names(google_page, ("Continue", "Allow", "계속", "허용")):
                time.sleep(2.0)
                dump_page_debug(output_root, "google-after-consent", google_page)
                continue
            if "accounts.google.com" not in safe_page_url(google_page).lower():
                break
            if google_page.is_closed():
                break
            time.sleep(1.0)
    except Exception:
        dump_page_debug(output_root, "google-login-exception", google_page)
        return False

    dump_context_pages(output_root, "after-google-login-attempt", context)
    return True


def wait_for_wrike_login(page, context, timeout_sec: int, output_root: Path, args: argparse.Namespace):
    deadline = time.monotonic() + max(10, int(timeout_sec))
    logged_in_page = find_wrike_authenticated_page(context)
    if logged_in_page is not None:
        return logged_in_page
    if not is_login_page(page):
        return page
    print("Wrike login required. Complete login in the opened browser window.")
    dump_login_debug(output_root, "before-login", page)
    dump_context_pages(output_root, "before-login", context)
    auto_login_attempted = False
    next_log_at = time.monotonic()
    while time.monotonic() < deadline:
        handle_wrike_login_continue(page, output_root)
        logged_in_page = find_wrike_authenticated_page(context)
        if logged_in_page is not None:
            dump_login_debug(output_root, "after-login-detected", logged_in_page)
            dump_context_pages(output_root, "after-login-detected", context)
            return logged_in_page
        if not is_login_page(page):
            dump_login_debug(output_root, "after-login-detected", page)
            dump_context_pages(output_root, "after-login-detected", context)
            return page
        if not auto_login_attempted:
            auto_login_attempted = True
            try_google_login(page, context, args, output_root)
        if time.monotonic() >= next_log_at:
            try:
                print(f"waiting login: url={safe_page_url(page)}")
            except Exception:
                pass
            next_log_at = time.monotonic() + 10.0
        time.sleep(1.0)
    dump_login_debug(output_root, "login-timeout", page)
    dump_context_pages(output_root, "login-timeout", context)
    raise RuntimeError("Wrike login timeout")


def read_editor_metadata(editor) -> dict[str, object]:
    try:
        return editor.evaluate(
            """(el) => {
                const attr = (node, name) => {
                    if (!node || !node.getAttribute) {
                        return "";
                    }
                    return String(node.getAttribute(name) || "");
                };
                const trail = [];
                let node = el;
                for (let index = 0; index < 4 && node; index += 1, node = node.parentElement) {
                    trail.push({
                        tag: String(node.tagName || ""),
                        role: attr(node, "role"),
                        automationId: attr(node, "data-automation-id"),
                        testId: attr(node, "data-testid"),
                        testAttr: attr(node, "data-test"),
                        ariaLabel: attr(node, "aria-label"),
                        placeholder: attr(node, "placeholder"),
                        className: String(node.className || "").slice(0, 200),
                    });
                }
                return {
                    tag: String(el.tagName || ""),
                    role: attr(el, "role"),
                    automationId: attr(el, "data-automation-id"),
                    testId: attr(el, "data-testid"),
                    testAttr: attr(el, "data-test"),
                    ariaLabel: attr(el, "aria-label"),
                    placeholder: attr(el, "placeholder"),
                    className: String(el.className || "").slice(0, 200),
                    textPreview: String(
                        el.innerText || el.textContent || el.value || ""
                    ).slice(0, 200),
                    ancestorTrail: trail,
                };
            }"""
        )
    except Exception:
        return {}


def iter_visible_locators(locator, limit: int = 20):
    try:
        count = min(locator.count(), int(limit))
    except Exception:
        count = 0
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                yield item
        except Exception:
            continue


def click_first_visible_locator(locator, limit: int = 20) -> bool:
    for item in iter_visible_locators(locator, limit=limit):
        try:
            item.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def activate_description_editor(page) -> bool:
    activation_targets = [
        page.get_by_text("Add a description", exact=False),
        page.get_by_text("write with AI", exact=False),
        page.locator('[data-automation-id*="description" i]'),
        page.locator('[data-testid*="description" i]'),
        page.locator('[data-test*="description" i]'),
    ]
    for locator in activation_targets:
        if click_first_visible_locator(locator, limit=10):
            page.wait_for_timeout(800)
            return True
    return False


def collect_editor_candidates(page) -> list[dict[str, object]]:
    selectors = [
        '[data-automation-id*="description" i] [contenteditable="true"]',
        '[data-test*="description" i] [contenteditable="true"]',
        '[data-testid*="description" i] [contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
        '[contenteditable="true"]',
        "textarea",
    ]
    candidates: list[dict[str, object]] = []
    for selector in selectors:
        locator = page.locator(selector)
        for item in iter_visible_locators(locator, limit=20):
            try:
                box = item.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            width = float(box.get("width", 0))
            height = float(box.get("height", 0))
            if width < 120 or height < 18:
                continue
            candidates.append(
                {
                    "locator": item,
                    "selector": selector,
                    "x": float(box.get("x", 0)),
                    "y": float(box.get("y", 0)),
                    "width": width,
                    "height": height,
                    "metadata": read_editor_metadata(item),
                }
            )
    return candidates


def score_editor_candidate(candidate: dict[str, object], viewport_height: float) -> float:
    width = float(candidate.get("width") or 0)
    height = float(candidate.get("height") or 0)
    x = float(candidate.get("x") or 0)
    y = float(candidate.get("y") or 0)
    metadata_obj = candidate.get("metadata") or {}
    metadata = str(json.dumps(metadata_obj, ensure_ascii=False)).lower()
    tag = str(metadata_obj.get("tag") or "").upper()
    aria_label = str(metadata_obj.get("ariaLabel") or "").lower()
    class_name = str(metadata_obj.get("className") or "").lower()

    score = width * max(height, 20.0)
    score -= y * 120.0

    if x < 160:
        score -= 150000.0
    if width < 260:
        score -= 50000.0
    if y > viewport_height * 0.7:
        score -= 200000.0
    if "description" in metadata:
        score += 400000.0
    if "comment" in metadata:
        score -= 250000.0
    if "send" in metadata:
        score -= 250000.0
    if tag == "TEXTAREA":
        score -= 300000.0
    if "work item title" in aria_label:
        score -= 500000.0
    if "placeholder" in metadata and "untitled" in metadata:
        score -= 200000.0
    if "quill-editor" in class_name:
        score += 250000.0
    if "comment-editor" in metadata or "comment-text-editor" in metadata:
        score -= 300000.0

    return score


def is_description_candidate(candidate: dict[str, object], viewport_height: float) -> bool:
    metadata_obj = candidate.get("metadata") or {}
    tag = str(metadata_obj.get("tag") or "").upper()
    class_name = str(metadata_obj.get("className") or "").lower()
    metadata = str(json.dumps(metadata_obj, ensure_ascii=False)).lower()
    y = float(candidate.get("y") or 0)

    if tag == "TEXTAREA":
        return False
    if "comment-editor" in metadata or "comment-text-editor" in metadata:
        return False
    if "ql-editor" not in class_name:
        return False
    if y > viewport_height * 0.7:
        return False
    return True


def find_task_body_editor(page):
    viewport = page.viewport_size or {"height": 1100}
    viewport_height = float(viewport.get("height") or 1100)

    best = None
    for _ in range(3):
        activate_description_editor(page)
        page.wait_for_timeout(600)

        candidates = collect_editor_candidates(page)
        preferred = [
            candidate
            for candidate in candidates
            if is_description_candidate(candidate, viewport_height)
        ]
        for candidate in preferred:
            score = score_editor_candidate(candidate, viewport_height)
            if best is None or score > best[0]:
                best = (score, candidate)
        if best is not None:
            return best[1]["locator"]

    raise RuntimeError("Wrike task body editor not found")


def read_editor_state(editor) -> dict[str, object]:
    try:
        html = editor.evaluate(
            """(el) => {
                if (typeof el.value === "string") {
                    return "";
                }
                return String(el.innerHTML || "");
            }"""
        )
    except Exception:
        html = ""
    try:
        text = editor.evaluate(
            """(el) => {
                if (typeof el.value === "string") {
                    return String(el.value || "");
                }
                return String(el.innerText || el.textContent || "");
            }"""
        )
    except Exception:
        text = ""
    return {
        "text": str(text or ""),
        "html": str(html or ""),
    }


def clear_editor(page, editor) -> None:
    origin_url = safe_page_url(page)

    def focus_editor() -> None:
        editor.evaluate(
            """(el) => {
                el.focus();
            }"""
        )

    def dom_select_all() -> None:
        editor.evaluate(
            """(el) => {
                el.focus();
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )

    focus_editor()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(300)

    for _ in range(3):
        state = read_editor_state(editor)
        current_url = safe_page_url(page)
        if origin_url and current_url and current_url != origin_url:
            raise RuntimeError(
                f"Wrike editor focus navigated away during clear: {current_url}"
            )
        if not str(state["text"]).strip():
            return
        focus_editor()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
        state = read_editor_state(editor)
        if not str(state["text"]).strip():
            return
        dom_select_all()
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
    raise RuntimeError("Wrike editor body was not cleared")


def save_editor_artifacts(variant_dir: Path, phase: str, page, editor) -> None:
    screenshot_path = variant_dir / f"{phase}.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    state = read_editor_state(editor)
    metadata = read_editor_metadata(editor)
    write_text(variant_dir / f"{phase}-editor-text.txt", state["text"])
    write_text(variant_dir / f"{phase}-editor-html.html", state["html"])
    snapshot = {
        "phase": phase,
        "url": str(page.url or ""),
        "title": str(page.title() or ""),
        "text_length": len(str(state["text"])),
        "html_length": len(str(state["html"])),
        "editor_metadata": metadata,
    }
    write_text(
        variant_dir / f"{phase}-snapshot.json",
        json.dumps(snapshot, ensure_ascii=False, indent=2),
    )


def open_task_page(page, task_url: str) -> None:
    page.goto(task_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)


def launch_wrike_context(playwright, profile_dir: Path):
    browser = None
    context = None
    try:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 1100},
        )
        return browser, context, "persistent"
    except Exception as exc:
        print(f"persistent context launch failed: {exc}")

    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(viewport={"width": 1440, "height": 1100})
    return browser, context, "ephemeral"


def run_live_probe(
    notion: Notion,
    bundles: dict[str, dict[str, object]],
    args: argparse.Namespace,
    output_root: Path,
) -> None:
    local_appdata = Path(str(Path.home()))
    env_local = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if env_local:
        local_appdata = Path(env_local)
    profile_dir = local_appdata / "windows-supporter" / "wrike-profile"

    with sync_playwright() as playwright:
        browser, context, launch_mode = launch_wrike_context(playwright, profile_dir)
        try:
            print(f"launch mode: {launch_mode}")
            page = context.pages[0] if context.pages else context.new_page()
            open_task_page(page, args.task_url)
            page = wait_for_wrike_login(page, context, args.login_timeout_sec, output_root, args)

            for variant, bundle in bundles.items():
                variant_dir = output_root / variant
                open_task_page(page, args.task_url)
                editor = find_task_body_editor(page)
                clear_editor(page, editor)
                save_editor_artifacts(variant_dir, "01-empty", page, editor)

                notion._set_clipboard_text_and_html(
                    str(bundle["plain_text"]),
                    bundle.get("cf_html"),
                    bundle.get("html_bytes"),
                )

                editor.click()
                page.keyboard.press("Control+V")
                page.wait_for_timeout(int(args.wait_after_paste_ms))
                save_editor_artifacts(variant_dir, "02-after-paste", page, editor)

                trigger_key = str(args.trigger_key or "").strip()
                if trigger_key:
                    page.keyboard.press(trigger_key)
                    page.wait_for_timeout(int(args.wait_after_trigger_ms))
                    save_editor_artifacts(variant_dir, "03-after-trigger", page, editor)

                clear_editor(page, editor)
                save_editor_artifacts(variant_dir, "04-cleared", page, editor)
        finally:
            context.close()
            if browser is not None:
                browser.close()


def main() -> int:
    args = parse_args()
    notion = Notion()
    variants = resolve_variants(notion, args)
    raw_text = load_fixture_text(args.fixture)
    output_root = resolve_output_root(args)

    bundles: dict[str, dict[str, object]] = {}
    for variant in variants:
        bundle = notion.build_wrike_clipboard_payload_bundle(raw_text, variant=variant)
        if bundle is None:
            raise RuntimeError(f"failed to build bundle for {variant}")
        bundles[variant] = bundle
        variant_dir = output_root / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        dump_variant_bundle(variant_dir, bundle)

    if args.dump_only:
        print(f"payload dumps saved: {output_root}")
        return 0

    run_live_probe(notion, bundles, args, output_root)
    print(f"probe artifacts saved: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
