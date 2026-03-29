import asyncio
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jmcomic
import pyzipper
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


DEFAULT_CONFIG = {
    "enabled": True,
    "trigger_keywords": ["/", "!", "漫画"],
    "allowed_group_ids": [],
    "allowed_user_ids": [],
    "deny_reply_enabled": False,
    "deny_reply_text": "你没有权限使用该功能。",
    "download_root": "data/qq_code_listener",
    "search_result_limit": 3,
    "zip_level": 9,
    "zip_password": "123456",
}


@dataclass
class AlbumInfo:
    album_id: str
    title: str
    intro: str
    author: str
    cover_url: str
    chapters: list[str]


@register(
    "qq_code_listener",
    "wzh",
    "禁漫查询下载（白名单+关键词触发）",
    "2.0.0",
)
class QQCodeListenerPlugin(Star):
    """AstrBot 插件：支持 jmcomic 查询/下载，自动生成 PDF 并压缩发送。"""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return

        message_text = (getattr(event, "message_str", "") or "").strip()
        if not message_text:
            return

        if not self._allowed_event(event):
            if bool(self.config.get("deny_reply_enabled", False)):
                yield event.plain_result(str(self.config.get("deny_reply_text") or "你没有权限使用该功能。"))
            return

        command_text = self._extract_command_text(message_text)
        if command_text is None:
            return

        parsed = self._parse_command(command_text)
        if parsed is None:
            help_text = (
                "指令格式:\n"
                "1) /漫画 搜索 关键词\n"
                "2) /漫画 下载 番号或链接\n"
                "关键词前缀可在插件配置 trigger_keywords 中自定义。"
            )
            yield event.plain_result(help_text)
            return

        action, payload = parsed
        if action == "search":
            yield event.plain_result("正在查询漫画，请稍候...")
            try:
                album = await asyncio.to_thread(self._search_album, payload)
                yield event.plain_result(self._format_album_info(album))
            except Exception as exc:
                logger.exception(f"[qq_code_listener] 查询失败: {exc}")
                yield event.plain_result(f"查询失败：{self._friendly_error(exc)}")
            return

        # action == "download"
        yield event.plain_result("正在查询并下载漫画，请稍候...")
        task_dir: Path | None = None
        try:
            album = await asyncio.to_thread(self._search_album, payload)
            yield event.plain_result(f"已定位漫画：{album.title}（ID: {album.album_id}），正在下载图片...")

            task_dir, image_files = await asyncio.to_thread(
                self._download_images,
                album.album_id,
            )
            if not image_files:
                raise RuntimeError("下载完成但未找到任何图片文件")

            yield event.plain_result("下载完成，正在生成 PDF...")
            pdf_path = await asyncio.to_thread(self._images_to_pdf, image_files, task_dir, album.album_id)

            yield event.plain_result("PDF 生成完成，正在转换为 TXT 后缀...")
            txt_path = await asyncio.to_thread(self._rename_pdf_to_txt, pdf_path)

            yield event.plain_result("后缀转换完成，正在加密压缩 ZIP...")
            zip_path = await asyncio.to_thread(self._zip_with_password, txt_path, task_dir, album.album_id)

            yield event.plain_result("压缩完成，正在发送文件...")
            result = self._build_file_result(event, zip_path)
            yield result
            yield event.plain_result("发送完成。")
        except Exception as exc:
            logger.exception(f"[qq_code_listener] 下载流程失败: {exc}")
            yield event.plain_result(f"处理失败：{self._friendly_error(exc)}")
        finally:
            if task_dir and task_dir.exists():
                try:
                    shutil.rmtree(task_dir, ignore_errors=True)
                except Exception:
                    pass

    def _extract_command_text(self, message_text: str) -> str | None:
        keywords = self._to_string_list(self.config.get("trigger_keywords", []))
        if not keywords:
            keywords = ["/", "!", "漫画"]

        for prefix in sorted(keywords, key=len, reverse=True):
            if not prefix:
                continue
            if message_text.startswith(prefix):
                body = message_text[len(prefix):].strip()
                if body.startswith("漫画"):
                    body = body[2:].strip()
                return body
        return None

    def _parse_command(self, command_text: str) -> tuple[str, str] | None:
        if not command_text:
            return None

        command_text = command_text.strip()

        if command_text.startswith("漫画"):
            command_text = command_text[2:].strip()

        patterns = [
            (r"^(搜索|search)\s+(.+)$", "search"),
            (r"^(下载|download|dl)\s+(.+)$", "download"),
        ]
        for pattern, action in patterns:
            match = re.match(pattern, command_text, flags=re.IGNORECASE)
            if match:
                return action, match.group(2).strip()

        # 允许直接输入番号/链接，默认为下载。
        if self._extract_album_id(command_text):
            return "download", command_text
        return None

    def _allowed_event(self, event: AstrMessageEvent) -> bool:
        group_id = self._get_first_attr(
            event,
            ["group_id", "groupid", "room_id", "channel_id", "conversation_id"],
        )
        user_id = self._get_first_attr(
            event,
            ["user_id", "userid", "sender_id", "from_user_id", "author_id"],
        )

        allowed_group_ids = set(self._to_string_list(self.config.get("allowed_group_ids", [])))
        allowed_user_ids = set(self._to_string_list(self.config.get("allowed_user_ids", [])))

        if allowed_group_ids and group_id and str(group_id) not in allowed_group_ids:
            return False
        if allowed_user_ids and user_id and str(user_id) not in allowed_user_ids:
            return False
        return True

    def _search_album(self, query: str) -> AlbumInfo:
        query = query.strip()
        album_id = self._extract_album_id(query)

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

    def _download_images(self, album_id: str) -> tuple[Path, list[Path]]:
        task_dir = self._build_base_dir() / f"task_{album_id}_{uuid.uuid4().hex[:8]}"
        task_dir.mkdir(parents=True, exist_ok=True)

        option = self._build_jm_option(base_dir=task_dir)

        if not hasattr(jmcomic, "download_album"):
            raise RuntimeError("当前 jmcomic 版本缺少 download_album 方法")

        # 使用 jmcomic 内置下载逻辑，稳定处理鉴权/图片地址等细节。
        jmcomic.download_album(album_id, option)

        image_files = sorted(
            [
                p
                for p in task_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
        )
        return task_dir, image_files

    def _images_to_pdf(self, image_files: list[Path], task_dir: Path, album_id: str) -> Path:
        if not image_files:
            raise RuntimeError("未找到可用于生成 PDF 的图片")

        rgb_images: list[Image.Image] = []
        try:
            for image_path in image_files:
                with Image.open(image_path) as img:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    rgb_images.append(img.copy())

            pdf_path = task_dir / f"{album_id}.pdf"
            first, rest = rgb_images[0], rgb_images[1:]
            first.save(pdf_path, save_all=True, append_images=rest)
            return pdf_path
        finally:
            for img in rgb_images:
                try:
                    img.close()
                except Exception:
                    pass

    def _rename_pdf_to_txt(self, pdf_path: Path) -> Path:
        if not pdf_path.exists():
            raise RuntimeError("PDF 文件不存在，无法转换后缀")

        txt_path = pdf_path.with_suffix(".txt")
        if txt_path.exists():
            txt_path.unlink()
        pdf_path.rename(txt_path)
        return txt_path

    def _zip_with_password(self, txt_path: Path, task_dir: Path, album_id: str) -> Path:
        if not txt_path.exists():
            raise RuntimeError("TXT 文件不存在，无法压缩")

        zip_path = task_dir / f"{album_id}.zip"
        level = int(self.config.get("zip_level", 9) or 9)
        level = max(0, min(9, level))
        password = str(self.config.get("zip_password") or "").strip()
        if not password:
            raise RuntimeError("未配置 zip_password，无法进行加密压缩")

        with pyzipper.AESZipFile(
            zip_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            compresslevel=level,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.write(txt_path, arcname=txt_path.name)
        return zip_path

    def _build_file_result(self, event: AstrMessageEvent, file_path: Path):
        # 适配不同 AstrBot 版本/适配器可能存在的文件发送接口。
        for method_name in [
            "file_result",
            "document_result",
            "local_file_result",
            "upload_file_result",
        ]:
            method = getattr(event, method_name, None)
            if callable(method):
                return method(str(file_path))

        logger.warning("[qq_code_listener] 当前事件对象无可用文件发送接口，退化为文本提示")
        return event.plain_result(f"文件已生成，但当前适配器不支持自动发送：{file_path}")

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

    def _build_base_dir(self) -> Path:
        configured = str(self.config.get("download_root") or DEFAULT_CONFIG["download_root"]).strip()
        return Path(configured)

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
    def _extract_album_id(text: str) -> str | None:
        match = re.search(r"(\d{5,10})", text or "")
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _get_first_attr(obj: Any, names: list[str]) -> Any:
        for name in names:
            value = getattr(obj, name, None)
            if value is not None and str(value).strip() != "":
                return value
        return None

    @staticmethod
    def _to_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        text = str(exc).strip()
        if not text:
            return "未知错误"
        return text

    @staticmethod
    def _format_album_info(album: AlbumInfo) -> str:
        chapter_preview = "\n".join(album.chapters[:8]) if album.chapters else "暂无章节信息"
        return (
            f"查询结果：\n"
            f"ID: {album.album_id}\n"
            f"标题: {album.title}\n"
            f"作者: {album.author}\n"
            f"简介: {album.intro[:180]}\n"
            f"封面: {album.cover_url or '暂无'}\n"
            f"章节预览:\n{chapter_preview}"
        )
