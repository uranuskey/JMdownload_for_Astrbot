import asyncio
import hashlib
import inspect
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
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
    from .services.cache_service import CacheService
    from .services.audit_service import AuditService
except ImportError:
    from plugin_types import AlbumInfo
    from services.manga_service import MangaService
    from services.package_service import PackageService
    from services.send_service import SendService
    from services.cache_service import CacheService
    from services.audit_service import AuditService


DEFAULT_CONFIG = {
    "enabled": True,
    "trigger_keywords": ["/", "!", "漫画"],
    "allowed_group_ids": [],
    "allowed_user_ids": [],
    "blacklist_group_ids": [],
    "blacklist_user_ids": [],
    "allow_group_admin_bypass": True,
    "deny_reply_enabled": False,
    "deny_reply_text": "你没有权限使用该功能。",
    "download_root": "data/JMdownload_for_Astrbot",
    "cache_root": "data/plugin_data/JMdownload_for_Astrbot/cache",
    "cache_ttl_hours": 72,
    "search_result_limit": 3,
    "download_profile": "balanced",
    "pdf_layout_mode": "multipage",
    "long_page_max_images": 80,
    "long_page_max_height": 60000,
    "zip_level": 9,
    "zip_password": "123456",
    "default_max_page": 200,
    "retry_per_chapter": 3,
    "download_concurrency_limit": 2,
    "group_download_concurrency_limit": 1,
    "daily_quota_per_user": 0,
    "cooldown_seconds": 0,
    "confirm_ttl_seconds": 180,
    "audit_log_path": "data/JMdownload_for_Astrbot/audit.jsonl",
    "admin_user_ids": [],
}

STATE_OPEN_KEY = "JMdownload_for_Astrbot_open"
STATE_MAX_PAGE_KEY = "JMdownload_for_Astrbot_max_page"
STATE_PENDING_CONFIRM_PREFIX = "JMdownload_for_Astrbot_pending_confirm_"


