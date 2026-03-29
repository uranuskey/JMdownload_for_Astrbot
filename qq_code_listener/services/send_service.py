from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import File


class SendService:
    @staticmethod
    async def send_file_chain(event: AstrMessageEvent, file_path: Path) -> bool:
        if not file_path.exists():
            return False

        try:
            chain = MessageEventResult()
            chain.chain.append(File(name=file_path.name, file=str(file_path.resolve())))
            await event.send(chain)
            return True
        except Exception as exc:
            logger.warning(f"[qq_code_listener] 消息链文件发送失败: {exc}")
            return False
