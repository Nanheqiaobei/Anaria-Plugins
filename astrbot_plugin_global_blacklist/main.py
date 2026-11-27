"""全局触发屏蔽插件。

名单内的用户无论身处哪个群聊，还是私聊，都不会触发机器人：
消息在进入模型调用之前就被终止，机器人不会思考、不会回复。
消息本身照常接收，只是不回应。

支持限时屏蔽，到期自动恢复。
数据保存在 data/plugin_data/astrbot_plugin_global_blacklist/blacklist.json，
外部手动编辑后会被自动热加载。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_global_blacklist"

# 一级博士，硬兜底，始终可操作
MASTER_QQ = ("YOUR_QQ_ID",)

COMMAND = "黑名单"

_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d|秒|分|分钟|小时|时|天)$", re.IGNORECASE)


def parse_duration(text: str) -> int | None:
    """把 1h / 30m / 2d / 3天 之类的写法转成秒，失败返回 None。"""
    text = (text or "").strip()
    match = _DURATION_RE.match(text)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2).lower()
    factor = {
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
    }[unit]
    return value * factor if value > 0 else None


def format_left(expires_at: float) -> str:
    """剩余时间的可读形式。"""
    left = int(expires_at - time.time())
    if left <= 0:
        return "已到期"
    if left >= 86400:
        return f"剩 {left // 86400} 天"
    if left >= 3600:
        return f"剩 {left // 3600} 小时"
    if left >= 60:
        return f"剩 {left // 60} 分钟"
    return f"剩 {left} 秒"


class GlobalBlacklist(Star):
    """全局触发屏蔽。

    指令（无需艾特，直接发）：
        /黑名单                     查看名单
        /黑名单 123456 原因          永久屏蔽
        /黑名单 123456 2h 原因       限时屏蔽，到期自动恢复
        /黑名单 -123456             移除
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        base = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        base.mkdir(parents=True, exist_ok=True)
        self.data_file = base / "blacklist.json"
        self._users: dict[str, dict[str, Any]] = {}
        self._mtime: float = 0.0
        self._load()

    # ==================== 存储 ====================

    def _load(self) -> None:
        users: dict[str, dict[str, Any]] = {}
        try:
            if self.data_file.exists():
                self._mtime = self.data_file.stat().st_mtime
                raw = json.loads(self.data_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw.get("users", {})
                    if isinstance(data, dict):
                        for k, v in data.items():
                            key = str(k).strip()
                            if key:
                                users[key] = v if isinstance(v, dict) else {}
        except Exception as e:
            logger.error(f"[global_blacklist] 读取名单失败: {e}")
        self._users = users

    def _save(self) -> None:
        try:
            payload = {
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "users": self._users,
            }
            tmp = self.data_file.with_name(self.data_file.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.data_file)
            self._mtime = self.data_file.stat().st_mtime
        except Exception as e:
            logger.error(f"[global_blacklist] 保存名单失败: {e}")

    def _refresh(self) -> None:
        """外部改动文件后自动重载。"""
        try:
            mtime = self.data_file.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    # ==================== 判定 ====================

    def _expired(self, info: dict[str, Any]) -> bool:
        expires_at = info.get("expires_at")
        try:
            if expires_at is None:
                return False
            return float(expires_at) <= time.time()
        except (TypeError, ValueError):
            return False

    def _purge_expired(self) -> bool:
        """清掉已到期的条目，有改动返回 True。"""
        gone = [qq for qq, info in self._users.items() if self._expired(info)]
        for qq in gone:
            self._users.pop(qq, None)
            logger.info(f"[global_blacklist] {qq} 屏蔽到期，自动恢复")
        if gone:
            self._save()
        return bool(gone)

    def _blocked(self, sender_id: str) -> bool:
        if not sender_id:
            return False
        self._refresh()
        info = self._users.get(sender_id)
        if info is None:
            return False
        if self._expired(info):
            self._purge_expired()
            return False
        return True

    def _skip_private(self, event: AstrMessageEvent) -> bool:
        if event.get_group_id():
            return False
        return not bool(self.config.get("enable_in_private", True))

    def _allow_list(self) -> set[str]:
        ids = set(MASTER_QQ)
        for item in self.config.get("allow_qq_list", []) or []:
            s = str(item).strip()
            if s:
                ids.add(s)
        return ids

    def _authorized(self, event: AstrMessageEvent) -> bool:
        sid = str(event.get_sender_id() or "").strip()
        return bool(sid) and sid in self._allow_list()

    # ==================== 主入口 ====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def intercept_event(self, event: AstrMessageEvent):
        """名单内用户的消息在模型调用前整条拦下；其余消息顺带处理管理指令。"""
        if not bool(self.config.get("enable", True)):
            return

        sid = str(event.get_sender_id() or "").strip()

        # 1. 黑名单拦截，最高优先，绝不回应
        if self._blocked(sid) and not self._skip_private(event):
            logger.info(
                f"[global_blacklist] 已拦截 {sid} (umo={event.unified_msg_origin})"
            )
            tip = str(self.config.get("tip_message", "") or "").strip()
            if tip:
                event.set_result(event.plain_result(tip))
            event.stop_event()
            return

        # 2. 管理指令
        if not self._authorized(event):
            return

        raw = (event.get_message_str() or "").strip()
        if raw.startswith("/"):
            raw = raw[1:].strip()
        if not raw.startswith(COMMAND):
            return
        rest = raw[len(COMMAND) :].strip()
        if rest and not (rest.startswith("-") or rest.split()[0].isdigit()):
            return

        event.should_call_llm(False)
        yield event.plain_result(self._handle_command(sid, rest))
        event.stop_event()

    def _handle_command(self, sender_id: str, rest: str) -> str:
        self._refresh()
        self._purge_expired()

        if not rest:
            if not self._users:
                return "名单是空的。"
            lines = [f"全局屏蔽名单（{len(self._users)} 人）："]
            for qq, info in self._users.items():
                reason = str(info.get("reason", "") or "")
                who = str(info.get("by", "") or "")
                expires_at = info.get("expires_at")
                tail = f"  {reason}" if reason else ""
                if expires_at:
                    tail += f" [{format_left(float(expires_at))}]"
                tail += f"（{who}）" if who else ""
                lines.append(f"{qq}{tail}")
            return "\n".join(lines)

        if rest.startswith("-"):
            target = rest[1:].strip().split()[0] if rest[1:].strip() else ""
            if not target:
                return "要移除谁？"
            if target not in self._users:
                return f"{target} 本来就不在名单里。"
            self._users.pop(target, None)
            self._save()
            return f"已把 {target} 移出名单。"

        parts = rest.split(None, 1)
        target = parts[0]
        if not target.isdigit() or len(target) < 5:
            return "QQ 号不对。"
        if target in MASTER_QQ:
            return "这个不能加。"

        tail_text = parts[1].strip() if len(parts) > 1 else ""
        seconds = None
        reason = tail_text
        if tail_text:
            head, _, remain = tail_text.partition(" ")
            seconds = parse_duration(head)
            if seconds is not None:
                reason = remain.strip()

        info: dict[str, Any] = {
            "reason": reason,
            "by": sender_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if seconds:
            info["expires_at"] = time.time() + seconds
        self._users[target] = info
        self._save()

        if seconds:
            return f"已屏蔽 {target} {format_duration_text(seconds)}，到点自动恢复。"
        return f"已屏蔽 {target}，任何群都不会再触发我。"

    # ==================== 兜底拦截 ====================

    @filter.on_waiting_llm_request()
    async def intercept_llm(self, event: AstrMessageEvent):
        """万一前面漏过，模型请求前再拦一次。"""
        if not bool(self.config.get("enable", True)):
            return
        sid = str(event.get_sender_id() or "").strip()
        if not self._blocked(sid) or self._skip_private(event):
            return
        event.should_call_llm(False)
        event.stop_event()

    # ==================== AI 工具 ====================

    @filter.llm_tool(name="global_blacklist")
    async def llm_global_blacklist(
        self,
        event: AstrMessageEvent,
        action: str,
        user_id: str = "",
        duration: str = "",
        reason: str = "",
    ):
        """
        管理全局触发屏蔽名单。被加入名单的人，无论在任何群聊或私聊，都不会触发机器人，消息会在模型调用之前被直接拦下，得不到任何回应。支持限时屏蔽，到期自动恢复。
        Args:
            action(string): 操作类型：add 添加、remove 移除、list 查看名单
            user_id(string): 要操作的 QQ 号，add 和 remove 时必填
            duration(string): 屏蔽时长，如 30m、2h、3d、7天；留空表示永久屏蔽
            reason(string): 添加的原因，可留空
        """
        if not self._authorized(event):
            yield "没有操作全局屏蔽名单的权限。"
            return

        action = (action or "").strip().lower()
        user_id = str(user_id or "").strip()

        if action in {"list", "查看", "列表"}:
            self._refresh()
            self._purge_expired()
            if not self._users:
                yield "名单是空的。"
                return
            items = []
            for qq, info in self._users.items():
                reason_text = str(info.get("reason", "") or "")
                expires_at = info.get("expires_at")
                piece = qq
                if reason_text:
                    piece += f"({reason_text})"
                if expires_at:
                    piece += f"[{format_left(float(expires_at))}]"
                items.append(piece)
            yield f"共 {len(self._users)} 人：" + "、".join(items)
            return

        if action in {"add", "添加", "加入"}:
            if not user_id.isdigit() or len(user_id) < 5:
                yield "QQ 号不对。"
                return
            if user_id in MASTER_QQ:
                yield "这个不能加。"
                return
            seconds = parse_duration(duration) if duration else None
            if duration and seconds is None:
                yield "时长看不懂，写 30m、2h、3d 这种。"
                return
            info: dict[str, Any] = {
                "reason": str(reason or ""),
                "by": str(event.get_sender_id() or ""),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if seconds:
                info["expires_at"] = time.time() + seconds
            self._users[user_id] = info
            self._save()
            if seconds:
                yield f"已把 {user_id} 屏蔽 {format_duration_text(seconds)}，到点自动恢复。"
            else:
                yield f"已把 {user_id} 加入全局屏蔽名单。"
            return

        if action in {"remove", "移除", "删除"}:
            if not user_id:
                yield "要移除谁？"
                return
            if user_id not in self._users:
                yield f"{user_id} 不在名单里。"
                return
            self._users.pop(user_id, None)
            self._save()
            yield f"已把 {user_id} 移出名单。"
            return

        yield "动作只能是 add、remove 或 list。"


def format_duration_text(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"
