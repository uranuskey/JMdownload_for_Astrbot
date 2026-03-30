import asyncio
import inspect
import re
import shutil
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .plugin_types import AlbumInfo
    from .services.manga_service import MangaService
    from .services.package_service import PackageService
    from .services.send_service import SendService
except ImportError:
    from plugin_types import AlbumInfo
    from services.manga_service import MangaService
    from services.package_service import PackageService
    from services.send_service import SendService


DEFAULT_CONFIG = {
    "enabled": True,
    "trigger_keywords": ["/", "!", "漫画"],
    "allowed_group_ids": [],
    "allowed_user_ids": [],
    "deny_reply_enabled": False,
    "deny_reply_text": "你没有权限使用该功能。",
    "download_root": "data/JMdownload_for_Astrbot",
    "search_result_limit": 3,
    "zip_level": 9,
    "zip_password": "123456",
    "default_max_page": 200,
    "retry_per_chapter": 3,
    "admin_user_ids": [],
}

STATE_OPEN_KEY = "JMdownload_for_Astrbot_open"
STATE_MAX_PAGE_KEY = "JMdownload_for_Astrbot_max_page"


@register(
    "JMdownload_for_Astrbot",
    "wzh",
    "禁漫查询下载（白名单+关键词触发）",
    "2.2.0",
)
class QQCodeListenerPlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.manga_service = MangaService(self.config)
        self.package_service = PackageService()
        self.send_service = SendService()

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

        # 管理命令
        admin_handled = await self._try_handle_admin_command(event, command_text)
        if admin_handled is not None:
            yield event.plain_result(admin_handled)
            return

        if not await self._is_feature_open():
            return

        parsed = self._parse_command(command_text)
        if parsed is None:
            help_text = (
                "指令格式:\n"
                "1) /jmcomic 422866\n"
                "2) /jmcomic 422866 p123456\n"
                "3) /jmcomic 搜索 关键词 [数量，默认3]\n"
                "管理员:\n"
                "- /jmcomic set maxpage 200\n"
                "- /jmcomic open|close\n"
                "兼容旧格式: /漫画 下载 422866"
            )
            yield event.plain_result(help_text)
            return

        action, payload = parsed
        if action == "search":
            try:
                keyword = str(payload.get("keyword") or "").strip()
                limit = int(payload.get("limit") or 3)
                albums = await asyncio.to_thread(self.manga_service.search_albums, keyword, limit)
                yield event.plain_result(self._format_search_results(keyword, albums))
            except Exception as exc:
                logger.exception(f"[JMdownload_for_Astrbot] 查询失败: {exc}")
                yield event.plain_result(f"查询失败：{self._friendly_error(exc)}")
            return

        task_dir: Path | None = None
        try:
            album_id = payload.get("album_id")
            chapter_id = payload.get("chapter_id")
            max_page = await self._get_max_page()
            retry_per_chapter = int(self.config.get("retry_per_chapter", 3) or 3)

            album = await asyncio.to_thread(self.manga_service.search_album, album_id)
            yield event.plain_result(f"已定位漫画：{album.title}（ID: {album.album_id}），正在下载图片...")

            task_dir, image_files = await asyncio.to_thread(
                self.manga_service.download_images,
                album.album_id,
                chapter_id,
                retry_per_chapter,
                max_page,
            )
            if not image_files:
                raise RuntimeError("下载完成但未找到任何图片文件")

            if len(image_files) <= 2 and len(album.chapters) >= 2:
                yield event.plain_result("该漫画章节较多但实际下载页数很少，可能是章节权限、限流或网络问题。")

            pdf_path = await asyncio.to_thread(self.package_service.images_to_pdf, image_files, task_dir, album.album_id)
            exe_path = await asyncio.to_thread(self.package_service.rename_pdf_to_exe, pdf_path)
            zip_path = await asyncio.to_thread(
                self.package_service.zip_with_password,
                exe_path,
                task_dir,
                album.album_id,
                int(self.config.get("zip_level", 9) or 9),
                str(self.config.get("zip_password") or "").strip(),
            )

            sent = await self.send_service.send_file_chain(event, zip_path)
            if sent:
                yield event.plain_result("发送完成。")
            else:
                yield event.plain_result(f"消息链文件发送失败，请手动取文件：{zip_path}")
        except Exception as exc:
            logger.exception(f"[JMdownload_for_Astrbot] 下载流程失败: {exc}")
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

    def _parse_command(self, command_text: str) -> tuple[str, str | dict[str, str | None]] | None:
        if not command_text:
            return None

        text = command_text.strip()
        text = re.sub(r"^/+", "", text)
        if text.startswith("漫画"):
            text = text[2:].strip()
        text = re.sub(r"^jmcomic\b", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return None

        patterns = [
            (r"^(搜索|search)\s+(.+)$", "search"),
            (r"^(下载|download|dl)\s+(.+)$", "download"),
        ]
        for pattern, action in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if match:
                payload_text = match.group(2).strip()
                if action == "search":
                    return "search", self._parse_search_payload(payload_text)
                return "download", self._parse_download_payload(payload_text)

        # 兼容: 唤醒词 + 纯数字
        number_match = re.match(r"^(\d+)(?:\s+p?(\d+))?$", text, flags=re.IGNORECASE)
        if number_match:
            payload = {
                "album_id": number_match.group(1),
                "chapter_id": number_match.group(2),
            }
            return "download", payload

        if self.manga_service.extract_album_id(text):
            return "download", self._parse_download_payload(text)
        return None

    def _parse_download_payload(self, text: str) -> dict[str, str | None]:
        album_id = self.manga_service.extract_album_id(text)
        chapter_id = self.manga_service.extract_chapter_id(text)
        return {
            "album_id": album_id,
            "chapter_id": chapter_id,
        }

    @staticmethod
    def _parse_search_payload(text: str) -> dict[str, str | int]:
        payload_text = (text or "").strip()
        if not payload_text:
            return {"keyword": "", "limit": 3}

        limit = 3
        match = re.match(r"^(.+?)\s+(\d+)$", payload_text)
        if match:
            payload_text = match.group(1).strip()
            limit = max(1, min(20, int(match.group(2))))

        return {"keyword": payload_text, "limit": limit}

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

    async def _try_handle_admin_command(self, event: AstrMessageEvent, command_text: str) -> str | None:
        text = command_text.strip().lstrip("/")

        # 支持 /jmcomic set maxpage x 或 /set maxpage x
        set_match = re.match(r"^(?:jmcomic\s+)?set\s+maxpage\s+(\d+)$", text, flags=re.IGNORECASE)
        if set_match:
            if not self._is_admin(event):
                return "无权限：仅管理员可设置最大页数。"
            value = max(1, int(set_match.group(1)))
            await self._set_kv(STATE_MAX_PAGE_KEY, value)
            return f"已设置最大下载页数为 {value}"

        # 支持 /jmcomic open|close，兼容多斜杠写法
        open_close_match = re.match(r"^jmcomic\s+(open|close)$", text, flags=re.IGNORECASE)
        if open_close_match:
            if not self._is_admin(event):
                return "无权限：仅管理员可开关功能。"
            action = open_close_match.group(1).lower()
            open_state = action == "open"
            await self._set_kv(STATE_OPEN_KEY, open_state)
            return "jmcomic 功能已开启" if open_state else "jmcomic 功能已关闭"

        return None

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = self._extract_sender_id(event)
        admins = set(self._to_string_list(self.config.get("admin_user_ids", [])))
        return bool(sender_id) and str(sender_id) in admins

    def _extract_sender_id(self, event: AstrMessageEvent) -> str:
        for name in ["get_sender_id"]:
            method = getattr(event, name, None)
            if callable(method):
                try:
                    value = method()
                    if value is not None and str(value).strip() != "":
                        return str(value)
                except Exception:
                    pass

        uid = self._get_first_attr(
            event,
            ["user_id", "userid", "sender_id", "from_user_id", "author_id"],
        )
        return "" if uid is None else str(uid)

    async def _is_feature_open(self) -> bool:
        value = await self._get_kv(STATE_OPEN_KEY, True)
        return bool(value)

    async def _get_max_page(self) -> int:
        default_value = int(self.config.get("default_max_page", 200) or 200)
        value = await self._get_kv(STATE_MAX_PAGE_KEY, default_value)
        try:
            return max(1, int(value))
        except Exception:
            return default_value

    async def _get_kv(self, key: str, default: Any) -> Any:
        getter = getattr(self, "get_kv_data", None)
        if not callable(getter):
            return default
        try:
            value = getter(key)
            if inspect.isawaitable(value):
                value = await value
            return default if value is None else value
        except Exception:
            return default

    async def _set_kv(self, key: str, value: Any) -> None:
        setter = getattr(self, "put_kv_data", None)
        if not callable(setter):
            return
        try:
            result = setter(key, value)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(f"[JMdownload_for_Astrbot] 写入 KV 失败: key={key}")

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
            f"热度: {album.heat_score}\n"
            f"简介: {album.intro[:180]}\n"
            f"封面: {album.cover_url or '暂无'}\n"
            f"章节预览:\n{chapter_preview}"
        )

    @staticmethod
    def _format_search_results(keyword: str, albums: list[AlbumInfo]) -> str:
        if not albums:
            return "未找到匹配漫画，请尝试更换关键词。"

        lines = [f"搜索结果（按热度排序，关键词: {keyword}）:"]
        for idx, album in enumerate(albums, start=1):
            lines.append(
                f"{idx}. [{album.album_id}] {album.title}\n"
                f"作者: {album.author}\n"
                f"热度: {album.heat_score}\n"
                f"简介: {album.intro[:120]}"
            )
        return "\n\n".join(lines)
