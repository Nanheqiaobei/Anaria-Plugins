"""安亦诗生图插件（gpt-image 系列）。

指令：
    /绘 提示词            文生图
    /改绘 提示词          带图或引用图 → 图生图 / 多图融合
    /动图 提示词          GIF 动图（带图就是改图动起来）
    /绘模型 [在线]        看可用模型
    /绘风格               看风格预设
    /绘历史 [n]           看最近生成
    /绘重发 序号          重发历史图
    /绘删 序号            删记录和文件
    /绘帮助

LLM 工具：
    yishi_draw            我自己判断该不该画、画什么
    yishi_draw_gif        动图版
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from . import gifutil
from .client import (
    DrawError,
    ImageClient,
    b64_to_bytes,
    save_bytes,
)

PLUGIN_NAME = "astrbot_plugin_yishi_draw"

# ---------------------------------------------------------------- 常量

STYLE_PROMPTS: dict[str, str] = {
    "realistic": "写实摄影风格，自然真实的光影、材质与景深，细节清晰",
    "anime": "日系动漫插画风格，干净线条，柔和赛璐璐上色，背景精致",
    "watercolor": "水彩画风格，轻柔晕染，纸纹质感，留白通透",
    "oil": "古典油画风格，厚涂笔触，层次丰富，光影厚重",
    "pixel": "像素艺术风格，有限色板，像素块清晰，复古游戏感",
    "sketch": "铅笔素描风格，手绘线条，明暗排线，纸张颗粒",
    "3d": "3D 渲染风格，柔和全局光照，圆润材质，细腻质感",
    "cyberpunk": "赛博朋克风格，霓虹灯光，湿润街道反光，冷调高对比",
    "ink": "中国水墨画风格，墨色浓淡变化，宣纸质感，留白意境",
    "flat": "扁平插画风格，简洁色块，干净构图，无渐变阴影",
    "cinematic": "电影感构图，宽银幕比例，戏剧性打光，色调分层",
    "cute": "可爱风格，圆润造型，明亮配色，讨喜表情",
}

STYLE_ALIASES: dict[str, str] = {
    "写实": "realistic", "真实": "realistic", "摄影": "realistic", "photo": "realistic",
    "anime": "anime", "动漫": "anime", "二次元": "anime", "日系": "anime", "漫画": "anime",
    "watercolor": "watercolor", "水彩": "watercolor",
    "oil": "oil", "油画": "oil", "厚涂": "oil",
    "pixel": "pixel", "像素": "pixel",
    "sketch": "sketch", "素描": "sketch", "线稿": "sketch", "铅笔": "sketch",
    "3d": "3d", "三维": "3d", "渲染": "3d",
    "cyberpunk": "cyberpunk", "赛博": "cyberpunk", "赛博朋克": "cyberpunk",
    "ink": "ink", "水墨": "ink", "国风": "ink", "水墨画": "ink",
    "flat": "flat", "扁平": "flat", "插画": "flat",
    "cinematic": "cinematic", "电影": "cinematic", "电影感": "cinematic",
    "cute": "cute", "可爱": "cute", "萌": "cute",
}

SIZE_ALIASES: dict[str, str] = {
    "方": "1024x1024", "方形": "1024x1024", "正方形": "1024x1024", "square": "1024x1024", "1:1": "1024x1024",
    "横": "1536x1024", "横版": "1536x1024", "横幅": "1536x1024", "landscape": "1536x1024", "16:9": "1536x1024", "3:2": "1536x1024",
    "竖": "1024x1536", "竖版": "1024x1536", "纵向": "1024x1536", "portrait": "1024x1536", "9:16": "1024x1536", "2:3": "1024x1536",
    "auto": "auto", "自动": "auto", "默认": "auto",
}

QUALITY_ALIASES: dict[str, str] = {
    "low": "low", "低": "low", "草稿": "low", "快": "low", "省": "low",
    "medium": "medium", "中": "medium", "标准": "medium", "普通": "medium",
    "high": "high", "高": "high", "精细": "high", "高清": "high",
    "auto": "auto", "自动": "auto",
}

_OPT_ALIAS: dict[str, str] = {
    "m": "model", "模型": "model", "model": "model",
    "s": "size", "size": "size", "尺寸": "size", "画幅": "size", "比例": "size", "ratio": "size",
    "q": "quality", "quality": "quality", "质量": "quality",
    "n": "num", "num": "num", "number": "num", "张数": "num", "数量": "num",
    "st": "style", "style": "style", "风格": "style",
    "neg": "negative", "negative": "negative", "负向": "negative", "负面": "negative", "排除": "negative",
    "out": "out", "输出": "out",
    "f": "file", "file": "file", "文件": "file", "参考": "file",
    "pre": "prefix", "prefix": "prefix", "前缀": "prefix",
    "fr": "frames", "frames": "frames", "帧": "frames", "帧数": "frames",
    "ms": "frame_ms", "frame_ms": "frame_ms", "间隔": "frame_ms", "帧间隔": "frame_ms", "时长": "frame_ms",
}

_VALUE_OPTS = {"model", "size", "quality", "num", "style", "negative", "out", "file", "prefix", "frames", "frame_ms"}
_BOOL_OPTS = {"raw", "url", "loop", "once"}

_BOOL_ALIAS: dict[str, str] = {
    "raw": "raw", "原型": "raw", "原样": "raw",
    "u": "url", "url": "url", "链接": "url",
    "loop": "loop", "循环": "loop",
    "once": "once", "一次": "once", "不循环": "once",
}


# ---------------------------------------------------------------- 工具函数


def _tokenize(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return (text or "").split()


def parse_args(text: str) -> tuple[str, dict[str, str]]:
    """从一段文本里剥出 -参数 / --参数，剩下的拼成提示词。"""
    tokens = _tokenize(text or "")
    prompt_parts: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and len(tok) > 1:
            body = tok.lstrip("-")
            key, _, inline = body.partition("=")
            key_l = key.strip()
            canon = _OPT_ALIAS.get(key_l.lower()) or _OPT_ALIAS.get(key_l)
            bool_canon = _BOOL_ALIAS.get(key_l.lower()) or _BOOL_ALIAS.get(key_l)
            if canon and canon in _VALUE_OPTS:
                if inline:
                    opts[canon] = inline.strip()
                    i += 1
                    continue
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    opts[canon] = tokens[i + 1].strip()
                    i += 2
                    continue
                opts[canon] = ""
                i += 1
                continue
            if bool_canon and bool_canon in _BOOL_OPTS:
                opts[bool_canon] = "1"
                i += 1
                continue
        prompt_parts.append(tok)
        i += 1
    return " ".join(prompt_parts).strip(), opts


def normalize_size(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    low = value.lower().replace(" ", "")
    if low in SIZE_ALIASES:
        return SIZE_ALIASES[low]
    if "x" in low and low.replace("x", "").isdigit():
        return low
    return value


def normalize_quality(raw: str) -> str:
    value = (raw or "").strip().lower()
    return QUALITY_ALIASES.get(value, value)


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}min"


def collect_images(event: AstrMessageEvent, limit: int = 4) -> list[Image]:
    """取当前消息里的图，没有就取引用消息里的图。"""
    if not hasattr(event, "get_messages"):
        return []
    direct: list[Image] = []
    quoted: list[Image] = []
    for comp in event.get_messages() or []:
        if isinstance(comp, Image):
            direct.append(comp)
        elif isinstance(comp, Reply):
            for sub in getattr(comp, "chain", None) or []:
                if isinstance(sub, Image):
                    quoted.append(sub)
    quoted_obj = getattr(event.message_obj, "quoted_message", None)
    if quoted_obj is not None:
        for sub in getattr(quoted_obj, "message", None) or []:
            if isinstance(sub, Image):
                quoted.append(sub)

    picked = direct or quoted
    seen: set[str] = set()
    result: list[Image] = []
    for img in picked:
        key = str(img.url or img.file or img.path or id(img))
        if key in seen:
            continue
        seen.add(key)
        result.append(img)
        if len(result) >= limit:
            break
    return result


async def image_to_bytes(comp: Image) -> bytes:
    try:
        import base64

        b64 = await comp.convert_to_base64()
        data = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001 - 组件内部异常类型不稳定
        raise DrawError(f"参考图读取失败：{exc}") from exc
    if not data:
        raise DrawError("参考图是空的")
    return data


# ---------------------------------------------------------------- 插件


@register(
    PLUGIN_NAME,
    "安亦诗",
    "基于 gpt-image 系列的生图插件：文生图、参考图改图、风格预设、画幅质量控制、本地归档、历史重发、LLM 自主调用",
    "1.1.0",
)
class YishiDrawPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = dict(config) if config else {}
        self._client: ImageClient | None = None
        self._cooldown_until: dict[str, float] = {}
        self._busy: set[str] = set()
        self._history: list[dict[str, Any]] = []
        self._data_dir = self._resolve_data_dir()
        self._image_dir = self._data_dir / "images"
        self._history_path = self._data_dir / "history.json"
        self._load_history()

    # ---------------- 基础设施 ----------------

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
            if not base.is_absolute():
                base = Path(__file__).resolve().parents[3] / base
        except Exception:  # noqa: BLE001 - 老版本兜底
            base = Path(__file__).resolve().parents[3] / "data" / "plugin_data"
        return base / PLUGIN_NAME

    def _c(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _c_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开", "开启", "是"}
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _c_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _api_base(self) -> str:
        return str(self._c("api_base", "")).strip().rstrip("/")

    def _api_key(self) -> str:
        return str(self._c("api_key", "")).strip()

    def _client_or_new(self) -> ImageClient:
        base = self._api_base()
        key = self._api_key()
        if not base:
            raise DrawError("插件没配置 api_base")
        if not key:
            raise DrawError("插件没配置 api_key")
        stale = (
            self._client is None
            or self._client.base_url != base
            or self._client.api_key != key
        )
        if stale:
            if self._client is not None:
                old = self._client
                try:
                    asyncio.get_running_loop().create_task(old.close())
                except RuntimeError:
                    pass
            self._client = ImageClient(
                base,
                key,
                timeout=self._c_int("request_timeout", 300),
                download_timeout=self._c_int("download_timeout", 120),
                proxy=str(self._c("proxy", "") or ""),
            )
        return self._client

    async def initialize(self) -> None:
        logger.info(
            f"[YishiDraw] 已加载：模型 {self._c('default_model', 'gpt-image-2.6')}，"
            f"{'LLM 工具开启' if self._c_bool('enable_llm_tool', True) else 'LLM 工具关闭'}"
        )

    async def terminate(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------- 权限与限额 ----------------

    def _master_qq(self) -> str:
        return str(self._c("master_qq", "YOUR_QQ_ID")).strip()

    def _allow_users(self) -> set[str]:
        raw = self._c("allow_users", [])
        if isinstance(raw, str):
            items = raw.replace("，", ",").split(",")
        elif isinstance(raw, (list, tuple)):
            items = [str(x) for x in raw]
        else:
            items = []
        return {str(x).strip() for x in items if str(x).strip()}

    def _is_master(self, uid: str) -> bool:
        return str(uid) == self._master_qq()

    def _access_error(self, event: AstrMessageEvent) -> str | None:
        uid = str(event.get_sender_id())
        if self._is_master(uid) or uid in self._allow_users():
            return None
        mode = str(self._c("access_mode", "admin")).strip().lower()
        if mode == "everyone":
            return None
        if mode == "master":
            return "这个只给博士用。"
        if event.is_admin():
            return None
        return "生图是管理指令，找管理员或者博士开。"

    def _cooldown_left(self, event: AstrMessageEvent) -> int:
        uid = str(event.get_sender_id())
        if self._is_master(uid) or event.is_admin() or uid in self._allow_users():
            return 0
        seconds = self._c_int("cooldown_seconds", 30)
        if seconds <= 0:
            return 0
        now = time.time()
        until = self._cooldown_until.get(uid, 0)
        if until > now:
            return int(until - now) + 1
        self._cooldown_until[uid] = now + seconds
        return 0

    def _max_n(self) -> int:
        return max(1, min(10, self._c_int("max_n", 4)))

    def _resolve_n(self, raw: str) -> int:
        if not raw:
            return max(1, min(self._max_n(), self._c_int("default_n", 1)))
        try:
            n = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            n = 1
        return max(1, min(self._max_n(), n))

    def _resolve_style(self, raw: str) -> tuple[str, str]:
        if not self._c_bool("enable_style", True):
            return "", ""
        value = (raw or "").strip()
        if not value:
            return "", ""
        low = value.lower()
        canon = STYLE_ALIASES.get(value) or STYLE_ALIASES.get(low) or (low if low in STYLE_PROMPTS else "")
        if not canon:
            return "", ""
        return canon, STYLE_PROMPTS.get(canon, "")

    # ---------------- 历史 ----------------

    def _load_history(self) -> None:
        try:
            if self._history_path.exists():
                raw = json.loads(self._history_path.read_text(encoding="utf-8"))
                records = raw.get("records") if isinstance(raw, dict) else raw
                if isinstance(records, list):
                    self._history = [r for r in records if isinstance(r, dict)]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YishiDraw] 历史记录读取失败：{exc}")
            self._history = []

    def _dump_history(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            limit = max(20, self._c_int("history_size", 200))
            self._history = self._history[-limit:]
            self._history_path.write_text(
                json.dumps({"records": self._history}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YishiDraw] 历史记录写入失败：{exc}")

    def _add_record(self, record: dict[str, Any]) -> dict[str, Any]:
        last_id = max((int(r.get("id", 0)) for r in self._history), default=0)
        record["id"] = last_id + 1
        self._history.append(record)
        self._dump_history()
        return record

    def _session_records(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        umo = getattr(event, "unified_msg_origin", "") or event.get_session_id()
        return [r for r in self._history if r.get("umo") == umo]

    def _cleanup_files(self) -> None:
        keep_days = self._c_int("keep_days", 7)
        if keep_days <= 0 or not self._image_dir.exists():
            return
        deadline = time.time() - keep_days * 86400
        for path in self._image_dir.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                continue

    # ---------------- 核心：生成 ----------------

    async def _generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        model: str = "",
        size: str = "",
        quality: str = "",
        n: int = 1,
        style: str = "",
        negative: str = "",
        use_ref: bool = False,
        send: bool = True,
        prefix: str = "img",
        reply_prefix: str = "",
        output_format: str = "",
        gif_frames: int | None = None,
        gif_frame_ms: int | None = None,
        gif_loop: bool = True,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise DrawError("要画什么，给个提示词")

        style_key, style_prompt = self._resolve_style(style)
        final_prompt = f"{style_prompt}，{prompt}" if style_prompt else prompt
        if negative.strip():
            final_prompt = f"{final_prompt}。画面中避免出现：{negative.strip()}"

        refs: list[bytes] = []
        if use_ref:
            comps = collect_images(event, limit=max(1, self._c_int("max_ref_images", 4)))
            if not comps:
                raise DrawError("要改图的话，带一张图或者引用一张图")
            for comp in comps:
                refs.append(await image_to_bytes(comp))

        use_model = (model or str(self._c("default_model", "gpt-image-2.6"))).strip()
        use_size = normalize_size(size or str(self._c("default_size", "1024x1024")))

        use_format = (output_format or "").strip().lower()
        want_gif = use_format == "gif"
        if want_gif and not (quality or "").strip():
            # 动图本来就慢，默认压到 low，想细一点再 -q high
            quality = str(self._c("gif_quality", "low"))
        use_quality = normalize_quality(quality or str(self._c("default_quality", "medium")))
        req_frames = 0
        req_ms = 0
        if want_gif:
            if not self._c_bool("enable_gif", True):
                raise DrawError("GIF 在插件配置里关掉了")
            n = 1  # GIF 一次只要一张，多了又慢又贵
            max_frames = max(2, min(16, self._c_int("gif_max_frames", 16)))
            raw_frames = self._c_int("gif_frames", 8) if gif_frames is None else int(gif_frames or 0)
            raw_ms = self._c_int("gif_frame_ms", 120) if gif_frame_ms is None else int(gif_frame_ms or 0)
            req_frames = max(2, min(max_frames, raw_frames)) if raw_frames else 0
            req_ms = max(20, min(2000, raw_ms)) if raw_ms else 0
            final_prompt = (
                f"{final_prompt}。短循环动图：主体只做简单重复的动作，"
                "机位、构图和背景保持稳定，首尾能自然衔接"
            )

        client = self._client_or_new()
        started = time.time()
        result = await client.generate(
            final_prompt,
            model=use_model,
            size=use_size,
            quality=use_quality,
            n=n,
            refs=refs,
            output_format="gif" if want_gif else "",
            frame_count=req_frames if want_gif else 0,
            frame_duration_ms=req_ms if want_gif else 0,
        )
        elapsed = time.time() - started

        files: list[Path] = []
        urls: list[str] = []
        gif_meta: dict[str, Any] | None = None
        for item in result["items"]:
            if item.get("b64"):
                data = b64_to_bytes(item["b64"])
            else:
                urls.append(item.get("url", ""))
                data = await client.fetch(item["url"])
            if want_gif or gifutil.is_gif(data):
                data, gif_meta = await asyncio.to_thread(
                    self._fit_gif, data, req_frames, req_ms, gif_loop
                )
            path = save_bytes(self._image_dir / time.strftime("%Y-%m"), data, prefix)
            files.append(path)

        record = self._add_record(
            {
                "ts": time.time(),
                "umo": getattr(event, "unified_msg_origin", "") or event.get_session_id(),
                "user": str(event.get_sender_id()),
                "prompt": prompt,
                "final_prompt": final_prompt,
                "style": style_key,
                "model": use_model,
                "size": use_size,
                "quality": use_quality,
                "n": len(result["items"]),
                "ref_count": len(refs),
                "format": "gif" if gif_meta else "image",
                "gif_frames": (gif_meta or {}).get("frames", 0),
                "gif_frame_ms": (gif_meta or {}).get("frame_ms", 0),
                "files": [str(p) for p in files],
                "urls": urls,
                "elapsed": round(elapsed, 2),
            }
        )

        sent = False
        if send and files:
            if gif_meta:
                caption = (
                    f"{reply_prefix}GIF · {gif_meta['frames']}帧 · "
                    f"{gif_meta['frame_ms']}ms/帧 · "
                    f"{gif_meta['width']}x{gif_meta['height']} · "
                    f"{use_model} · {human_duration(elapsed)}"
                )
            else:
                caption = (
                    f"{reply_prefix}"
                    f"{len(files)}张 · {use_model} · {use_size} · {use_quality} · {human_duration(elapsed)}"
                )
            chain: list[Any] = [Plain(caption)]
            chain.extend(Image.fromFileSystem(str(p)) for p in files)
            await event.send(MessageChain(chain))
            sent = True

        self._cleanup_files()
        return {
            "record": record,
            "files": files,
            "urls": urls,
            "elapsed": elapsed,
            "sent": sent,
            "model": use_model,
            "size": use_size,
            "quality": use_quality,
            "gif": gif_meta,
        }

    def _fit_gif(
        self, data: bytes, frames: int, frame_ms: int, loop: bool
    ) -> tuple[bytes, dict[str, Any]]:
        """上游不一定吃 frame_count / frame_duration_ms，这里本地校正一遍。"""
        try:
            info = gifutil.gif_info(data)
        except gifutil.GifError as exc:
            raise DrawError(str(exc)) from exc
        need_frames = bool(frames) and info["frames"] != frames
        need_ms = bool(frame_ms) and info["frame_ms"] != frame_ms
        need_loop = loop and info.get("loop") is None
        if not (need_frames or need_ms or need_loop):
            return data, info
        try:
            out, rebuilt = gifutil.rebuild_gif(
                data,
                frames=frames if need_frames else 0,
                frame_ms=frame_ms if need_ms else 0,
                loop=0 if loop else 1,
            )
        except gifutil.GifError as exc:
            raise DrawError(str(exc)) from exc
        info.update(rebuilt)
        return out, info

    async def _run_guarded(self, event: AstrMessageEvent, prompt: str, **kwargs: Any) -> tuple[str | None, dict[str, Any] | None]:
        """统一的权限、冷却、并发与错误处理。"""
        denied = self._access_error(event)
        if denied:
            return denied, None
        left = self._cooldown_left(event)
        if left:
            return f"慢点，{left} 秒后再来。", None
        key = getattr(event, "unified_msg_origin", "") or event.get_session_id()
        if key in self._busy:
            return "还在画上一张，等这个出完。", None
        self._busy.add(key)
        try:
            result = await self._generate(event, prompt, **kwargs)
            return None, result
        except DrawError as exc:
            return f"没画成：{exc}", None
        except Exception as exc:  # noqa: BLE001
            logger.exception("[YishiDraw] 生成失败")
            return f"没画成：{exc}", None
        finally:
            self._busy.discard(key)

    # ---------------- 指令 ----------------

    @filter.command("绘", alias={"绘图", "ydraw", "亦绘"}, desc="按提示词生成图片")
    async def cmd_draw(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """按提示词生成图片。"""
        prompt, opts = parse_args(str(text or ""))
        if not prompt:
            yield event.plain_result(self._usage_hint())
            return
        err, result = await self._run_guarded(
            event,
            prompt,
            model=opts.get("model", ""),
            size=opts.get("size", ""),
            quality=opts.get("quality", ""),
            n=self._resolve_n(opts.get("num", "")),
            style=opts.get("style", ""),
            negative=opts.get("negative", ""),
            use_ref="file" in opts,
            send=opts.get("out", "send") != "save",
            prefix=opts.get("prefix", "img"),
        )
        if err:
            yield event.plain_result(err)
            return
        if result and opts.get("url"):
            yield event.plain_result("\n".join(result["urls"]) or "上游没给链接")
        elif result and opts.get("out", "") == "save" and result["files"]:
            yield event.plain_result("存好了：\n" + "\n".join(str(p) for p in result["files"]))

    @filter.command("改绘", alias={"yedit", "改画"}, desc="带图或引用图，按提示词改图")
    async def cmd_edit(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """带图或引用图，按提示词改图 / 多图融合。"""
        prompt, opts = parse_args(str(text or ""))
        if not prompt:
            yield event.plain_result("要说清楚想改成什么样，比如：/改绘 换成水彩风格")
            return
        err, _ = await self._run_guarded(
            event,
            prompt,
            model=opts.get("model", ""),
            size=opts.get("size", ""),
            quality=opts.get("quality", ""),
            n=self._resolve_n(opts.get("num", "")),
            style=opts.get("style", ""),
            negative=opts.get("negative", ""),
            use_ref=True,
            send=opts.get("out", "send") != "save",
            prefix=opts.get("prefix", "edit"),
        )
        if err:
            yield event.plain_result(err)

    @filter.command("动图", alias={"gif", "动画", "ygif"}, desc="生成 GIF 动图")
    async def cmd_gif(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """生成 GIF：不带图是文生动图，带图或引用图就是把那张图动起来。"""
        prompt, opts = parse_args(str(text or ""))
        if not prompt:
            yield event.plain_result(
                "用法：/动图 一个圆脑袋小人挥手  -fr 8 -ms 120\n"
                "带图或引用图就是让原图动起来；-once 只播一次，默认无限循环"
            )
            return
        err, _ = await self._run_guarded(
            event,
            prompt,
            model=opts.get("model", ""),
            size=opts.get("size", ""),
            quality=opts.get("quality", ""),
            n=1,
            style=opts.get("style", ""),
            negative=opts.get("negative", ""),
            use_ref="file" in opts or bool(collect_images(event)),
            send=opts.get("out", "send") != "save",
            prefix="gif",
            output_format="gif",
            gif_frames=self._parse_int(opts.get("frames", "")),
            gif_frame_ms=self._parse_int(opts.get("frame_ms", "")),
            gif_loop="once" not in opts,
        )
        if err:
            yield event.plain_result(err)

    def _parse_int(self, raw: str) -> int | None:
        text = str(raw or "").strip().lower().rstrip("帧s毫秒")
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None


    @filter.command("绘模型", alias={"ydrawmodel", "绘图模型"}, desc="查看可用生图模型")
    async def cmd_models(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """查看可用模型：/绘模型 看本地配置，/绘模型 在线 拉接口"""
        arg = str(text or "").strip()
        configured = [str(self._c("default_model", "gpt-image-2.6"))]
        extra = self._c("model_options", [])
        if isinstance(extra, (list, tuple)):
            configured.extend(str(x) for x in extra if str(x).strip())
        lines = ["当前默认：" + str(self._c("default_model", "gpt-image-2.6"))]
        if arg in {"在线", "online", "拉取", "list"}:
            try:
                client = self._client_or_new()
                models = await client.list_models()
                lines.append("接口可用：" + ("、".join(models) if models else "空"))
            except DrawError as exc:
                lines.append(f"拉取失败：{exc}")
        else:
            lines.append("常用：" + "、".join(dict.fromkeys(configured)))
            lines.append("想看接口全部：/绘模型 在线")
        yield event.plain_result("\n".join(lines))

    @filter.command("绘风格", alias={"ydrawstyle", "绘图风格"}, desc="查看风格预设")
    async def cmd_styles(self, event: AstrMessageEvent):
        """查看内置风格预设。"""
        names = "、".join(STYLE_PROMPTS.keys())
        yield event.plain_result(
            "风格（-st 后面填）：\n"
            f"{names}\n"
            "也认中文：写实、动漫、水彩、油画、像素、素描、3d、赛博、水墨、扁平、电影、可爱"
        )

    @filter.command("绘历史", alias={"ydrawlog", "绘图历史"}, desc="查看最近生成记录")
    async def cmd_history(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """查看当前会话最近的生成记录。"""
        try:
            limit = max(1, min(20, int(str(text or "6").strip())))
        except ValueError:
            limit = 6
        records = self._session_records(event)[-limit:]
        if not records:
            yield event.plain_result("这里还没画过东西。")
            return
        base_id = int(self._session_records(event)[0]["id"])
        lines = []
        for rec in records:
            idx = int(rec["id"]) - base_id + 1
            snippet = str(rec.get("prompt", ""))[:40]
            kind = (
                f"GIF {rec.get('gif_frames', '?')}帧"
                if rec.get("format") == "gif"
                else f"{rec.get('n', 1)}张"
            )
            lines.append(f"{idx}. {snippet} · {kind} · {rec.get('model', '')}")
        yield event.plain_result("本会话最近记录：\n" + "\n".join(lines) + "\n重发：/绘重发 序号")

    @filter.command("绘重发", alias={"ydrawresend", "绘图重发"}, desc="重发历史图片")
    async def cmd_resend(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """重发本会话某条历史记录里的图片。"""
        try:
            idx = int(str(text or "").strip())
        except ValueError:
            yield event.plain_result("用法：/绘重发 1")
            return
        records = self._session_records(event)
        if not records:
            yield event.plain_result("这里还没画过东西。")
            return
        base_id = int(records[0]["id"])
        target = next((r for r in records if int(r["id"]) - base_id + 1 == idx), None)
        if target is None:
            yield event.plain_result(f"没有第 {idx} 条。")
            return
        files = [Path(p) for p in target.get("files", []) if Path(p).exists()]
        if not files:
            yield event.plain_result("那条的图已经被清掉了，重画一遍吧。")
            return
        chain: list[Any] = [Plain(f"第 {idx} 条：{str(target.get('prompt', ''))[:40]}")]
        chain.extend(Image.fromFileSystem(str(p)) for p in files)
        await event.send(MessageChain(chain))

    @filter.command("绘删", alias={"ydrawdel", "绘图删除"}, desc="删除历史记录与本地图片")
    async def cmd_delete(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """删除本会话某条历史记录及其本地文件。"""
        denied = self._access_error(event)
        if denied:
            yield event.plain_result(denied)
            return
        try:
            idx = int(str(text or "").strip())
        except ValueError:
            yield event.plain_result("用法：/绘删 1")
            return
        records = self._session_records(event)
        base_id = int(records[0]["id"]) if records else 0
        target = next((r for r in records if int(r["id"]) - base_id + 1 == idx), None)
        if target is None:
            yield event.plain_result(f"没有第 {idx} 条。")
            return
        uid = str(event.get_sender_id())
        if not self._is_master(uid) and str(target.get("user")) != uid and not event.is_admin():
            yield event.plain_result("只能删自己画的那条。")
            return
        removed = 0
        for p in target.get("files", []):
            try:
                Path(p).unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        self._history = [r for r in self._history if r.get("id") != target.get("id")]
        self._dump_history()
        yield event.plain_result(f"删了第 {idx} 条，连带 {removed} 张图。")

    @filter.command("绘帮助", alias={"ydraw", "yhelp", "绘图帮助"}, desc="生图插件用法")
    async def cmd_help(self, event: AstrMessageEvent):
        """生图插件用法说明。"""
        yield event.plain_result(self._help_text())

    def _usage_hint(self) -> str:
        return "用法：/绘 一只在雨里走路的灰猫 --st 写实 --s 竖 --q high"

    def _help_text(self) -> str:
        return (
            "【生图】\n"
            "1. /绘 提示词 —— 文生图\n"
            "2. /改绘 提示词 —— 带图或引用图，改图/多图融合\n"
            "3. /动图 提示词 —— GIF 动图；带图或引用图就是让原图动起来\n"
            "   动图参数：-fr 帧数(2-16)  -ms 每帧毫秒(20-2000)  -once 只播一次\n"
            "   参数：-m 模型  -s 画幅  -q 质量  -n 张数  -st 风格  -neg 排除  -out save  -url  -nosave\n"
            "   画幅：方(1024x1024) 横(1536x1024) 竖(1024x1536) auto\n"
            "   质量：低 low / 中 medium / 高 high\n"
            "4. /绘模型 [在线] —— 看模型\n"
            "5. /绘风格 —— 看风格预设\n"
            "6. /绘历史 [n] —— 看本会话记录\n"
            "7. /绘重发 序号 —— 重发历史图\n"
            "8. /绘删 序号 —— 删记录和本地文件\n"
            f"当前默认：{self._c('default_model', 'gpt-image-2.6')} · "
            f"{self._c('default_size', '1024x1024')} · {self._c('default_quality', 'medium')} · "
            f"动图 {self._c('gif_frames', 8)}帧/{self._c('gif_frame_ms', 120)}ms"
        )

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="yishi_draw")
    async def yishi_draw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        style: str = "",
        size: str = "",
        quality: str = "",
        n: int = 1,
        use_reference: bool = False,
        negative: str = "",
    ) -> str:
        """生成图片并直接把图发给用户；也可以拿当前消息或引用消息里的图当参考图来改图。

        当用户要求画图、生图、配图、做封面、把某张图改成别的样子、融合几张图时调用。
        生成结果会自动发送到当前会话，你只需要简短告知结果，不要复述提示词。

        Args:
            prompt(string): 画面描述，尽量写清主体、动作、场景、光线、构图、画风。
            style(string): 可选风格键，realistic/anime/watercolor/oil/pixel/sketch/3d/cyberpunk/ink/flat/cinematic/cute，留空不套风格。
            size(string): 可选画幅，1024x1024 方形、1536x1024 横幅、1024x1536 竖幅、auto，留空用默认。
            quality(string): 可选质量，low 快省、medium 标准、high 精细，留空用默认。
            n(number): 生成张数，默认 1。
            use_reference(boolean): 是否把当前消息或引用消息里的图片作为参考图做改图，默认 false。
            negative(string): 可选，画面里要避免的元素，如 多余手指、文字水印。
        """
        if not self._c_bool("enable_llm_tool", True):
            return "生图工具在插件配置里关掉了。"
        if self._c_bool("llm_tool_respect_access", True):
            denied = self._access_error(event)
            if denied:
                return f"没权限：{denied}"

        err, result = await self._run_guarded(
            event,
            prompt,
            model="",
            size=size,
            quality=quality,
            n=self._resolve_n(str(n or 1)),
            style=style,
            negative=negative,
            use_ref=bool(use_reference),
            send=True,
            prefix="tool",
            reply_prefix="",
        )
        if err:
            return err
        assert result is not None
        rec = result["record"]
        return (
            f"已生成 {rec['n']} 张图并发送（模型 {result['model']}，{result['size']}，"
            f"{result['quality']}，耗时 {human_duration(result['elapsed'])}，记录号 {rec['id']}）。"
        )

    @filter.llm_tool(name="yishi_draw_gif")
    async def yishi_draw_gif(
        self,
        event: AstrMessageEvent,
        prompt: str,
        style: str = "",
        quality: str = "",
        frames: int = 0,
        frame_ms: int = 0,
        use_reference: bool = False,
        loop: bool = True,
        negative: str = "",
    ) -> str:
        """生成一张会动的 GIF 动图并直接发给用户；也可以让当前消息或引用消息里的图动起来。

        当用户要动图、GIF、表情包动起来、让某张图做个小动作时调用。
        只做短循环动图，不适合长视频；每次只出一张。

        Args:
            prompt(string): 画面和动作描述，写清主体、具体动作（挥手、眨眼、跳动、转圈）、场景与画风。
            style(string): 可选风格键，realistic/anime/watercolor/oil/pixel/sketch/3d/cyberpunk/ink/flat/cinematic/cute。
            quality(string): 可选质量，low 快省、medium 标准、high 精细，动图建议 low 或 medium。
            frames(number): 帧数，2 到 16，留空用插件默认；帧数越多越慢越贵。
            frame_ms(number): 每帧毫秒，20 到 2000，留空用插件默认；越小播得越快。
            use_reference(boolean): 是否用当前消息或引用消息里的图当参考，让那张图动起来。
            loop(boolean): 是否无限循环，默认 true。
            negative(string): 可选，画面里要避免的元素。
        """
        if not self._c_bool("enable_llm_tool", True):
            return "生图工具在插件配置里关掉了。"
        if self._c_bool("llm_tool_respect_access", True):
            denied = self._access_error(event)
            if denied:
                return f"没权限：{denied}"

        err, result = await self._run_guarded(
            event,
            prompt,
            model="",
            size="",
            quality=quality,
            n=1,
            style=style,
            negative=negative,
            use_ref=bool(use_reference),
            send=True,
            prefix="gif",
            output_format="gif",
            gif_frames=int(frames or 0) or None,
            gif_frame_ms=int(frame_ms or 0) or None,
            gif_loop=bool(loop),
        )
        if err:
            return err
        assert result is not None
        meta = result.get("gif") or {}
        return (
            f"动图做好了，已发送（{meta.get('frames', '?')}帧，{meta.get('frame_ms', '?')}ms/帧，"
            f"{meta.get('width', '?')}x{meta.get('height', '?')}，"
            f"耗时 {human_duration(result['elapsed'])}，记录号 {result['record']['id']}）。"
        )
