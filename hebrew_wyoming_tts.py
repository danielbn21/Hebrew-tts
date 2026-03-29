"""
Hebrew/English Wyoming TTS Server
Run with: uv run python hebrew_wyoming_tts.py

Voices are auto-discovered from the voices/ directory:
  voices/he/shaul.onnx + shaul.onnx.json
  voices/en/joe.onnx   + joe.onnx.json

The client sends per-language settings in the event data:
  he_voice  — key of the Hebrew voice to use (e.g. "he/shaul")
  en_voice  — key of the English voice to use (e.g. "en/joe")
  he_speed  — length_scale for Hebrew segments (lower = faster, default 1.0)
  en_speed  — length_scale for English segments (lower = faster, default 1.0)
"""

import asyncio
import logging
import numpy as np
from pathlib import Path

from wyoming.server import AsyncServer, AsyncEventHandler
from wyoming.tts import Synthesize
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import Describe, Info, TtsProgram, TtsVoice, Attribution
from phonikud_tts import Phonikud, phonemize, Piper

_LOGGER = logging.getLogger(__name__)

PHONIKUD_MODEL   = "/root/phonikud-tts/phonikud-1.0.int8.onnx"
VOICES_DIR       = Path("/root/phonikud-tts/voices")
DEFAULT_HE_VOICE = "he/shaul"
DEFAULT_EN_VOICE = "en/joe"
DEFAULT_SPEED    = 1.0


def split_segments(text):
    """Split mixed Hebrew/English text into [(segment, lang), ...] tuples."""
    segments     = []
    current      = []
    current_lang = None

    for char in text:
        if '\u0590' <= char <= '\u05FF':
            lang = "he"
        elif char.isascii() and char.isalpha():
            lang = "en"
        else:
            lang = current_lang or "en"

        if lang != current_lang and current_lang is not None:
            seg = "".join(current).strip()
            if seg:
                segments.append((seg, current_lang))
            current = []

        current_lang = lang
        current.append(char)

    if current:
        seg = "".join(current).strip()
        if seg:
            segments.append((seg, current_lang or "en"))

    return segments


def discover_voices(voices_dir):
    """
    Scan voices_dir for language subfolders containing .onnx files.
    Returns dict: { "he/shaul": {"lang": "he", "name": "shaul", "piper": Piper(...)} }
    """
    voices = {}
    for lang_dir in sorted(voices_dir.iterdir()):
        if not lang_dir.is_dir():
            continue
        lang = lang_dir.name
        for onnx_file in sorted(lang_dir.glob("*.onnx")):
            config_file = onnx_file.with_suffix(".onnx.json")
            if not config_file.exists():
                _LOGGER.warning(f"No config for {onnx_file}, skipping")
                continue
            key = f"{lang}/{onnx_file.stem}"
            _LOGGER.info(f"Loading voice: {key}")
            voices[key] = {
                "lang":  lang,
                "name":  onnx_file.stem,
                "piper": Piper(str(onnx_file), str(config_file)),
            }
            _LOGGER.info(f"Loaded: {key}")
    return voices


