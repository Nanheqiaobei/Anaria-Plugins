import base64
import io
import json
import os
import re
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except Exception:  # pragma: no cover
    def get_astrbot_temp_path() -> str:
        return tempfile.gettempdir()


PLUGIN_NAME = "astrbot_plugin_gemini_tts"

NEW_NATURAL_VOICES = [
    "Arlo", "Bodi", "Brio", "Cleo", "Cruz", "Daro", "Elio", "Enya", "Enzo", "Finn",
    "Fola", "Gero", "Hali", "Jett", "Jori", "Kira", "Knox", "Koda", "Lora", "Ludo",
    "Lumi", "Mako", "Milo", "Neno", "Nika", "Nyla", "Olin", "Rami", "Riko", "Rina",
    "Sami", "Sola", "Tari", "Tavi", "Tova", "Varo", "Veda", "Zali", "Zeno", "Zuri",
]

CLASSIC_CELESTIAL_VOICES = [
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam",
    "Aoede", "Autonoe", "Callirrhoe", "Charon", "Despina",
    "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus",
    "Kore", "Laomedeia", "Leda", "Orus", "Puck",
    "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar",
    "Sulafat", "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi",
]

ALL_VOICES = NEW_NATURAL_VOICES + CLASSIC_CELESTIAL_VOICES
VOICE_LOWER_MAP = {v.lower(): v for v in ALL_VOICES}

AVAILABLE_MODELS = [
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-pro-preview-tts",
    "gemini-2.5-flash-preview-tts",
]

AVAILABLE_MODES = ["openai", "gemini_native", "gemini_stream", "interactions"]


def _pcm_to_wav_bytes(
    pcm_bytes: bytes,
    channels: int = 1,
    sample_width: int = 2,
    sample_rate: int = 24000,
) -> bytes:
    """将裸 PCM (24kHz / 16bit / mono) 封装为标准 RIFF WAV 字节流。"""
    if pcm_bytes[:4] == b"RIFF" and len(pcm_bytes) > 44:
        return pcm_bytes
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _mask_key(key: str) -> str:
    if not key:
        return "（未填写）"
    if len(key) <= 10:
        return key[:2] + "***" + key[-2:]
    return key[:6] + "..." + key[-4:]


def _clean_text_for_tts(text: str) -> str:
    """清理不适合直接朗读的占位符（如 [meme:xxx]）。"""
    cleaned = re.sub(r"\[(?:meme|表情):[^\]]+\]", "", text or "")
    return cleaned.strip()


