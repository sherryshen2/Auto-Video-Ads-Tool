"""Shopee video promotion configuration, receipts and one-shot Publish orchestration."""
from __future__ import annotations

import base64
import copy
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from browser_link import ScanCancelled


RECEIPTS_PATH = Path(__file__).resolve().parent / '.runtime' / 'promotions.json'
DO_NOT_REPUBLISH = {'发布成功', '已提交审核', '结果待确认', '发布中'}
receipt_lock = threading.RLock()


def timestamp() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def parameters(row: dict) -> dict:
    duration = str(row.get('duration') or '').strip()
    if duration.casefold() in ('no end date', '不限期', '无结束日期'):
        days = 0
    else:
        match = re.fullmatch(r'(\d+)(?:\.0+)?\s*(?:days?|天)?', duration, re.IGNORECASE)
        if not match or int(match[1]) < 1:
            raise ValueError('Duration 必须为正整数天数或 No End Date。')
        days = int(match[1])
    amount = re.sub(r'^(?:RM|MYR)\s*', '', str(row.get('daily_budget') or '').strip(), flags=re.I)
    if not re.fullmatch(r'\d+(?:\.\d+)?', amount):
        raise ValueError('Daily budget 必须为明确的正数金额。')
    try:
        budget = Decimal(amount)
        if budget < 1 or budget > Decimal('10000000000') or budget != budget.quantize(Decimal('.01')):
            raise ValueError('Daily budget 必须在 App 提示的 RM1～RM10b 范围内，最多两位小数。')
    except InvalidOperation as error:
        raise ValueError('无法解析 Daily budget。') from error
    objective = re.sub(r'\s+', ' ', str(row.get('objective') or '').strip()).casefold()
    objectives = {'gmv max': 'GMV Max', 'boost views': 'Boost Views'}
    if objective not in objectives:
        raise ValueError('Objective 必须与 App 选项一致：GMV Max 或 Boost Views。')
    return {'days': days, 'duration': 'No End Date' if days == 0 else f'{days} day',
            'daily_budget': format(budget, '.2f'), 'objective': objectives[objective]}


def matching_parameters(rows: list[dict]) -> dict:
    choices = [parameters(row) for row in rows]
    if not choices or any(choice != choices[0] for choice in choices[1:]):
        raise ValueError('同一视频在 Excel 中的投广参数冲突，未发布。请统一 Duration、Daily budget、Objective。')
    return choices[0]


