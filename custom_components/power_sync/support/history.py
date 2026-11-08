"""Byte/age/count bounded local evidence; serialized records own their data."""
from __future__ import annotations

from collections import deque
import json

from .protocol import encode


class Recorder:
    def __init__(self, *, max_bytes=8 * 1024 * 1024, max_age=86400, max_count=6000):
        self.max_bytes, self.max_age, self.max_count = max_bytes, max_age, max_count
        self._rows = deque()
        self.bytes = self.dropped = 0

    def trim(self, now):
        while self._rows and (self._rows[0][0] < now - self.max_age or self.bytes > self.max_bytes or len(self._rows) > self.max_count):
            _, raw = self._rows.popleft()
            self.bytes -= len(raw)
            self.dropped += 1

    def append(self, frame):
        raw = encode(frame)
        if len(raw) > min(self.max_bytes, 256 * 1024):
            self.dropped += 1
            return
        self._rows.append((frame["captured_at"], raw))
        self.bytes += len(raw)
        self.trim(frame["captured_at"])

    def window(self, start, end, *, category=None):
        records, size, truncated = [], 0, False
        for stamp, raw in self._rows:
            if not start <= stamp <= end:
                continue
            row = json.loads(raw)
            if category:
                if not row.get(category):
                    continue
                row = {"captured_at": stamp, "records": row.get(category, [])}
            cost = len(encode(row))
            if size + cost > 192 * 1024:
                truncated = True
                break
            records.append(row)
            size += cost
        return {"schema_version": 1, "category": category or "history", "source": "support_recorder",
            "requested_start": start, "requested_end": end, "records": records,
            "availability": "available" if records else "not_recorded", "truncated": truncated,
            "dropped_records": self.dropped, "coverage_start": self._rows[0][0] if self._rows else None,
            "coverage_end": self._rows[-1][0] if self._rows else None}

    def dump(self):
        return [json.loads(raw) for _, raw in self._rows]

    def restore(self, records, now):
        for row in records[:self.max_count] if isinstance(records, list) else []:
            if isinstance(row, dict) and isinstance(row.get("captured_at"), (int, float)) and now - self.max_age <= row["captured_at"] <= now:
                self.append(row)
        self.trim(now)
