"""Local speech: faster-whisper for voice notes in, piper for voice replies out.

Both run on this machine, so audio never goes to a third party. Adapted from
sir-vibe-a-lot (github.com/meinharrd/sir-vibe-a-lot), with a voice per
detected language.
"""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

log = logging.getLogger(__name__)

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
VOICES_DIR = Path(os.environ.get("VOICES_DIR", Path(__file__).resolve().parent.parent / "state" / "voices"))
VOICES = {"en": "en_US-lessac-medium", "de": "de_DE-thorsten-medium"}

_whisper = None
_piper: dict[str, object] = {}
_stt_lock = asyncio.Lock()
_tts_lock = asyncio.Lock()


def _transcribe_sync(path: str) -> tuple[str, str]:
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log.info("loading whisper %s", WHISPER_MODEL)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = _whisper.transcribe(path, vad_filter=True)
    return "".join(s.text for s in segments).strip(), info.language


async def transcribe(path: str) -> tuple[str, str]:
    """(text, language code) for an audio file."""
    async with _stt_lock:
        return await asyncio.to_thread(_transcribe_sync, path)


def guess_language(text: str) -> str:
    """'de' or 'en', from common function words; good enough to pick a voice."""
    words = re.findall(r"\w+", text.lower())
    de = sum(w in {"und", "der", "die", "das", "ist", "nicht", "ich", "du", "mit", "für", "auf", "ein", "eine"}
             for w in words)
    en = sum(w in {"and", "the", "is", "not", "you", "with", "for", "on", "a", "an", "of", "to"} for w in words)
    return "de" if de > en else "en"


def _voice(lang: str):
    name = VOICES.get(lang, VOICES["en"])
    if name not in _piper:
        from piper import PiperVoice
        path = VOICES_DIR / f"{name}.onnx"
        if not path.exists():
            raise FileNotFoundError(f"run: python -m piper.download_voices {name} --data-dir {VOICES_DIR}")
        _piper[name] = PiperVoice.load(str(path))
    return _piper[name]


def speakable(text: str, limit: int = 2500) -> str:
    """Strip Markdown and code so the speech sounds natural."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#`>|~]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _synthesize_sync(text: str, lang: str, out_ogg: str):
    voice = _voice(lang)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        with wave.open(wav_path, "wb") as w:
            voice.synthesize_wav(text, w)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
                        "-c:a", "libopus", "-b:a", "32k", "-application", "voip", out_ogg], check=True)
    finally:
        Path(wav_path).unlink(missing_ok=True)


async def synthesize(text: str, out_ogg: str, lang: str | None = None):
    """Render text to an OGG/Opus Telegram voice note."""
    async with _tts_lock:
        await asyncio.to_thread(_synthesize_sync, speakable(text), lang or guess_language(text), out_ogg)
