"""Conservative screenshot-anchored automation for the documented QQ mini-app screens."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import re
import sys
import time
import unicodedata
from pathlib import Path

import cv2
import numpy as np
from PIL import ImageGrab


APP_TITLE = "报名接龙工具"
REFERENCE_WIDTH = 621
REFERENCE_HEIGHT = 1344
_MODULE_DIR = Path(__file__).resolve().parent
_BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", _MODULE_DIR))
_EXECUTABLE_DIR = Path(sys.executable).resolve().parent
_ASSET_DIR_CANDIDATES = tuple(dict.fromkeys((
    _BUNDLE_DIR / "assets",
    _MODULE_DIR / "assets",
    _EXECUTABLE_DIR / "_internal" / "assets",
    _EXECUTABLE_DIR / "assets",
)))
ASSETS = next((path for path in _ASSET_DIR_CANDIDATES if path.is_dir()), _ASSET_DIR_CANDIDATES[0])
THRESHOLD = 0.78
EARLY_MODAL_THRESHOLD = 0.80
_OCR_ENGINE = None


class VisualAutomationError(RuntimeError):
    pass


def _get_ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise VisualAutomationError("缺少中文文字识别依赖 RapidOCR；请安装 requirements.txt 并预下载 OCR 模型。") from exc
        try:
            _OCR_ENGINE = RapidOCR()
        except Exception as exc:
            raise VisualAutomationError(f"中文文字识别模型不可用：{exc}") from exc
    return _OCR_ENGINE


def _normalize_label(text: str) -> tuple[str, bool]:
    text = unicodedata.normalize("NFKC", str(text)).strip().casefold()
    required = "必填" in text or "必需" in text
    text = text.replace("必填", "").replace("必需", "").replace("选填", "")
    return re.sub(r"[\s:：*＊·•，。、《》()（）\[\]【】]", "", text), required


def _is_input_hint(text: str) -> bool:
    """Return True for OCR text that belongs to an input, not to its label."""
    normalized, _ = _normalize_label(text)
    # The mini-program renders these hints in the same left-aligned area as
    # labels on narrow windows (for example, the supplied form screenshot).
    # They must never be treated as unmapped fields.
    return normalized in {
        "点这里输入", "点击这里输入", "点击输入", "请输入", "请输入内容",
        "点此输入", "点击此处输入", "请选择", "请选择内容", "点击选择",
        "点这里选择", "选择日期", "选择时间",
    }


def _ocr_lines(image):
    engine = _get_ocr_engine()
    pixels = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    result = engine(pixels)
    if result is None or getattr(result, "boxes", None) is None:
        return []
    lines = []
    for text, score, box in zip(result.txts, result.scores, result.boxes):
        points = np.asarray(box, dtype=float).reshape(-1, 2)
        if points.size == 0:
            continue
        lines.append({
            "text": str(text), "score": float(score),
            "x1": float(points[:, 0].min()), "x2": float(points[:, 0].max()),
            "y1": float(points[:, 1].min()), "y2": float(points[:, 1].max()),
        })
    return lines


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def _visible_app_windows():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    found = []
    enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if APP_TITLE in title.value:
            rect = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if width >= 500 and height >= 900:
                    found.append((hwnd, title.value, rect.left, rect.top, width, height))
        return True

    callback = enum_type(visit)
    user32.EnumWindows.argtypes = [enum_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(callback, 0)
    return found


def _window_pid(hwnd):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
        raise VisualAutomationError("无法读取 QQ 窗口所属进程。")
    return int(process_id.value)


def _choose_window(binding=None):
    windows = _visible_app_windows()
    if not windows:
        raise VisualAutomationError("没有找到可见的“报名接龙工具”窗口。请先打开目标活动页。")
    # Multiple native/legacy wrappers can describe the same app; prefer the foreground window.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow()
    foreground_matches = [w for w in windows if w[0] == foreground]
    if len(foreground_matches) == 1:
        selected = foreground_matches[0]
    elif len(windows) == 1:
        selected = windows[0]
    else:
        rects = {(w[2], w[3], w[4], w[5]) for w in windows}
        if len(rects) == 1:
            selected = windows[0]
        else:
            raise VisualAutomationError("发现多个不同位置的同名 QQ 窗口。请把目标小程序窗口置于前台后重试。")

    hwnd, title, left, top, width, height = selected
    if binding:
        if int(binding.get("hwnd", -1)) != int(hwnd) or int(binding.get("pid", -1)) != _window_pid(hwnd):
            raise VisualAutomationError("当前 QQ 窗口与预绑定窗口不一致。请重新执行“连接并预检”。")
    ratio = width / height
    expected = REFERENCE_WIDTH / REFERENCE_HEIGHT
    if abs(ratio - expected) > 0.035:
        raise VisualAutomationError("QQ 小程序窗口比例与截图不符。请恢复为截图中的竖屏比例后再执行。")
    return selected


def preflight_page(activate=True, binding=None):
    """Read-only preflight and bind to the current QQ mini-program window."""
    for module_name in ("cv2", "numpy", "PIL"):
        try:
            __import__(module_name)
        except ImportError as exc:
            raise VisualAutomationError(f"缺少运行依赖 {module_name}，请重新安装 requirements.txt。") from exc
    _get_ocr_engine()
    for asset_name in ("detail-signup.png", "form-top.png", "form-submit.png", "success-top.png", "too-early-dialog.png"):
        if not any((asset_dir / asset_name).is_file() for asset_dir in _ASSET_DIR_CANDIDATES):
            checked_paths = "；".join(str(asset_dir / asset_name) for asset_dir in _ASSET_DIR_CANDIDATES)
            raise VisualAutomationError(f"找不到页面识别模板：{asset_name}。已检查：{checked_paths}")

    bounds = _choose_window(binding)
    if activate:
        _activate(bounds[0])
    image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
    scores = _page_scores(image)
    if scores["detail"] < THRESHOLD:
        raise VisualAutomationError(
            f"已找到 QQ 窗口，但当前画面未识别为活动详情页（分数 {scores['detail']:.2f}）。"
            "请打开目标活动详情页，保持页面可见后重新预检。"
        )
    return {
        "hwnd": int(bounds[0]),
        "pid": _window_pid(bounds[0]),
        "title": bounds[1],
        "bounds": [int(v) for v in bounds[2:]],
        "detail_score": round(scores["detail"], 3),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _activate(hwnd):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            time.sleep(0.08)
            return
        time.sleep(0.025)
    raise VisualAutomationError("无法将 QQ 小程序窗口切到前台。请先把目标活动页置于前台并保持可见，再安排任务。")


def _capture(bounds):
    left, top, width, height = bounds
    try:
        return ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True).convert("RGB")
    except Exception as exc:
        raise VisualAutomationError(f"无法读取 QQ 窗口画面：{exc}") from exc


def _template_score(image, asset_name, box):
    x, y, w, h = box
    factor = image.width / REFERENCE_WIDTH
    sx, sy = round(x * factor), round(y * factor)
    sw, sh = max(1, round(w * factor)), max(1, round(h * factor))
    region = image.crop((sx, sy, sx + sw, sy + sh))
    asset_path = next((asset_dir / asset_name for asset_dir in _ASSET_DIR_CANDIDATES
                       if (asset_dir / asset_name).is_file()), ASSETS / asset_name)
    template = cv2.imread(str(asset_path), cv2.IMREAD_COLOR)
    if template is None:
        raise VisualAutomationError(f"无法读取界面识别资源：{asset_path}")
    template = cv2.resize(template, (sw, sh), interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_CUBIC)
    screen = cv2.cvtColor(np.asarray(region), cv2.COLOR_RGB2GRAY)
    template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    score = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)[0][0]
    return float(score)


def _page_scores(image):
    return {
        "detail": _template_score(image, "detail-signup.png", (413, 1253, 207, 89)),
        "form_top": _template_score(image, "form-top.png", (0, 0, 621, 126)),
        "form_submit": _template_score(image, "form-submit.png", (14, 1266, 593, 65)),
        "success": _template_score(image, "success-top.png", (14, 90, 593, 330)),
        "too_early": _template_score(image, "too-early-dialog.png", (94, 576, 434, 274)),
    }


def _click_ratio(bounds, x_ratio, y_ratio):
    hwnd, _, left, top, width, height = bounds
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    user32.SetForegroundWindow(hwnd)
    x, y = left + round(width * x_ratio), top + round(height * y_ratio)
    if not user32.SetCursorPos(x, y):
        raise VisualAutomationError("无法将鼠标移动到报名页面。")
    time.sleep(0.025)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.035)


ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


def _send_input(vk=0, scan=0, flags=0):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    entry = INPUT(type=1, union=INPUT_UNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))
    if user32.SendInput(1, ctypes.byref(entry), ctypes.sizeof(INPUT)) != 1:
        raise VisualAutomationError(f"Windows 未接受键盘输入（错误码 {ctypes.get_last_error()}）。")


def _select_all_and_type(value: str):
    # SendInput uses a 16-bit scan field for KEYEVENTF_UNICODE; keybd_event only
    # accepts an 8-bit scan code and corrupts many Chinese characters.
    _send_input(vk=0x11)  # Ctrl down
    _send_input(vk=ord("A"))
    _send_input(vk=ord("A"), flags=0x0002)
    _send_input(vk=0x11, flags=0x0002)
    for character in value:
        codepoints = character.encode("utf-16-le", errors="surrogatepass")
        for offset in range(0, len(codepoints), 2):
            unit = int.from_bytes(codepoints[offset:offset + 2], "little")
            _send_input(scan=unit, flags=0x0004)
            _send_input(scan=unit, flags=0x0004 | 0x0002)


def _build_fill_plan(image, fields):
    configured = {}
    for field in fields:
        label = str(field.get("label", "")).strip()
        normalized, _ = _normalize_label(label)
        if not normalized:
            raise VisualAutomationError("报名字段名称为空，请填写小程序表单上显示的字段名。")
        if normalized in configured:
            raise VisualAutomationError(f"报名字段“{label}”重复，无法确定对应输入框。")
        configured[normalized] = {"label": label, "value": str(field.get("value", ""))}
    if not configured:
        raise VisualAutomationError("请至少配置一个报名字段。")

    width, height = image.size
    lines = _ocr_lines(image)
    y_min, y_max = height * 0.12, height * 0.78
    matches = {}
    required_rows = []
    for line in lines:
        center_y = (line["y1"] + line["y2"]) / 2
        if not y_min <= center_y <= y_max or line["score"] < 0.45:
            continue
        normalized, required = _normalize_label(line["text"])
        if required and len(normalized) <= 14 and line["x2"] - line["x1"] < width * 0.30:
            required_rows.append(center_y)
        if normalized not in configured:
            continue
        if normalized in matches:
            raise VisualAutomationError(f"页面字段“{configured[normalized]['label']}”识别到多个位置，已停止以避免填错。")
        matches[normalized] = {**line, "center_y": center_y, "normalized": normalized}

    if not matches:
        labels = "、".join(item["label"] for item in configured.values())
        raise VisualAutomationError(f"没有在报名页识别到已配置字段（{labels}）；页面标签可能不同，未填写或提交。")

    # Include short labels between matched fields so vertical spacing remains correct
    # when the mini program inserts an extra row; an unmapped row aborts safely.
    matched_centers = [item["center_y"] for item in matches.values()]
    band_top = max(y_min, min(matched_centers) - height * 0.035)
    band_bottom = min(y_max, max(matched_centers) + height * 0.035)
    row_labels = {}
    for line in lines:
        center_y = (line["y1"] + line["y2"]) / 2
        line_width = line["x2"] - line["x1"]
        if not band_top <= center_y <= band_bottom or line["score"] < 0.45:
            continue
        if _is_input_hint(line["text"]):
            continue
        normalized, _ = _normalize_label(line["text"])
        if not normalized or len(normalized) > 12:
            continue
        if normalized not in configured:
            if line["x1"] <= width * 0.38 and line_width <= width * 0.32:
                raise VisualAutomationError(f"页面还包含未配置的字段“{line['text']}”；请先添加该字段及预填内容。")
            continue
        if normalized in row_labels and abs(row_labels[normalized]["center_y"] - center_y) > height * 0.015:
            raise VisualAutomationError(f"页面字段“{configured[normalized]['label']}”位置不唯一，已停止。")
        row_labels[normalized] = {**line, "center_y": center_y, "normalized": normalized}

    ordered_rows = sorted(row_labels.values(), key=lambda item: item["center_y"])
    if not ordered_rows:
        raise VisualAutomationError("未能定位报名字段的页面位置；未填写或提交。")

    for marker_y in required_rows:
        nearest = min(ordered_rows, key=lambda item: abs(item["center_y"] - marker_y))
        if abs(nearest["center_y"] - marker_y) > max(22, height * 0.022):
            raise VisualAutomationError("报名页存在未配置的必填字段；请补充字段映射后重试。")

    gaps = [b["center_y"] - a["center_y"] for a, b in zip(ordered_rows, ordered_rows[1:])]
    step = float(np.median(gaps)) if gaps else height * 0.0915
    if step < 24:
        raise VisualAutomationError("报名字段间距识别异常；未填写或提交。")

    plan = []
    for index, row in enumerate(ordered_rows):
        if index + 1 < len(ordered_rows):
            target_y = (row["center_y"] + ordered_rows[index + 1]["center_y"]) / 2
            row_step = ordered_rows[index + 1]["center_y"] - row["center_y"]
        else:
            row_step = gaps[-1] if gaps else step
            target_y = row["center_y"] + row_step / 2
        field = configured[row["normalized"]]
        if not field["value"].strip():
            raise VisualAutomationError(f"页面字段“{field['label']}”没有预填内容；未提交。")
        half_height = max(8, round(row_step * 0.24))
        plan.append({
            "label": field["label"], "value": field["value"],
            "x_ratio": 0.50, "y_ratio": target_y / height,
            "region": (round(width * 0.05), max(0, round(target_y - half_height)),
                       round(width * 0.95), min(height, round(target_y + half_height))),
        })
    return plan, len(configured) - len(plan)


def visual_signup(task: dict, binding=None, early_retry_seconds=30) -> str:
    """Match a known screen before every action; abort rather than click an unknown page."""
    try:
        early_retry_seconds = int(early_retry_seconds)
    except (TypeError, ValueError):
        raise VisualAutomationError("未开始时的自动重试时长配置无效，请设置为 1 到 600 秒。")
    if not 1 <= early_retry_seconds <= 600:
        raise VisualAutomationError("未开始时的自动重试时长须为 1 到 600 秒。")
    fields = task.get("fields", [])
    configured_labels = [_normalize_label(field.get("label", ""))[0] for field in fields]
    if not fields or any(not label for label in configured_labels) or len(set(configured_labels)) != len(configured_labels):
        raise VisualAutomationError("请配置至少一个唯一的页面字段名称，并填写对应的预填内容。")

    if not binding:
        raise VisualAutomationError("没有有效的 QQ 页面预绑定。请在报名任务中先点击“连接并预检 QQ 页面”。")
    bounds = _choose_window(binding)
    _activate(bounds[0])
    image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
    scores = _page_scores(image)
    if scores["detail"] < THRESHOLD:
        raise VisualAutomationError(f"当前画面不像已配置的活动详情页（识别分数 {scores['detail']:.2f}）；未点击。")

    # The event page keeps the blue signup action in the bottom-right navigation slot.
    _click_ratio(bounds, 0.833, 0.966)
    initial_deadline = time.monotonic() + 2.5
    early_deadline = None
    form_ready = False
    early_retries = 0
    while True:
        image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
        scores = _page_scores(image)
        if scores["form_top"] >= THRESHOLD and scores["form_submit"] >= THRESHOLD:
            form_ready = True
            break
        if scores["too_early"] >= EARLY_MODAL_THRESHOLD:
            if early_deadline is None:
                early_deadline = time.monotonic() + early_retry_seconds
            if time.monotonic() >= early_deadline:
                raise VisualAutomationError(f"首次检测到“接龙未到开始时间”弹窗后 {early_retry_seconds} 秒仍未进入填写页（已重试 {early_retries} 次）；已停止，没有填写或提交。")
            # Modal's sole action is the centered “我知道了” button.
            _click_ratio(bounds, 0.50, 0.602)
            close_deadline = min(time.monotonic() + 0.65, early_deadline)
            dismissed = False
            while time.monotonic() < close_deadline:
                image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
                if _template_score(image, "too-early-dialog.png", (94, 576, 434, 274)) < EARLY_MODAL_THRESHOLD:
                    dismissed = True
                    break
                time.sleep(0.02)
            if not dismissed:
                raise VisualAutomationError("点击“我知道了”后弹窗仍未消失；已停止，没有再次点击报名。")
            image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
            if _page_scores(image)["detail"] < THRESHOLD:
                raise VisualAutomationError("弹窗关闭后未确认回到活动详情页；已停止，没有再次点击报名。")
            _click_ratio(bounds, 0.833, 0.966)
            early_retries += 1
            continue
        deadline = early_deadline or initial_deadline
        if time.monotonic() >= deadline:
            if early_deadline:
                raise VisualAutomationError(f"首次检测到“接龙未到开始时间”弹窗后 {early_retry_seconds} 秒仍未进入填写页（已重试 {early_retries} 次）；已停止，没有填写或提交。")
            break
        time.sleep(0.025)
    if not form_ready:
        raise VisualAutomationError("点击报名后没有识别到填写页；已停止，没有填写或提交。")

    # Match visible form labels to configured values; order and extra saved fields
    # do not determine where text is entered.
    fill_plan, skipped_fields = _build_fill_plan(image, fields)
    before_fill = image.copy()
    for item in fill_plan:
        _click_ratio(bounds, item["x_ratio"], item["y_ratio"])
        _select_all_and_type(item["value"])
        time.sleep(0.12)  # Let the embedded Chromium form repaint before the next field.

    # The documented form shows the private-information checkbox checked by default.
    image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
    scores = _page_scores(image)
    if scores["form_top"] < THRESHOLD or scores["form_submit"] < THRESHOLD:
        raise VisualAutomationError("填写期间页面布局发生变化；已停止，没有点击提交。请检查已填内容。")
    for item in fill_plan:
        old = np.asarray(before_fill.crop(item["region"]), dtype=np.int16)
        new = np.asarray(image.crop(item["region"]), dtype=np.int16)
        if old.shape != new.shape or np.count_nonzero(np.max(np.abs(old - new), axis=2) > 20) < 10:
            raise VisualAutomationError(f"字段“{item['label']}”没有观察到填入变化；已停止，没有点击提交。请检查页面。")
    _click_ratio(bounds, 0.50, 0.966)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        image = _capture((bounds[2], bounds[3], bounds[4], bounds[5]))
        if _template_score(image, "success-top.png", (14, 90, 593, 330)) >= THRESHOLD:
            skipped_text = f"；另有 {skipped_fields} 项预设字段未出现在页面，已跳过" if skipped_fields else ""
            return f"报名成功：已识别到提交成功页面；按字段名称匹配并填写 {len(fill_plan)} 项{skipped_text}。"
        time.sleep(0.05)
    return "已点击提交，但未能确认成功页面；请人工核实，不会自动重试。"
