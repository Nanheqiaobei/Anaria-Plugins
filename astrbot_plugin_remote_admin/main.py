"""跨群远程管理。

在私聊（或任意会话）里直接对指定群执行管理动作：禁言、解禁、踢人、全员禁言、撤回消息、发群消息、按昵称找人。
附带账号资料性别修改（set_qq_profile）。

工具：
    list_qq_groups()                     列出机器人所在的群（群号、群名、人数）
    group_admin(action, group_id, ...)   对指定群执行动作

权限：只认运行时 QQ 号，名单见 ADMIN_IDS 与配置 extra_admins。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

MASTER_QQ = "YOUR_QQ_ID"

ADMIN_IDS = {
    "YOUR_QQ_ID",  # 博士
    "1002153674",  # aa剑侠
    "1022156836",  # luojiaping
    "167141625",  # 云曦
    "3925346097",  # tux
    "3462831700",  # Ww
}

DURATION_RE = re.compile(r"^(\d+)(s|m|h|d|秒|分|分钟|小时|时|天)$", re.IGNORECASE)
UNIT = {
    "s": 1,
    "秒": 1,
    "m": 60,
    "分": 60,
    "分钟": 60,
    "h": 3600,
    "时": 3600,
    "小时": 3600,
    "d": 86400,
    "天": 86400,
}

ACT_ALIAS = {
    "禁言": "ban",
    "解禁": "unban",
    "踢": "kick",
    "踢出": "kick",
    "全员禁言": "whole_ban",
    "解除全员禁言": "whole_unban",
    "撤回": "recall",
    "发消息": "send",
    "成员": "members",
    "找人": "find",
    "性别": "sex",
}

CACHE_TTL = 30


def parse_duration(text: str) -> int | None:
    """2h / 30m / 600 / 3天 转秒。纯数字按秒。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    m = DURATION_RE.match(text)
    if not m:
        return None
    return int(m.group(1)) * UNIT[m.group(2).lower()]


def human(seconds: int) -> str:
    if seconds <= 0:
        return "0"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _extract_list(result: Any) -> list:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return data
    return []


