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
        query = query.strip()
        album_id = self.extract_album_id(query)

        option = self._build_jm_option(base_dir=self._build_base_dir())
        client = option.new_jm_client()

        if album_id:
            album = self._get_album_detail(client, album_id)
            return self._normalize_album(album)

        search_obj = None
        if hasattr(client, "search_site"):
            search_obj = client.search_site(query, page=1)
        elif hasattr(client, "search"):
            search_obj = client.search(query, page=1)

        candidate_ids: list[str] = []
        if search_obj is not None:
            if hasattr(search_obj, "iter_id_title_tag"):
                for item in search_obj.iter_id_title_tag():
                    if not item:
                        continue
                    candidate_ids.append(str(item[0]))
                    if len(candidate_ids) >= int(self.config.get("search_result_limit", 3)):
                        break
            elif isinstance(search_obj, list):
                for item in search_obj:
                    item_id = getattr(item, "album_id", None) or getattr(item, "id", None)
                    if item_id:
                        candidate_ids.append(str(item_id))

        if not candidate_ids:
            raise RuntimeError("未找到匹配漫画，请尝试更换关键词或直接输入番号")

        album = self._get_album_detail(client, candidate_ids[0])
        return self._normalize_album(album)

    def download_images(self, album_id: str) -> tuple[Path, list[Path]]:
        task_dir = self._build_base_dir() / f"task_{album_id}_{uuid.uuid4().hex[:8]}"
        task_dir.mkdir(parents=True, exist_ok=True)

        option = self._build_jm_option(base_dir=task_dir)
        if not hasattr(jmcomic, "download_album"):
            raise RuntimeError("当前 jmcomic 版本缺少 download_album 方法")

        # 优先按章节逐个下载，减少部分大本子下载不全的概率。
        downloaded = False
        try:
            client = option.new_jm_client()
            album = self._get_album_detail(client, album_id)
            photo_ids = self._extract_photo_ids(album)
            if photo_ids and hasattr(jmcomic, "download_photo"):
                for photo_id in photo_ids:
                    jmcomic.download_photo(photo_id, option)
                downloaded = True
        except Exception as exc:
            logger.warning(f"[qq_code_listener] 逐章节下载失败，回退整本下载: {exc}")

        if not downloaded:
            jmcomic.download_album([album_id], option)

        image_files = sorted(
            [
                p
                for p in task_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
        )
        logger.info(f"[qq_code_listener] album={album_id} 下载图片数量: {len(image_files)}")
        return task_dir, image_files

    @staticmethod
    def extract_album_id(text: str) -> str | None:
        match = re.search(r"(\d{5,10})", text or "")
        if match:
            return match.group(1)
        return None

    def _build_base_dir(self) -> Path:
        configured = str(self.config.get("download_root") or "data/qq_code_listener").strip()
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
        )

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
