"""主动私聊 / 联络博士。

给安亦诗一个稳定的出口：需要联系博士、或在别的会话里想把话送到某个人私聊时，
不依赖当前会话，直接按 QQ 号发过去。

指令：
    /私信 123456 内容        手动发一条（仅白名单可用）
    /私信测试                发测试消息给自己（仅白名单可用）

工具：
    send_private_message(text, target="博士")
"""

from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

MASTER_QQ = "YOUR_QQ_ID"

# 同一句话 10 秒内不重复发
DEDUP_WINDOW = 10


class MasterLink(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._recent: dict[str, float] = {}

    # ==================== 基础 ====================

    def _master_qq(self) -> str:
        qq = str(self.config.get("master_qq", "") or "").strip()
        return qq or MASTER_QQ

    def _allowed_targets(self) -> set[str]:
        ids = {self._master_qq(), MASTER_QQ}
        for item in self.config.get("extra_targets", []) or []:
            s = str(item).strip()
            if s:
                ids.add(s)
        return ids

    def _authorized(self, event: AstrMessageEvent) -> bool:
        sid = str(event.get_sender_id() or "").strip()
        return bool(sid) and sid in self._allowed_targets()

    def _resolve_target(self, target: str) -> str:
        t = (target or "").strip()
        if not t or t in {"博士", "master", "博士私聊", "主人"}:
            return self._master_qq()
        return t

    def _platform_candidates(self, event: AstrMessageEvent | None) -> list[str]:
        ids: list[str] = []

        def push(pid: str) -> None:
            pid = (pid or "").strip()
            if pid and pid not in ids:
                ids.append(pid)

        push(str(self.config.get("platform_id", "") or ""))
        if event is not None:
            try:
                push(str(event.get_platform_id()))
            except Exception:
                pass
        try:
            for inst in self.context.platform_manager.platform_insts:
                meta = inst.meta()
                if meta.name in {"aiocqhttp", "qq_official_webhook", "qq_official"}:
                    push(str(meta.id))
        except Exception:
            pass
        return ids

    async def _send_private(
        self, text: str, target_qq: str, event: AstrMessageEvent | None = None
    ) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "内容是空的"

        now = time.time()
        last = self._recent.get(text)
        if last and now - last < DEDUP_WINDOW:
            return True, "这句刚发过，不重复"
        if len(self._recent) > 64:
            self._recent = {k: v for k, v in self._recent.items() if now - v < 300}

        chain = MessageChain([Plain(text)])
        tried: list[str] = []
        for pid in self._platform_candidates(event):
            session = f"{pid}:FriendMessage:{target_qq}"
            tried.append(pid)
            try:
                ok = await self.context.send_message(session, chain)
                if ok:
                    self._recent[text] = now
                    return True, pid
            except Exception as e:
                logger.debug(f"[master_link] {session} 发送失败: {e}")
        return False, "平台没接通（试过：" + "、".join(tried or ["无"]) + "）"

    # ==================== 工具 ====================

    @filter.llm_tool(name="send_private_message")
    async def send_private_message(
        self,
        event: AstrMessageEvent,
        text: str,
        target: str = "博士",
    ):
        """
        主动发一条私聊消息给指定对象，和当前所在会话无关。适合需要通知博士、或想把话单独送到某人私聊时使用。
        Args:
            text(string): 要发送的内容，纯文本，简短说清，别堆长段落
            target(string): 收件人，填"博士"或 QQ 号，留空默认发给博士
        """
        qq = self._resolve_target(target)
        if qq not in self._allowed_targets():
            return f"没有给 {qq} 发私聊的权限。"
        ok, info = await self._send_private(text, qq, event)
        if ok:
            return f"已发到 {qq} 私聊（{info}）。"
        return f"没发出去：{info}"

    # ==================== 指令 ====================

    @filter.command("私信")
    async def cmd_private(self, event: AstrMessageEvent):
        if not self._authorized(event):
            return
        raw = (event.get_message_str() or "").strip()
        arg = raw.split(" ", 1)[1].strip() if " " in raw else ""
        parts = arg.split(None, 1)

        if not parts or not arg:
            yield event.plain_result("格式：/私信 123456 内容")
            return

        if parts[0] in {"测试", "test"}:
            ok, info = await self._send_private(
                f"链路测试，时间 {time.strftime('%H:%M:%S')}", self._master_qq(), event
            )
            yield event.plain_result(
                f"已发到博士私聊（{info}）" if ok else f"没发出去：{info}"
            )
            return

        target = parts[0]
        content = parts[1].strip() if len(parts) > 1 else ""
        if not target.isdigit() or not content:
            yield event.plain_result("格式：/私信 123456 内容")
            return
        if target not in self._allowed_targets():
            yield event.plain_result("这个号不在可发范围里。")
            return
        ok, info = await self._send_private(content, target, event)
        yield event.plain_result(f"已发出（{info}）" if ok else f"没发出去：{info}")