@register(
    PLUGIN_NAME,
    "博士 & 安亦诗",
    "Gemini 3.8 Flash TTS 插件（支持 70 音色、Key 配置、OpenAI/原生/流式/Interactions 四种模式及多说话人对话）",
    "1.0.0",
)
class GeminiTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _cfg_str(self, key: str, default: str = "") -> str:
        val = self.config.get(key, default)
        return str(val).strip() if val is not None else default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in {"1", "true", "yes", "on", "开启", "开"}
        return bool(val)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except Exception:
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except Exception:
            return default

    def _api_base(self) -> str:
        base = self._cfg_str("api_base", "https://generativelanguage.googleapis.com").rstrip("/")
        return base or "https://generativelanguage.googleapis.com"

    def _api_key(self) -> str:
        return self._cfg_str("api_key", "")

    def _default_model(self) -> str:
        return self._cfg_str("model", "gemini-3.8-flash-tts") or "gemini-3.8-flash-tts"

    def _default_voice(self) -> str:
        custom = self._cfg_str("custom_voice", "")
        if custom:
            return custom
        return self._cfg_str("voice", "Kore") or "Kore"

    def _default_mode(self) -> str:
        mode = self._cfg_str("api_mode", "openai")
        return mode if mode in AVAILABLE_MODES else "openai"

    def _default_format(self) -> str:
        fmt = self._cfg_str("response_format", "wav").lower()
        return fmt if fmt in {"wav", "mp3"} else "wav"

    def _default_speed(self) -> float:
        spd = self._cfg_float("speed", 1.0)
        return max(0.25, min(4.0, spd))

    def _timeout_sec(self) -> int:
        return max(10, self._cfg_int("timeout", 120))

    async def _save_config(self):
        try:
            if hasattr(self.config, "save_config_async"):
                await self.config.save_config_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[GeminiTTS] 保存配置失败: {e}")

    def _check_config_perm(self, event: AstrMessageEvent) -> bool:
        if not self._cfg_bool("admin_only_config", False):
            return True
        return event.is_admin()

    def _temp_file_path(self, ext: str = "wav") -> str:
        temp_dir = Path(get_astrbot_temp_path())
        temp_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_temp_files(temp_dir)
        fname = f"gemini_tts_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{ext}"
        return str((temp_dir / fname).resolve())

    def _cleanup_old_temp_files(self, temp_dir: Path, keep_recent: int = 20, max_age_sec: int = 600):
        try:
            files = sorted(
                temp_dir.glob("gemini_tts_*.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            now = time.time()
            for idx, f in enumerate(files):
                if idx >= keep_recent and (now - f.stat().st_mtime) > max_age_sec:
                    f.unlink(missing_ok=True)
        except Exception:
            pass

    def _speaker_voice_map(self) -> dict[str, str]:
        raw = self._cfg_str("default_speaker_voices", "")
        mapping: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                mapping[k] = self._normalize_voice(v)
        return mapping

    def _normalize_voice(self, voice_name: str) -> str:
        v = (voice_name or "").strip()
        if not v:
            return self._default_voice()
        return VOICE_LOWER_MAP.get(v.lower(), v)

    def _extract_command_body(self, event: AstrMessageEvent, cmd_names: tuple[str, ...]) -> str:
        raw = (event.message_str or "").strip()
        if not raw:
            return ""
        sorted_cmds = sorted(cmd_names, key=len, reverse=True)
        pattern = r"^(?:[/!！.。]?\s*)(?:" + "|".join(re.escape(c) for c in sorted_cmds) + r")(?:\s+|(?=\n)|$)"
        m = re.match(pattern, raw, flags=re.IGNORECASE)
        if m:
            return raw[m.end():].strip()
        return raw

    def _parse_tts_flags(self, body: str) -> tuple[dict[str, Any], str]:
        """从指令文本开头解析可选参数 (-v/-s/-i/-m/--mode/-f)，保留正文原有引号与换行。"""
        opts: dict[str, Any] = {}
        s = body.lstrip()
        flag_re = re.compile(
            r'^(?P<flag>--voice|-v|--speed|-s|--style|--instructions|-i|--model|-m|--mode|--format|-f)\s+'
            r'(?:"(?P<dq>[^"]*)"|\'(?P<sq>[^\']*)\'|(?P<word>\S+))(?:\s+|$)'
        )
        while s:
            m = flag_re.match(s)
            if not m:
                break
            flag = m.group("flag")
            val = m.group("dq") if m.group("dq") is not None else (
                m.group("sq") if m.group("sq") is not None else m.group("word")
            )
            if flag in {"-v", "--voice"}:
                opts["voice"] = self._normalize_voice(val)
            elif flag in {"-s", "--speed"}:
                try:
                    opts["speed"] = max(0.25, min(4.0, float(val)))
                except ValueError:
                    pass
            elif flag in {"-i", "--style", "--instructions"}:
                opts["style"] = val
            elif flag in {"-m", "--model"}:
                opts["model"] = val
            elif flag == "--mode":
                if val in AVAILABLE_MODES:
                    opts["mode"] = val
            elif flag in {"-f", "--format"}:
                if val.lower() in {"wav", "mp3"}:
                    opts["response_format"] = val.lower()
            s = s[m.end():]
        return opts, s.strip()

    async def _fetch_models_api(self) -> tuple[list[str], str | None]:
        key = self._api_key()
        if not key:
            return [], "未配置 API Key，请先在插件配置中填写 api_key。"
        url = f"{self._api_base()}/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
        session = await self._get_session()
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    return [], f"HTTP {resp.status}: {err_text[:300]}"
                data = await resp.json()
                items = data.get("data") if isinstance(data, dict) else data
                if isinstance(items, list):
                    ids = [str(x.get("id", x)) if isinstance(x, dict) else str(x) for x in items]
                    return ids, None
                return [], f"未识别的返回结构: {str(data)[:200]}"
        except Exception as e:
            return [], f"请求失败: {e}"

    async def _fetch_voices_api(self, model: str) -> tuple[list[str], str | None]:
        key = self._api_key()
        if not key:
            return [], "未配置 API Key，请先在插件配置中填写 api_key。"
        url = f"{self._api_base()}/v1/models/{model}"
        headers = {"Authorization": f"Bearer {key}"}
        session = await self._get_session()
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    return [], f"HTTP {resp.status}: {err_text[:300]}"
                data = await resp.json()
                voices = (data.get("capability_options") or {}).get("voices") or []
                if isinstance(voices, list) and voices:
                    return [str(v) for v in voices], None
                return [], "该模型未返回 capability_options.voices 字段。"
        except Exception as e:
            return [], f"请求失败: {e}"

    async def _synthesize_single(
        self,
        text: str,
        *,
        voice: str | None = None,
        style: str | None = None,
        speed: float | None = None,
        model: str | None = None,
        mode: str | None = None,
        response_format: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        调用 Gemini TTS 服务生成单人语音文件。
        返回: (本地音频路径, 元信息字典)
        """
        key = self._api_key()
        if not key:
            raise ValueError("尚未配置 API Key，请前往 AstrBot 插件配置页填写 `api_key`。")

        base = self._api_base()
        use_model = model or self._default_model()
        use_voice = self._normalize_voice(voice or self._default_voice())
        use_style = style if style is not None else self._cfg_str("default_style", "")
        use_speed = speed if speed is not None else self._default_speed()
        use_mode = mode or self._default_mode()
        use_format = (response_format or self._default_format()).lower()
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec())
        session = await self._get_session()
        meta: dict[str, Any] = {"model": use_model, "voice": use_voice, "mode": use_mode}

        if use_mode == "openai":
            url = f"{base}/v1/audio/speech"
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            payload: dict[str, Any] = {
                "model": use_model,
                "input": text,
                "voice": use_voice,
                "response_format": use_format,
                "speed": use_speed,
            }
            if use_style:
                payload["instructions"] = use_style

            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"OpenAI TTS 接口错误 ({resp.status}): {err[:300]}")
                audio_bytes = await resp.read()
                if not audio_bytes:
                    raise RuntimeError("TTS 服务返回了空音频。")
                out_path = self._temp_file_path(use_format)
                Path(out_path).write_bytes(audio_bytes)
                return out_path, meta

        if use_mode == "gemini_native":
            url = f"{base}/v1beta/models/{use_model}:generateContent"
            headers = {
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            }
            part_obj: dict[str, Any] = {"text": text}
            if use_style:
                part_obj["speechMetadata"] = {"speaker": "Speaker", "style": use_style}
                speech_cfg: dict[str, Any] = {
                    "multiSpeakerVoiceConfig": {
                        "mode": self._cfg_str("multi_speaker_mode", "CONVERSATIONAL"),
                        "speakerVoiceConfigs": [
                            {
                                "speaker": "Speaker",
                                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": use_voice}},
                            }
                        ],
                    }
                }
            else:
                speech_cfg = {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": use_voice}}
                }

            payload = {
                "contents": [{"parts": [part_obj]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": speech_cfg,
                },
            }
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"Gemini 原生接口错误 ({resp.status}): {err[:300]}")
                data = await resp.json()

            pcm_bytes = self._extract_pcm_from_candidates(data)
            wav_bytes = _pcm_to_wav_bytes(pcm_bytes)
            out_path = self._temp_file_path("wav")
            Path(out_path).write_bytes(wav_bytes)
            return out_path, meta

        if use_mode == "gemini_stream":
            url = f"{base}/v1beta/models/{use_model}:streamGenerateContent?alt=sse"
            headers = {
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            }
            payload = {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": use_voice}}
                    },
                },
            }
            chunks: list[bytes] = []
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"Gemini 流式接口错误 ({resp.status}): {err[:300]}")
                buf = ""
                async for raw_chunk in resp.content.iter_chunked(8192):
                    buf += raw_chunk.decode("utf-8", errors="ignore")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            chunk_json = json.loads(data_str)
                            pcm_part = self._extract_pcm_from_candidates(chunk_json)
                            if pcm_part:
                                chunks.append(pcm_part)
                        except Exception:
                            continue
                if buf.strip().startswith("data:"):
                    data_str = buf.strip()[5:].strip()
                    if data_str and data_str != "[DONE]":
                        try:
                            chunk_json = json.loads(data_str)
                            pcm_part = self._extract_pcm_from_candidates(chunk_json)
                            if pcm_part:
                                chunks.append(pcm_part)
                        except Exception:
                            pass

            if not chunks:
                raise RuntimeError("Gemini 流式接口未返回任何音频分块。")
            wav_bytes = _pcm_to_wav_bytes(b"".join(chunks))
            out_path = self._temp_file_path("wav")
            Path(out_path).write_bytes(wav_bytes)
            return out_path, meta

        if use_mode == "interactions":
            url = f"{base}/v1/interactions"
            headers = {
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            }
            payload = {
                "model": use_model,
                "input": text,
                "response_format": {"type": "audio", "mime_type": "audio/wav"},
                "generation_config": {"speech_config": [{"voice": use_voice}]},
                "stream": False,
            }
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"Interactions 接口错误 ({resp.status}): {err[:300]}")
                data = await resp.json()

            steps = data.get("steps") or []
            audio_item = None
            for step in steps:
                for content_item in step.get("content") or []:
                    if content_item.get("type") == "audio" and content_item.get("data"):
                        audio_item = content_item
                        break
                if audio_item:
                    break
            if not audio_item:
                raise RuntimeError(f"Interactions 返回中未找到音频数据: {str(data)[:300]}")

            raw_audio = base64.b64decode(audio_item["data"])
            sample_rate = int(audio_item.get("sample_rate") or 24000)
            channels = int(audio_item.get("channels") or 1)
            wav_bytes = _pcm_to_wav_bytes(raw_audio, channels=channels, sample_width=2, sample_rate=sample_rate)
            out_path = self._temp_file_path("wav")
            Path(out_path).write_bytes(wav_bytes)

            if data.get("usage"):
                meta["usage"] = data["usage"]
            return out_path, meta

        raise ValueError(f"未知的调用方式: {use_mode}")

    @staticmethod
    def _extract_pcm_from_candidates(data: dict[str, Any]) -> bytes:
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"响应中缺少 candidates: {str(data)[:300]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        collected = []
        for p in parts:
            media = p.get("inlineData") or p.get("inline_data")
            if media and media.get("data"):
                collected.append(base64.b64decode(media["data"]))
        if not collected:
            raise RuntimeError(f"响应 parts 中未包含音频 inlineData: {str(data)[:300]}")
        return b"".join(collected)

    async def _synthesize_dialogue(
        self,
        script_text: str,
        *,
        model: str | None = None,
        mode: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        """
        多说话人对话合成（文档方式② 4.2 & 4.3）：
        支持每行：
          角色: 台词
          角色[音色]: 台词
          角色[音色|风格指令]: 台词
        """
        key = self._api_key()
        if not key:
            raise ValueError("尚未配置 API Key，请前往 AstrBot 插件配置页填写 `api_key`。")

        line_re = re.compile(
            r"^\s*([^:\[\]：]+?)(?:\[([^\]|]+?)(?:\|([^\]]+))?\])?\s*[:：]\s*(.+?)\s*$"
        )
        speaker_voices = self._speaker_voice_map()
        fallback_pool = [self._default_voice(), "Kore", "Fenrir", "Puck", "Charon", "Aoede"]
        fallback_idx = 0

        entries: list[dict[str, str]] = []
        used_voices: dict[str, str] = {}
        has_style = False

        for raw_line in script_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = line_re.match(line)
            if not m:
                raise ValueError(
                    f"格式无法解析：「{line}」\n请使用 `角色: 台词` 或 `角色[音色|风格]: 台词` 格式，每句一行。"
                )
            speaker = m.group(1).strip()
            explicit_voice = (m.group(2) or "").strip()
            explicit_style = (m.group(3) or "").strip()
            utterance = m.group(4).strip()

            if explicit_voice:
                used_voices[speaker] = self._normalize_voice(explicit_voice)
            elif speaker not in used_voices:
                if speaker in speaker_voices:
                    used_voices[speaker] = speaker_voices[speaker]
                else:
                    used_voices[speaker] = self._normalize_voice(
                        fallback_pool[fallback_idx % len(fallback_pool)]
                    )
                    fallback_idx += 1

            if explicit_style:
                has_style = True

            entries.append(
                {
                    "speaker": speaker,
                    "text": utterance,
                    "style": explicit_style,
                }
            )

        if not entries:
            raise ValueError("对话文本不能为空。")

        use_model = model or self._default_model()
        dialogue_mode = mode or self._cfg_str("multi_speaker_mode", "CONVERSATIONAL")
        if dialogue_mode not in {"CONVERSATIONAL", "VERBATIM"}:
            dialogue_mode = "CONVERSATIONAL"

        if has_style:
            parts = []
            for e in entries:
                part: dict[str, Any] = {"text": e["text"]}
                meta_obj: dict[str, str] = {"speaker": e["speaker"]}
                if e["style"]:
                    meta_obj["style"] = e["style"]
                part["speechMetadata"] = meta_obj
                parts.append(part)
        else:
            joined_script = "\n".join(f"{e['speaker']}: {e['text']}" for e in entries)
            parts = [{"text": joined_script}]

        speaker_voice_configs = [
            {
                "speaker": spk,
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v}},
            }
            for spk, v in used_voices.items()
        ]

        url = f"{self._api_base()}/v1beta/models/{use_model}:generateContent"
        headers = {
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        }
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "multiSpeakerVoiceConfig": {
                        "mode": dialogue_mode,
                        "speakerVoiceConfigs": speaker_voice_configs,
                    }
                },
            },
        }

        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec())
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"多说话人接口错误 ({resp.status}): {err[:300]}")
            data = await resp.json()

        pcm_bytes = self._extract_pcm_from_candidates(data)
        wav_bytes = _pcm_to_wav_bytes(pcm_bytes)
        out_path = self._temp_file_path("wav")
        Path(out_path).write_bytes(wav_bytes)
        return out_path, used_voices

    # ==================== 指令区域 ====================

    @filter.command("tts", alias={"语音", "朗读", "配音"})
    async def cmd_tts(self, event: AstrMessageEvent):
        """将文本合成为语音。支持参数：-v 音色 -s 语速 -i \"风格\" -m 模型 --mode 调用方式"""
        body = self._extract_command_body(event, ("tts", "语音", "朗读", "配音"))
        opts, text = self._parse_tts_flags(body)
        if not text:
            yield event.plain_result(
                "用法：/tts 要朗读的文本\n"
                "可选参数：-v 音色  -s 语速(如0.9)  -i \"风格指令\"  -m 模型  --mode (openai/gemini_native/gemini_stream/interactions)\n"
                "示例：/tts -v Puck -s 0.9 -i \"温柔慢速\" 你好，这是语音测试。"
            )
            return

        try:
            audio_path, meta = await self._synthesize_single(
                text,
                voice=opts.get("voice"),
                style=opts.get("style"),
                speed=opts.get("speed"),
                model=opts.get("model"),
                mode=opts.get("mode"),
                response_format=opts.get("response_format"),
            )
            yield event.chain_result([Record.fromFileSystem(audio_path)])
            if self._cfg_bool("show_usage", False) and meta.get("usage"):
                u = meta["usage"]
                yield event.plain_result(
                    f"[Usage] input={u.get('total_input_tokens')}, "
                    f"output={u.get('total_output_tokens')}, total={u.get('total_tokens')}"
                )
        except Exception as e:
            logger.error(f"[GeminiTTS] 合成失败: {e}")
            yield event.plain_result(f"语音合成失败：{e}")

    @filter.command("对话tts", alias={"dialogue", "对话语音", "多角色配音"})
    async def cmd_dialogue_tts(self, event: AstrMessageEvent):
        """多角色对话语音合成。每行格式：角色[音色|风格]: 台词"""
        body = self._extract_command_body(event, ("对话tts", "dialogue", "对话语音", "多角色配音"))
        if not body:
            yield event.plain_result(
                "用法：/对话tts（换行写台词，格式为 `角色: 台词` 或 `角色[音色|风格]: 台词`）\n"
                "示例：\n"
                "/对话tts\n"
                "Dad[Fenrir]: Good morning, Lingling.\n"
                "Lingling[Kore|cheerful]: Good morning, Dad!"
            )
            return

        try:
            audio_path, _ = await self._synthesize_dialogue(body)
            yield event.chain_result([Record.fromFileSystem(audio_path)])
        except Exception as e:
            logger.error(f"[GeminiTTS] 对话合成失败: {e}")
            yield event.plain_result(f"对话语音合成失败：{e}")

    @filter.command("tts音色", alias={"音色列表", "voices"})
    async def cmd_voices(self, event: AstrMessageEvent, arg: str = ""):
        """查看内置 70 个音色列表，或加「在线」实时查询接口音色"""
        arg_clean = (arg or "").strip()
        if arg_clean in {"在线", "online", "刷新", "refresh"}:
            model = self._default_model()
            voices, err = await self._fetch_voices_api(model)
            if err:
                yield event.plain_result(f"在线查询音色失败：{err}")
                return
            yield event.plain_result(
                f"模型 {model} 在线音色列表（共 {len(voices)} 个）：\n" + ", ".join(voices)
            )
            return

        current = self._default_voice()
        msg = (
            f"当前默认音色：{current}\n\n"
            f"【新一代自然音色（{len(NEW_NATURAL_VOICES)} 个）】\n"
            + ", ".join(NEW_NATURAL_VOICES)
            + f"\n\n【经典天体名音色（{len(CLASSIC_CELESTIAL_VOICES)} 个）】\n"
            + ", ".join(CLASSIC_CELESTIAL_VOICES)
            + "\n\n常用起点：Kore（女声/中文自然）、Puck（中性/童声）、Fenrir（偏男声）\n"
            + "切换命令：/tts切换音色 <音色名>｜在线拉取：/tts音色 在线"
        )
        yield event.plain_result(msg)

    @filter.command("tts切换音色", alias={"切换音色", "setvoice"})
    async def cmd_set_voice(self, event: AstrMessageEvent, voice_name: str = ""):
        """切换默认音色并保存配置"""
        if not self._check_config_perm(event):
            yield event.plain_result("当前插件已开启仅管理员修改设置，无权切换全局音色。")
            return
        target = (voice_name or "").strip()
        if not target:
            yield event.plain_result(f"当前默认音色：{self._default_voice()}\n用法：/tts切换音色 <音色名>")
            return

        matched = VOICE_LOWER_MAP.get(target.lower())
        if matched:
            self.config["voice"] = matched
            self.config["custom_voice"] = ""
            await self._save_config()
            yield event.plain_result(f"默认音色已切换为：{matched}")
        else:
            # 允许使用自定义新音色（写入 custom_voice）
            self.config["custom_voice"] = target
            await self._save_config()
            yield event.plain_result(
                f"未在内置 70 音色列表中匹配到「{target}」，已将其写入自定义音色（custom_voice）并生效。"
            )

    @filter.command("tts模式", alias={"tts_mode"})
    async def cmd_set_mode(self, event: AstrMessageEvent, mode: str = ""):
        """切换调用方式：openai / gemini_native / gemini_stream / interactions"""
        if not self._check_config_perm(event):
            yield event.plain_result("当前插件已开启仅管理员修改设置，无权切换调用方式。")
            return
        m = (mode or "").strip()
        if m not in AVAILABLE_MODES:
            yield event.plain_result(
                f"当前调用方式：{self._default_mode()}\n"
                f"可选值：{' / '.join(AVAILABLE_MODES)}\n"
                f"用法：/tts模式 <方式名>"
            )
            return
        self.config["api_mode"] = m
        await self._save_config()
        yield event.plain_result(f"默认调用方式已切换为：{m}")

    @filter.command("tts自动", alias={"tts_auto"})
    async def cmd_toggle_auto(self, event: AstrMessageEvent, action: str = ""):
        """开启或关闭 AI 回复自动转语音"""
        if not self._check_config_perm(event):
            yield event.plain_result("当前插件已开启仅管理员修改设置，无权开关自动语音。")
            return
        act = (action or "").strip().lower()
        if act in {"开", "开启", "on", "true", "1"}:
            self.config["auto_tts_enable"] = True
            await self._save_config()
            yield event.plain_result("已开启 AI 回复自动转语音。")
        elif act in {"关", "关闭", "off", "false", "0"}:
            self.config["auto_tts_enable"] = False
            await self._save_config()
            yield event.plain_result("已关闭 AI 回复自动转语音。")
        else:
            state = "开启" if self._cfg_bool("auto_tts_enable", False) else "关闭"
            yield event.plain_result(f"当前自动转语音状态：{state}\n用法：/tts自动 开 或 /tts自动 关")

    @filter.command("tts模型", alias={"tts状态", "tts_status"})
    async def cmd_status(self, event: AstrMessageEvent, arg: str = ""):
        """查看当前 Gemini TTS 配置状态，加「在线」可测试接口连通性并拉取可用模型"""
        arg_clean = (arg or "").strip()
        if arg_clean in {"在线", "online", "测试", "test"}:
            models, err = await self._fetch_models_api()
            if err:
                yield event.plain_result(f"接口连通性测试失败：{err}")
                return
            tts_models = [m for m in models if "tts" in m.lower()]
            display = tts_models if tts_models else models
            yield event.plain_result(
                f"接口连通正常！共发现 {len(models)} 个模型，TTS 相关模型：\n" + "\n".join(display)
            )
            return

        status_msg = (
            "【Gemini TTS 当前配置】\n"
            f"接口地址：{self._api_base()}\n"
            f"API Key：{_mask_key(self._api_key())}\n"
            f"当前模型：{self._default_model()}\n"
            f"当前音色：{self._default_voice()}\n"
            f"调用方式：{self._default_mode()}\n"
            f"输出格式：{self._default_format()}（OpenAI模式）\n"
            f"默认语速：{self._default_speed()}\n"
            f"默认风格：{self._cfg_str('default_style', '') or '（无）'}\n"
            f"自动转语音：{'开启' if self._cfg_bool('auto_tts_enable', False) else '关闭'}\n"
            f"AI 主动工具：{'开启' if self._cfg_bool('enable_llm_tool', True) else '关闭'}\n"
            "提示：发送 `/tts模型 在线` 可测试接口连通性。"
        )
        yield event.plain_result(status_msg)

    @filter.command("tts帮助", alias={"tts_help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 Gemini TTS 插件全部命令说明"""
        yield event.plain_result(
            "【Gemini TTS 插件指令】\n"
            "1. /tts [参数] 文本 —— 合成语音（可用 /语音 /朗读 /配音）\n"
            "   参数：-v 音色  -s 语速  -i \"风格\"  -m 模型  --mode 模式\n"
            "2. /对话tts —— 多角色对话合成（每行 `角色[音色|风格]: 台词`）\n"
            "3. /tts音色 [在线] —— 查看 70 个可用音色或在线拉取\n"
            "4. /tts切换音色 <音色名> —— 切换默认音色\n"
            "5. /tts模式 <方式> —— 切换 openai/gemini_native/gemini_stream/interactions\n"
            "6. /tts自动 <开|关> —— 开关 AI 回复自动转语音\n"
            "7. /tts模型 [在线] —— 查看当前配置或测试接口连通性"
        )

    # ==================== LLM 工具与自动转语音 ====================

    @filter.llm_tool(name="gemini_tts_speak")
    async def gemini_tts_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        voice: str = "",
        style: str = "",
        speed: float = 1.0,
    ) -> str:
        """将指定文本合成为语音并直接发送给用户。当用户明确要求用语音回复、朗读某段文字或指定某种音色说话时调用。

        Args:
            text(string): 要合成为语音的文本内容。
            voice(string): 可选音色名，如 Kore、Puck、Fenrir、Charon、Aoede、Leda 等；留空则使用默认音色。
            style(string): 可选风格指令（如语速、语气、口音），留空则使用默认设置。
            speed(number): 语速倍数，如 0.7、1.0、1.5，默认 1.0。
        """
        if not self._cfg_bool("enable_llm_tool", True):
            return "Gemini TTS 工具当前已在插件配置中关闭。"
        clean = _clean_text_for_tts(text)
        if not clean:
            return "合成内容为空，未发送语音。"
        try:
            use_voice = voice.strip() if voice and voice.strip() else None
            use_style = style.strip() if style and style.strip() else None
            use_speed = float(speed) if speed else self._default_speed()
            audio_path, meta = await self._synthesize_single(
                clean,
                voice=use_voice,
                style=use_style,
                speed=use_speed,
            )
            await event.send(MessageChain([Record.fromFileSystem(audio_path)]))
            info = f"已生成并发送语音（模型: {meta.get('model')}，音色: {meta.get('voice')}）。"
            if self._cfg_bool("show_usage", False) and meta.get("usage"):
                u = meta["usage"]
                info += f" token用量: in={u.get('total_input_tokens')}, out={u.get('total_output_tokens')}."
            return info
        except Exception as e:
            logger.error(f"[GeminiTTS] LLM 工具调用失败: {e}")
            return f"语音合成失败：{e}"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """开启 auto_tts_enable 后，将 LLM 生成的文本回复自动合成为语音。"""
        if not self._cfg_bool("auto_tts_enable", False):
            return
        if not self._api_key():
            return

        result = event.get_result()
        if not result or not result.chain:
            return
        if not result.is_llm_result():
            return
        if any(isinstance(c, Record) for c in result.chain):
            return

        text_parts = [c.text for c in result.chain if isinstance(c, Plain) and c.text]
        clean_text = _clean_text_for_tts("".join(text_parts))
        min_len = max(1, self._cfg_int("auto_tts_min_len", 2))
        max_len = max(min_len, self._cfg_int("auto_tts_max_len", 300))

        if len(clean_text) < min_len or len(clean_text) > max_len:
            return

        try:
            audio_path, _ = await self._synthesize_single(clean_text)
            record_comp = Record.fromFileSystem(audio_path)
            if self._cfg_bool("auto_tts_keep_text", True):
                result.chain.append(record_comp)
            else:
                new_chain = [c for c in result.chain if not isinstance(c, Plain)]
                new_chain.append(record_comp)
                result.chain = new_chain
        except Exception as e:
            logger.warning(f"[GeminiTTS] 自动转语音跳过（{e}）")
