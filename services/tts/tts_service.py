# services/tts/tts_service.py
"""Multi-provider TTS service — dispatches to local Kokoro (ONNX/GPU), OpenAI-compatible API, or browser."""

import io
import wave
import logging
import hashlib
import httpx
from pathlib import Path
from typing import Optional, Dict, Any

from src.constants import TTS_CACHE_DIR

logger = logging.getLogger(__name__)


def _safe_speed(value, default: float = 1.0) -> float:
    """Parse the stored tts_speed defensively. The settings layer tolerates
    corrupt/agent-written config, so a non-numeric or empty value (e.g. an agent
    setting "speech speed" = "fast", or a hand-edited settings.json) must not
    crash synthesis or the stats endpoint with a ValueError."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if speed > 0 else default


class TTSService:
    """Multi-provider TTS service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        — no TTS
      "browser"         — client-side Web Speech API (no server synthesis)
      "local"           — kokoro-onnx on CPU/Apple Silicon (Metal via ONNX)
      "endpoint:<id>"   — OpenAI-compatible /audio/speech via ModelEndpoint
    """

    def __init__(self, cache_dir: str = TTS_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._kokoro = (
            None  # lazy-init (_KokoroPipeline — GPU/torch path, kept for CUDA)
        )
        self._kokoro_onnx = None  # lazy-init (_KokoroOnnxPipeline — CPU/Apple Silicon)

    # ── Settings ──

    def _load_settings(self) -> dict:
        from src.settings import load_settings

        saved = load_settings()
        return {
            "tts_enabled": saved.get("tts_enabled", True),
            "tts_provider": saved.get("tts_provider", "disabled"),
            "tts_model": saved.get("tts_model", "tts-1"),
            "tts_voice": saved.get("tts_voice", "alloy"),
            "tts_speed": saved.get("tts_speed", "1"),
        }

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return False
        provider = settings["tts_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "local":
            onnx = self._get_kokoro_onnx()
            return onnx is not None and onnx.available
        if provider.startswith("endpoint:"):
            return True  # assume reachable; errors surface at synthesis time
        return False

    # ── Cache ──

    def _cache_key(
        self, text: str, provider: str, model: str, voice: str, speed: float = 1.0
    ) -> str:
        raw = f"{provider}|{model}|{voice}|{speed}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[bytes]:
        for ext in (".mp3", ".wav"):
            path = self.cache_dir / f"{key}{ext}"
            if path.exists():
                return path.read_bytes()
        return None

    def _put_cache(self, key: str, data: bytes):
        ext = (
            ".mp3"
            if (
                len(data) >= 3
                and (
                    data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
                )
            )
            else ".wav"
        )
        (self.cache_dir / f"{key}{ext}").write_bytes(data)

    def clear_cache(self):
        count = 0
        for f in self.cache_dir.glob("*.*"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached TTS files")

    # ── Kokoro ONNX (local — CPU / Apple Silicon) ──

    def _get_kokoro_onnx(self):
        if self._kokoro_onnx is None:
            self._kokoro_onnx = _KokoroOnnxPipeline()
        return self._kokoro_onnx

    # ── API endpoint ──

    def _synthesize_api(
        self, text: str, endpoint_id: str, model: str, voice: str, speed: float = 1.0
    ) -> Optional[bytes]:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                logger.error(f"TTS endpoint {endpoint_id} not found")
                return None
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()

        url = base_url + "/audio/speech"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }

        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=60)
            r.raise_for_status()
            logger.info(f"API TTS: {len(r.content)} bytes from {base_url}")
            return r.content
        except Exception as e:
            logger.error(f"API TTS synthesis failed: {e}")
            return None

    # ── Public interface ──

    def synthesize(self, text: str, use_cache: bool = True) -> Optional[bytes]:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return None
        provider = settings["tts_provider"]
        model = settings["tts_model"]
        voice = settings["tts_voice"]
        speed = _safe_speed(settings.get("tts_speed", "1"))

        if provider in ("disabled", "browser"):
            return None

        if len(text) > 5000:
            text = text[:5000]

        if use_cache:
            key = self._cache_key(text, provider, model, voice, speed)
            cached = self._get_cached(key)
            if cached:
                logger.info(f"TTS cache hit ({len(text)} chars)")
                return cached

        audio_data = None

        if provider == "local":
            onnx = self._get_kokoro_onnx()
            if onnx and onnx.available:
                audio_data = onnx.synthesize_raw(text, voice)
            else:
                logger.warning("kokoro-onnx TTS not available")
                return None
        elif provider.startswith("endpoint:"):
            endpoint_id = provider.split(":", 1)[1]
            audio_data = self._synthesize_api(text, endpoint_id, model, voice, speed)
        else:
            logger.error(f"Unknown TTS provider: {provider}")
            return None

        if audio_data and use_cache:
            key = self._cache_key(text, provider, model, voice, speed)
            self._put_cache(key, audio_data)

        return audio_data

    def synthesize_to_base64(self, text: str) -> Optional[str]:
        import base64

        audio = self.synthesize(text)
        if audio:
            return base64.b64encode(audio).decode("utf-8")
        return None

    def set_voice(self, voice: str):
        """Legacy no-op — voice is now managed via admin settings."""

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = settings["tts_provider"]
        tts_enabled = settings.get("tts_enabled", True)

        cache_files = list(self.cache_dir.glob("*.wav")) + list(
            self.cache_dir.glob("*.mp3")
        )
        cache_size = sum(f.stat().st_size for f in cache_files)

        is_available = self.available and tts_enabled
        stats = {
            "available": is_available,
            "ready": is_available,
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            "cache_entries": len(cache_files),
            "cache_size_mb": round(cache_size / (1024 * 1024), 2),
        }

        if provider == "local":
            onnx = self._get_kokoro_onnx()
            stats["model"] = (
                "kokoro-onnx (ONNX)"
                if (onnx and onnx.available)
                else "kokoro-onnx (not loaded)"
            )
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]

        return stats


class _KokoroOnnxPipeline:
    """Encapsulates the kokoro-onnx pipeline — runs on CPU or Apple Silicon via ONNX Runtime."""

    # Default sample rate produced by kokoro-onnx
    SAMPLE_RATE = 24000

    # Search order for the ONNX model files. The pipecat cache location is
    # checked first because that's where the user's pre-downloaded weights live
    # (~/.cache/pipecat/kokoro-onnx/). The project root is a fallback for
    # manually placed files.
    _SEARCH_DIRS = [
        Path.home() / ".cache" / "pipecat" / "kokoro-onnx",
        Path("."),
    ]

    def __init__(self):
        self.pipeline = None
        self.available = False
        self._init()

    @classmethod
    def _find_model_files(cls) -> tuple[Optional[Path], Optional[Path]]:
        """Return (onnx_path, voices_path) from the first directory that has both."""
        for d in cls._SEARCH_DIRS:
            onnx = d / "kokoro-v1.0.onnx"
            voices = d / "voices-v1.0.bin"
            if onnx.exists() and voices.exists():
                return onnx, voices
        return None, None

    def _init(self):
        try:
            from kokoro_onnx import Kokoro
        except ImportError as e:
            logger.warning(f"kokoro-onnx not available: {e}")
            logger.warning("Install with: pip install kokoro-onnx")
            return

        onnx_path, voices_path = self._find_model_files()
        if onnx_path is None:
            searched = ", ".join(str(d) for d in self._SEARCH_DIRS)
            logger.warning(
                f"kokoro-onnx model files not found (searched: {searched}). "
                "Expected kokoro-v1.0.onnx + voices-v1.0.bin in "
                "~/.cache/pipecat/kokoro-onnx/ or the project root."
            )
            return

        try:
            import os as _os
            import platform as _platform

            # On Apple Silicon, use CoreML (ANE/GPU) via explicit SessionOptions so
            # we can set ModelCacheDirectory to a writable path — bypassing the default
            # /var/folders temp dir that macOS sandbox may deny.
            # Use CoreML path when: Apple Silicon AND either no provider is
            # explicitly set, OR the user has explicitly requested CoreML.
            # In both cases we supply ModelCacheDirectory so the compiled model
            # goes to a writable path instead of /var/folders (which macOS may deny).
            _existing_provider = _os.environ.get("ONNX_PROVIDER", "")
            _use_coreml = (
                _platform.machine() == "arm64"
                and _platform.system() == "Darwin"
                and _existing_provider in ("", "CoreMLExecutionProvider")
            )
            if _use_coreml:
                import onnxruntime as _rt

                _cache_dir = str(Path.home() / ".cache" / "odysseus" / "coreml")
                _os.makedirs(_cache_dir, exist_ok=True)
                _providers = [
                    ("CoreMLExecutionProvider", {"ModelCacheDirectory": _cache_dir}),
                    "CPUExecutionProvider",
                ]
                try:
                    _sess = _rt.InferenceSession(
                        str(onnx_path),
                        providers=_providers,
                    )
                    self.pipeline = Kokoro.from_session(_sess, str(voices_path))
                    self.available = True
                    logger.info(
                        f"kokoro-onnx TTS pipeline loaded (CoreML, cache={_cache_dir}) "
                        f"from {onnx_path.parent}"
                    )
                except Exception as cml_err:
                    logger.warning(
                        f"kokoro-onnx CoreML init failed ({cml_err}), falling back to CPU"
                    )
                    self.pipeline = Kokoro(str(onnx_path), str(voices_path))
                    self.available = True
                    logger.info(
                        f"kokoro-onnx TTS pipeline loaded (CPU fallback) from {onnx_path.parent}"
                    )
            else:
                self.pipeline = Kokoro(str(onnx_path), str(voices_path))
                self.available = True
                logger.info(f"kokoro-onnx TTS pipeline loaded from {onnx_path.parent}")
        except Exception as e:
            logger.error(f"kokoro-onnx init failed: {e}", exc_info=True)

    # Max characters per chunk passed to Kokoro.create().
    # kokoro-onnx truncates phonemes at 510 tokens and then crashes with
    # IndexError: index 510 is out of bounds for axis 0 with size 510
    # when voice[len(tokens)] is called with len == 510 (off-by-one in the lib).
    # ~200 chars produces ~500 phonemes on average English text — safe headroom.
    _MAX_CHUNK_CHARS = 200

    @staticmethod
    def _split_sentences(text: str):
        """Split text on sentence boundaries to stay within Kokoro's phoneme limit."""
        import re

        # Split on sentence-ending punctuation followed by whitespace
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p.strip() for p in parts if p.strip()]

    def synthesize_raw(self, text: str, voice: str = "af_heart") -> Optional[bytes]:
        """Synthesize text to WAV bytes using kokoro-onnx.

        Long texts are split into sentence-sized chunks before passing to
        Kokoro.create() to avoid the IndexError that occurs when the phoneme
        count hits exactly MAX_PHONEME_LENGTH (510) — an off-by-one bug in
        kokoro-onnx where voice[len(tokens)] crashes at the boundary.
        """
        if not self.available:
            return None
        try:
            import numpy as np

            sentences = self._split_sentences(text)
            if not sentences:
                return None

            # Further split any sentence longer than _MAX_CHUNK_CHARS by words
            chunks = []
            for sent in sentences:
                if len(sent) <= self._MAX_CHUNK_CHARS:
                    chunks.append(sent)
                else:
                    words = sent.split()
                    current = ""
                    for word in words:
                        if (
                            current
                            and len(current) + 1 + len(word) > self._MAX_CHUNK_CHARS
                        ):
                            chunks.append(current)
                            current = word
                        else:
                            current = (current + " " + word).strip()
                    if current:
                        chunks.append(current)

            sample_rate = self.SAMPLE_RATE
            audio_parts = []
            for chunk in chunks:
                try:
                    samples, sr = self.pipeline.create(
                        chunk, voice=voice, speed=1.0, lang="en-us"
                    )
                    if samples is not None and len(samples) > 0:
                        sample_rate = sr
                        audio_parts.append(samples)
                except Exception as chunk_err:
                    logger.warning(f"kokoro-onnx chunk failed (skipping): {chunk_err}")
                    continue

            if not audio_parts:
                return None

            samples = (
                np.concatenate(audio_parts) if len(audio_parts) > 1 else audio_parts[0]
            )

            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes((samples * 32767).astype(np.int16).tobytes())
            return buf.getvalue()
        except Exception as e:
            logger.error(f"kokoro-onnx synthesis failed: {e}", exc_info=True)
            return None


# Module-level singleton
_tts_service = None


def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
