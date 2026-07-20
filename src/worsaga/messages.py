"""Notification and message readers."""

from __future__ import annotations

import time
from typing import Any

from worsaga.client import MoodleClient
from worsaga.models import as_bool, as_int, notification_record


def _sender(payload: dict[str, Any]) -> str:
    return str(
        payload.get("userfromfullname")
        or payload.get("fromfullname")
        or payload.get("sender")
        or payload.get("userfrom")
        or ""
    )


def normalize_notifications(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Normalize Moodle popup notification payloads."""
    notifications = payload.get("notifications", []) if isinstance(payload, dict) else payload
    records: list[dict[str, Any]] = []
    for item in notifications if isinstance(notifications, list) else []:
        if not isinstance(item, dict):
            continue
        records.append(
            notification_record(
                notification_id=item.get("id", item.get("notificationid", "")),
                notification_type=str(item.get("notificationtype") or item.get("type") or "notification"),
                subject=item.get("subject") or item.get("smallmessage") or "",
                body=item.get("fullmessagehtml") or item.get("fullmessage") or item.get("contexturlname") or "",
                sender=_sender(item),
                course_id=as_int(item.get("courseid")),
                created_at=as_int(item.get("timecreated", item.get("created"))),
                read=as_bool(item.get("read"), None),
                view_url=str(item.get("contexturl") or ""),
            )
        )
    records.sort(key=lambda r: r["created_at"] or 0, reverse=True)
    return records


def normalize_messages(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Normalize Moodle message payloads."""
    messages = payload.get("messages", []) if isinstance(payload, dict) else payload
    records: list[dict[str, Any]] = []
    for item in messages if isinstance(messages, list) else []:
        if not isinstance(item, dict):
            continue
        records.append(
            notification_record(
                notification_id=item.get("id", item.get("messageid", "")),
                notification_type="message",
                subject=item.get("subject") or item.get("smallmessage") or "Message",
                body=item.get("fullmessagehtml") or item.get("fullmessage") or item.get("text") or "",
                sender=_sender(item),
                course_id=as_int(item.get("courseid")),
                created_at=as_int(item.get("timecreated", item.get("created"))),
                read=as_bool(item.get("read"), None),
                view_url=str(item.get("contexturl") or ""),
            )
        )
    records.sort(key=lambda r: r["created_at"] or 0, reverse=True)
    return records


def get_notifications(client: MoodleClient, unread_only: bool = False) -> list[dict[str, Any]]:
    """Return popup notifications without marking them read."""
    payload = client.get_popup_notifications(unread_only=unread_only)
    records = normalize_notifications(payload)
    if unread_only:
        records = [record for record in records if record.get("read") is not True]
    return records


def get_messages(client: MoodleClient, since_days: int | None = None) -> list[dict[str, Any]]:
    """Return inbox messages without marking them read."""
    since_time = None
    if since_days is not None:
        since_time = int(time.time()) - since_days * 86400
    payload = client.get_messages(since_time=since_time)
    records = normalize_messages(payload)
    if since_time is not None:
        records = [
            record for record in records
            if record.get("created_at") is not None and record["created_at"] >= since_time
        ]
    return records
