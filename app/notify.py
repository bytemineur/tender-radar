"""告警与订阅推送。

webhook 走通用 JSON POST，兼容企业微信机器人 / 飞书机器人 / Slack Incoming Webhook
（三者的 JSON 字段不同，这里统一成 {msgtype/text/content} 并按 URL 特征自动适配）。
未配置 webhook 时只写日志，不抛错——绝不因为通知失败影响采集主流程。
"""

from __future__ import annotations

import logging

import httpx

from .config import settings

logger = logging.getLogger("tender-radar.notify")


def _post(payload: dict) -> None:
    if not settings.notify.enabled or not settings.notify.webhook:
        logger.info("通知未启用，跳过")
        return
    url = settings.notify.webhook
    try:
        # 企业微信机器人与飞书机器人都是 {msgtype, ...} 结构
        with httpx.Client(timeout=10) as client:
            client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("通知发送失败: %s", exc)


def notify_error(source_id: str, message: str) -> None:
    """采集失败告警。"""
    text = f"[招标雷达] 来源 {source_id} 采集异常\n{message}"
    logger.warning(text)
    _post({"msgtype": "text", "text": {"content": text[:2000]}})


def notify_new_items(items: list[dict]) -> None:
    """关键词订阅命中推送。"""
    lines = [f"[招标雷达] 命中订阅关键词 {len(items)} 条"]
    for it in items[:10]:
        budget = (
            f"{it['budget'] / 10000:.1f}万" if it.get("budget") else "未披露"
        )
        lines.append(
            f"· {it['title'][:48]}\n  采购人：{it.get('purchaser') or '未知'}｜"
            f"预算：{budget}｜截止：{it.get('deadline') or '未披露'}"
        )
    text = "\n".join(lines)
    logger.info(text)
    _post({"msgtype": "text", "text": {"content": text[:2000]}})
