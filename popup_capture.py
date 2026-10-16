"""Capture and read transient Seller publish messages locally on macOS."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import threading

from promotion import classify_popup

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'native' / 'PopupOCR.m'
BINARY = ROOT / '.runtime' / 'popup-ocr'
BUILD_LOCK = threading.Lock()


def prepare_popup_reader() -> Path:
    with BUILD_LOCK:
        if BINARY.exists() and BINARY.stat().st_mtime >= SOURCE.stat().st_mtime:
            return BINARY
        BINARY.parent.mkdir(parents=True, exist_ok=True)
        cache = BINARY.parent / 'clang-module-cache'
        cache.mkdir(exist_ok=True)
        compiled = subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-fmodules',
                                   '-fmodules-cache-path=' + str(cache), '-framework', 'Foundation',
                                   '-framework', 'Vision', str(SOURCE), '-o', str(BINARY)],
                                  capture_output=True, text=True, timeout=90)
        if compiled.returncode:
            raise RuntimeError('本机弹窗文字识别程序编译失败：' + compiled.stderr[-600:])
        return BINARY


def read_popup_capture(response: dict, capture_id: str, run_adb) -> dict:
    if response.get('result') == 'not_published':
        return response
    if not re.fullmatch(r'[0-9a-f]{32}', capture_id):
        raise ValueError('发布截图编号无效。')
    result = dict(response)
    remote = '/data/local/tmp/videoqa-popup-' + capture_id
    destination = ROOT / '.runtime' / 'publish-popups' / capture_id
    destination.mkdir(parents=True, exist_ok=True)
    native = str(result.get('popup_text') or '').strip()
    if native:
        result['popup_source'] = '控件 / 提示事件'
    pulled_ok = False
    try:
        pulled = run_adb('pull', remote + '/.', str(destination), timeout=20)
        if pulled.returncode:
            raise RuntimeError((pulled.stderr or pulled.stdout or '未取得发布截图').strip()[-300:])
        frames = sorted(destination.glob('frame-*.png'))
        before = destination / 'before.png'
        if not frames or not before.exists():
            raise RuntimeError('发布前后截图不完整，无法可靠识别新增提示。')
        pulled_ok = True
        process = subprocess.run([str(prepare_popup_reader()), str(before), *map(str, frames)],
                                 capture_output=True, text=True, timeout=45)
        if process.returncode:
            raise RuntimeError(process.stderr[-300:] or '本机弹窗文字识别未完成。')
        readings = json.loads(process.stdout)
        (destination / 'ocr.json').write_text(json.dumps(readings, ensure_ascii=False, indent=2))
        baseline = next((item for item in readings if item.get('file') == 'before.png'), None)
        if not baseline or baseline.get('error'):
            raise RuntimeError('发布前画面文字未能读取，无法排除页面原有内容。')
        normalize = lambda value: re.sub(r'\W+', '', value).casefold()
        existing = {normalize(line) for line in baseline.get('lines', [])}
        messages = [native] if native else []
        for item in readings:
            if item.get('file') == 'before.png' or item.get('error'):
                continue
            added = [line for line in item.get('lines', []) if normalize(line) not in existing]
            text = '\n'.join(added).strip()
            # Save all OCR output as evidence, but classify only frames with a
            # result message, excluding unchanged form labels and balances.
            if text and classify_popup(text) != '结果待确认' and text not in messages:
                messages.append(text)
        if len(messages) > (1 if native else 0):
            result['popup_source'] = '控件 / 提示事件 + 本机截图识别' if native else '本机截图识别'
        result['popup_text'] = '\n'.join(messages)
        result['popup_capture_path'] = str(destination)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result['popup_capture_error'] = str(error)
        result['popup_capture_path'] = str(destination)
    finally:
        # Each attempt owns this UUID directory. Local evidence stays available.
        if pulled_ok:
            try:
                run_adb('shell', 'rm', '-rf', remote, timeout=8)
            except (OSError, subprocess.SubprocessError):
                pass
    return result