@register(
    "JMdownload_for_Astrbot",
    "wzh",
    "禁漫查询下载（白名单+关键词触发）",
    "2.3.0",
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
        self.cache_service = CacheService(self.config)
        self.audit_service = AuditService(self.config)
        self._queue_lock = asyncio.Lock()
        self._global_waiting = 0
        self._scope_waiting: dict[str, int] = {}
        self._group_semaphores: dict[str, asyncio.Semaphore] = {}
        global_limit = max(1, int(self.config.get("download_concurrency_limit", 2) or 2))
        self._global_semaphore = asyncio.Semaphore(global_limit)

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
            yield event.plain_result(self._build_help_text())
            return

        action, payload = parsed
        sender_id = self._extract_sender_id(event)
        group_id = str(
            self._get_first_attr(
                event,
                ["group_id", "groupid", "room_id", "channel_id", "conversation_id"],
            )
            or ""
        )

        if action == "help":
            yield event.plain_result(self._build_help_text())
            return

        if action == "doctor":
            doctor_text = await asyncio.to_thread(self._build_doctor_text)
            yield event.plain_result(doctor_text)
            return

        if action == "stats":
            stat_text = await asyncio.to_thread(self._build_stats_text)
            yield event.plain_result(stat_text)
            return

        if action == "confirm_no":
            await self._clear_pending_confirm(event)
            yield event.plain_result("已取消待确认下载任务。")
            return

        if action == "confirm_yes":
            pending = await self._load_pending_confirm(event)
            if pending is None:
                yield event.plain_result("当前没有待确认任务，或确认已超时。")
                return
            await self._clear_pending_confirm(event)
            payload = {
                "album_id": str(pending.get("album_id") or ""),
                "chapter_id": pending.get("chapter_id"),
                "confirmed": True,
            }
            expire_at = int(pending.get("expire_at") or 0)
            remain = max(0, expire_at - int(time.time()))
            yield event.plain_result(f"已确认，开始任务（原确认剩余 {remain} 秒）。")

        if action == "search":
            t0 = time.time()
            try:
                keyword = str(payload.get("keyword") or "").strip()
                limit = int(payload.get("limit") or 3)
                page = int(payload.get("page") or 1)
                if bool(payload.get("next")):
                    keyword, limit, page = await self._resolve_next_search_state(event)

                albums = await asyncio.to_thread(self.manga_service.search_albums, keyword, limit, page)
                await self._save_search_state(event, keyword, limit, page)
                yield event.plain_result(self._format_search_results(keyword, albums, page))
                await asyncio.to_thread(
                    self.audit_service.log_event,
                    "search",
                    True,
                    sender_id,
                    group_id,
                    int((time.time() - t0) * 1000),
                    {"keyword": keyword, "page": page, "limit": limit},
                )
            except Exception as exc:
                logger.exception(f"[JMdownload_for_Astrbot] 查询失败: {exc}")
                yield event.plain_result(f"查询失败：{self._friendly_error(exc)}")
                await asyncio.to_thread(
                    self.audit_service.log_event,
                    "search",
                    False,
                    sender_id,
                    group_id,
                    int((time.time() - t0) * 1000),
                )
            return

        if action == "download" and not bool(payload.get("confirmed")):
            album_id = str(payload.get("album_id") or "")
            chapter_id = payload.get("chapter_id")
            max_page = await self._get_max_page()

            inspect_info = await asyncio.to_thread(self.manga_service.inspect_album_pages, album_id, chapter_id)
            chapter_count = int(inspect_info.get("chapter_count") or 0)
            total_pages = int(inspect_info.get("total_pages") or 0)
            unknown_pages = bool(int(inspect_info.get("unknown_pages") or 0))

            total_pages_text = "未知" if unknown_pages and total_pages <= 0 else str(total_pages)
            yield event.plain_result(
                f"下载预检：预计章节数 {chapter_count}，预计总页数 {total_pages_text}，当前上限 {max_page}。"
            )

            if (not unknown_pages) and total_pages > max_page > 0:
                ttl = max(30, int(self.config.get("confirm_ttl_seconds", 180) or 180))
                expire_at = int(time.time()) + ttl
                await self._save_pending_confirm(
                    event,
                    {
                        "album_id": album_id,
                        "chapter_id": chapter_id,
                        "expire_at": expire_at,
                    },
                )
                yield event.plain_result(
                    f"警告：预计总页数 {total_pages} 超过上限 {max_page}。"
                    f"如需继续请在 {ttl} 秒内发送 /yes，否则任务取消。"
                )
                return

        # 下载类请求的冷却和配额限制（仅在实际开始下载前生效）
        rate_limited = await self._check_rate_limit(event)
        if rate_limited:
            yield event.plain_result(rate_limited)
            return

        task_dir: Path | None = None
        t0 = time.time()
        try:
            album_id = payload.get("album_id")
            chapter_id = payload.get("chapter_id")
            max_page = await self._get_max_page()
            retry_per_chapter = int(self.config.get("retry_per_chapter", 3) or 3)
            profile = self._normalize_profile(self.config.get("download_profile", "balanced"))
            layout_mode = self._normalize_layout_mode(self.config.get("pdf_layout_mode", "multipage"))
            long_page_max_images = int(self.config.get("long_page_max_images", 80) or 80)
            long_page_max_height = int(self.config.get("long_page_max_height", 60000) or 60000)
            zip_level = int(self.config.get("zip_level", 9) or 9)
            zip_password = str(self.config.get("zip_password") or "").strip()

            cached_zip = await asyncio.to_thread(
                self.cache_service.get_cached_zip,
                str(album_id or ""),
                chapter_id,
                max_page,
                zip_level,
                zip_password,
                profile,
            )
            if cached_zip and cached_zip.exists():
                yield event.plain_result(f"命中缓存，正在发送：{cached_zip.name}")
                sent = await self.send_service.send_file_chain(event, cached_zip)
                if sent:
                    yield event.plain_result("发送完成。")
                else:
                    yield event.plain_result(f"缓存文件发送失败，请手动取文件：{cached_zip}")
                await asyncio.to_thread(
                    self.audit_service.log_event,
                    "download",
                    sent,
                    sender_id,
                    group_id,
                    int((time.time() - t0) * 1000),
                    {"album_id": album_id, "chapter_id": chapter_id, "cache_hit": True},
                )
                return

            scope = self._build_scope_key(event)
            async with self._acquire_download_slot(scope) as queue_info:
                ahead = int(queue_info.get("ahead", 0))
                if ahead > 0:
                    yield event.plain_result(f"任务较多，已加入队列，前方约 {ahead} 个任务。")

                album = await asyncio.to_thread(self.manga_service.search_album, album_id)
                yield event.plain_result(f"已定位漫画：{album.title}（ID: {album.album_id}），正在下载图片...")

                task_dir, image_files, failed_photos = await asyncio.to_thread(
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

                if failed_photos:
                    preview = ", ".join(failed_photos[:10])
                    suffix = " ..." if len(failed_photos) > 10 else ""
                    yield event.plain_result(f"补偿重试后仍有章节失败（{len(failed_photos)}）：{preview}{suffix}")

                pdf_path = await asyncio.to_thread(
                    self.package_service.images_to_pdf,
                    image_files,
                    task_dir,
                    album.album_id,
                    profile,
                    layout_mode,
                    long_page_max_images,
                    long_page_max_height,
                )
                exe_path = await asyncio.to_thread(self.package_service.rename_pdf_to_exe, pdf_path)
                zip_path = await asyncio.to_thread(
                    self.package_service.zip_with_password,
                    exe_path,
                    task_dir,
                    album.album_id,
                    zip_level,
                    zip_password,
                )

                cache_path = await asyncio.to_thread(
                    self.cache_service.save_cache,
                    zip_path,
                    album.album_id,
                    chapter_id,
                    max_page,
                    zip_level,
                    zip_password,
                    profile,
                )

                sent = await self.send_service.send_file_chain(event, cache_path)
                if sent:
                    yield event.plain_result("发送完成。")
                else:
                    yield event.plain_result(f"消息链文件发送失败，请手动取文件：{cache_path}")

                await asyncio.to_thread(
                    self.audit_service.log_event,
                    "download",
                    sent,
                    sender_id,
                    group_id,
                    int((time.time() - t0) * 1000),
                    {
                        "album_id": album.album_id,
                        "chapter_id": chapter_id,
                        "cache_hit": False,
                        "failed_chapters": failed_photos,
                        "profile": profile,
                        "layout_mode": layout_mode,
                    },
                )
        except Exception as exc:
            logger.exception(f"[JMdownload_for_Astrbot] 下载流程失败: {exc}")
            yield event.plain_result(f"处理失败：{self._friendly_error(exc)}")
            await asyncio.to_thread(
                self.audit_service.log_event,
                "download",
                False,
                sender_id,
                group_id,
                int((time.time() - t0) * 1000),
                {"album_id": payload.get("album_id"), "chapter_id": payload.get("chapter_id")},
            )
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

    def _parse_command(self, command_text: str) -> tuple[str, dict[str, Any]] | None:
        if not command_text:
            return None

        text = command_text.strip()
        text = re.sub(r"^/+", "", text)
        if text.startswith("漫画"):
            text = text[2:].strip()
        text = re.sub(r"^jmcomic\b", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return None

        if re.match(r"^(help|帮助)$", text, flags=re.IGNORECASE):
            return "help", {}
        if re.match(r"^(doctor|自检)$", text, flags=re.IGNORECASE):
            return "doctor", {}
        if re.match(r"^(stats|统计)$", text, flags=re.IGNORECASE):
            return "stats", {}
        if re.match(r"^(next|下页)$", text, flags=re.IGNORECASE):
            return "search", {"next": True}
        if re.match(r"^(yes|确认)$", text, flags=re.IGNORECASE):
            return "confirm_yes", {}
        if re.match(r"^(no|取消)$", text, flags=re.IGNORECASE):
            return "confirm_no", {}

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
    def _parse_search_payload(text: str) -> dict[str, Any]:
        payload_text = (text or "").strip()
        if not payload_text:
            return {"keyword": "", "limit": 3, "page": 1}

        if re.match(r"^(next|下页)$", payload_text, flags=re.IGNORECASE):
            return {"next": True}

        tokens = [t for t in payload_text.split() if t.strip()]
        keyword_parts: list[str] = []
        limit = 3
        page = 1

        for token in tokens:
            page_match = re.match(r"^[pP](\d+)$", token)
            if page_match:
                page = max(1, int(page_match.group(1)))
                continue
            if token.isdigit():
                limit = max(1, min(20, int(token)))
                continue
            keyword_parts.append(token)

        keyword = " ".join(keyword_parts).strip()
        return {"keyword": keyword, "limit": limit, "page": page}

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
        blacklist_group_ids = set(self._to_string_list(self.config.get("blacklist_group_ids", [])))
        blacklist_user_ids = set(self._to_string_list(self.config.get("blacklist_user_ids", [])))

        if blacklist_group_ids and group_id and str(group_id) in blacklist_group_ids:
            return False
        if blacklist_user_ids and user_id and str(user_id) in blacklist_user_ids:
            return False

        if allowed_group_ids and group_id and str(group_id) not in allowed_group_ids:
            return False
        if allowed_user_ids and user_id and str(user_id) not in allowed_user_ids:
            allow_admin_bypass = bool(self.config.get("allow_group_admin_bypass", True))
            if not (allow_admin_bypass and self._is_group_admin_event(event)):
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

    def _is_group_admin_event(self, event: AstrMessageEvent) -> bool:
        role = str(self._get_first_attr(event, ["sender_role", "role", "member_role"]) or "").lower()
        if role in {"admin", "owner", "group_admin", "administrator"}:
            return True

        flag = self._get_first_attr(event, ["is_admin", "is_group_admin", "is_owner"])
        if isinstance(flag, bool):
            return flag
        return str(flag).lower() in {"true", "1", "yes"}

    async def _check_rate_limit(self, event: AstrMessageEvent) -> str | None:
        sender_id = self._extract_sender_id(event)
        if not sender_id:
            return None

        cooldown = max(0, int(self.config.get("cooldown_seconds", 0) or 0))
        if cooldown > 0:
            key = f"JMdownload_for_Astrbot_cooldown_{sender_id}"
            last_ts = float(await self._get_kv(key, 0) or 0)
            now = time.time()
            remain = int(cooldown - (now - last_ts))
            if remain > 0:
                return f"操作过于频繁，请 {remain} 秒后再试。"
            await self._set_kv(key, now)

        quota = max(0, int(self.config.get("daily_quota_per_user", 0) or 0))
        if quota > 0:
            day = datetime.now().strftime("%Y%m%d")
            key = f"JMdownload_for_Astrbot_quota_{day}_{sender_id}"
            used = int(await self._get_kv(key, 0) or 0)
            if used >= quota:
                return f"你今日下载额度已用尽（{quota}/{quota}）。"
            await self._set_kv(key, used + 1)
        return None

    def _build_scope_key(self, event: AstrMessageEvent) -> str:
        group_id = self._get_first_attr(
            event,
            ["group_id", "groupid", "room_id", "channel_id", "conversation_id"],
        )
        if group_id is not None and str(group_id).strip() != "":
            return f"group:{group_id}"
        return f"user:{self._extract_sender_id(event) or 'unknown'}"

    def _get_group_semaphore(self, scope_key: str) -> asyncio.Semaphore:
        if scope_key not in self._group_semaphores:
            limit = max(1, int(self.config.get("group_download_concurrency_limit", 1) or 1))
            self._group_semaphores[scope_key] = asyncio.Semaphore(limit)
        return self._group_semaphores[scope_key]

    @asynccontextmanager
    async def _acquire_download_slot(self, scope_key: str):
        sem = self._get_group_semaphore(scope_key)
        async with self._queue_lock:
            self._global_waiting += 1
            self._scope_waiting[scope_key] = self._scope_waiting.get(scope_key, 0) + 1
            ahead = max(self._global_waiting - 1, self._scope_waiting[scope_key] - 1)

        entered = False
        try:
            async with self._global_semaphore:
                async with sem:
                    entered = True
                    async with self._queue_lock:
                        self._global_waiting = max(0, self._global_waiting - 1)
                        self._scope_waiting[scope_key] = max(0, self._scope_waiting.get(scope_key, 1) - 1)
                    yield {"ahead": ahead}
        finally:
            if not entered:
                async with self._queue_lock:
                    self._global_waiting = max(0, self._global_waiting - 1)
                    self._scope_waiting[scope_key] = max(0, self._scope_waiting.get(scope_key, 1) - 1)

    def _search_state_key(self, event: AstrMessageEvent) -> str:
        sender_id = self._extract_sender_id(event)
        scope = self._build_scope_key(event)
        raw = f"{scope}|{sender_id}"
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return f"JMdownload_for_Astrbot_search_state_{digest}"

    def _pending_confirm_key(self, event: AstrMessageEvent) -> str:
        sender_id = self._extract_sender_id(event)
        scope = self._build_scope_key(event)
        raw = f"{scope}|{sender_id}|confirm"
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return f"{STATE_PENDING_CONFIRM_PREFIX}{digest}"

    async def _save_pending_confirm(self, event: AstrMessageEvent, payload: dict[str, Any]) -> None:
        key = self._pending_confirm_key(event)
        await self._set_kv(key, payload)

    async def _load_pending_confirm(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        key = self._pending_confirm_key(event)
        value = await self._get_kv(key, None)
        if not isinstance(value, dict):
            return None
        expire_at = int(value.get("expire_at") or 0)
        if expire_at <= int(time.time()):
            await self._clear_pending_confirm(event)
            return None
        return value

    async def _clear_pending_confirm(self, event: AstrMessageEvent) -> None:
        key = self._pending_confirm_key(event)
        await self._set_kv(key, None)

    async def _save_search_state(self, event: AstrMessageEvent, keyword: str, limit: int, page: int) -> None:
        key = self._search_state_key(event)
        value = {
            "keyword": str(keyword or "").strip(),
            "limit": max(1, int(limit or 3)),
            "page": max(1, int(page or 1)),
        }
        await self._set_kv(key, value)

    async def _resolve_next_search_state(self, event: AstrMessageEvent) -> tuple[str, int, int]:
        key = self._search_state_key(event)
        state = await self._get_kv(key, None)
        if not isinstance(state, dict):
            raise RuntimeError("没有可翻页的历史搜索，请先执行一次“搜索 关键词”。")

        keyword = str(state.get("keyword") or "").strip()
        limit = max(1, int(state.get("limit") or 3))
        page = max(1, int(state.get("page") or 1)) + 1
        if not keyword:
            raise RuntimeError("没有可翻页的历史搜索，请先执行一次“搜索 关键词”。")
        return keyword, limit, page

    def _normalize_profile(self, value: Any) -> str:
        profile = str(value or "balanced").strip().lower()
        if profile not in {"fast", "balanced", "high"}:
            return "balanced"
        return profile

    def _normalize_layout_mode(self, value: Any) -> str:
        mode = str(value or "multipage").strip().lower()
        if mode not in {"multipage", "longpage"}:
            return "multipage"
        return mode

    def _build_help_text(self) -> str:
        return (
            "指令格式:\n"
            "1) /jmcomic 422866\n"
            "2) /jmcomic 422866 p123456\n"
            "3) /jmcomic 搜索 关键词 [数量] [p页码]\n"
            "4) /jmcomic next（翻到上一搜索的下一页）\n"
            "5) /jmcomic help | doctor | stats\n"
            "6) 当超上限预警时，发送 /yes 继续（3分钟内有效），/no 取消\n"
            "管理员:\n"
            "- /jmcomic set maxpage 200\n"
            "- /jmcomic open|close\n"
            "说明:\n"
            "- 搜索默认返回 3 条，最大 20 条\n"
            "- 支持缓存复用、失败章节补偿重试、下载队列限流"
        )

    def _build_doctor_text(self) -> str:
        info = self.manga_service.doctor_check()
        profile = self._normalize_profile(self.config.get("download_profile", "balanced"))
        layout_mode = self._normalize_layout_mode(self.config.get("pdf_layout_mode", "multipage"))
        return (
            "系统自检:\n"
            f"- download_root: {info.get('download_root', 'unknown')}\n"
            f"- jm_client: {info.get('jm_client', 'unknown')}\n"
            f"- cache_root: {self.config.get('cache_root', 'data/plugin_data/JMdownload_for_Astrbot/cache')}\n"
            f"- profile: {profile}\n"
            f"- pdf_layout_mode: {layout_mode}\n"
            f"- 并发限制: 全局 {self.config.get('download_concurrency_limit', 2)} / 同群 {self.config.get('group_download_concurrency_limit', 1)}"
        )

    def _build_stats_text(self) -> str:
        summary = self.audit_service.summarize(days=7)
        return (
            "近 7 天统计:\n"
            f"- 总请求: {summary.get('total', 0)}\n"
            f"- 成功: {summary.get('success', 0)}\n"
            f"- 失败: {summary.get('failed', 0)}\n"
            f"- 搜索: {summary.get('search', 0)}\n"
            f"- 下载: {summary.get('download', 0)}\n"
            f"- 缓存命中: {summary.get('cache_hit', 0)}\n"
            f"- 平均耗时: {summary.get('avg_duration_ms', 0)} ms"
        )

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
    def _format_search_results(keyword: str, albums: list[AlbumInfo], page: int) -> str:
        if not albums:
            return "未找到匹配漫画，请尝试更换关键词。"

        lines = [f"搜索结果（按热度排序，关键词: {keyword}，第 {page} 页）:"]
        for idx, album in enumerate(albums, start=1):
            lines.append(
                f"{idx}. [{album.album_id}] {album.title}\n"
                f"作者: {album.author}\n"
                f"热度: {album.heat_score}\n"
                f"简介: {album.intro[:120]}"
            )
        lines.append("发送 /jmcomic next 可查看下一页。")
        return "\n\n".join(lines)
