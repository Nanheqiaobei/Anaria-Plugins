"""GIF 的检查与本地重编码。

上游行为不统一：generations 端点会忽略 frame_count / frame_duration_ms，
edits 端点才吃这两个参数。所以返回值统一在这里过一遍，保证用户要的帧数和
播放速度是准的，必要时也能抽帧、缩放、统一调色板。
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageSequence

MAX_GIF_BYTES = 8 * 1024 * 1024


class GifError(Exception):
    """GIF 处理失败。"""


def is_gif(data: bytes) -> bool:
    return data[:6] in (b"GIF87a", b"GIF89a")


def gif_info(data: bytes) -> dict[str, Any]:
    try:
        im = Image.open(io.BytesIO(data))
        n = getattr(im, "n_frames", 1)
        durations: list[int] = []
        for i in range(n):
            im.seek(i)
            durations.append(int(im.info.get("duration") or 100))
    except Exception as exc:  # noqa: BLE001 - Pillow 异常类型不稳定
        raise GifError(f"GIF 解析失败：{exc}") from exc
    return {
        "frames": n,
        "width": im.size[0],
        "height": im.size[1],
        "durations": durations,
        "frame_ms": durations[0] if durations else 100,
        "total_ms": sum(durations),
        "loop": im.info.get("loop"),
        "bytes": len(data),
    }


def _load_frames(data: bytes) -> tuple[list[Image.Image], list[int]]:
    im = Image.open(io.BytesIO(data))
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(im):
        durations.append(int(frame.info.get("duration") or 100))
        frames.append(frame.convert("RGBA"))
    return frames, durations


def _pick(frames: list[Image.Image], durations: list[int], target: int) -> tuple[list[Image.Image], list[int]]:
    n = len(frames)
    if target <= 0 or target == n:
        return frames, durations
    if target >= n:
        # 放不了帧，重复最末帧补足
        extra = target - n
        return frames + [frames[-1].copy() for _ in range(extra)], durations + [durations[-1]] * extra
    if target == 1:
        return [frames[0]], [durations[0]]
    # 均匀采样，保留首帧和末帧
    idx: list[int] = []
    step = (n - 1) / (target - 1)
    for i in range(target):
        idx.append(min(n - 1, round(i * step)))
    seen: list[int] = []
    for i in idx:
        if not seen or i > seen[-1]:
            seen.append(i)
    return [frames[i] for i in seen], [durations[i] for i in seen]


def _unify_palette(frames: list[Image.Image]) -> list[Image.Image]:
    if not frames:
        return []
    base = frames[0].convert("RGB")
    palette = base.quantize(colors=255)
    out: list[Image.Image] = []
    for frame in frames:
        out.append(frame.convert("RGB").quantize(palette=palette, dither=Image.FLOYDSTEINBERG))
    return out


def rebuild_gif(
    data: bytes,
    *,
    frames: int = 0,
    frame_ms: int = 0,
    loop: int | None = 0,
    max_width: int = 0,
) -> tuple[bytes, dict[str, Any]]:
    """按需求重编码 GIF。

    frames: 目标帧数，0 表示保持原帧数
    frame_ms: 每帧毫秒，0 表示保持原时长
    loop: 0 无限循环，None 保持原有设置
    max_width: 超过这个宽度就等比缩小，0 表示不动
    """
    try:
        origin_frames, durations = _load_frames(data)
    except Exception as exc:  # noqa: BLE001
        raise GifError(f"GIF 读取失败：{exc}") from exc
    if not origin_frames:
        raise GifError("GIF 里没有帧")

    picked, picked_durs = _pick(origin_frames, durations, frames)

    if max_width and picked[0].width > max_width:
        ratio = max_width / picked[0].width
        size = (max_width, max(1, round(picked[0].height * ratio)))
        picked = [p.resize(size, Image.LANCZOS) for p in picked]

    if frame_ms and frame_ms > 0:
        per = [max(20, min(2000, int(frame_ms)))] * len(picked)
    else:
        per = picked_durs

    converted = _unify_palette(picked)

    loop_arg = 0 if loop is None else max(0, int(loop))

    buf = io.BytesIO()
    try:
        converted[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=converted[1:],
            duration=per,
            loop=loop_arg,
            disposal=2,
            optimize=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise GifError(f"GIF 重编码失败：{exc}") from exc

    out = buf.getvalue()
    if len(out) > MAX_GIF_BYTES:
        raise GifError(f"重编码后 GIF 超过 {MAX_GIF_BYTES // 1024 // 1024}MB")
    info = {
        "frames": len(picked),
        "width": picked[0].width,
        "height": picked[0].height,
        "frame_ms": per[0] if per else 100,
        "total_ms": sum(per),
        "bytes": len(out),
        "origin_frames": len(origin_frames),
    }
    return out, info
