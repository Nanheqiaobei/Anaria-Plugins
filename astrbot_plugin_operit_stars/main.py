import aiohttp
from datetime import datetime, timezone, timedelta
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_operit_stars",
    "Anaria",
    "GitHub 仓库（默认 Operit / Operit2）实时状态与 Star 数查询插件",
    "1.1.0",
)
class OperitStarsPlugin(Star):
    # repo 参数的别名表，值 None 表示"全部仓库"
    REPO_ALIASES = {
        "": None,
        "all": None,
        "全部": None,
        "两个": None,
        "operit": "AAswordman/Operit",
        "operit1": "AAswordman/Operit",
        "一代": "AAswordman/Operit",
        "1": "AAswordman/Operit",
        "operit2": "AAswordman/Operit2",
        "op2": "AAswordman/Operit2",
        "二代": "AAswordman/Operit2",
        "2": "AAswordman/Operit2",
    }

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.default_repo = self.config.get("default_repo", "AAswordman/Operit")
        self.secondary_repo = self.config.get("secondary_repo", "AAswordman/Operit2")
        self.github_token = self.config.get("github_token", "").strip()

    def _resolve_repos(self, repo: str) -> list[str]:
        """把用户/AI 传来的 repo 解析成实际要查的仓库列表，空值代表全部。"""
        key = (repo or "").strip().lower()
        if key in self.REPO_ALIASES:
            mapped = self.REPO_ALIASES[key]
            if mapped is None:
                return [self.default_repo, self.secondary_repo]
            return [mapped]
        return [repo.strip()]

    async def _fetch_repo_info(self, repo: str):
        url = f"https://api.github.com/repos/{repo}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-OperitStars",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        return await resp.json(), None
                    elif resp.status == 404:
                        return None, f"未找到仓库 {repo}，请检查仓库名是否正确。"
                    elif resp.status == 403:
                        return None, "GitHub API 触发速率限制，请稍后再试或配置 Token。"
                    else:
                        return None, f"请求失败，HTTP 状态码: {resp.status}"
        except Exception as e:
            logger.error(f"[OperitStars] 请求 GitHub 失败: {e}")
            return None, f"网络请求异常: {e}"

    def _format_time(self, iso_str: str) -> str:
        try:
            utc_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            cst_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
            return cst_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return iso_str

    async def _collect(self, repos: list[str]):
        """逐个拉取，返回 (成功列表, 错误列表)。"""
        ok, errs = [], []
        for repo in repos:
            data, err = await self._fetch_repo_info(repo)
            if err or not data:
                errs.append(f"{repo}: {err or '未知错误'}")
            else:
                ok.append(data)
        return ok, errs

    def _format_full(self, data: dict) -> str:
        return (
            f"项目：{data.get('full_name')}\n"
            f"⭐ Stars：{data.get('stargazers_count', 0):,}\n"
            f"🍴 Forks：{data.get('forks_count', 0):,}\n"
            f"📌 Issues：{data.get('open_issues_count', 0)} (未关闭)\n"
            f"🕒 最近推送：{self._format_time(data.get('pushed_at', ''))}\n"
            f"🔗 链接：{data.get('html_url', '')}"
        )

    @filter.command("operit星数", alias={"operit状态", "查星数", "项目星数", "star"})
    async def get_stars(self, event: AstrMessageEvent, repo: str = ""):
        """查询 Operit 或指定 GitHub 仓库的实时星数及状态（默认两个仓库一起查）"""
        repos = self._resolve_repos(repo)
        ok, errs = await self._collect(repos)

        if not ok:
            yield event.plain_result("查询失败: " + "；".join(errs))
            return

        blocks = ["【GitHub 实时状态】"]
        for data in ok:
            blocks.append(self._format_full(data))
        if errs:
            blocks.append("部分失败：" + "；".join(errs))
        yield event.plain_result("\n\n".join(blocks))

    @filter.llm_tool(name="query_github_repo_stars")
    async def query_github_repo_stars(self, event: AstrMessageEvent, repo: str = "") -> str:
        """查询 Operit 系列仓库的实时 Star、Fork、Issue 状态。

        repo 可填：留空或 all（Operit 与 Operit2 一起查）、operit、operit2，
        也可以直接填 owner/repo 查任意仓库。
        """
        repos = self._resolve_repos(repo)
        ok, errs = await self._collect(repos)

        lines = []
        for data in ok:
            lines.append(
                f"仓库: {data.get('full_name')}\n"
                f"Stars: {data.get('stargazers_count')}\n"
                f"Forks: {data.get('forks_count')}\n"
                f"Open Issues: {data.get('open_issues_count')}\n"
                f"最后推送时间: {data.get('pushed_at')}"
            )
        if errs:
            lines.append("查询失败: " + "；".join(errs))
        return "\n\n".join(lines) if lines else "查询失败: 未知错误"
