import re
import uuid
from pathlib import Path
from typing import Any

import jmcomic
from astrbot.api import logger

try:
    from ..plugin_types import AlbumInfo
except ImportError:
    from plugin_types import AlbumInfo


class MangaService:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def search_album(self, query: str) -> AlbumInfo:
        albums = self.search_albums(query, limit=1)
        if not albums:
            raise RuntimeError("未找到匹配漫画，请尝试更换关键词或直接输入番号")
        return albums[0]

    def search_albums(self, query: str, limit: int = 3) -> list[AlbumInfo]:
        query = query.strip()
        if not query:
            raise RuntimeError("搜索关键词不能为空")

        limit = max(1, int(limit or 1))
        album_id = self.extract_album_id(query)

        option = self._build_jm_option(base_dir=self._build_base_dir())
        client = option.new_jm_client()

        if album_id:
            album = self._get_album_detail(client, album_id)
            normalized = self._normalize_album(album)
            normalized.heat_score = self._extract_heat_score(album)
            return [normalized]

        search_obj = None
        if hasattr(client, "search_site"):
            search_obj = client.search_site(query, page=1)
        elif hasattr(client, "search"):
            search_obj = client.search(query, page=1)

        configured_pool = int(self.config.get("search_result_limit", 3) or 3)
        candidate_pool = max(configured_pool, limit * 3)
        candidate_pool = min(50, max(10, candidate_pool))
        candidate_ids = self._extract_candidate_ids(search_obj, candidate_pool)

        if not candidate_ids:
            raise RuntimeError("未找到匹配漫画，请尝试更换关键词或直接输入番号")

        albums: list[AlbumInfo] = []
        for candidate_id in candidate_ids:
            try:
                album = self._get_album_detail(client, candidate_id)
                normalized = self._normalize_album(album)
                normalized.heat_score = self._extract_heat_score(album)
                albums.append(normalized)
            except Exception as exc:
                logger.warning(f"[JMdownload_for_Astrbot] 获取候选详情失败 id={candidate_id}, err={exc}")

        if not albums:
            raise RuntimeError("搜索结果解析失败，请稍后重试")

        albums.sort(key=lambda item: item.heat_score, reverse=True)
        return albums[:limit]

    def download_images(
        self,
        album_id: str,
        chapter_id: str | None = None,
        retry_per_chapter: int = 3,
        max_pages: int | None = None,
    ) -> tuple[Path, list[Path]]:
        task_dir = self._build_base_dir() / f"task_{album_id}_{uuid.uuid4().hex[:8]}"
        task_dir.mkdir(parents=True, exist_ok=True)

        option = self._build_jm_option(base_dir=task_dir)
        if not hasattr(jmcomic, "download_album"):
            raise RuntimeError("当前 jmcomic 版本缺少 download_album 方法")

        # 优先按章节逐个下载，支持重试与断点续下。
        downloaded = False
        try:
            client = option.new_jm_client()
            album = self._get_album_detail(client, album_id)
            photo_ids = self._extract_photo_ids(album)
            if chapter_id:
                chapter_id = str(chapter_id)
                photo_ids = [pid for pid in photo_ids if str(pid) == chapter_id]
                if not photo_ids:
                    raise RuntimeError(f"未找到章节 {chapter_id}")

            if photo_ids and hasattr(jmcomic, "download_photo"):
                for photo_id in photo_ids:
                    self._download_photo_with_retry(
                        photo_id=str(photo_id),
                        option=option,
                        task_dir=task_dir,
                        max_retry=max(1, int(retry_per_chapter or 1)),
                    )
                downloaded = True
        except Exception as exc:
            logger.warning(f"[JMdownload_for_Astrbot] 逐章节下载失败，回退整本下载: {exc}")

        if not downloaded:
            jmcomic.download_album([album_id], option)

        image_files = sorted(
            [
                p
                for p in task_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
        )

        if max_pages is not None and max_pages > 0 and len(image_files) > max_pages:
            image_files = image_files[:max_pages]

        logger.info(f"[JMdownload_for_Astrbot] album={album_id} 下载图片数量: {len(image_files)}")
        return task_dir, image_files

    @staticmethod
    def extract_album_id(text: str) -> str | None:
        match = re.search(r"(\d+)", text or "")
        if match:
            return match.group(1)
        return None

    @staticmethod
    def extract_chapter_id(text: str) -> str | None:
        # 兼容 p456 / P456 / chapter456
        match = re.search(r"(?:\bp|\bchapter)\s*(\d+)", text or "", flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _build_base_dir(self) -> Path:
        configured = str(self.config.get("download_root") or "data/JMdownload_for_Astrbot").strip()
        return Path(configured)

    def _build_jm_option(self, base_dir: Path):
        base_dir.mkdir(parents=True, exist_ok=True)
        return jmcomic.JmOption.construct(
            {
                "dir_rule": {
                    "base_dir": str(base_dir),
                    "rule": "Bd_Aid_Ep_Pindex",
                },
                "download": {
                    "cache": False,
                },
            }
        )

    @staticmethod
    def _get_album_detail(client: Any, album_id: str):
        if hasattr(client, "get_album_detail"):
            return client.get_album_detail(album_id)
        if hasattr(client, "get_album"):
            return client.get_album(album_id)
        raise RuntimeError("当前 jmcomic client 不支持获取漫画详情")

    def _normalize_album(self, album: Any) -> AlbumInfo:
        if album is None:
            raise RuntimeError("漫画不存在")

        album_id = str(getattr(album, "album_id", None) or getattr(album, "id", ""))
        title = str(getattr(album, "name", None) or getattr(album, "title", None) or "未知标题")
        intro = str(getattr(album, "description", None) or getattr(album, "comment", None) or "暂无简介")
        author = str(getattr(album, "author", None) or getattr(album, "works", None) or "未知作者")
        cover_url = str(
            getattr(album, "cover", None)
            or getattr(album, "cover_url", None)
            or getattr(album, "thumb", None)
            or ""
        )

        chapters: list[str] = []
        for attr_name in ["episode_list", "chapter_list", "episodes", "photos"]:
            obj = getattr(album, attr_name, None)
            if not obj:
                continue
            try:
                for item in obj:
                    chapter_title = str(
                        getattr(item, "name", None)
                        or getattr(item, "title", None)
                        or getattr(item, "id", None)
                        or item
                    )
                    chapters.append(chapter_title)
            except TypeError:
                continue
            if chapters:
                break

        if not album_id:
            raise RuntimeError("未获取到漫画 ID")

        return AlbumInfo(
            album_id=album_id,
            title=title,
            intro=intro,
            author=author,
            cover_url=cover_url,
            chapters=chapters,
            heat_score=self._extract_heat_score(album),
        )

    @staticmethod
    def _extract_candidate_ids(search_obj: Any, max_count: int) -> list[str]:
        ids: list[str] = []

        def _append_candidate(item_id: Any) -> None:
            if item_id is None:
                return
            value = str(item_id).strip()
            if not value or value in ids:
                return
            ids.append(value)

        if search_obj is None:
            return ids

        if hasattr(search_obj, "iter_id_title_tag"):
            for item in search_obj.iter_id_title_tag():
                if not item:
                    continue
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    _append_candidate(item[0])
                if len(ids) >= max_count:
                    return ids

        list_sources = []
        if isinstance(search_obj, list):
            list_sources.append(search_obj)
        for attr_name in ["albums", "result", "results", "content", "list"]:
            value = getattr(search_obj, attr_name, None)
            if isinstance(value, list):
                list_sources.append(value)

        for source in list_sources:
            for item in source:
                _append_candidate(getattr(item, "album_id", None) or getattr(item, "id", None) or item)
                if len(ids) >= max_count:
                    return ids

        return ids

    @staticmethod
    def _extract_heat_score(album: Any) -> int:
        # 兼容不同字段命名，按权重合成一个可比较的“热度分”。
        weighted_fields = [
            ("total_views", 1),
            ("views", 1),
            ("view_count", 1),
            ("month_views", 1),
            ("week_views", 1),
            ("total_likes", 20),
            ("likes", 20),
            ("like_count", 20),
            ("favorite_count", 10),
            ("favorites", 10),
            ("comment_count", 3),
            ("comments_count", 3),
        ]
        score = 0
        for field_name, weight in weighted_fields:
            score += MangaService._safe_int(getattr(album, field_name, None)) * weight
        return max(0, int(score))

    @staticmethod
    def _safe_int(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip().replace(",", "")
        match = re.search(r"-?\d+", text)
        if not match:
            return 0
        try:
            return int(match.group(0))
        except Exception:
            return 0

    @staticmethod
    def _extract_photo_ids(album: Any) -> list[str]:
        ids: list[str] = []
        for attr_name in ["photos", "episode_list", "chapter_list", "episodes"]:
            obj = getattr(album, attr_name, None)
            if not obj:
                continue
            try:
                for item in obj:
                    photo_id = (
                        getattr(item, "photo_id", None)
                        or getattr(item, "id", None)
                        or getattr(item, "album_id", None)
                    )
                    if photo_id:
                        ids.append(str(photo_id))
            except TypeError:
                continue
            if ids:
                break
        # 去重保持顺序
        seen = set()
        ordered: list[str] = []
        for pid in ids:
            if pid in seen:
                continue
            seen.add(pid)
            ordered.append(pid)
        return ordered

    @staticmethod
    def _count_images(task_dir: Path) -> int:
        return len(
            [
                p
                for p in task_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
        )

    def _download_photo_with_retry(self, photo_id: str, option: Any, task_dir: Path, max_retry: int) -> None:
        # 断点续下：若该章节目录已经存在图片则直接跳过重下。
        existing = [p for p in task_dir.rglob(f"*{photo_id}*") if p.is_file()]
        if existing:
            logger.info(f"[JMdownload_for_Astrbot] 章节已存在，跳过: {photo_id}")
            return

        last_exc: Exception | None = None
        for idx in range(1, max_retry + 1):
            before = self._count_images(task_dir)
            try:
                jmcomic.download_photo(photo_id, option)
                after = self._count_images(task_dir)
                if after > before:
                    return
            except Exception as exc:
                last_exc = exc
                logger.warning(f"[JMdownload_for_Astrbot] 章节下载失败 photo={photo_id}, retry={idx}/{max_retry}, err={exc}")

        if last_exc is not None:
            raise RuntimeError(f"章节下载失败: {photo_id}, err={last_exc}")
