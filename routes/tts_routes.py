# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser).
"""

import asyncio
from functools import partial
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)


class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio" or "base64"


def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return tts_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text.

        Kokoro ONNX inference is CPU-bound and synchronous — run it in a
        thread-pool executor so the FastAPI event loop stays unblocked and
        can accept other requests (e.g. chat stream tokens) while synthesis runs.
        """
        try:
            if not tts_service.available:
                raise HTTPException(
                    status_code=503, detail={"message": "TTS service not available"}
                )

            loop = asyncio.get_event_loop()

            if request.format == "base64":
                audio_b64 = await loop.run_in_executor(
                    None, partial(tts_service.synthesize_to_base64, request.text)
                )
                if not audio_b64:
                    raise HTTPException(
                        status_code=500, detail={"message": "Synthesis failed"}
                    )
                return {"audio": audio_b64}

            else:  # audio format
                audio_data = await loop.run_in_executor(
                    None, partial(tts_service.synthesize, request.text)
                )
                if not audio_data:
                    raise HTTPException(
                        status_code=500, detail={"message": "Synthesis failed"}
                    )

                # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
                is_mp3 = audio_data[:3] == b"ID3" or (
                    len(audio_data) >= 2
                    and audio_data[0] == 0xFF
                    and (audio_data[1] & 0xE0) == 0xE0
                )
                mime = "audio/mpeg" if is_mp3 else "audio/wav"
                return Response(
                    content=audio_data,
                    media_type=mime,
                    headers={
                        "Content-Disposition": "inline; filename=speech.mp3"
                        if "mpeg" in mime
                        else "inline; filename=speech.wav"
                    },
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail={"message": f"Synthesis failed: {str(e)}"}
            )

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