def read_receipts() -> dict:
    with receipt_lock:
        try:
            data = json.loads(RECEIPTS_PATH.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError('投广提交记录格式异常，请先恢复记录，避免重复发布。')
        return data


def write_receipt(code: str, receipt: dict) -> None:
    with receipt_lock:
        records = read_receipts()
        records[code] = copy.deepcopy(receipt)
        RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = RECEIPTS_PATH.with_suffix('.tmp')
        with temporary.open('w') as output:
            json.dump(records, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(RECEIPTS_PATH)


def confirm_receipt(code: str, operation_id: str, status: str) -> dict:
    """Resolve one completed unknown attempt; callers exclude an active attempt."""
    if not code or not operation_id or status not in ('发布成功', '发布失败'):
        raise ValueError('请选择一条待确认的投广记录，标记为成功或失败。')
    with receipt_lock:
        receipt = read_receipts().get(code)
        if not receipt or receipt.get('operation_id') != operation_id:
            raise RuntimeError('这条投广记录已更新，请等待页面刷新后重新选择。')
        # Retrying the same request after a lost response must be harmless.
        if receipt.get('manual_confirmation', {}).get('status') == status and receipt.get('status') == status:
            return receipt
        if receipt.get('status') != '结果待确认':
            raise RuntimeError('只有“结果待确认”的投广记录可以手动选择结果。')
        receipt['automatic_result'] = {key: copy.deepcopy(receipt.get(key, '')) for key in
                                       ('status', 'error', 'popup_text', 'popup_source', 'finished_at')}
        receipt['manual_confirmation'] = dict(status=status, confirmed_at=timestamp(), source='user')
        receipt['status'] = status
        receipt['error'] = '用户手动确认发布失败。' if status == '发布失败' else ''
        write_receipt(code, receipt)
        return receipt


def settled_codes() -> set[str]:
    return {code for code, receipt in read_receipts().items()
            if receipt.get('status') in DO_NOT_REPUBLISH}


def classify_popup(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    failure = re.search(r'\b(?:failed|failure|unsuccessful|insufficient|unable|rejected|cannot|not enough|not eligible|error)\b|\bnot\s+(?:\w+\s+){0,3}(?:successful(?:ly)?|published|created|submitted|done)\b|失败|余额不足|无法|不符合', text, re.I)
    success = re.search(r'\b(?:done|success|successfully)\b|\b(?:ad|ads|campaign)\s+(?:has been|have been|was|were|is|are)\s+(?:published|created|submitted)\b|发布成功|创建成功|提交成功', text, re.I)
    review = re.search(r'under review|pending review|submitted for review|审核中|待审核', text, re.I)
    # A failure dialog can also have a Done dismissal button.
    if failure:
        return '发布失败'
    if success:
        return '发布成功'
    if review:
        return '已提交审核'
    return '结果待确认'


def promote(code: str, rows: list[dict], call, cancel, save, progress,
            force_success: bool = False) -> dict:
    """call invokes one Android helper step; save attaches the receipt to history."""
    receipt = dict(video_code=code, operation_id=uuid.uuid4().hex, status='准备投广',
                   parameters={}, popup_text='', error='', publish_attempted=False,
                   started_at=timestamp(), finished_at='', attempts=1, return_error='')
    opened = False
    journal_ready = False

    def persist():
        write_receipt(code, receipt)
        save(copy.deepcopy(receipt))

    def check_cancelled():
        if cancel.is_set():
            raise ScanCancelled()

    def step(name, expected, params=None, timeout=45, label=None):
        arguments = dict(params or {})
        deadline = time.monotonic() + timeout
        last_detail = ''
        while time.monotonic() < deadline:
            check_cancelled()
            encoded = base64.b64encode(json.dumps(arguments, ensure_ascii=False).encode()).decode()
            result = call(name + ':' + encoded)
            check_cancelled()
            if name == 'ad-budget':
                if isinstance(result.get('budget_state'), dict):
                    arguments['budget_state'] = result['budget_state']
                if 'budget_observed' in result:
                    receipt['budget_observed'] = result['budget_observed']
                    persist()
            if result.get('result') == expected:
                return result
            if result.get('result') != 'pending':
                raise RuntimeError(f'{label or name}失败：' + (result.get('error') or str(result.get('result'))))
            detail = result.get('detail') or result.get('error') or ''
            if detail and detail != last_detail:
                progress(detail + '…')
                last_detail = detail
            if name == 'ad-share-menu' and result.get('entry_button'):
                receipt['entry_button'] = result['entry_button']
                persist()
            if name == 'ad-open' and result.get('menu_promote_clicked'):
                arguments['menu_promote_clicked'] = True
                receipt['menu_promote_clicked_at'] = timestamp()
                persist()
            cancel.wait(.2)
        raise RuntimeError(f'{label or name}超时。' + last_detail)

    try:
        previous = read_receipts().get(code, {})
        if previous.get('status') in DO_NOT_REPUBLISH and not (force_success and previous['status'] == '发布成功'):
            reused = copy.deepcopy(previous)
            if reused['status'] == '发布中':
                reused['status'] = '结果待确认'
                reused['error'] = '上次 Publish 后未取得确定结果，本次未重复发布。'
                write_receipt(code, reused)
            save(reused)
            return reused
        receipt['attempts'] = int(previous.get('attempts') or 0) + 1
        if previous:
            receipt['previous_attempts'] = copy.deepcopy(previous.get('previous_attempts', []))
            receipt['previous_attempts'].append({key: copy.deepcopy(value) for key, value in previous.items()
                                                 if key != 'previous_attempts'})
        journal_ready = True
        receipt['parameters'] = matching_parameters(rows)
        persist()
        check_cancelled()
        receipt['status'] = '打开分享菜单'
        persist()
        progress('正在点击视频上的 More / Promote，等待 5 秒加载分享菜单…')
        opened = True
        menu = step('ad-share-menu', 'menu_open', timeout=30, label='打开视频分享菜单')
        receipt['entry_button'] = menu.get('entry_button', receipt.get('entry_button', ''))
        receipt['share_menu_opened_at'] = timestamp()
        receipt['status'] = '进入投广页面'
        persist()
        progress('分享菜单已打开，正在点击左下方橙色 Promote 按钮…')
        step('ad-open', 'opened', timeout=30, label='点击菜单内 Promote 并进入投广页面')
        receipt['ad_form_opened_at'] = timestamp()
        persist()
        for field, label in (('duration', 'Duration'), ('budget', 'Daily Budget'), ('objective', 'Objective')):
            receipt['status'] = '填写参数'
            persist()
            progress(f'正在按 Excel 选择 {label}…')
            step('ad-' + field, 'configured', receipt['parameters'],
                 timeout=70 if field == 'duration' else 60 if field == 'budget' else 30,
                 label=f'填写并确认 {label}')
        verified = step('ad-verify', 'verified', receipt['parameters'], timeout=20)
        receipt['verified_values'] = {key: verified.get(key) for key in ('duration', 'daily_budget', 'objective')}
        check_cancelled()
        # Durable intent is written BEFORE the sole Publish action. A crash or a
        # timeout after this point is unknown, never an invitation to click again.
        progress('三项参数已确认，正在点击 Publish 并读取弹窗…')
        check_cancelled()
        receipt.update(status='发布中', publish_attempted=True, published_at=timestamp())
        persist()
        if cancel.is_set():
            receipt.update(publish_attempted=False, published_at='')
            raise ScanCancelled()
        publish_parameters = dict(receipt['parameters'], capture_id=receipt['operation_id'])
        encoded = base64.b64encode(json.dumps(publish_parameters).encode()).decode()
        response = call('ad-publish:' + encoded)
        receipt['popup_text'] = response.get('popup_text', '')
        receipt['error'] = response.get('error', '')
        for key in ('popup_source', 'popup_capture_error', 'popup_capture_path'):
            if response.get(key):
                receipt[key] = response[key]
        if response.get('result') == 'not_published':
            receipt.update(status='发布失败', publish_attempted=False)
        elif response.get('result') in ('publish_attempted', 'publish_unknown', 'timeout'):
            receipt['status'] = classify_popup(receipt['popup_text'])
            if receipt['status'] == '发布失败' and not receipt['error']:
                receipt['error'] = receipt['popup_text']
            if receipt['status'] == '结果待确认' and not receipt['error']:
                receipt['error'] = '未读到可确定成功或失败的弹窗，不自动重复 Publish。'
        else:
            receipt['status'] = '结果待确认'
            receipt['error'] = receipt['error'] or 'Publish 后未取得完整响应，不自动重复发布。'
        receipt['finished_at'] = timestamp()
        persist()
    except ScanCancelled:
        receipt.update(status='结果待确认' if receipt['publish_attempted'] else '已停止',
                       error='用户停止操作。' if not receipt['publish_attempted'] else 'Publish 后停止，需检查实际投广结果。',
                       finished_at=timestamp())
        if journal_ready:
            persist()
        else:
            save(copy.deepcopy(receipt))
        raise
    except Exception as error:
        receipt.update(status='结果待确认' if receipt['publish_attempted'] else '发布失败',
                       error=str(error), finished_at=timestamp())
        if journal_ready:
            persist()
        else:
            save(copy.deepcopy(receipt))
    finally:
        if opened:
            progress('投广处理已结束，正在返回视频页面…')
            try:
                for _ in range(6):
                    response = call('ad-return')
                    if response.get('result') in ('returned', 'grid'):
                        receipt['return_page'] = response['result']
                        break
                    if response.get('result') != 'pending':
                        raise RuntimeError(response.get('error') or '无法返回视频页面。')
                else:
                    raise RuntimeError('回退后仍未找到视频页面。')
            except Exception as error:
                receipt['return_error'] = str(error)
            if journal_ready:
                persist()
            else:
                save(copy.deepcopy(receipt))
    return receipt
