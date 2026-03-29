import asyncio
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
    "download_root": "data/qq_code_listener",
    "search_result_limit": 3,
    "zip_level": 9,
    "zip_password": "123456",
}


@register(
    "qq_code_listener",
    "wzh",
    "禁漫查询下载（白名单+关键词触发）",
    "2.1.0",
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
            try:
                album = await asyncio.to_thread(self.manga_service.search_album, payload)
                yield event.plain_result(self._format_album_info(album))
            except Exception as exc:
                logger.exception(f"[qq_code_listener] 查询失败: {exc}")
                yield event.plain_result(f"查询失败：{self._friendly_error(exc)}")
            return

        task_dir: Path | None = None
        try:
            album = await asyncio.to_thread(self.manga_service.search_album, payload)
            yield event.plain_result(f"已定位漫画：{album.title}（ID: {album.album_id}），正在下载图片...")

            task_dir, image_files = await asyncio.to_thread(self.manga_service.download_images, album.album_id)
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

        text = command_text.strip()
        if text.startswith("漫画"):
            text = text[2:].strip()

        patterns = [
            (r"^(搜索|search)\s+(.+)$", "search"),
            (r"^(下载|download|dl)\s+(.+)$", "download"),
        ]
        for pattern, action in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if match:
                return action, match.group(2).strip()

        if self.manga_service.extract_album_id(text):
            return "download", text
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
