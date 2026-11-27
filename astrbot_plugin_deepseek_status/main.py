import asyncio
import json
import re
import aiohttp
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain


@register(
    "astrbot_plugin_deepseek_status",
    "Anaria",
    "DeepSeek 官方服务状态监控与群推送插件",
    "1.0.0",
)
class DeepSeekStatusPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.target_groups = self.config.get("target_groups", ["1051920958"])
        self.check_interval = int(self.config.get("check_interval", 60))
        self.last_change_id = None
        self._task = None
        self._running = True

        # 启动后台监控循环
        self._task = asyncio.create_task(self._monitor_loop())

    async def _fetch_latest_change(self):
        url = "https://status.deepseek.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    html = await resp.text()

            raw_chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?[^\\])"\]\)', html)
            for chunk in raw_chunks:
                if "changes" in chunk:
                    try:
                        decoded = json.loads(f'"{chunk}"')
                        idx = decoded.find('"changes":')
                        if idx != -1:
                            changes, _ = json.JSONDecoder().raw_decode(decoded[idx + 10 :])
                            if changes and isinstance(changes, list):
                                return changes[0]
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"[DeepSeekStatus] 获取状态失败: {e}")
        return None

    async def _monitor_loop(self):
        # 启动时先获取一次基准
        initial = await self._fetch_latest_change()
        if initial:
            self.last_change_id = initial.get("change_id")

        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                latest = await self._fetch_latest_change()
                if not latest:
                    continue

                cid = latest.get("change_id")
                status = latest.get("status")
                title = latest.get("title", "未知事件")

                # 如果有新事件，或者状态更新了
                if cid and self.last_change_id and cid != self.last_change_id:
                    self.last_change_id = cid
                    msg = f"【DeepSeek 服务状态提醒】\n事件：{title}\n当前状态：{status}\n详情查看：https://status.deepseek.com/"
                    await self._broadcast(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DeepSeekStatus] 监控循环异常: {e}")

    async def _broadcast(self, text: str):
        # 广播给配置的群
        for gid in self.target_groups:
            try:
                session_id = f"aiocqhttp:GroupMessage:{gid}"
                # 使用 platform_manager 或 context 发送
                # 兼容 astrbot 内部广播接口
                if hasattr(self.context, "send_message"):
                    await self.context.send_message(session_id, [Plain(text)])
                elif hasattr(self.context, "get_platform"):
                    p = self.context.get_platform("aiocqhttp")
                    if p:
                        await p.send_msg(session_id, [Plain(text)])
            except Exception as e:
                logger.error(f"[DeepSeekStatus] 推送群 {gid} 失败: {e}")

    @filter.command("ds状态")
    async def check_now(self, event: AstrMessageEvent):
        """手动查询 DeepSeek 官方服务最新状态"""
        change = await self._fetch_latest_change()
        if not change:
            yield event.plain_result("暂时无法获取 DeepSeek 官方状态。")
            return

        title = change.get("title", "无标题")
        status = change.get("status", "未知")
        desc = change.get("description", "").strip()

        if str(status).lower() == "resolved":
            yield event.plain_result("【DeepSeek 最新状态】\n服务正常（All Systems Operational）\n当前无未解决故障，全部服务运转正常。\n官网：https://status.deepseek.com/")
            return

        yield event.plain_result(f"【DeepSeek 最新状态】\n事件：{title}\n状态：{status}\n说明：{desc}\n官网：https://status.deepseek.com/")

    async def terminate(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[DeepSeekStatus] 插件已停止。")
