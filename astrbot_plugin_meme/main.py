import mimetypes
import re
from pathlib import Path
import aiohttp
from astrbot.api.all import *
from astrbot.api.event import filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

PLUGIN_NAME = "astrbot_plugin_meme"

MODIFIERS = ["小", "大", "超", "很", "特别", "非常", "十分", "太", "真"]

def _strip_modifiers(text: str) -> str:
    res = text
    changed = True
    while changed:
        changed = False
        for m in MODIFIERS:
            if res.startswith(m) and len(res) > len(m):
                res = res[len(m):]
                changed = True
    return res

def _split_keywords(text: str) -> list[str]:
    # 拆分中文逗号、英文逗号、顿号、下划线、减号、斜杠、竖线、分号、空格
    return [p.strip() for p in re.split(r'[,，、_\-/|;；\s]+', text) if p.strip()]

@register(
    PLUGIN_NAME,
    "博士 & 安亦诗",
    "本地表情包插件，支持面板自定义目录、Web 管理页面、URL 表情及群指令",
    "1.5.0"
)
class MemePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 注册 Web API
        context.register_web_api(f"/{PLUGIN_NAME}/list", self.api_list_memes, ["GET"], "获取表情列表")
        context.register_web_api(f"/{PLUGIN_NAME}/upload", self.api_upload_meme, ["POST"], "上传表情图片")
        context.register_web_api(f"/{PLUGIN_NAME}/delete", self.api_delete_meme, ["POST"], "删除本地表情")
        context.register_web_api(f"/{PLUGIN_NAME}/image", self.api_get_image, ["GET"], "获取本地表情预览图")

    @property
    def meme_dir(self) -> Path:
        dir_str = self.config.get("meme_dir") or "/AstrBot/data/plugins/astrbot_plugin_meme/memes"
        p = Path(dir_str.strip())
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _get_url_memes(self) -> dict:
        url_text = self.config.get("custom_url_memes", "") or ""
        url_map = {}
        for line in url_text.strip().splitlines():
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    url_map[k] = v
        return url_map

    def _get_meme_catalog(self) -> list[dict]:
        valid_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        catalog = []
        m_dir = self.meme_dir
        if m_dir.exists():
            for f in sorted(m_dir.iterdir(), key=lambda x: x.name):
                if f.is_file() and f.suffix.lower() in valid_exts:
                    raw_name = f.stem
                    catalog.append({
                        "name": raw_name,
                        "target": str(f.resolve()),
                        "keywords": _split_keywords(raw_name)
                    })
        url_map = self._get_url_memes()
        for k, v in url_map.items():
            catalog.append({
                "name": k,
                "target": v,
                "keywords": _split_keywords(k)
            })
        return catalog

    def _find_meme(self, query: str) -> str | None:
        q = (query or "").strip()
        if not q:
            return None
        catalog = self._get_meme_catalog()

        # 1. 精确匹配完整名或某关键词
        for item in catalog:
            if q == item["name"] or q in item["keywords"]:
                return item["target"]

        # 2. 去除修饰词匹配（例如“小无语”->“无语”，“非常开心”->“开心”）
        sq = _strip_modifiers(q)
        if sq and sq != q:
            for item in catalog:
                if sq == item["name"] or sq in item["keywords"]:
                    return item["target"]
                for kw in item["keywords"]:
                    if _strip_modifiers(kw) == sq:
                        return item["target"]

        # 3. 相似度打分匹配（兼顾包含关系与长短贴近度，防止误伤）
        best_score = 0.0
        best_target = None
        for item in catalog:
            for kw in item["keywords"]:
                score = 0.0
                if q == kw:
                    score = 100.0
                elif q in kw:
                    score = (len(q) / len(kw)) * 90.0
                elif kw in q:
                    if len(kw) >= 2 or len(q) == 1:
                        score = (len(kw) / len(q)) * 80.0
                elif sq and (sq in kw or kw in sq):
                    score = (min(len(sq), len(kw)) / max(len(sq), len(kw))) * 75.0

                if score > best_score:
                    best_score = score
                    best_target = item["target"]

        if best_score >= 40.0:
            return best_target

        # 4. 兜底：原始文件名包含匹配（长度>=2）
        for item in catalog:
            if len(q) >= 2 and (q in item["name"] or item["name"] in q):
                return item["target"]

        return None

    def _get_meme_map(self) -> dict:
        catalog = self._get_meme_catalog()
        memes = {}
        for item in catalog:
            memes[item["name"]] = item["target"]
            for kw in item["keywords"]:
                if kw not in memes:
                    memes[kw] = item["target"]
        return memes

    # --- Web API Handlers ---

    async def api_list_memes(self):
        valid_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        local_items = []
        m_dir = self.meme_dir
        if m_dir.exists():
            for f in sorted(m_dir.iterdir(), key=lambda x: x.name):
                if f.is_file() and f.suffix.lower() in valid_exts:
                    local_items.append({
                        "name": f.stem,
                        "filename": f.name,
                        "type": "local",
                        "size": f.stat().st_size
                    })

        url_map = self._get_url_memes()
        url_items = [{"name": k, "url": v, "type": "url"} for k, v in url_map.items()]

        return json_response({
            "status": "ok",
            "data": {
                "meme_dir": str(self.meme_dir),
                "local_memes": local_items,
                "url_memes": url_items,
                "total": len(local_items) + len(url_items)
            }
        })

    async def api_upload_meme(self):
        form = await request.form()
        files = await request.files()
        upload = files.get("file")
        if not upload or not isinstance(upload, PluginUploadFile):
            return error_response("未检测到上传的文件", status_code=400)

        custom_name = form.get("name", "").strip() or request.query.get("name", "").strip()
        orig_name = upload.filename or "meme.png"
        suffix = Path(orig_name).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            return error_response(f"不支持的图片格式: {suffix}", status_code=400)

        stem = custom_name if custom_name else Path(orig_name).stem
        # 移除非法字符
        stem = "".join(c for c in stem if c not in r'\/:*?"<>|').strip()
        if not stem:
            stem = "meme"

        target_file = self.meme_dir / f"{stem}{suffix}"
        await upload.save(target_file)
        return json_response({
            "status": "ok",
            "message": f"表情「{stem}」上传成功",
            "data": {
                "name": stem,
                "filename": target_file.name
            }
        })

    async def api_delete_meme(self):
        payload = await request.json(default={})
        name = payload.get("filename") or payload.get("name")
        if not name:
            return error_response("缺少要删除的文件名或名称", status_code=400)

        # 安全检查，防止路径穿越
        name = Path(name).name
        m_dir = self.meme_dir
        deleted = False

        # 如果直接传了带后缀的文件名
        target = m_dir / name
        if target.exists() and target.is_file():
            target.unlink()
            deleted = True
        else:
            # 尝试按表情名遍历匹配
            for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                candidate = m_dir / f"{name}{ext}"
                if candidate.exists() and candidate.is_file():
                    candidate.unlink()
                    deleted = True
                    break

        if not deleted:
            return error_response("未找到该本地表情文件", status_code=404)

        return json_response({"status": "ok", "message": f"表情「{name}」已删除"})

    async def api_get_image(self):
        filename = request.query.get("filename", "")
        if not filename:
            return error_response("缺少文件名", status_code=400)
        filename = Path(filename).name
        target = self.meme_dir / filename
        if not target.exists() or not target.is_file():
            return error_response("图片不存在", status_code=404)

        mime, _ = mimetypes.guess_type(str(target))
        return file_response(target, content_type=mime or "image/png")

    # --- 群聊指令与 LLM Tool ---

    @command("存表情")
    async def add_meme(self, event: AstrMessageEvent, name: str):
        '''保存附带或引用的图片为表情包，用法: /存表情 [名称]'''
        admin_only = self.config.get("admin_only", True)
        if admin_only and not (event.is_admin() or event.is_operator() or event.get_sender_id() in ["YOUR_QQ_ID"]):
            yield event.plain_result("只有管理员可以添加表情包哦。")
            return

        name = name.strip()
        if not name:
            yield event.plain_result("请指定表情名称，例如：/存表情 开心")
            return

        img_url = None
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                img_url = comp.url or comp.path
                break
        
        if not img_url and event.message_obj.quoted_message:
            for comp in event.message_obj.quoted_message.message:
                if isinstance(comp, Image):
                    img_url = comp.url or comp.path
                    break

        if not img_url:
            yield event.plain_result("请在发送指令时附带一张图片，或者引用包含图片的消息。")
            return

        try:
            if img_url.startswith("http://") or img_url.startswith("https://"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url, timeout=15) as resp:
                        if resp.status != 200:
                            yield event.plain_result(f"下载图片失败，状态码: {resp.status}")
                            return
                        data = await resp.read()
                        ext = ".png"
                        c_type = resp.headers.get("Content-Type", "")
                        if "jpeg" in c_type or "jpg" in c_type:
                            ext = ".jpg"
                        elif "gif" in c_type:
                            ext = ".gif"
                        elif "webp" in c_type:
                            ext = ".webp"
            else:
                path_obj = Path(img_url)
                if not path_obj.exists():
                    yield event.plain_result("无法读取本地图片文件。")
                    return
                data = path_obj.read_bytes()
                ext = path_obj.suffix or ".png"

            target_path = self.meme_dir / f"{name}{ext}"
            target_path.write_bytes(data)
            yield event.plain_result(f"表情「{name}」已保存成功！已存放至: {self.meme_dir.name}/{name}{ext}")
        except Exception as e:
            yield event.plain_result(f"保存表情时出错: {str(e)}")

    @command("表情列表")
    async def list_memes(self, event: AstrMessageEvent):
        '''查看当前所有可用的表情包'''
        memes = self._get_meme_map()
        if not memes:
            yield event.plain_result(f"当前表情库还是空的，目录位于: {self.meme_dir}。可以用「/存表情 名称」存入或在管理页面上传。")
            return
        names = "、".join(memes.keys())
        yield event.plain_result(f"当前可用表情（共 {len(memes)} 个，路径: {self.meme_dir.name}）：\n{names}")

    @llm_tool(name="send_meme")
    async def send_meme(self, event: AstrMessageEvent, name: str):
        '''当聊到合适话题、需要发表情包表达生动情绪时调用此工具。
        Args:
            name(string): 要发送的表情名称。
        '''
        target = self._find_meme(name)

        if not target:
            logger.warning(f"[send_meme] 未找到与「{name}」匹配的表情，已静默跳过。")
            return f"未找到名为 {name} 的表情，已静默跳过。"

        if target.startswith("http://") or target.startswith("https://"):
            return event.chain_result([Image.fromURL(target)])
        else:
            return event.chain_result([Image.fromFileSystem(target)])

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        '''在消息发送前拦截，解析纯文本里的 [meme:表情名] 或 [表情:表情名] 语法并替换为图片组件'''
        result = event.get_result()
        if not result or not result.chain:
            return

        pattern = re.compile(r'\[(?:meme|表情):([^\]]+)\]')

        new_chain = []
        modified = False

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0
                for match in pattern.finditer(text):
                    modified = True
                    start, end = match.span()
                    meme_name = match.group(1).strip()

                    # 截取匹配项前面的纯文本
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))

                    # 查找对应表情
                    target = self._find_meme(meme_name)

                    if target:
                        if target.startswith("http://") or target.startswith("https://"):
                            new_chain.append(Image.fromURL(target))
                        else:
                            new_chain.append(Image.fromFileSystem(target))
                    else:
                        logger.warning(f"[meme_filter] 未找到名为 {meme_name} 的表情，保留原占位符")
                        new_chain.append(Plain(match.group(0)))

                    last_idx = end

                if last_idx < len(text):
                    if last_idx > 0:
                        new_chain.append(Plain(text[last_idx:]))
                    else:
                        new_chain.append(comp)
            else:
                new_chain.append(comp)

        if modified:
            result.chain = new_chain

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        '''在请求 LLM 前动态注入当前表情库的可用表情列表及别名，确保 LLM 准确感知'''
        catalog = self._get_meme_catalog()
        if not catalog:
            return

        lines = []
        for item in catalog:
            kws = " / ".join(item["keywords"])
            lines.append(f"- {item['name']}（别名/情绪：{kws}）")

        nl = chr(10)
        meme_list_text = nl.join(lines)
        prompt_segment = (
            f"{nl}{nl}[可用表情包列表]{nl}"
            f"若需配合语境使用表情包，可在正文内嵌 [meme:表情名]（推荐）或调用 send_meme。当前确切可用的表情及别名如下：{nl}"
            f"{meme_list_text}{nl}"
            "请严格选用上述别名或名称，禁止凭空捏造不存在的表情名。"
        )

        if req.system_prompt:
            req.system_prompt += prompt_segment
        else:
            req.system_prompt = prompt_segment

