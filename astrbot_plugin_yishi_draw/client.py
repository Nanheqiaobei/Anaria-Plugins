"""gpt-image 系列接口客户端。

只做三件事：
1. 文生图（/v1/images/generations）
2. 参考图改图（/v1/images/edits，multipart，可多图）
3. 把结果 URL 拉回本地字节
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from pathlib import Path
from typing import Any

import aiohttp

MAX_PROMPT_CHARS = 4000
MAX_REF_IMAGE_BYTES = 12 * 1024 * 1024


class DrawError(Exception):
    """生图链路里的可读错误。"""


def guess_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/png"


def ext_for(data: bytes) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }.get(guess_mime(data), ".png")


def _translate_error(status: int, body: str) -> str:
    snippet = (body or "").strip().replace("\n", " ")
    if len(snippet) > 300:
        snippet = snippet[:300] + "..."
    if status in (401, 403):
        return f"接口拒绝访问（{status}），大概是 key 失效或没权限。{snippet}"
    if status == 404:
        return f"接口地址不对（404），检查 base_url 是否带 /v1。{snippet}"
    if status == 429:
        return f"被限流了（429），等一会儿再试。{snippet}"
    if status in (400, 422):
        return f"请求参数被拒（{status}）：{snippet}"
    if status >= 500:
        return f"上游服务出错（{status}），稍后重试。{snippet}"
    return f"接口返回 {status}：{snippet}"


class ImageClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: int = 300,
        download_timeout: int = 120,
        proxy: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = max(10, int(timeout))
        self.download_timeout = max(10, int(download_timeout))
        self.proxy = proxy.strip() or None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- 基础 ----------

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise DrawError("没有配置 api_base")
        base = self.base_url
        # 允许用户直接填到 /v1
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}{path}"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise DrawError("没有配置 api_key")
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _post(self, path: str, *, json_body: dict[str, Any] | None = None, form: aiohttp.FormData | None = None) -> dict[str, Any]:
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        url = self._url(path)
        try:
            async with session.post(
                url,
                headers=self._headers(),
                json=json_body,
                data=form,
                timeout=timeout,
                proxy=self.proxy,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise DrawError(_translate_error(resp.status, text))
        except aiohttp.ClientConnectorError as exc:
            raise DrawError(f"连不上接口：{exc}") from exc
        except asyncio.TimeoutError as exc:
            raise DrawError(f"接口超时（{self.timeout}s），图可能太大或服务忙") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DrawError(f"接口返回的不是 JSON：{text[:200]}") from exc

    # ---------- 生图 ----------

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        size: str = "",
        quality: str = "",
        n: int = 1,
        refs: list[bytes] | None = None,
        output_format: str = "",
        frame_count: int = 0,
        frame_duration_ms: int = 0,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise DrawError("提示词是空的")
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS]

        refs = refs or []
        # GIF 相关的两个参数只在上游认的时候传，别把普通生图搞脏
        gif_fields: dict[str, str] = {}
        if output_format == "gif":
            if frame_count:
                gif_fields["frame_count"] = str(int(frame_count))
            if frame_duration_ms:
                gif_fields["frame_duration_ms"] = str(int(frame_duration_ms))
        if refs:
            form = aiohttp.FormData(quote_fields=False)
            form.add_field("model", model)
            form.add_field("prompt", prompt)
            form.add_field("n", str(max(1, n)))
            if size:
                form.add_field("size", size)
            if quality:
                form.add_field("quality", quality)
            if output_format:
                form.add_field("output_format", output_format)
            for k, v in gif_fields.items():
                form.add_field(k, v)
            for idx, data in enumerate(refs):
                if len(data) > MAX_REF_IMAGE_BYTES:
                    raise DrawError(
                        f"第 {idx + 1} 张参考图超过 {MAX_REF_IMAGE_BYTES // 1024 // 1024}MB，先压一下"
                    )
                form.add_field(
                    "image[]",
                    data,
                    filename=f"ref_{idx + 1}{ext_for(data)}",
                    content_type=guess_mime(data),
                )
            payload = await self._post("/v1/images/edits", form=form)
        else:
            body: dict[str, Any] = {"model": model, "prompt": prompt, "n": max(1, n)}
            if size:
                body["size"] = size
            if quality:
                body["quality"] = quality
            if output_format:
                body["output_format"] = output_format
            for k, v in gif_fields.items():
                body[k] = int(v)
            payload = await self._post("/v1/images/generations", json_body=body)

        items = normalize_payload(payload)
        if not items:
            raise DrawError(f"接口没给图片：{str(payload)[:300]}")
        return {
            "items": items,
            "usage": payload.get("usage") if isinstance(payload, dict) else None,
            "revised_prompt": (items[0].get("revised_prompt") if items else "") or "",
            "raw": payload if not items else None,
        }

    async def list_models(self) -> list[str]:
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=30)
        async with session.get(
            self._url("/v1/models"),
            headers=self._headers(),
            timeout=timeout,
            proxy=self.proxy,
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise DrawError(_translate_error(resp.status, text))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DrawError("模型列表解析失败") from exc
        data = payload.get("data") if isinstance(payload, dict) else payload
        models: list[str] = []
        for item in data or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
            elif isinstance(item, str):
                models.append(item)
        return models

    # ---------- 下载 ----------

    async def fetch(self, url: str) -> bytes:
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self.download_timeout)
        last_error = ""
        for attempt in range(3):
            try:
                async with session.get(
                    url,
                    timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; AstrBot/1.0)"},
                    proxy=self.proxy,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if not data:
                            raise DrawError("下载到的图片是空的")
                        return data
                    last_error = f"HTTP {resp.status}"
                    if resp.status not in (408, 429, 500, 502, 503, 504):
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = str(exc)
            if attempt < 2:
                await asyncio.sleep(1.2 * (attempt + 1))
        raise DrawError(f"图片下载失败：{last_error}")


def normalize_payload(payload: Any) -> list[dict[str, Any]]:
    """把各种形态的返回统一成 [{url} 或 {b64}] 列表。"""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("data")
    if raw is None:
        raw = payload.get("images") or payload.get("output") or []
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, str):
        raw = [{"url": raw}]
    items: list[dict[str, Any]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            if entry.startswith("http"):
                items.append({"url": entry})
            elif entry.startswith("data:"):
                try:
                    items.append({"b64": entry.split(",", 1)[1]})
                except IndexError:
                    continue
            continue
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or entry.get("image_url") or entry.get("image")
        b64 = entry.get("b64_json") or entry.get("base64") or entry.get("b64")
        item: dict[str, Any] = {}
        if url:
            item["url"] = str(url)
        if b64:
            item["b64"] = str(b64)
        if entry.get("revised_prompt"):
            item["revised_prompt"] = str(entry["revised_prompt"])
        if item:
            items.append(item)
    return items


def b64_to_bytes(b64: str) -> bytes:
    text = b64.strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise DrawError("接口返回的 base64 图片无法解析") from exc


def save_bytes(directory: Path, data: bytes, prefix: str) -> Path:
    import time

    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"{prefix}_{stamp}_{int(time.time() * 1000) % 1000:03d}{ext_for(data)}"
    path = directory / name
    path.write_bytes(data)
    return path