class HebrewTTSHandler(AsyncEventHandler):
    def __init__(self, phonikud, voices, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phonikud = phonikud
        self.voices   = voices
        # Fallback defaults if client sends no voice preference
        self.default_voice = {
            "he": DEFAULT_HE_VOICE if DEFAULT_HE_VOICE in voices
                  else next((k for k, v in voices.items() if v["lang"] == "he"), None),
            "en": DEFAULT_EN_VOICE if DEFAULT_EN_VOICE in voices
                  else next((k for k, v in voices.items() if v["lang"] == "en"), None),
        }
        _LOGGER.info(f"Default voices: {self.default_voice}")

    def get_piper(self, lang, requested_voice):
        """Return (piper, voice_key) for requested voice, falling back to language default."""
        # Exact key match e.g. "he/shaul"
        if requested_voice in self.voices and self.voices[requested_voice]["lang"] == lang:
            return self.voices[requested_voice]["piper"], requested_voice
        # Short name match e.g. "shaul"
        for key, v in self.voices.items():
            if v["lang"] == lang and v["name"] == requested_voice:
                return v["piper"], key
        # Language default
        default_key = self.default_voice.get(lang)
        if default_key and default_key in self.voices:
            return self.voices[default_key]["piper"], default_key
        return None, None

    async def handle_event(self, event):

        # ── Describe: advertise available voices ───────────────────────────────
        if Describe.is_type(event.type):
            tts_voices = [
                TtsVoice(
                    name=key,
                    attribution=Attribution(name="phonikud-tts", url=""),
                    installed=True,
                    description=f"{v['lang'].upper()} - {v['name']}",
                    version=None,
                    languages=["he" if v["lang"] == "he" else "en"],
                )
                for key, v in self.voices.items()
            ]
            await self.write_event(Info(
                tts=[TtsProgram(
                    name="phonikud-tts",
                    attribution=Attribution(
                        name="thewh1teagle",
                        url="https://github.com/thewh1teagle/phonikud-tts"
                    ),
                    installed=True,
                    description="Hebrew/English TTS",
                    version=None,
                    voices=tts_voices,
                )]
            ).event())
            return True

        # ── Synthesize ─────────────────────────────────────────────────────────
        if Synthesize.is_type(event.type):
            synthesize = Synthesize.from_event(event)
            text       = synthesize.text

            # Read per-language settings sent by the client in event.data
            raw_data = event.data or {}
            he_voice = raw_data.get("he_voice", "")
            en_voice = raw_data.get("en_voice", "")
            he_speed = float(raw_data.get("he_speed", DEFAULT_SPEED))
            en_speed = float(raw_data.get("en_speed", DEFAULT_SPEED))

            _LOGGER.info(
                f"Text: '{text[:60]}' | "
                f"he_voice='{he_voice}' he_speed={he_speed} | "
                f"en_voice='{en_voice}' en_speed={en_speed}"
            )

            segments    = split_segments(text)
            all_samples = []
            final_rate  = 22050

            for seg_text, lang in segments:
                req_voice = he_voice if lang == "he" else en_voice
                speed     = he_speed if lang == "he" else en_speed
                piper, used_voice = self.get_piper(lang, req_voice)

                if piper is None:
                    _LOGGER.warning(f"No voice for lang={lang}, skipping segment")
                    continue

                _LOGGER.info(f"  [{lang}] {used_voice} speed={speed}: '{seg_text[:40]}'")

                if lang == "he":
                    with_diacritics = self.phonikud.add_diacritics(seg_text)
                    phones          = phonemize(with_diacritics)
                    samples, rate   = piper.create(
                        phones, is_phonemes=True, length_scale=speed
                    )
                else:
                    samples, rate = piper.create(seg_text, length_scale=speed)

                all_samples.append(np.array(samples))
                final_rate = rate

            if all_samples:
                combined = np.concatenate(all_samples)
                pcm      = (combined * 32767).astype(np.int16).tobytes()
            else:
                pcm = b""

            await self.write_event(
                AudioStart(rate=final_rate, width=2, channels=1).event()
            )
            chunk_size = 4096
            for i in range(0, len(pcm), chunk_size):
                await self.write_event(AudioChunk(
                    audio=pcm[i:i + chunk_size],
                    rate=final_rate, width=2, channels=1
                ).event())
            await self.write_event(AudioStop().event())
            return True

        return True


async def main():
    logging.basicConfig(level=logging.INFO)
    _LOGGER.info("Loading Phonikud model...")
    phonikud = Phonikud(PHONIKUD_MODEL)

    _LOGGER.info(f"Discovering voices in {VOICES_DIR}...")
    voices = discover_voices(VOICES_DIR)
    if not voices:
        _LOGGER.error("No voices found! Check your voices/ directory.")
        return
    _LOGGER.info(f"Loaded {len(voices)} voices: {list(voices.keys())}")

    server = AsyncServer.from_uri("tcp://0.0.0.0:10200")
    _LOGGER.info("Listening on port 10200")
    await server.run(
        lambda *a, **kw: HebrewTTSHandler(phonikud, voices, *a, **kw)
    )

asyncio.run(main())
