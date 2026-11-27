"""Operit2-wiki 本地知识库：定时自动同步 + 一键拉取。

- 指令：同步wiki / wiki状态
- LLM 工具：sync_operit_wiki
- 后台任务：按 interval_hours 周期 fetch，有更新就 ff-only 拉取并推送给 notify_session
"""

import asyncio
import os
import subprocess

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

DEFAULT_REPO = "/AstrBot/data/安亦诗本地记忆库/Operit知识储存库/Operit2-wiki"
DEFAULT_ADMIN = "YOUR_QQ_ID"


@register(
    "astrbot_plugin_wiki_sync",
    "Anaria",
    "Operit2-wiki 本地知识库定时同步与一键拉取插件",
    "1.0.0",
)
class WikiSyncPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.repo_path = str(self.config.get("repo_path") or DEFAULT_REPO).strip()
        self.branch = str(self.config.get("branch") or "main").strip()
        try:
            self.interval_hours = float(self.config.get("interval_hours", 6) or 0)
        except (TypeError, ValueError):
            self.interval_hours = 6.0
        self.notify_enabled = bool(self.config.get("notify_enabled", True))
        self.notify_session = str(self.config.get("notify_session") or "").strip()

        admin_raw = self.config.get("admin_qqs") or []
        if isinstance(admin_raw, str):
            admin_raw = [x for x in admin_raw.replace("，", ",").split(",") if x.strip()]
        self.admin_qqs = [str(x).strip() for x in admin_raw if str(x).strip()] or [DEFAULT_ADMIN]

        self._lock = asyncio.Lock()
        self._running = True
        self._task = None
        if self.interval_hours > 0:
            self._task = asyncio.create_task(self._loop())

    # ---------------- git ----------------

    def _git(self, *args, timeout: int = 180) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", self.repo_path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _sync_blocking(self, do_pull: bool) -> dict:
        """阻塞执行 fetch/对比/可选 pull，返回结果字典。在线程里调用。"""
        res = {"ok": False, "updated": False, "error": "", "detail": "", "pulled": False}
        if not os.path.isdir(os.path.join(self.repo_path, ".git")):
            res["error"] = f"不是 git 仓库：{self.repo_path}"
            return res

        remote_ref = f"origin/{self.branch}"
        try:
            fetched = self._git("fetch", "--prune", "origin", timeout=180)
            if fetched.returncode != 0:
                res["error"] = (fetched.stderr or fetched.stdout).strip()[-300:]
                return res

            local = self._git("rev-parse", "HEAD").stdout.strip()
            remote = self._git("rev-parse", remote_ref).stdout.strip()
            if not remote:
                res["error"] = f"找不到远端分支 {remote_ref}"
                return res
            if local == remote:
                res.update({"ok": True, "detail": f"已是最新（{local[:7]}）"})
                return res

            behind = self._git("rev-list", "--count", f"HEAD..{remote_ref}").stdout.strip() or "?"
            name_status = self._git("diff", "--name-status", f"HEAD..{remote_ref}").stdout
            entries = []
            for line in name_status.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                entries.append((parts[0][:1], parts[-1]))

            added = sum(1 for s, _ in entries if s == "A")
            deleted = sum(1 for s, _ in entries if s == "D")
            modified = len(entries) - added - deleted

            review_pages = {p for _, p in entries if p.startswith("review/") and p.endswith(".md")}
            wiki_pages = {p for _, p in entries if p.startswith("wiki/") and p.endswith(".md")}

            tops: dict[str, int] = {}
            for _, p in entries:
                top = p.split("/")[0] if "/" in p else "(根目录)"
                tops[top] = tops.get(top, 0) + 1
            top_txt = "、".join(
                f"{k} {v}" for k, v in sorted(tops.items(), key=lambda kv: -kv[1])[:5]
            )

            res.update({
                "ok": True,
                "updated": True,
                "local": local,
                "remote": remote,
                "commits": behind,
                "files": len(entries),
                "added": added,
                "deleted": deleted,
                "modified": modified,
                "review_pages": len(review_pages),
                "wiki_pages": len(wiki_pages),
                "top": top_txt,
            })

            if do_pull:
                pulled = self._git("pull", "--ff-only", "origin", self.branch, timeout=240)
                res["pulled"] = pulled.returncode == 0
                if not pulled.returncode == 0:
                    res["error"] = (pulled.stderr or pulled.stdout).strip()[-300:]
            return res

        except subprocess.TimeoutExpired:
            res["error"] = "git 操作超时"
            return res
        except Exception as e:  # noqa: BLE001
            res["error"] = str(e)
            return res

    # ---------------- 文案 ----------------

    @staticmethod
    def _format(res: dict) -> str:
        if not res.get("ok"):
            return f"同步失败：{res.get('error') or '未知错误'}"

        if not res.get("updated"):
            return f"Operit2-wiki {res.get('detail')}。"

        head = "【Operit2-wiki 已更新】" if res.get("pulled") else "【Operit2-wiki 有更新】"
        lines = [
            head,
            f"{res.get('local', '')[:7]} → {res.get('remote', '')[:7]}（{res.get('commits')} 个提交）",
            f"文件 {res.get('files', 0)} 项：新增 {res.get('added', 0)} / 修改 {res.get('modified', 0)} / 删除 {res.get('deleted', 0)}",
            f"条目：review {res.get('review_pages', 0)} 篇，wiki 正文 {res.get('wiki_pages', 0)} 篇",
        ]
        if res.get("top"):
            lines.append(f"目录：{res['top']}")
        if not res.get("pulled"):
            lines.append("未拉取，需要的话说一声。")
        elif res.get("error"):
            lines.append(f"拉取出错：{res['error']}")
        return "\n".join(lines)

    # ---------------- 权限 / 推送 ----------------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if str(event.get_sender_id()) in self.admin_qqs:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    async def _notify(self, text: str) -> None:
        if not self.notify_session:
            return
        try:
            await self.context.send_message(self.notify_session, [Plain(text)])
        except Exception as e:  # noqa: BLE001
            logger.error(f"[WikiSync] 推送失败：{e}")

    # ---------------- 后台循环 ----------------

    async def _loop(self) -> None:
        await asyncio.sleep(90)
        while self._running:
            try:
                async with self._lock:
                    res = await asyncio.to_thread(self._sync_blocking, True)
                if res.get("ok") and res.get("updated") and res.get("pulled"):
                    logger.info(f"[WikiSync] 自动更新 {res.get('commits')} 个提交")
                    if self.notify_enabled:
                        await self._notify("（自动）" + self._format(res))
                elif not res.get("ok"):
                    logger.warning(f"[WikiSync] 自动同步失败：{res.get('error')}")
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[WikiSync] 循环异常：{e}")
            await asyncio.sleep(max(self.interval_hours, 0.2) * 3600)

    # ---------------- 指令 ----------------

    @filter.command("同步wiki", alias={"拉取wiki", "更新wiki", "wiki同步"})
    async def sync_cmd(self, event: AstrMessageEvent):
        """把本地 Operit2-wiki 知识库拉取到最新"""
        if not self._is_admin(event):
            yield event.plain_result("没权限。")
            return
        async with self._lock:
            res = await asyncio.to_thread(self._sync_blocking, True)
        yield event.plain_result(self._format(res))

    @filter.command("wiki状态", alias={"wiki检查"})
    async def status_cmd(self, event: AstrMessageEvent):
        """只检查 Operit2-wiki 有没有更新，不拉取"""
        if not self._is_admin(event):
            yield event.plain_result("没权限。")
            return
        async with self._lock:
            res = await asyncio.to_thread(self._sync_blocking, False)
        yield event.plain_result(self._format(res))

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="sync_operit_wiki")
    async def sync_operit_wiki(self, event: AstrMessageEvent, check_only: bool = False) -> str:
        """同步本地 Operit2-wiki 知识库到最新版本。

        Args:
            check_only (bool): 为 true 时只检查有没有更新，不执行拉取
        """
        if not self._is_admin(event):
            return "没权限操作知识库同步。"
        async with self._lock:
            res = await asyncio.to_thread(self._sync_blocking, not check_only)
        return self._format(res)

    async def terminate(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