class RemoteAdmin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._group_client: dict[str, Any] = {}
        self._group_info: dict[str, dict] = {}
        self._cache_at: float = 0.0

    # ==================== 初始化 ====================

    async def initialize(self) -> None:
        """配置里写了 apply_sex 就自动改一次资料性别，改完落标记不再重复。"""
        target = str(self.config.get("apply_sex", "") or "").strip()
        if target not in {"男", "女", "male", "female", "1", "2"}:
            return
        sex = 2 if target in {"女", "female", "2"} else 1
        flag_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_remote_admin"
        )
        try:
            flag_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        flag = flag_dir / f"sex_{sex}.done"
        if flag.exists():
            return

        for _ in range(20):
            await asyncio.sleep(3)
            for client in self._clients():
                try:
                    params: dict[str, Any] = {"sex": sex}
                    try:
                        info = await client.call_action("get_login_info")
                        if isinstance(info, dict) and info.get("nickname"):
                            params["nickname"] = str(info["nickname"])
                    except Exception:
                        pass
                    await client.call_action("set_qq_profile", **params)
                    try:
                        flag.write_text("done", encoding="utf-8")
                    except Exception:
                        pass
                    logger.info(f"[remote_admin] 资料性别已改为 {sex}")
                    return
                except Exception as e:
                    logger.debug(f"[remote_admin] set_qq_profile 失败: {e}")
        logger.warning("[remote_admin] 改性别没成功，接口可能不支持")

    # ==================== 权限 ====================

    def _admins(self) -> set[str]:
        ids = set(ADMIN_IDS)
        for item in self.config.get("extra_admins", []) or []:
            s = str(item).strip()
            if s:
                ids.add(s)
        return ids

    def _authorized(self, event: AstrMessageEvent) -> bool:
        sid = str(event.get_sender_id() or "").strip()
        return bool(sid) and sid in self._admins()

    # ==================== 平台 ====================

    def _clients(self) -> list[Any]:
        out = []
        try:
            for inst in self.context.platform_manager.platform_insts:
                if not isinstance(inst, AiocqhttpAdapter):
                    continue
                try:
                    client = inst.get_client()
                except Exception as e:
                    logger.debug(f"[remote_admin] 取 client 失败: {e}")
                    continue
                if client is not None:
                    out.append(client)
        except Exception as e:
            logger.warning(f"[remote_admin] 列举平台失败: {e}")
        return out

    async def _refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._cache_at < CACHE_TTL and self._group_client:
            return
        mapping: dict[str, Any] = {}
        infos: dict[str, dict] = {}
        for client in self._clients():
            try:
                result = await client.call_action("get_group_list")
            except Exception as e:
                logger.debug(f"[remote_admin] get_group_list 失败: {e}")
                continue
            for item in _extract_list(result):
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("group_id", "")).strip()
                if not gid or gid in mapping:
                    continue
                mapping[gid] = client
                infos[gid] = {
                    "name": str(item.get("group_name", "") or ""),
                    "count": item.get("member_count")
                    or item.get("max_member_count")
                    or "",
                }
        self._group_client = mapping
        self._group_info = infos
        self._cache_at = time.time()

    async def _client_for(self, group_id: str) -> Any | None:
        await self._refresh()
        client = self._group_client.get(group_id)
        if client is None:
            await self._refresh(force=True)
            client = self._group_client.get(group_id)
        if client is None:
            clients = self._clients()
            return clients[0] if clients else None
        return client

    async def _call(self, group_id: str, action: str, **params) -> Any:
        client = await self._client_for(group_id)
        if client is None:
            raise RuntimeError("没有可用的平台连接")
        return await client.call_action(action, group_id=int(group_id), **params)

    # ==================== 工具 ====================

    @filter.llm_tool(name="list_qq_groups")
    async def list_qq_groups(self, event: AstrMessageEvent):
        """
        列出机器人在的所有 QQ 群，含群号、群名、人数。跨群管理前先用它确认群号。
        """
        if not self._authorized(event):
            return "没有查看群列表的权限。"
        await self._refresh(force=True)
        if not self._group_client:
            return "没拿到群列表，可能平台没连上。"
        lines = [f"共 {len(self._group_client)} 个群："]
        for gid in self._group_client:
            info = self._group_info.get(gid, {})
            name = info.get("name") or "未命名"
            count = info.get("count") or "?"
            lines.append(f"{gid} {name}（{count} 人）")
        return "\n".join(lines)

    @filter.llm_tool(name="group_admin")
    async def group_admin(
        self,
        event: AstrMessageEvent,
        action: str,
        group_id: str = "",
        target: str = "",
        duration: str = "",
        text: str = "",
    ):
        """
        对指定 QQ 群执行远程管理动作，可以在私聊里直接调用，不需要身处该群。群里调用时 group_id 可留空表示当前群。
        Args:
            action(string): 动作：ban 禁言、unban 解禁、kick 踢出、whole_ban 开启全员禁言、whole_unban 关闭全员禁言、recall 撤回消息、send 在群里发消息、members 查成员、find 按昵称找人、sex 改账号资料性别
            group_id(string): 目标群号；留空表示当前所在群（sex 动作不需要）
            target(string): 目标用户 QQ 号（ban/unban/kick 必填；recall 时可填要撤回的消息 ID）
            duration(string): 禁言时长，如 2h、30m、600、3天；unban 不用填
            text(string): send 的内容；find 时填要搜索的昵称；sex 时填 男/女
        """
        if not self._authorized(event):
            return "没有跨群管理的权限。"

        act = str(action or "").strip().lower()
        act = ACT_ALIAS.get(act, act)
        target = str(target or "").strip()
        text = str(text or "").strip()

        # 改资料性别，不需要群号
        if act in {"sex", "profile", "set_sex"}:
            sex = {"女": 2, "female": 2, "2": 2, "男": 1, "male": 1, "1": 1}.get(text)
            if sex is None:
                return "性别只认 男 或 女。"
            clients = self._clients()
            if not clients:
                return "平台没连上。"
            client = clients[0]
            params: dict[str, Any] = {"sex": sex}
            try:
                info = await client.call_action("get_login_info")
                if isinstance(info, dict) and info.get("nickname"):
                    params["nickname"] = str(info["nickname"])
            except Exception:
                pass
            try:
                await client.call_action("set_qq_profile", **params)
                return f"账号资料性别已改为{'女' if sex == 2 else '男'}。"
            except Exception as e:
                return f"没改成：{type(e).__name__} {e}"

        gid = str(group_id or "").strip() or str(event.get_group_id() or "").strip()
        if not gid.isdigit():
            return "群号不对，先用 list_qq_groups 看看。"

        try:
            if act == "ban":
                if not target.isdigit():
                    return "要禁言谁？给个 QQ 号。"
                seconds = parse_duration(duration) if duration else 600
                if seconds is None:
                    return "时长看不懂，写 2h、30m、600 这种。"
                await self._call(
                    gid, "set_group_ban", user_id=int(target), duration=seconds
                )
                return f"已在 {gid} 禁言 {target} {human(seconds)}。"

            if act == "unban":
                if not target.isdigit():
                    return "要给谁解禁？给个 QQ 号。"
                await self._call(gid, "set_group_ban", user_id=int(target), duration=0)
                return f"已在 {gid} 解除 {target} 的禁言。"

            if act == "kick":
                if not target.isdigit():
                    return "要踢谁？给个 QQ 号。"
                await self._call(
                    gid, "set_group_kick", user_id=int(target), reject_add_request=False
                )
                return f"已把 {target} 踢出 {gid}。"

            if act == "whole_ban":
                await self._call(gid, "set_group_whole_ban", enable=True)
                return f"已开启 {gid} 全员禁言。"

            if act == "whole_unban":
                await self._call(gid, "set_group_whole_ban", enable=False)
                return f"已关闭 {gid} 全员禁言。"

            if act == "recall":
                msg_id = target or text
                if not str(msg_id).isdigit():
                    return "撤回需要消息 ID。"
                client = await self._client_for(gid)
                if client is None:
                    return "平台没连上。"
                await client.call_action("delete_msg", message_id=int(msg_id))
                return f"已撤回消息 {msg_id}。"

            if act == "send":
                if not text:
                    return "要发什么？"
                await self._call(gid, "send_group_msg", message=text)
                return f"已发到 {gid}。"

            if act in {"members", "find"}:
                client = await self._client_for(gid)
                if client is None:
                    return "平台没连上。"
                result = await client.call_action(
                    "get_group_member_list", group_id=int(gid)
                )
                items = [m for m in _extract_list(result) if isinstance(m, dict)]
                kw = text if act == "find" else ""
                if kw:
                    hits = [
                        m
                        for m in items
                        if kw in str(m.get("card") or "")
                        or kw in str(m.get("nickname") or "")
                    ]
                    if not hits:
                        return f"{gid} 里没找到含「{kw}」的成员。"
                    lines = [f"匹配「{kw}」共 {len(hits)} 人："]
                    for m in hits[:20]:
                        card = m.get("card") or m.get("nickname") or ""
                        role = m.get("role") or ""
                        lines.append(f"{m.get('user_id')} {card} {role}".strip())
                    return "\n".join(lines)
                lines = [f"{gid} 共 {len(items)} 人（前 30）："]
                for m in items[:30]:
                    card = m.get("card") or m.get("nickname") or ""
                    role = m.get("role") or ""
                    lines.append(f"{m.get('user_id')} {card} {role}".strip())
                return "\n".join(lines)

        except Exception as e:
            logger.warning(f"[remote_admin] {act} 失败: {e}")
            return f"没做成：{type(e).__name__} {e}"

        return (
            "动作不认识，可选 ban / unban / kick / whole_ban / whole_unban / "
            "recall / send / members / find / sex。"
        )

    # ==================== 指令 ====================

    @filter.command("群管", alias={"远程管理"})
    async def cmd_remote(self, event: AstrMessageEvent):
        """/群管 <动作> <群号> <QQ> [时长]"""
        if not self._authorized(event):
            return
        raw = (event.get_message_str() or "").strip()
        arg = raw.split(" ", 1)[1].strip() if " " in raw else ""
        parts = arg.split()
        if len(parts) < 2:
            yield event.plain_result(
                "格式：/群管 禁言 群号 QQ 时长　/群管 解禁 群号 QQ　/群管 踢 群号 QQ"
            )
            return
        action = parts[0]
        gid = parts[1]
        target = parts[2] if len(parts) > 2 else ""
        duration = parts[3] if len(parts) > 3 else ""
        result = await self.group_admin(
            event, action=action, group_id=gid, target=target, duration=duration
        )
        yield event.plain_result(str(result))
