import hashlib
import shutil
import time
from pathlib import Path


class CacheService:
    def __init__(self, config: dict):
        self.config = config

    def get_cached_zip(
        self,
        album_id: str,
        chapter_id: str | None,
        max_page: int,
        zip_level: int,
        zip_password: str,
        profile: str,
    ) -> Path | None:
        self._cleanup_expired()
        key = self._build_key(album_id, chapter_id, max_page, zip_level, zip_password, profile)
        cached = self._cache_root() / f"{key}.zip"
        if not cached.exists():
            return None
        if self._is_expired(cached):
            try:
                cached.unlink()
            except Exception:
                pass
            return None
        return cached

    def save_cache(
        self,
        source_zip: Path,
        album_id: str,
        chapter_id: str | None,
        max_page: int,
        zip_level: int,
        zip_password: str,
        profile: str,
    ) -> Path:
        if not source_zip.exists():
            raise RuntimeError("缓存写入失败：源 ZIP 不存在")

        root = self._cache_root()
        root.mkdir(parents=True, exist_ok=True)
        key = self._build_key(album_id, chapter_id, max_page, zip_level, zip_password, profile)
        target = root / f"{key}.zip"
        shutil.copy2(source_zip, target)
        return target

    def _cache_root(self) -> Path:
        configured = str(self.config.get("cache_root") or "data/plugin_data/JMdownload_for_Astrbot/cache").strip()
        return Path(configured)

    def _ttl_seconds(self) -> int:
        hours = int(self.config.get("cache_ttl_hours", 72) or 72)
        return max(1, hours) * 3600

    def _is_expired(self, file_path: Path) -> bool:
        ttl = self._ttl_seconds()
        age = max(0, int(time.time() - file_path.stat().st_mtime))
        return age > ttl

    def _cleanup_expired(self) -> None:
        root = self._cache_root()
        if not root.exists():
            return
        for item in root.glob("*.zip"):
            try:
                if self._is_expired(item):
                    item.unlink()
            except Exception:
                continue

    @staticmethod
    def _build_key(
        album_id: str,
        chapter_id: str | None,
        max_page: int,
        zip_level: int,
        zip_password: str,
        profile: str,
    ) -> str:
        raw = "|".join(
            [
                str(album_id or ""),
                str(chapter_id or ""),
                str(max_page or 0),
                str(zip_level or 0),
                str(profile or "balanced"),
                str(zip_password or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
