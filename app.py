#!/usr/bin/env python3
"""Local Shopee video recognition for an Android emulator. Uses Python stdlib."""

from __future__ import annotations

import csv
import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree as ET

from build_android_helper import build as build_android_helper, build_clipboard
from browser_link import ChromeLinkResolver, BrowserLinkError, ScanCancelled, shopee_url, final_video_code
from promotion import promote, read_receipts, write_receipt, confirm_receipt, DO_NOT_REPUBLISH
from popup_capture import prepare_popup_reader, read_popup_capture


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

ALIASES = {
    "videoid": "video_id",
    "视频id": "video_id",
    "视频编号": "video_id",
    "视频ID": "video_id",
    "videolink": "video_link",
    "视频链接": "video_link",
    "视频链接地址": "video_link",
    "duration": "duration",
    "时长": "duration",
    "视频时长": "duration",
    "dailybudget": "daily_budget",
    "日预算": "daily_budget",
    "每日预算": "daily_budget",
    "objective": "objective",
    "目标": "objective",
    "广告目标": "objective",
}
REQUIRED_FIELDS = ("video_id", "video_link", "duration", "daily_budget", "objective")
XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}

scan_lock = threading.RLock()
fast_ui_lock = threading.RLock()
fast_ui_ready: dict[str, str] = {}
clipboard_ready: dict[str, str] = {}
scan_stop = threading.Event()
scan_resume = threading.Event()
resume_action: dict = {}
SESSION_PATH = ROOT / '.runtime' / 'session.json'
scan_state: dict = {
    "running": False,
    "phase": "idle",
    "message": "",
    "serial": None,
    "error": None,
    "rows": [],
    "scanned": [],
    "run_id": "",
    "worker_active": False,
}


def json_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def normalize_header(value: object) -> str:
    text = str(value or "").strip().lower()
    compact = re.sub(r"[\s_\-./:：()（）]+", "", text)
    return ALIASES.get(compact, "")


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def extract_video_code(value: str) -> str:
    """Extract the Shopee share-video identifier from direct or universal links."""
    candidates = [str(value or "").strip()]
    for _ in range(3):
        current = candidates[-1]
        decoded = unquote(current)
        if decoded == current or decoded in candidates:
            break
        candidates.append(decoded)
    for candidate in list(candidates):
        parsed = urlparse(candidate)
        for redirect in parse_qs(parsed.query).get("redir", []):
            candidates.append(redirect)
            candidates.append(unquote(redirect))
    for candidate in candidates:
        match = re.search(r"/share-video/([A-Za-z0-9_-]+={0,2})(?=[/?&#]|$)", candidate, re.IGNORECASE)
        if match:
            return match.group(1).rstrip("=")
    return ""


