"""Open one dedicated Chrome tab and read its final URL through Chrome's macOS scripting API."""
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

FINAL_URL_TIMEOUT_SECONDS = 100


class ScanCancelled(Exception):
    pass


class BrowserLinkError(RuntimeError):
    def __init__(self, message, final_url=''):
        super().__init__(message)
        self.final_url = final_url


def shopee_url(value):
    parsed = urlparse(str(value or '').strip())
    host = (parsed.hostname or '').lower()
    return (parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password
            and parsed.port in (None, 80, 443)
            and bool(host == 'shp.ee' or host.endswith('.shp.ee') or re.fullmatch(
                r'(?:[a-z0-9-]+\.)*shopee\.(?:com\.(?:my|br|mx|co|ar)|co\.(?:id|th)|sg|my|vn|ph|tw|cl|pl)', host)))


def final_video_code(value):
    """Only a real share-video address, never the redir query of an unresolved short link."""
    if not shopee_url(value):
        return ''
    match = re.search(r'/share-video/([A-Za-z0-9_-]+={0,2})/?$', unquote(urlparse(value).path))
    return match.group(1).rstrip('=') if match else ''


SCRIPT = '''on run argv
    tell application id "com.google.Chrome"
        set operation to item 1 of argv
        if operation is "create" then
            set targetWindow to make new window with properties {bounds:{80, 80, 980, 760}}
            try
                set targetTab to active tab of targetWindow
                set zoomed of targetWindow to false
                set bounds of targetWindow to {80, 80, 980, 760}
                return (id of targetWindow as text) & linefeed & (id of targetTab as text)
            on error errorMessage number errorNumber
                try
                    close targetWindow
                end try
                error errorMessage number errorNumber
            end try
        end if
        set targetWindow to missing value
        repeat with candidate in windows
            if (id of candidate as text) is (item 2 of argv) then
                set targetWindow to contents of candidate
                exit repeat
            end if
        end repeat
        if targetWindow is missing value then
            if operation is "close" then return "gone"
            error "Recognition window no longer exists" number -1728
        end if
        if operation is "close" then
            close targetWindow
            repeat with candidate in windows
                if (id of candidate as text) is (item 2 of argv) then return "pending"
            end repeat
            return "closed"
        end if
        set targetTab to missing value
        repeat with candidate in tabs of targetWindow
            if (id of candidate as text) is (item 3 of argv) then
                set targetTab to contents of candidate
                exit repeat
            end if
        end repeat
        if targetTab is missing value then error "Recognition tab no longer exists" number -1728
        if operation is "navigate" then
            set URL of targetTab to item 4 of argv
            return "opened"
        end if
        return (URL of targetTab as text) & linefeed & (loading of targetTab as text)
    end tell
end run
'''



class ChromeLinkResolver:
    def __init__(self):
        self.window_id = None
        self.tab_id = None

    def command(self, *arguments, timeout=20):
        try:
            result = subprocess.run(['/usr/bin/osascript', '-', *arguments], input=SCRIPT,
                                    text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise BrowserLinkError('Chrome 没有响应。请检查电脑上的自动化授权提示。') from error
        if result.returncode:
            detail = result.stderr.strip()
            # Keep the owned window ID even when its original tab disappears.
            if '-1743' in detail or 'not authorized' in detail.lower():
                raise BrowserLinkError('macOS 尚未允许本地服务控制 Chrome。请在系统设置 → 隐私与安全性 → 自动化中允许启动服务的应用控制 Google Chrome。')
            raise BrowserLinkError('Chrome 识别窗口操作失败：' + detail[-220:])
        return result.stdout.strip().splitlines()

    def resolve(self, link, cancel, progress):
        primary_error = None
        final_url = ''
        try:
            # Retry any previous cleanup before creating another window.
            self.close()
            final_url = self.read_final_url(link, cancel, progress)
            return final_url
        except (BrowserLinkError, ScanCancelled) as error:
            primary_error = error
            raise
        finally:
            try:
                self.close()
            except BrowserLinkError as cleanup_error:
                if isinstance(primary_error, ScanCancelled):
                    raise ScanCancelled(str(cleanup_error)) from cleanup_error
                detail = f'{primary_error}；{cleanup_error}' if primary_error else str(cleanup_error)
                raise BrowserLinkError(detail, getattr(primary_error, 'final_url', '') or final_url) from cleanup_error

    def close(self):
        if self.window_id is None:
            return
        last_error = '识别窗口仍未关闭。'
        for attempt in range(3):
            try:
                result = self.command('close', self.window_id, timeout=8)
                if result and result[0] in ('closed', 'gone'):
                    self.window_id = self.tab_id = None
                    return
            except BrowserLinkError as error:
                last_error = str(error)
            if attempt < 2:
                time.sleep(0.25)
        # Retain IDs on failure so cleanup can be retried; never accumulate new windows.
        raise BrowserLinkError('关闭本次识别的 Chrome 窗口失败：' + last_error)

    def read_final_url(self, link, cancel, progress):
        if not shopee_url(link):
            raise BrowserLinkError('复制结果不是支持的 Shopee http/https 链接。')
        if cancel.is_set():
            raise ScanCancelled()
        if not Path('/Applications/Google Chrome.app').exists():
            raise BrowserLinkError('电脑上未找到 Google Chrome。')
        identifiers = self.command('create')
        if len(identifiers) != 2 or not all(value.isdigit() for value in identifiers):
            raise BrowserLinkError('Chrome 没有返回识别窗口信息。')
        # Record ownership before network navigation, so navigation timeouts still clean up.
        self.window_id, self.tab_id = identifiers
        if cancel.is_set():
            raise ScanCancelled()
        deadline = time.monotonic() + FINAL_URL_TIMEOUT_SECONDS
        self.command('navigate', self.window_id, self.tab_id, link,
                     timeout=min(20, max(0.001, deadline - time.monotonic())))
        previous, stable_since, latest = '', time.monotonic(), ''
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise ScanCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                current = self.command('read', self.window_id, self.tab_id,
                                       timeout=min(20, remaining))
            except BrowserLinkError as error:
                if time.monotonic() >= deadline:
                    break
                raise BrowserLinkError(str(error), latest) from error
            if time.monotonic() >= deadline:
                break
            latest = current[0] if current else ''
            loading = len(current) < 2 or current[1].lower() != 'false'
            if latest != previous:
                previous, stable_since = latest, time.monotonic()
                progress(latest)
            if not loading and time.monotonic() - stable_since >= 1.5 and final_video_code(latest):
                return latest
            cancel.wait(min(0.35, max(0, deadline - time.monotonic())))
        raise BrowserLinkError(f'浏览器在 {FINAL_URL_TIMEOUT_SECONDS} 秒内未得到可提取视频代码的最终地址。请检查网络或 Chrome 页面。', latest)
