"""幂等事件存储：按 event_id 去重，保留原发生时间。

来源系统（网点、支付系统）重试时必须沿用原 event_id 与 occurred_at；
存储层据此保证重复通知不产生重复效果，接收时间单独记录，不参与业务时限。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from src.intervention import events as ev


@dataclass
class StoredEvent:
    envelope: dict           # 原始信封，occurred_at 不被改写
    received_at: datetime    # 本服务接收时间，仅用于审计


class EventStore:
    def __init__(self) -> None:
        self._by_id: dict[str, StoredEvent] = {}
        self._versions: dict[tuple[str, str], dict[int, str]] = {}
        self._order: list[StoredEvent] = []

    @staticmethod
    def canonical(envelope: dict) -> str:
        return json.dumps(envelope, ensure_ascii=False, sort_keys=True)

    def by_id(self, event_id: str) -> StoredEvent | None:
        return self._by_id.get(event_id)

    def has_version(self, aggregate_type: str, aggregate_id: str, version: int) -> bool:
        return version in self._versions.get((aggregate_type, aggregate_id), {})

    def commit(self, envelope: dict, received_at: datetime) -> StoredEvent:
        stored = StoredEvent(envelope=envelope, received_at=received_at)
        self._by_id[envelope["event_id"]] = stored
        key = (envelope["aggregate_type"], envelope["aggregate_id"])
        self._versions.setdefault(key, {})[envelope["version"]] = envelope["event_id"]
        self._order.append(stored)
        return stored

    def all(self) -> list[StoredEvent]:
        """按事实发生时间（occurred_at）排序的全部事件，乱序到达也能收敛。"""

        def key(stored: StoredEvent) -> tuple[datetime, str]:
            parsed = ev.parse_time(stored.envelope.get("occurred_at"))
            return (parsed or datetime.min.replace(tzinfo=timezone.utc), stored.envelope.get("event_id", ""))

        return sorted(self._order, key=key)
