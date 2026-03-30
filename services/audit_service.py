import json
import time
from pathlib import Path
from typing import Any


class AuditService:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def log_event(
        self,
        action: str,
        success: bool,
        user_id: str,
        group_id: str,
        duration_ms: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "ts": int(time.time()),
            "action": action,
            "success": bool(success),
            "user_id": str(user_id or ""),
            "group_id": str(group_id or ""),
            "duration_ms": int(max(0, duration_ms)),
            "extra": extra or {},
        }
        path = self._audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def summarize(self, days: int = 7) -> dict[str, Any]:
        days = max(1, int(days or 7))
        begin_ts = int(time.time()) - days * 86400
        path = self._audit_path()
        if not path.exists():
            return {
                "days": days,
                "total": 0,
                "success": 0,
                "failed": 0,
                "search": 0,
                "download": 0,
                "cache_hit": 0,
                "avg_duration_ms": 0,
            }

        total = 0
        success = 0
        failed = 0
        search = 0
        download = 0
        cache_hit = 0
        duration_sum = 0

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue

                if int(row.get("ts", 0)) < begin_ts:
                    continue

                total += 1
                if bool(row.get("success")):
                    success += 1
                else:
                    failed += 1

                action = str(row.get("action") or "")
                if action == "search":
                    search += 1
                if action == "download":
                    download += 1

                duration_sum += int(row.get("duration_ms") or 0)
                extra = row.get("extra") or {}
                if bool(extra.get("cache_hit")):
                    cache_hit += 1

        avg_duration = int(duration_sum / total) if total > 0 else 0
        return {
            "days": days,
            "total": total,
            "success": success,
            "failed": failed,
            "search": search,
            "download": download,
            "cache_hit": cache_hit,
            "avg_duration_ms": avg_duration,
        }

    def _audit_path(self) -> Path:
        configured = str(self.config.get("audit_log_path") or "data/JMdownload_for_Astrbot/audit.jsonl").strip()
        return Path(configured)