def parse_csv(content: bytes) -> list[list[str]]:
    text = content.decode("utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[clean_cell(cell) for cell in row] for row in csv.reader(io.StringIO(text), dialect)]


def parse_xlsx(content: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        names = set(book.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("文件不是有效的 .xlsx 工作簿。")

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", XML_NS):
                shared_strings.append("".join(node.text or "" for node in item.findall(".//main:t", XML_NS)))

        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        sheet = workbook.find("main:sheets/main:sheet", XML_NS)
        if sheet is None:
            raise ValueError("工作簿中没有可读取的工作表。")
        relationship_id = sheet.attrib.get(f"{{{XML_NS['rel']}}}id")
        relationships = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        target = None
        for relationship in relationships.findall("pkg:Relationship", XML_NS):
            if relationship.attrib.get("Id") == relationship_id:
                target = relationship.attrib.get("Target")
                break
        if not target:
            raise ValueError("无法定位第一个工作表。")
        sheet_path = target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = "xl/" + sheet_path
        if sheet_path not in names:
            raise ValueError("工作表文件不存在。")

        root = ET.fromstring(book.read(sheet_path))
        rows: list[list[str]] = []
        for row in root.findall(".//main:sheetData/main:row", XML_NS):
            values: dict[int, str] = {}
            max_col = -1
            for cell in row.findall("main:c", XML_NS):
                reference = cell.attrib.get("r", "")
                letters = re.match(r"([A-Z]+)", reference)
                if not letters:
                    continue
                col_index = 0
                for letter in letters.group(1):
                    col_index = col_index * 26 + ord(letter) - ord("A") + 1
                col_index -= 1
                max_col = max(max_col, col_index)
                cell_type = cell.attrib.get("t", "")
                value_node = cell.find("main:v", XML_NS)
                if cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//main:t", XML_NS))
                elif value_node is None:
                    value = ""
                elif cell_type == "s":
                    try:
                        value = shared_strings[int(value_node.text or "0")]
                    except (ValueError, IndexError):
                        value = ""
                else:
                    value = value_node.text or ""
                values[col_index] = value
            rows.append([values.get(index, "") for index in range(max_col + 1)])
        return rows


def records_from_rows(rows: list[list[str]]) -> list[dict]:
    header_index = None
    field_columns: dict[str, int] = {}
    for row_index, row in enumerate(rows[:20]):
        mapped = {normalize_header(value): index for index, value in enumerate(row) if normalize_header(value)}
        if all(field in mapped for field in REQUIRED_FIELDS):
            header_index = row_index
            field_columns = mapped
            break
    if header_index is None:
        expected = "、".join(("Video ID", "Video Link", "Duration", "Daily budget", "Objective"))
        raise ValueError(f"前 20 行中未找到包含全部必需列的标题行：{expected}。")

    records = []
    seen_ids: set[str] = set()
    for line_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(clean_cell(value) for value in row):
            continue
        item = {field: clean_cell(row[index]) if index < len(row) else "" for field, index in field_columns.items()}
        missing = [field for field in REQUIRED_FIELDS if not item.get(field)]
        link = item.get("video_link", "")
        parsed = urlparse(link)
        if link and (parsed.scheme not in ("http", "https") or not parsed.netloc):
            missing.append("有效的 http/https 视频链接")
        video_code = extract_video_code(link)
        if link and not video_code:
            missing.append("Video Link 中未找到 /share-video/ 视频代码")
        video_id = item.get("video_id", "")
        duplicate = bool(video_id and video_id in seen_ids)
        if video_id:
            seen_ids.add(video_id)
        records.append(
            {
                **item,
                "expected_code": video_code,
                "row_number": line_number,
                "valid": not missing,
                "errors": missing,
                "duplicate_id": duplicate,
            }
        )
    if not records:
        raise ValueError("表格标题行下方没有数据记录。")
    return records


def find_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    candidates = [
        Path.home() / "Library/Android/sdk/platform-tools/adb",
        Path(os.environ.get("ANDROID_SDK_ROOT", "")) / "platform-tools/adb",
        Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools/adb",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def run_adb(adb_path: str, serial: str, *args: str, timeout: int = 12) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb_path, "-s", serial, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def connected_emulators() -> tuple[list[dict], str | None, str | None]:
    adb_path = find_adb()
    if not adb_path:
        return [], None, "找不到 adb。请安装 Android SDK Platform-Tools，或设置 ANDROID_SDK_ROOT。"
    try:
        result = subprocess.run([adb_path, "devices", "-l"], capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], adb_path, f"无法读取模拟器状态：{error}"
    devices = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("emulator-"):
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices, adb_path, None


def update_scan(**changes: object) -> None:
    with scan_lock:
        scan_state.update(changes)
        save_scan_locked()


def save_scan_locked() -> None:
    if not scan_state.get('run_id'):
        return
    scan_state['revision'] = int(scan_state.get('revision', 0)) + 1
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json_bytes(scan_state)
    temporary = SESSION_PATH.with_suffix('.tmp')
    temporary.write_bytes(payload)
    temporary.replace(SESSION_PATH)
    archive = SESSION_PATH.parent / 'sessions' / (scan_state['run_id'] + '.json')
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix('.tmp')
    temporary.write_bytes(payload)
    temporary.replace(archive)


def history_link(entry: dict) -> str:
    return str(entry.get('copied_link') or entry.get('link') or '').strip()


def completed_entry(entry: dict) -> bool:
    return bool(entry.get('code')) and entry.get('status') in ('目标视频', '非目标', '已完成')


def selected_scan_rows() -> list[dict]:
    rows = scan_state.get('rows', [])
    selected = scan_state.get('selected_row_numbers')
    if selected is None:  # Restore runs saved before row selection was added.
        return rows
    selected_keys = {str(value) for value in selected}
    return [row for row in rows if str(row.get('row_number')) in selected_keys]


def deduplicate_history(entries: list[dict]) -> list[dict]:
    """Keep the first completed result, otherwise the latest attempt, for each Copy Link."""
    unique: list[dict] = []
    positions: dict[str, int] = {}
    for entry in entries:
        key = history_link(entry)
        if not key or key not in positions:
            if key:
                positions[key] = len(unique)
            unique.append(entry)
        elif not completed_entry(unique[positions[key]]):
            unique[positions[key]] = entry
    return unique


def claim_copied_link(pending: dict, link: str) -> tuple[dict, bool]:
    """Atomically attach a copied link to one history row before opening Chrome."""
    link = link.strip()
    with scan_lock:
        existing = next((row for row in scan_state['scanned']
                         if row is not pending and history_link(row) == link), None)
        if existing is None:
            pending.update(copied_link=link, link=link, final_link='')
            save_scan_locked()
            return pending, False

        scan_state['scanned'] = [row for row in scan_state['scanned'] if row is not pending]
        if completed_entry(existing):
            # Do not change its original status, timestamp, matching result or attempts.
            scan_state['cached_skips'] = int(scan_state.get('cached_skips', 0)) + 1
            save_scan_locked()
            return existing, True

        sequence = existing['sequence']
        attempts = int(existing.get('attempts') or 1) + 1
        existing.clear()
        existing.update(pending, sequence=sequence, attempts=attempts,
                        copied_link=link, link=link, final_link='')
        save_scan_locked()
        return existing, False


def restore_scan() -> None:
    try:
        saved = json.loads(SESSION_PATH.read_text())
        if not isinstance(saved.get('rows'), list) or not isinstance(saved.get('scanned'), list):
            return
        scan_state.update(saved)
        if scan_state.get('running'):
            scan_state.update(phase='interrupted', message='服务已重启，之前的记录已恢复。可重新开始识别。', error=None)
            for row in scan_state['scanned']:
                if row.get('status') == '处理中':
                    row.update(status='已中断', error='服务重启，未完成当前视频。')
                elif row.get('status') == '读取失败，待跳过':
                    row['status'] = '读取失败，未跳过'
        scan_state.update(running=False, worker_active=False, active_promotion_code='')
        scan_state['scanned'] = deduplicate_history(scan_state['scanned'])
        # The durable ledger also recovers a manual choice if saving the session
        # was interrupted after the receipt had already reached disk.
        for code, receipt in read_receipts().items():
            if receipt.get('status') == '发布中':
                receipt.update(status='结果待确认', error='上次 Publish 未取得完整结果，请确认实际投广结果。')
                write_receipt(code, receipt)
            attach_promotion_locked(code, receipt)
        save_scan_locked()
    except (OSError, ValueError, TypeError):
        pass


def clear_history() -> dict:
    """Clear traversal results from this app's saved runs, retaining their Excel rows."""
    with scan_lock:
        if scan_state.get('worker_active') or scan_state.get('running'):
            raise RuntimeError('请先停止视频识别，等待停止完成后再清空历史记录。')
        cleared = dict(scanned=[], current=0, cached_skips=0, error=None, share_seconds=None,
                       running=False, worker_active=False, phase='idle',
                       completion_reason='', matched_target_total=0, unmatched_targets=[],
                       message='历史遍历记录已清空，Excel 清单已保留。')
        archive_dir = SESSION_PATH.parent / 'sessions'
        for archive in archive_dir.glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}', archive.stem):
                continue
            saved = json.loads(archive.read_text())
            saved.update(cleared)
            saved['revision'] = int(saved.get('revision', 0)) + 1
            temporary = archive.with_suffix('.tmp')
            temporary.write_bytes(json_bytes(saved))
            temporary.replace(archive)
        scan_state.update(cleared)
        save_scan_locked()
        return copy.deepcopy(scan_state)


def scan_is_running() -> bool:
    with scan_lock:
        return bool(scan_state["running"]) and not scan_stop.is_set()


def ui_hierarchy(adb_path: str, serial: str) -> ET.Element:
    path = "/sdcard/video_link_qa_window.xml"
    dumped = run_adb(adb_path, serial, "shell", "uiautomator", "dump", path, timeout=25)
    if dumped.returncode != 0:
        raise RuntimeError((dumped.stderr or dumped.stdout or "无法读取模拟器界面。").strip())
    result = run_adb(adb_path, serial, "shell", "cat", path, timeout=15)
    if result.returncode != 0 or "<hierarchy" not in result.stdout:
        raise RuntimeError("无法读取模拟器控件树。")
    start = result.stdout.find("<hierarchy")
    return ET.fromstring(result.stdout[start:])


def node_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
    return tuple(map(int, match.groups())) if match else None


def node_center(node: ET.Element) -> tuple[int, int] | None:
    bounds = node_bounds(node)
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def find_text_node(root: ET.Element, text: str) -> ET.Element | None:
    return next((node for node in root.iter("node") if node.attrib.get("text", "").strip().casefold() == text.casefold()), None)


def tap_element(adb_path: str, serial: str, node: ET.Element) -> None:
    point = node_center(node)
    if not point:
        raise RuntimeError("无法确定模拟器控件的位置。")
    run_adb(adb_path, serial, "shell", "input", "tap", str(point[0]), str(point[1]))


def prepare_fast_ui(adb_path: str, serial: str) -> None:
    """Build once and deploy only when the helper has changed."""
    with fast_ui_lock:
        sdk = Path(adb_path).resolve().parent.parent
        artifact = build_android_helper(sdk)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if fast_ui_ready.get(serial) == digest:
            return
        pushed = run_adb(adb_path, serial, "push", str(artifact), "/data/local/tmp/video-buttons.jar")
        if pushed.returncode != 0:
            raise RuntimeError((pushed.stderr or pushed.stdout or "无法准备按钮识别程序。").strip())
        fast_ui_ready[serial] = digest


class ShareActionError(RuntimeError):
    def __init__(self, message: str, button: str = '', dismiss: bool = False):
        super().__init__(message)
        self.button = button
        self.dismiss = dismiss


def run_fast_ui(adb_path: str, serial: str, mode: str) -> dict:
    with fast_ui_lock:
        prepare_fast_ui(adb_path, serial)
        action = run_adb(
            adb_path, serial, 'shell',
            'CLASSPATH=/system/framework/uiautomator.jar:/data/local/tmp/video-buttons.jar',
            'app_process', '/system/bin', 'VideoButtons', mode, timeout=30 if mode.startswith('ad-publish:') else 20,
        )
    result = next((json.loads(line) for line in reversed(action.stdout.splitlines())
                   if line.strip().startswith('{')), None)
    if action.returncode != 0 or not isinstance(result, dict):
        fast_ui_ready.pop(serial, None)
        raise RuntimeError((action.stderr or action.stdout or '按钮识别没有返回结果。').strip()[-300:])
    if mode.startswith('ad-publish:'):
        params = json.loads(base64.b64decode(mode.split(':', 1)[1]))
        update_scan(phase='promoting', message='Publish 已处理，正在读取弹窗文字和本机截图…')
        result = read_popup_capture(result, params['capture_id'],
                                    lambda *args, **kwargs: run_adb(adb_path, serial, *args, **kwargs))
    return result


def try_open_copy_link(adb_path: str, serial: str) -> dict:
    """Locate and click exact share labels in one ADB operation, without a full XML dump."""
    started = time.monotonic()
    try:
        result = run_fast_ui(adb_path, serial, 'copy-link')
        elapsed = round(time.monotonic() - started, 2)
        print(f"Share controls: {result.get('result')} in {elapsed}s; {json.dumps(result)}", flush=True)
        update_scan(share_seconds=elapsed)
        status = result.get("result")
        button = result.get('entry_button', '')
        if status == "copied":
            return result
        if status == "wrong_app":
            raise ShareActionError("当前前台不是 Shopee Seller 视频页。", button, bool(button))
        if status == "more_missing":
            raise ShareActionError("当前视频没有找到 More 或同位置的 Promote 按钮。")
        if status == "copy_missing":
            raise ShareActionError(f"{button or '视频入口'} 已点击并等待 5 秒，但没有出现 Copy Link。", button, True)
        raise ShareActionError(result.get("error") or "按钮暂时无法点击。", button,
                               bool(button) or status in ('timeout', 'error', 'click_failed'))
    except ShareActionError:
        raise
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        raise ShareActionError(f"复制链接失败：{error}", dismiss=True) from error


def prepare_clipboard(adb_path: str, serial: str) -> None:
    artifact = build_clipboard(Path(adb_path).resolve().parent.parent)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if clipboard_ready.get(serial) == digest:
        return
    installed = run_adb(adb_path, serial, 'install', '-r', '-t', str(artifact), timeout=45)
    if installed.returncode or 'Success' not in installed.stdout:
        raise RuntimeError('无法准备模拟器剪贴板读取：' + (installed.stderr or installed.stdout)[-300:])
    clipboard_ready[serial] = digest


def read_copied_link(adb_path: str, serial: str, copied_after: int) -> str:
    request = uuid.uuid4().hex
    started = run_adb(adb_path, serial, 'shell', 'am', 'start', '-W', '-n',
                      'local.videoqa.clipboard/.ClipboardActivity', '--es', 'request', request,
                      '--el', 'since', str(copied_after), timeout=12)
    if started.returncode or 'Error:' in started.stdout:
        raise RuntimeError('无法打开模拟器链接读取程序。')
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if scan_stop.is_set():
            raise ScanCancelled()
        result = run_adb(adb_path, serial, 'shell', 'run-as', 'local.videoqa.clipboard',
                         'cat', 'files/clipboard.json', timeout=8)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {}
        if data.get('request') == request:
            if data.get('status') != 'ok':
                raise RuntimeError(data.get('error') or '读取剪贴板失败。')
            link = str(data.get('link') or '').strip()
            if not shopee_url(link) or int(data.get('copied_at', 0)) < copied_after:
                raise RuntimeError('没有读取到本次新复制的 Shopee 链接，请重试。')
            return link
        scan_stop.wait(0.15)
    raise RuntimeError('读取剪贴板超时，请重试当前视频。')


def navigate_to_first_video(adb_path: str, serial: str) -> None:
    update_scan(phase="opening_seller", message="正在打开 Shopee Seller 首页…", serial=serial, error=None)
    opened = run_adb(
        adb_path,
        serial,
        "shell",
        "am",
        "start",
        "-n",
        "com.shopee.shopeeseller/com.shopee.app.ui.home.HomeActivity_",
        timeout=20,
    )
    if opened.returncode != 0:
        raise RuntimeError((opened.stderr or opened.stdout or "无法打开 Shopee Seller。").strip())
    scan_stop.wait(2.5)
    if not scan_is_running():
        return

    found_video_entry = False
    for _ in range(8):
        check_cancelled()
        root = ui_hierarchy(adb_path, serial)
        check_cancelled()
        entry = find_text_node(root, "Shopee Video")
        if entry is not None:
            tap_element(adb_path, serial, entry)
            found_video_entry = True
            break
        run_adb(adb_path, serial, "shell", "input", "swipe", "280", "213", "43", "213", "450")
        scan_stop.wait(0.5)
    if not found_video_entry:
        raise RuntimeError("Home 功能区中没有找到 Shopee Video。请确认已登录 Seller App。")

    if not scan_is_running():
        return
    update_scan(phase="opening_video_list", message="已打开 Shopee Video，正在读取视频列表…")
    scan_stop.wait(2.5)
    if not scan_is_running():
        return
    root = ui_hierarchy(adb_path, serial)
    check_cancelled()
    first_video = next(
        (node for node in root.iter("node") if node.attrib.get("content-desc", "").strip().casefold() == "click video 0"),
        None,
    )
    if first_video is None:
        raise RuntimeError("Shopee Video 页面没有显示第一条视频卡片。请先检查登录状态和视频列表。")
    tap_element(adb_path, serial, first_video)


def check_cancelled() -> None:
    if scan_stop.is_set():
        raise ScanCancelled()


def mark_entry(entry: dict, **changes) -> None:
    with scan_lock:
        entry.update(changes)
        save_scan_locked()


def attach_promotion_locked(code: str, receipt: dict) -> None:
    scan_state.setdefault('promotion_results', {})[code] = copy.deepcopy(receipt)
    for recorded in scan_state.get('scanned', []):
        if recorded.get('code') == code:
            recorded['promotion'] = copy.deepcopy(receipt)


def mark_promotion(entry: dict, receipt: dict) -> None:
    with scan_lock:
        entry['promotion'] = copy.deepcopy(receipt)
        attach_promotion_locked(entry['code'], receipt)
        save_scan_locked()


def confirm_promotion(code: str, operation_id: str, status: str) -> dict:
    with scan_lock:
        if code and scan_state.get('active_promotion_code') == code:
            raise RuntimeError('此视频的投广流程尚未结束，请等待返回视频页后再确认结果。')
        receipt = confirm_receipt(code, operation_id, status)
        attach_promotion_locked(code, receipt)
        if scan_state.get('phase') == 'complete':
            found = {row['code'] for row in scan_state.get('scanned', []) if completed_entry(row)}
            message, _ = scan_result_message(selected_scan_rows(), found,
                                            scan_state.get('completion_reason', 'targets_found'))
            if scan_state.get('home_return_error'):
                message += ' 返回主页未完成：' + scan_state['home_return_error']
            elif scan_state.get('home_returned'):
                message += ' 已返回 Shopee Seller APP 主页。'
            scan_state['message'] = message
        save_scan_locked()
        return copy.deepcopy(scan_state)


def promotion_attempt_key(receipt: dict) -> tuple:
    manual = receipt.get('manual_confirmation', {})
    return receipt.get('operation_id'), manual.get('confirmed_at'), manual.get('status')


def wait_for_action(message: str, allow_retry=True) -> dict:
    scan_resume.clear()
    with scan_lock:
        resume_action.clear()
        scan_state.update(phase="awaiting_action", message=message, error=message, allow_retry=allow_retry)
        save_scan_locked()
    while not scan_resume.wait(0.25):
        check_cancelled()
    check_cancelled()
    with scan_lock:
        action = dict(resume_action)
        scan_state.update(error=None)
    return action


def advance_video(adb_path: str, serial: str, next_index: int,
                  dismiss_menu: bool = False, check_grid: bool = False,
                  swipe: bool = True) -> bool:
    """Swipe the player first; inspect the grid only to recover a stalled position."""
    check_cancelled()
    if dismiss_menu:
        # A share sheet or Promote page can consume the swipe. Return to the player first.
        try:
            prepared = run_fast_ui(adb_path, serial, 'dismiss-menu')
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f'Share sheet recovery: {error}', flush=True)
            prepared = {'result': 'unavailable'}
        check_cancelled()
        if prepared.get('result') == 'grid':
            check_grid = True
        elif prepared.get('result') == 'wrong_app':
            raise RuntimeError('模拟器当前不在 Shopee Seller，请返回视频播放页。')
        elif prepared.get('result') not in ('ready', 'dismissed'):
            # This path is used only after an attempted share action. Do not block a
            # failed video on another accessibility timeout.
            dismissed = run_adb(adb_path, serial, 'shell', 'input', 'keyevent', '4')
            if dismissed.returncode:
                raise RuntimeError((dismissed.stderr or dismissed.stdout or '无法关闭视频分享菜单。').strip())
            scan_stop.wait(0.5)
    check_cancelled()
    if swipe:
        moved = run_adb(adb_path, serial, 'shell', 'input', 'swipe',
                        '160', '520', '160', '118', '350')
        if moved.returncode:
            raise RuntimeError((moved.stderr or moved.stdout or '模拟器未执行下一条视频的滑动。').strip())
        scan_stop.wait(1.2)
    check_cancelled()
    if check_grid:
        # A repeated link can mean the player reached the bottom. Never treat it
        # alone as completion: confirm the next card or the actual list boundary.
        result = run_fast_ui(adb_path, serial, f'grid-next:{next_index}')
        check_cancelled()
        if result.get('result') == 'list_end':
            return False
        if result.get('result') != 'opened':
            raise RuntimeError(f"视频已滑动，但还不能确认第 {next_index + 1} 条卡片或列表末尾（{result.get('result')}）。")
    return True


def scan_result_message(rows: list[dict], found_codes: set[str], reason: str) -> tuple[str, list[dict]]:
    unmatched = [
        {key: row.get(key) for key in ('row_number', 'video_id', 'expected_code')}
        for row in rows if not row.get('expected_code') or row['expected_code'] not in found_codes
    ]
    matched = len(rows) - len(unmatched)
    if reason == 'targets_found':
        message = f"Excel 中全部 {len(rows)} 条目标视频已找到，遍历已自动结束。"
    else:
        message = f"Seller 视频列表已遍历到底。Excel 目标已匹配 {matched}/{len(rows)} 条，另有 {len(unmatched)} 条未匹配；失败和跳过记录已保留。"
    if scan_state.get('promote'):
        results = scan_state.get('promotion_results', {})
        codes = {row.get('expected_code') for row in rows if row.get('expected_code')}
        statuses = [results.get(code, {}).get('status') for code in codes]
        message += (f" 投广结果：成功 {statuses.count('发布成功')}，待审核 {statuses.count('已提交审核')}，"
                    f"失败 {statuses.count('发布失败')}，待确认 {sum(value in ('结果待确认', '发布中') for value in statuses)}。")
    return message, unmatched


def finish_scan(rows: list[dict], found_codes: set[str], reason: str, adb_path: str, serial: str) -> None:
    message, unmatched = scan_result_message(rows, found_codes, reason)
    update_scan(phase='returning_home', completion_reason=reason,
                message=message + ' 正在点击返回，回到 Shopee Seller APP 主页…', error=None,
                matched_target_total=len(rows) - len(unmatched), unmatched_targets=unmatched,
                home_returned=False, home_return_error='')
    home_error = ''
    try:
        check_cancelled()
        returned = run_fast_ui(adb_path, serial, 'return-home')
        check_cancelled()
        if returned.get('result') != 'home':
            raise RuntimeError(returned.get('error') or f"尚未确认 APP 主页（{returned.get('result')}）。")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        home_error = str(error)
    with scan_lock:
        # A manual choice may have arrived while the app was returning Home.
        message, _ = scan_result_message(rows, found_codes, reason)
        update_scan(phase='complete', message=message + (
                        ' 返回主页未完成：' + home_error if home_error else ' 已返回 Shopee Seller APP 主页。'),
                    error=home_error or None, home_returned=not home_error, home_return_error=home_error,
                    finished_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))


def scan_start_worker(rows: list[dict], serial: str, adb_path: str) -> None:
    entry = None
    browser = ChromeLinkResolver()
    try:
        promotion_enabled = bool(scan_state.get('promote'))
        attempted_ads = {}
        with scan_lock:
            receipts = read_receipts() if promotion_enabled else {}
            for code, receipt in receipts.items():
                if receipt.get('status') == '发布中':
                    receipt.update(status='结果待确认', error='上次 Publish 未取得完整结果，请确认实际投广结果。')
                    write_receipt(code, receipt)
            found_codes = {row['code'] for row in scan_state['scanned'] if completed_entry(row)}
            all_codes = {row.get('expected_code') for row in scan_state.get('rows', [])}
            scan_state['promotion_results'] = {code: copy.deepcopy(receipt) for code, receipt in receipts.items()
                                               if code in all_codes}
            for recorded in scan_state['scanned']:
                if recorded.get('code') in receipts:
                    recorded['promotion'] = copy.deepcopy(receipts[recorded['code']])
            save_scan_locked()

        def ad_handled(code, receipt):
            # Selecting an already successful video schedules a fresh attempt
            # in this run; later encounters still reuse this run's result.
            if receipt.get('status') == '发布成功' and code not in attempted_ads:
                return False
            return (receipt.get('status') in DO_NOT_REPUBLISH or
                    (code in attempted_ads and attempted_ads[code] == promotion_attempt_key(receipt)))

        def all_targets_handled():
            latest = read_receipts() if promotion_enabled else {}
            return all(row.get('expected_code') and row['expected_code'] in found_codes
                       and (not promotion_enabled or ad_handled(row['expected_code'], latest.get(row['expected_code'], {})))
                       for row in rows)

        if all_targets_handled():
            finish_scan(rows, found_codes, 'targets_found', adb_path, serial)
            return
        update_scan(phase="preparing", message="正在准备按钮识别和模拟器链接读取…")
        if promotion_enabled:
            prepare_popup_reader()
        prepare_fast_ui(adb_path, serial)
        check_cancelled()
        prepare_clipboard(adb_path, serial)
        check_cancelled()
        navigate_to_first_video(adb_path, serial)
        check_cancelled()
        seen_links: set[str] = set()
        video_index = 0
        verified_index = 0
        position_verified = True
        verify_each_position = False
        unreadable_streak = 0
        index = 0
        while not scan_stop.is_set():
            index += 1
            entry = dict(sequence=index, video_index=video_index, code="", link="", copied_link="", final_link="", video_ids=[],
                         row_numbers=[], status="处理中", duplicate=False, error="", errors=[],
                         attempts=0, recorded_at="", source="自动")
            with scan_lock:
                entry['sequence'] = max((int(row.get('sequence') or 0)
                                         for row in scan_state['scanned']), default=0) + 1
                scan_state['scanned'].append(entry)
                scan_state.update(current=index)
                save_scan_locked()
            dismiss_menu = False
            cached_complete = False
            force_grid = False
            try:
                mark_entry(entry, attempts=1, status="处理中", error="")
                update_scan(phase="copying", message=f"第 {index} 个视频：优先查找 More，找不到则尝试同位置的 Promote；点击后等待 5 秒再复制链接…")
                copied = try_open_copy_link(adb_path, serial)
                check_cancelled()
                mark_entry(entry, share_button=copied.get('entry_button', ''))
                update_scan(phase="reading_clipboard", message=f"第 {index} 个视频：正在读取刚复制的链接…")
                link = read_copied_link(adb_path, serial, int(copied['copy_started_at']))
                check_cancelled()
                entry, cached_complete = claim_copied_link(entry, link)
                if cached_complete:
                    update_scan(phase='reused', message=f"第 {index} 个视频的 Copy Link 已有完成记录，保留原结果，跳过浏览器识别。", error=None)
                else:
                    retry_note = '重试历史异常记录；' if entry['attempts'] > 1 else ''
                    update_scan(phase="opening_browser", message=f"第 {index} 个视频：{retry_note}正在 Chrome 小窗口中等待最终地址（最多 100 秒）…")
                    final_link = browser.resolve(link, scan_stop, lambda url: mark_entry(entry, final_link=url))
                    check_cancelled()
                    code = final_video_code(final_link)
                    if not code:
                        raise BrowserLinkError("浏览器最终地址中没有视频代码。", final_link)
                    matches = [row for row in rows if row.get('expected_code') == code]
                    with scan_lock:
                        duplicate = any(previous is not entry and previous.get('code') == code
                                        for previous in scan_state['scanned'])
                    mark_entry(entry, code=code, final_link=final_link,
                               video_ids=[str(row.get('video_id', '')) for row in matches],
                               row_numbers=[row.get('row_number') for row in matches], duplicate=duplicate,
                               status="目标视频" if matches else "非目标", error="",
                               recorded_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
                    update_scan(phase="recorded", message=f"第 {index} 个视频已记录：{'目标视频，立即处理当前视频的投广。' if matches and promotion_enabled else '目标视频。' if matches else '非目标，准备切换下一条。'}", error=None)
                found_codes.add(entry['code'])
                if not promotion_enabled and all_targets_handled():
                    finish_scan(rows, found_codes, 'targets_found', adb_path, serial)
                    return
            except ScanCancelled:
                raise
            except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
                check_cancelled()
                if cached_complete:
                    raise
                if isinstance(error, BrowserLinkError) and error.final_url:
                    mark_entry(entry, final_link=error.final_url)
                if isinstance(error, ShareActionError):
                    mark_entry(entry, share_button=error.button)
                    dismiss_menu = error.dismiss
                detail = str(error)
                mark_entry(entry, status="读取失败，待跳过", error=detail, errors=entry['errors'] + [detail],
                           recorded_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
                update_scan(phase="skipping", message=f"第 {index} 个视频读取失败，已记录原因，正在自动跳过…", error=None)
            check_cancelled()
            # Finish promotion for THIS player before reaching advance_video.
            # Cached recognition follows the same path; it only skips the browser.
            current_promotion = None
            if promotion_enabled and completed_entry(entry):
                matches = [row for row in rows if row.get('expected_code') == entry['code']]
                if matches:
                    code = entry['code']
                    # Manual choices for other videos can arrive during a run.
                    # Claim this code before reading its latest durable decision.
                    with scan_lock:
                        scan_state['active_promotion_code'] = code
                        save_scan_locked()
                    try:
                        receipt = read_receipts().get(code, {})
                        if not ad_handled(code, receipt):
                            update_scan(phase='promoting', message=f'第 {index} 个视频已识别为目标，立即执行当前视频的投广；投广结果保存并完成回退后才切换下一条。', error=None)
                            try:
                                receipt = promote(code, matches,
                                                  lambda mode: run_fast_ui(adb_path, serial, mode), scan_stop,
                                                  lambda value: mark_promotion(entry, value),
                                                  lambda message: update_scan(phase='promoting', message=f'第 {index} 个目标视频：{message}', error=None),
                                                  force_success=receipt.get('status') == '发布成功')
                            except ScanCancelled:
                                raise
                            except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
                                receipt = copy.deepcopy(entry.get('promotion', {}))
                                receipt.update(status='结果待确认' if receipt.get('publish_attempted') else '发布失败',
                                               error=str(error), return_error='需要恢复视频页面。')
                            # A manually confirmed failure changes the attempt key,
                            # making it eligible at the next encounter, even this run.
                            attempted_ads[code] = promotion_attempt_key(receipt)
                            dismiss_menu = bool(receipt.get('return_error'))
                            force_grid = receipt.get('return_page') == 'grid'
                        if receipt:
                            mark_promotion(entry, receipt)
                            current_promotion = receipt
                    finally:
                        update_scan(active_promotion_code='')
            check_cancelled()
            if all_targets_handled():
                finish_scan(rows, found_codes, 'targets_found', adb_path, serial)
                return
            identity = entry.get('code') or entry.get('copied_link')
            repeated_link = bool(identity and identity in seen_links and not position_verified)
            if identity:
                seen_links.add(identity)
                unreadable_streak = 0
                verify_each_position = False
                if not repeated_link:
                    verified_index = video_index
            else:
                unreadable_streak += 1
                if unreadable_streak >= 3:
                    verify_each_position = True
            check_grid = repeated_link or verify_each_position or force_grid
            # If a swipe did not advance (or its result is unknown), locate the
            # pending card again instead of skipping an additional list position.
            next_index = (verified_index + 1 if verify_each_position else
                          video_index if repeated_link else video_index + 1)
            next_message = '已有完成记录，保留原结果' if cached_complete else '已记录'
            if current_promotion:
                next_message = f"的投广处理已结束（{current_promotion.get('status', '结果待确认')}），结果已保存"
                if current_promotion.get('return_error'):
                    next_message += '，正在恢复视频页面'
            update_scan(phase="opening_next", message=f"第 {index} 个视频{next_message}，正在播放页滑动到下一条…", error=None)
            navigation_attempts = 0
            swipe_next = True
            while True:
                try:
                    has_next = advance_video(adb_path, serial, next_index=next_index,
                                             dismiss_menu=dismiss_menu, check_grid=check_grid,
                                             swipe=swipe_next)
                    if entry.get('status') == '读取失败，待跳过':
                        mark_entry(entry, status='读取失败，已跳过')
                    if not has_next:
                        finish_scan(rows, found_codes, 'list_exhausted', adb_path, serial)
                        return
                    video_index = next_index
                    position_verified = check_grid
                    if position_verified:
                        verified_index = video_index
                    break
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    check_cancelled()
                    navigation_attempts += 1
                    if not cached_complete:
                        mark_entry(entry, navigation_error=str(error))
                    # Recover automatically first. A per-video failure should never
                    # immediately leave the run waiting for the user's next click.
                    if navigation_attempts < 3:
                        update_scan(phase='opening_next', message=f"下一条切换暂未完成，正在自动恢复（{navigation_attempts}/2）…", error=None)
                        check_grid = True
                        dismiss_menu = False
                        swipe_next = False
                        scan_stop.wait(1)
                        continue
                    wait_for_action(f"第 {index} 个视频记录已保存，但模拟器切换仍失败：{error}。请恢复 Seller 后继续下一条。", allow_retry=False)
                    navigation_attempts = 0
                    check_grid = True
                    dismiss_menu = False
                    swipe_next = False
        check_cancelled()
    except ScanCancelled:
        if entry and entry.get('status') == '处理中':
            mark_entry(entry, status="已停止", error="用户停止了当前视频的处理。")
        elif entry and entry.get('status') == '读取失败，待跳过':
            mark_entry(entry, status='读取失败，未跳过')
        update_scan(phase="stopped", message="识别已停止，Excel 清单和全部遍历记录已保存。", error=None)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, ET.ParseError) as error:
        update_scan(phase="error", error=str(error), message=str(error))
    finally:
        try:
            browser.close()
        except BrowserLinkError as error:
            update_scan(cleanup_error=str(error), message=f"识别已结束，但浏览器窗口清理未完成：{error}")
        update_scan(running=False, worker_active=False, active_promotion_code='')


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoRecognition/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def respond(self, status: int, data: object, content_type: str = "application/json; charset=utf-8") -> None:
        body = data if isinstance(data, bytes) else json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/device":
            devices, adb_path, error = connected_emulators()
            available = next((device for device in devices if device["state"] == "device"), None)
            self.respond(
                200,
                {
                    "devices": devices,
                    "connected": available is not None,
                    "serial": available["serial"] if available else None,
                    "adb_found": adb_path is not None,
                    "error": error,
                },
            )
            return
        if self.path == "/api/scan/status":
            with scan_lock:
                snapshot = copy.deepcopy(scan_state)
            self.respond(200, snapshot)
            return
        if self.path in ("/", "/index.html"):
            try:
                self.respond(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self.respond(500, {"error": "index.html 不存在。"})
            return
        self.respond(404, {"error": "未找到此路径。"})

    def read_json(self) -> dict | None:
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_UPLOAD_BYTES:
                raise ValueError('请求为空或超过 50 MB。')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('请求必须是 JSON 对象。')
            return body
        except (ValueError, UnicodeDecodeError) as error:
            self.respond(400, {'error': str(error)})
            return None

    def do_POST(self) -> None:  # noqa: N802
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'):
            self.respond(403, {'error': '请通过本机网页服务操作。'})
            return
        request_url = urlparse(self.path)
        request_path = request_url.path
        if request_path == "/api/parse":
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                self.respond(413, {"error": "文件为空或超过 50 MB 限制。"})
                return
            name = parse_qs(request_url.query).get("name", [""])[0]
            suffix = Path(name).suffix.lower()
            content = self.rfile.read(length)
            try:
                if suffix == ".csv":
                    matrix = parse_csv(content)
                elif suffix == ".xlsx":
                    matrix = parse_xlsx(content)
                else:
                    raise ValueError("请上传 .xlsx 或 .csv 文件。旧版 .xls 暂不支持。")
                records = records_from_rows(matrix)
            except (UnicodeDecodeError, zipfile.BadZipFile, ET.ParseError, ValueError, csv.Error) as error:
                self.respond(400, {"error": str(error)})
                return
            self.respond(200, {"records": records, "count": len(records), "source": name})
            return

        if request_path == "/api/scan/clear":
            try:
                snapshot = clear_history()
            except RuntimeError as error:
                self.respond(409, {'error': str(error)})
                return
            except (OSError, ValueError, TypeError) as error:
                self.respond(500, {'error': f'历史记录未全部清空，请重试：{error}'})
                return
            self.respond(200, {'cleared': True, 'scan': snapshot})
            return

        if request_path == "/api/promotion/confirm":
            body = self.read_json()
            if body is None:
                return
            if any(not isinstance(body.get(key), str) for key in ('video_code', 'operation_id', 'status')):
                self.respond(400, {'error': '缺少视频代码、投广操作编号或确认结果。'})
                return
            try:
                snapshot = confirm_promotion(body['video_code'], body['operation_id'], body['status'])
            except ValueError as error:
                self.respond(400, {'error': str(error)})
                return
            except RuntimeError as error:
                self.respond(409, {'error': str(error)})
                return
            except OSError as error:
                self.respond(500, {'error': f'确认结果保存未完成，请重试：{error}'})
                return
            self.respond(200, {'confirmed': True, 'scan': snapshot})
            return

        if request_path == "/api/scan/start":
            body = self.read_json()
            if body is None:
                return
            all_rows = body.get("rows", [])
            if not isinstance(all_rows, list) or not all_rows or not all(isinstance(row, dict) for row in all_rows):
                self.respond(400, {"error": "没有可识别的视频清单。"})
                return
            for row in all_rows:
                row['expected_code'] = extract_video_code(row.get('video_link', ''))
            selected = body.get('selected_row_numbers')
            known = {str(row.get('row_number')) for row in all_rows}
            if not isinstance(selected, list) or not selected or any(str(value) not in known for value in selected):
                self.respond(400, {"error": "请先在目标视频列表中选择本轮要匹配和投广的视频。"})
                return
            selected_keys = {str(value) for value in selected}
            rows = [row for row in all_rows if str(row.get('row_number')) in selected_keys]
            if any(not row['expected_code'] for row in rows):
                self.respond(400, {"error": "选中的视频有无法解析的 Video Link，请取消选中或修正文件。"})
                return
            devices, adb_path, error = connected_emulators()
            emulator = next((device for device in devices if device["state"] == "device"), None)
            if error or not adb_path or not emulator:
                self.respond(503, {"error": error or "没有已连接的 Android 模拟器。请启动模拟器并等待设备在线。"})
                return
            with scan_lock:
                if scan_state.get('worker_active'):
                    self.respond(409, {"error": "已有识别任务在运行或停止中，请等待当前操作结束。"})
                    return
                scan_stop.clear()
                run_id = uuid.uuid4().hex
                history = deduplicate_history(scan_state.get('scanned', []))
                scan_state.clear()
                scan_state.update(running=True, worker_active=True, phase="starting", message="正在启动识别…",
                                  serial=emulator['serial'], error=None, target_total=len(rows), current=0,
                                  rows=all_rows, selected_row_numbers=[str(row['row_number']) for row in rows],
                                  file=str(body.get('file') or ''), run_id=run_id,
                                  scanned=history, cached_skips=0, promote=body.get('promote') is True,
                                  promotion_results={}, active_promotion_code='')
                save_scan_locked()
            threading.Thread(target=scan_start_worker, args=(rows, emulator['serial'], adb_path), daemon=True).start()
            self.respond(202, {"started": True, "run_id": run_id, "serial": emulator['serial'], "total": len(rows)})
            return

        if request_path == "/api/scan/resume":
            body = self.read_json()
            if body is None:
                return
            mode = body.get('mode', 'retry')
            link = str(body.get('link') or '').strip()
            if mode not in ('retry', 'skip'):
                self.respond(400, {'error': '不支持此操作。'})
                return
            try:
                if link and not shopee_url(link):
                    raise ValueError('请补充完整的 Shopee http/https 视频链接。')
            except ValueError as error:
                self.respond(400, {'error': str(error)})
                return
            with scan_lock:
                if not scan_state.get('running') or scan_state.get('phase') != 'awaiting_action' or scan_stop.is_set():
                    self.respond(409, {'error': '当前没有等待处理的视频。'})
                    return
                if not scan_state.get('allow_retry', True) and mode != 'skip':
                    self.respond(409, {'error': '当前视频已经记录，请继续下一条或停止识别。'})
                    return
                resume_action.update(mode=mode, link=link)
                scan_state.update(phase='resuming', message='正在继续识别…', error=None)
                scan_resume.set()
            self.respond(202, {'resuming': True})
            return

        if request_path == "/api/scan/stop":
            scan_stop.set()
            scan_resume.set()
            with scan_lock:
                active = scan_state.get('worker_active', False)
                scan_state.update(phase='stopping' if active else 'stopped',
                                  message='正在停止，已完成的记录会保留…' if active else '识别已停止，记录已保存。', error=None)
                save_scan_locked()
            self.respond(200, {'stopping': active, 'stopped': not active})
            return
        self.respond(404, {"error": "未找到此路径。"})


def main() -> None:
    restore_scan()
    print(f"Video recognition is ready at http://{HOST}:{PORT}")
    print("Keep this terminal open while using the tool. Press Ctrl+C to stop.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
