"""
Hebrew TTS Reader for Windows
Requirements: pip install keyboard pyperclip sounddevice soundfile numpy wyoming pystray pillow
Run as Administrator for global hotkey support.

Hotkeys (configurable in Settings):
  Ctrl+Alt+H  — Piper TTS (auto-fallback to Windows TTS if server unreachable)
  Ctrl+Alt+W  — Windows TTS (always local)

Both hotkeys:
  First press on new text  → fetch and play
  Same text, playing       → pause
  Same text, paused        → resume
  Different text           → stop current, start new
"""

import asyncio
import threading
import subprocess
import tempfile
import os
import logging
import sys
import json
import winreg
import tkinter as tk
from tkinter import ttk
import numpy as np
import sounddevice as sd
import soundfile as sf
import keyboard
import pyperclip
import pystray
from pystray import MenuItem as item, Menu
from PIL import Image, ImageDraw, ImageFont
from wyoming.client import AsyncClient
from wyoming.audio import AudioStart, AudioChunk, AudioStop
from wyoming.event import Event
from wyoming.info import Describe, Info

# ── Logging ────────────────────────────────────────────────────────────────────
_LOGGER = logging.getLogger(__name__)

def setup_logging(debug=False):
    """Configure logging level. Call once at startup and again when toggled."""
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler("hebrew_tts.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_FILE    = "hebrew_tts_config.json"
DEFAULT_CONFIG = {
    "lxc_host":       "192.168.1.244",
    "lxc_port":       10200,
    "piper_hotkey":   "ctrl+alt+h",
    "windows_hotkey": "ctrl+alt+w",
    # Piper — separate voice and speed per language
    # speed = length_scale: lower is faster (0.5 fast … 1.0 normal … 2.0 slow)
    "he_voice":       "",
    "he_speed":       1.0,
    "en_voice":       "",
    "en_speed":       1.0,
    # Windows TTS
    "windows_voice":  "",
    "windows_speed":  1.0,
    # Debug
    "debug_logging":  False,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                c = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                c.setdefault(k, v)
            return c
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(c):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(c, f, indent=2)
    except Exception as e:
        _LOGGER.error(f"Failed to save config: {e}")

cfg = load_config()
setup_logging(cfg.get("debug_logging", False))

# ── Playback state ─────────────────────────────────────────────────────────────
class State:
    IDLE    = "idle"
    PLAYING = "playing"
    PAUSED  = "paused"

state       = State.IDLE
audio_data  = None    # float32 numpy array — unified pipeline for both engines
play_pos    = 0       # current frame (enables frame-accurate pause/resume)
sample_rate = 22050
stream      = None    # active sd.OutputStream
last_text   = None
tray_icon   = None

# Available voices (populated at startup / on settings open)
piper_voices_he = []   # voice keys for Hebrew  e.g. ["he/shaul"]
piper_voices_en = []   # voice keys for English e.g. ["en/joe", "en/amy"]
windows_voices  = []   # Windows SAPI/OneCore display names

# ── Tray icon ──────────────────────────────────────────────────────────────────
def make_tray_image(color="green"):
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    palette = {"green": "#27ae60", "yellow": "#f39c12", "red": "#e74c3c", "blue": "#2980b9"}
    draw.ellipse([4, 4, 60, 60], fill=palette.get(color, "#27ae60"))
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    draw.text((14, 14), "א", fill="white", font=font)
    return img

def update_tray(color="green", tooltip=None):
    if tray_icon:
        tray_icon.icon = make_tray_image(color)
        if tooltip:
            tray_icon.title = tooltip

# ── Unified audio playback ─────────────────────────────────────────────────────
def make_stream():
    global play_pos, state

    def callback(outdata, frames, time_info, status):
        global play_pos, state
        remaining = len(audio_data) - play_pos
        if remaining <= 0:
            outdata[:] = 0
            state = State.IDLE
            update_tray("green", "Hebrew TTS — Ready")
            raise sd.CallbackStop()
        n = min(frames, remaining)
        outdata[:n, 0] = audio_data[play_pos:play_pos + n]
        if n < frames:
            outdata[n:] = 0
        play_pos += n

    return sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32",
                           callback=callback)

def start_playback():
    global stream, state
    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
    stream = make_stream()
    stream.start()
    state = State.PLAYING
    update_tray("yellow", "Hebrew TTS — Playing...")
    _LOGGER.info(f"Playback started at frame {play_pos}")

def pause_playback():
    global stream, state
    if stream is not None:
        try:
            stream.stop()
        except Exception:
            pass
    state = State.PAUSED
    update_tray("green", "Hebrew TTS — Paused")
    _LOGGER.info(f"Paused at frame {play_pos}")

def stop_playback():
    global stream, state, play_pos
    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        stream = None
    play_pos = 0
    state    = State.IDLE
    update_tray("green", "Hebrew TTS — Ready")


# ── Piper voice list from Wyoming server ───────────────────────────────────────
def fetch_piper_voices():
    global piper_voices_he, piper_voices_en
    try:
        async def _fetch():
            async with AsyncClient.from_uri(
                f"tcp://{cfg['lxc_host']}:{cfg['lxc_port']}"
            ) as client:
                await asyncio.wait_for(client.write_event(Describe().event()), timeout=5.0)
                event = await asyncio.wait_for(client.read_event(), timeout=5.0)
                if event and Info.is_type(event.type):
                    info = Info.from_event(event)
                    he, en = [], []
                    for prog in info.tts:
                        for v in prog.voices:
                            if "he" in v.languages:
                                he.append(v.name)
                            else:
                                en.append(v.name)
                    return he, en
            return [], []

        he, en = asyncio.run(_fetch())
        piper_voices_he = he
        piper_voices_en = en
        _LOGGER.info(f"Piper HE voices: {he}")
        _LOGGER.info(f"Piper EN voices: {en}")
    except Exception as e:
        _LOGGER.warning(f"Could not fetch Piper voices: {e}")

# ── Windows TTS — synthesize to WAV then play via sounddevice ─────────────────
def fetch_windows(text):
    global audio_data, sample_rate

    voice     = cfg.get("windows_voice", "")
    speed     = float(cfg.get("windows_speed", 1.0))
    sapi_rate = max(-10, min(10, int((speed - 1.0) * 10)))

    # Write text to a temp file — avoids ALL quoting/escaping issues
    txt_tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False,
                                          mode="w", encoding="utf-8")
    txt_tmp.write(text)
    txt_tmp.close()
    txt_file = txt_tmp.name.replace("\\", "\\\\")

    wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_tmp.close()
    wav_file = wav_tmp.name.replace("\\", "\\\\")

    safe_voice = voice.replace("'", "''")

    ps_script = f"""
Add-Type -AssemblyName System.Speech
$s    = New-Object System.Speech.Synthesis.SpeechSynthesizer
$text = [System.IO.File]::ReadAllText('{txt_file}')

if ('{safe_voice}' -ne '') {{
    try {{ $s.SelectVoice('{safe_voice}') }} catch {{
        try {{
            $reg    = 'HKLM:\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens'
            $tokens = Get-ChildItem $reg -ErrorAction Stop
            foreach ($t in $tokens) {{
                $name = (Get-ItemProperty $t.PSPath).'(default)'
                if ($name -eq '{safe_voice}') {{
                    $dest = 'HKLM:\\SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens\\' + $t.PSChildName
                    if (-not (Test-Path $dest)) {{ Copy-Item $t.PSPath $dest -Recurse }}
                    $s.SelectVoice('{safe_voice}')
                    break
                }}
            }}
        }} catch {{}}
    }}
}}

$s.Rate = {sapi_rate}
$s.Volume = 100
$s.SetOutputToWaveFile('{wav_file}')
$s.Speak($text)
$s.Dispose()
Remove-Item '{txt_file}' -ErrorAction SilentlyContinue
Write-Output "OK"
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=30
        )
        _LOGGER.info(f"Windows TTS: {result.stdout.strip()} | err: {result.stderr.strip()[:200]}")
    except Exception as e:
        _LOGGER.error(f"Windows TTS subprocess error: {e}")
        return False

    wav_real = wav_tmp.name
    if not os.path.exists(wav_real) or os.path.getsize(wav_real) < 100:
        _LOGGER.error("Windows TTS produced no output file")
        return False

    try:
        raw, rate = sf.read(wav_real, dtype="float32")
        os.unlink(wav_real)
    except Exception as e:
        _LOGGER.error(f"Failed to read WAV: {e}")
        return False

    if raw.ndim > 1:
        raw = raw.mean(axis=1)

    audio_data  = raw
    sample_rate = int(rate)
    _LOGGER.info(f"Windows TTS: {len(audio_data)/rate:.1f}s voice='{voice}' rate={sapi_rate}")
    return True

# ── Windows voice enumeration via registry (SAPI5 + OneCore hives) ─────────────
def get_windows_voices():
    voices = set()
    hives  = [
        r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
        r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens",
    ]
    for hive_path in hives:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path)
            i   = 0
            while True:
                try:
                    token_name = winreg.EnumKey(key, i)
                    token_key  = winreg.OpenKey(key, token_name)
                    try:
                        display, _ = winreg.QueryValueEx(token_key, "")
                        if display:
                            voices.add(display)
                    except FileNotFoundError:
                        pass
                    winreg.CloseKey(token_key)
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except Exception as e:
            _LOGGER.warning(f"Could not read {hive_path}: {e}")
    result = sorted(voices)
    _LOGGER.info(f"Windows voices ({len(result)}): {result}")
    return result

# ── Text grabbing ──────────────────────────────────────────────────────────────
def grab_selected_text(release_keys):
    for k in release_keys:
        keyboard.release(k)
    threading.Event().wait(0.1)

    # Run clipboard access in its own thread to avoid OpenClipboard errors
    # from being called inside the keyboard hook callback thread
    result = [None]
    error  = [None]

    def _do_clipboard():
        try:
            old_clip = pyperclip.paste()
            keyboard.send("ctrl+c")
            threading.Event().wait(0.3)
            # Retry clipboard read a few times in case it's briefly locked
            for _ in range(5):
                try:
                    new_text = pyperclip.paste().strip()
                    break
                except Exception:
                    threading.Event().wait(0.05)
            else:
                new_text = ""
            pyperclip.copy(old_clip)
            result[0] = new_text
        except Exception as e:
            error[0] = e
            result[0] = ""

    t = threading.Thread(target=_do_clipboard)
    t.start()
    t.join(timeout=3.0)

    if error[0]:
        _LOGGER.warning(f"Clipboard error: {error[0]}")
    return result[0] or ""


# ── Shared: handle new/same text logic ────────────────────────────────────────
def handle_text(new_text, fetch_fn, engine_label, tray_color):
    global state, play_pos, last_text, audio_data

    same_text = (new_text == last_text)

    if same_text or not new_text:
        # Toggle pause / resume on same text
        if state == State.PLAYING:
            pause_playback()
        elif state == State.PAUSED:
            start_playback()
        elif state == State.IDLE and audio_data is not None:
            play_pos = 0
            start_playback()
        return

    # New text
    stop_playback()
    last_text  = new_text
    audio_data = None
    update_tray(tray_color, f"Hebrew TTS — {engine_label} fetching...")

    def run():
        global audio_data, play_pos
        success = fetch_fn(new_text)
        if success and audio_data is not None:
            play_pos = 0
            start_playback()
        else:
            update_tray("red", f"Hebrew TTS — {engine_label} failed")

    threading.Thread(target=run, daemon=True).start()

# ── Streaming chunk player ─────────────────────────────────────────────────────
import queue
import re

# Playback queue — holds pre-synthesized numpy arrays ready to play
_audio_queue    = queue.Queue()
_fetch_thread   = None
_playback_thread = None
_pipeline_stop  = threading.Event()
_pipeline_pause = threading.Event()

def split_into_sentences(text, max_chars=300):
    """
    Split text into chunks for streaming.
    Cuts on: sentence endings, paragraph breaks, clause punctuation.
    Never cuts in the middle of a word — always on a space if hard cut is needed.
    """
    # First split on paragraph breaks
    paragraphs = re.split(r'\n\s*\n', text.strip())
    
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        # Split paragraph into sentences on . ! ? and Hebrew equivalents
        sentences = re.split(r'(?<=[.!?؟。\n])\s+', para)
        
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            if len(current) + len(sentence) <= max_chars:
                current += (" " if current else "") + sentence
            else:
                if current:
                    chunks.append(current.strip())
                
                # Sentence itself is longer than max_chars
                # Split on clause punctuation , ; :
                if len(sentence) > max_chars:
                    clauses = re.split(r'(?<=[,;:،])\s+', sentence)
                    current = ""
                    for clause in clauses:
                        if len(current) + len(clause) <= max_chars:
                            current += (" " if current else "") + clause
                        else:
                            if current:
                                chunks.append(current.strip())
                            # Clause still too long — cut at last space before max_chars
                            while len(clause) > max_chars:
                                cut = clause.rfind(' ', 0, max_chars)
                                if cut == -1:
                                    cut = max_chars  # no space found, hard cut
                                chunks.append(clause[:cut].strip())
                                clause = clause[cut:].strip()
                            current = clause
                else:
                    current = sentence
        
        if current:
            chunks.append(current.strip())

    return [c for c in chunks if c.strip()]

async def _synthesize_chunk(text):
    """Synthesize a single chunk via Piper. Returns numpy float32 array or None."""
    host     = cfg["lxc_host"]
    port     = cfg["lxc_port"]
    he_voice = cfg.get("he_voice", "")
    en_voice = cfg.get("en_voice", "")
    he_speed = float(cfg.get("he_speed", 1.0))
    en_speed = float(cfg.get("en_speed", 1.0))

    chunks = []
    rate   = 22050
    try:
        async with AsyncClient.from_uri(f"tcp://{host}:{port}") as client:
            synth_event = Event(type="synthesize", data={
                "text":     text,
                "he_voice": he_voice,
                "en_voice": en_voice,
                "he_speed": he_speed,
                "en_speed": en_speed,
            })
            await asyncio.wait_for(client.write_event(synth_event), timeout=5.0)
            while True:
                event = await asyncio.wait_for(client.read_event(), timeout=15.0)
                if event is None:
                    break
                if AudioStart.is_type(event.type):
                    rate = AudioStart.from_event(event).rate
                elif AudioChunk.is_type(event.type):
                    ev  = AudioChunk.from_event(event)
                    pcm = np.frombuffer(ev.audio, dtype=np.int16).astype(np.float32) / 32768.0
                    chunks.append(pcm)
                elif AudioStop.is_type(event.type):
                    break
    except Exception as e:
        _LOGGER.warning(f"Chunk synthesis failed: {e}")
        return None, None

    if not chunks:
        return None, None
    return np.concatenate(chunks), rate


def _fetch_pipeline(sentences):
    """Background thread: synthesize sentences one by one, push to _audio_queue."""
    for sentence in sentences:
        if _pipeline_stop.is_set():
            break
        _LOGGER.info(f"Fetching chunk: '{sentence[:50]}'")
        audio, rate = asyncio.run(_synthesize_chunk(sentence))
        if _pipeline_stop.is_set():
            break
        if audio is not None:
            _audio_queue.put((audio, rate))
            _LOGGER.info(f"Queued chunk {len(audio)/rate:.1f}s")
        else:
            _LOGGER.warning(f"Chunk failed, skipping: '{sentence[:40]}'")

    _audio_queue.put(None)  # sentinel — signals end of stream
    _LOGGER.info("Fetch pipeline done")


def _playback_pipeline():
    """Background thread: pull chunks from queue and play sequentially."""
    global audio_data, sample_rate, play_pos, stream, state

    while True:
        # Wait for next chunk (or pause)
        while _pipeline_pause.is_set() and not _pipeline_stop.is_set():
            threading.Event().wait(0.05)

        if _pipeline_stop.is_set():
            break

        try:
            item = _audio_queue.get(timeout=15.0)
        except queue.Empty:
            _LOGGER.warning("Playback pipeline timed out waiting for chunk")
            break

        if item is None:
            # End of stream
            break

        chunk_audio, chunk_rate = item

        if _pipeline_stop.is_set():
            break

        # Install as current audio_data and play
        audio_data  = chunk_audio
        sample_rate = chunk_rate
        play_pos    = 0
        start_playback()

        # Wait for this chunk to finish playing (or be stopped/paused)
        while True:
            if _pipeline_stop.is_set():
                stop_playback()
                return

            if _pipeline_pause.is_set():
                pause_playback()
                # Wait until resumed or stopped
                while _pipeline_pause.is_set() and not _pipeline_stop.is_set():
                    threading.Event().wait(0.05)
                if _pipeline_stop.is_set():
                    stop_playback()
                    return
                # Resume — restart the current chunk from where we paused
                start_playback()

            if state == State.IDLE:
                # Chunk finished naturally
                break

            threading.Event().wait(0.02)

    state = State.IDLE
    update_tray("green", "Hebrew TTS — Ready")
    _LOGGER.info("Playback pipeline done")


def start_streaming_pipeline(sentences):
    """Start fetch + playback pipelines for a list of sentence chunks."""
    global _fetch_thread, _playback_thread

    # Reset pipeline state
    _pipeline_stop.clear()
    _pipeline_pause.clear()
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break

    _fetch_thread = threading.Thread(
        target=_fetch_pipeline, args=(sentences,), daemon=True
    )
    _playback_thread = threading.Thread(
        target=_playback_pipeline, daemon=True
    )
    _fetch_thread.start()
    _playback_thread.start()


def stop_pipeline():
    """Stop both pipelines immediately."""
    _pipeline_stop.set()
    _pipeline_pause.clear()
    stop_playback()
    # Drain queue so fetch thread unblocks
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break


def pause_pipeline():
    _pipeline_pause.set()
    _LOGGER.info("Pipeline paused")


def resume_pipeline():
    _pipeline_pause.clear()
    _LOGGER.info("Pipeline resumed")
    

# ── Hotkey: Piper with auto-fallback to Windows TTS ───────────────────────────
def handle_piper_hotkey():
    global state, last_text, audio_data

    new_text  = grab_selected_text(["ctrl", "alt", "h"])
    same_text = (new_text == last_text)

    if same_text or not new_text:
        # Toggle pause/resume
        if state == State.PLAYING:
            pause_pipeline()
            state = State.PAUSED
            update_tray("green", "Hebrew TTS — Paused")
        elif state == State.PAUSED:
            resume_pipeline()
            state = State.PLAYING
            update_tray("yellow", "Hebrew TTS — Playing...")
        elif state == State.IDLE and last_text:
            # Replay from beginning
            sentences = split_into_sentences(last_text)
            stop_pipeline()
            update_tray("yellow", "Hebrew TTS — Fetching...")
            start_streaming_pipeline(sentences)
        return

    # New text
    stop_pipeline()
    last_text = new_text
    audio_data = None

    # Try Piper pipeline
    sentences = split_into_sentences(new_text)
    _LOGGER.info(f"Split into {len(sentences)} chunks: {[s[:30] for s in sentences]}")
    update_tray("yellow", "Hebrew TTS — Fetching...")
    state = State.PLAYING

    def run():
        # Test server reachability with first chunk
        first_audio, first_rate = asyncio.run(_synthesize_chunk(sentences[0]))
        if first_audio is None:
            _LOGGER.info("Piper unavailable — falling back to Windows TTS")
            update_tray("blue", "Hebrew TTS — Fetching (Windows TTS)...")
            success = fetch_windows(new_text)
            if success and audio_data is not None:
                global play_pos
                play_pos = 0
                start_playback()
            else:
                update_tray("red", "Hebrew TTS — Both engines failed")
            return

        # First chunk ready — push it and start pipeline for the rest
        stop_pipeline()
        _pipeline_stop.clear()
        _pipeline_pause.clear()
        while not _audio_queue.empty():
            try:
                _audio_queue.get_nowait()
            except queue.Empty:
                break

        _audio_queue.put((first_audio, first_rate))

        # Fetch remaining sentences in background
        remaining = sentences[1:]
        if remaining:
            t = threading.Thread(
                target=_fetch_pipeline, args=(remaining,), daemon=True
            )
            t.start()
        else:
            _audio_queue.put(None)  # sentinel if only one chunk

        # Start playback pipeline
        global _playback_thread
        _playback_thread = threading.Thread(
            target=_playback_pipeline, daemon=True
        )
        _playback_thread.start()

    threading.Thread(target=run, daemon=True).start()


# ── Hotkey: Windows TTS always ────────────────────────────────────────────────
def handle_windows_hotkey():
    new_text = grab_selected_text(["ctrl", "alt", "w"])
    handle_text(new_text, fetch_windows, "Windows TTS", "blue")

# ── Hotkey registration ────────────────────────────────────────────────────────
_registered_hotkeys = {}

def register_hotkeys():
    global _registered_hotkeys
    for hk in list(_registered_hotkeys.values()):
        try:
            keyboard.remove_hotkey(hk)
        except Exception:
            pass
    _registered_hotkeys = {}
    ph = cfg.get("piper_hotkey",   "ctrl+alt+h")
    wh = cfg.get("windows_hotkey", "ctrl+alt+w")
    try:
        _registered_hotkeys["piper"]   = keyboard.add_hotkey(ph, handle_piper_hotkey)
        _registered_hotkeys["windows"] = keyboard.add_hotkey(wh, handle_windows_hotkey)
        _LOGGER.info(f"Hotkeys: Piper={ph}  Windows={wh}")
    except Exception as e:
        _LOGGER.error(f"Hotkey registration failed: {e}")

# ── Settings window ────────────────────────────────────────────────────────────
settings_window = None

def open_settings():
    global settings_window
    if settings_window is not None:
        try:
            settings_window.lift()
            settings_window.focus_force()
            return
        except Exception:
            settings_window = None

    win = tk.Tk()
    settings_window = win
    win.title("Hebrew TTS — Settings")
    win.geometry("580x860")
    win.resizable(False, False)
    win.configure(bg="#1e1e2e")

    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("TLabel",    background="#1e1e2e", foreground="#cdd6f4", font=("Segoe UI", 10))
    style.configure("TFrame",    background="#1e1e2e")
    style.configure("TEntry",    fieldbackground="#313244", foreground="#cdd6f4", insertcolor="#cdd6f4")
    style.configure("TCombobox", fieldbackground="#313244", background="#313244",
                                 foreground="#cdd6f4", selectbackground="#45475a")
    style.configure("TScale",    background="#1e1e2e", troughcolor="#313244")
    style.configure("TButton",   background="#89b4fa", foreground="#1e1e2e",
                                 font=("Segoe UI", 10, "bold"), padding=6)
    style.map("TButton",         background=[("active", "#74c7ec")])
    style.configure("Test.TButton", background="#a6e3a1", foreground="#1e1e2e",
                                    font=("Segoe UI", 9, "bold"), padding=4)
    style.map("Test.TButton",    background=[("active", "#94d9a0")])
    style.configure("TCheckbutton", background="#1e1e2e", foreground="#cdd6f4",
                                    font=("Segoe UI", 10))
    style.map("TCheckbutton",    background=[("active", "#1e1e2e")])

    def section(label):
        ttk.Frame(win, height=6).pack()
        f = ttk.Frame(win)
        f.pack(fill="x", padx=16, pady=(4, 2))
        ttk.Label(f, text=label, font=("Segoe UI", 9, "bold"),
                  foreground="#89b4fa").pack(anchor="w")
        tk.Frame(win, height=1, bg="#45475a").pack(fill="x", padx=16)

    def voice_row(label, var, values):
        f = ttk.Frame(win)
        f.pack(fill="x", padx=16, pady=3)
        ttk.Label(f, text=label, width=10).pack(side="left")
        cb = ttk.Combobox(f, textvariable=var, values=values, state="readonly", width=30)
        cb.pack(side="left")
        return cb

    def speed_row(label, speed_var, speed_lbl_var):
        def on_change(v):
            speed_lbl_var.set(f"{round(float(v), 2):.2f}x")
        f = ttk.Frame(win)
        f.pack(fill="x", padx=16, pady=3)
        ttk.Label(f, text=label, width=10).pack(side="left")
        ttk.Scale(f, from_=0.5, to=2.0, orient="horizontal",
                  variable=speed_var, command=on_change,
                  length=260).pack(side="left", padx=(0, 6))
        ttk.Label(f, textvariable=speed_lbl_var, width=6).pack(side="left")
        ttk.Label(f, text="(0.5=fast · 2.0=slow)",
                  foreground="#6c7086", font=("Segoe UI", 8)).pack(side="left")

    # ── Piper Hebrew ───────────────────────────────────────────────────────────
    section("Piper — Hebrew Voice  (Ctrl+Alt+H)")
    he_voice_var   = tk.StringVar(value=cfg.get("he_voice", ""))
    he_speed_var   = tk.DoubleVar(value=float(cfg.get("he_speed", 1.0)))
    he_speed_label = tk.StringVar(value=f"{cfg.get('he_speed', 1.0):.2f}x")
    voice_row("Voice:", he_voice_var, piper_voices_he)
    speed_row("Speed:", he_speed_var, he_speed_label)

    def test_piper_he():
        cfg["he_voice"] = he_voice_var.get()
        cfg["he_speed"] = round(he_speed_var.get(), 2)
        save_config(cfg)
        def run():
            update_tray("yellow", "Hebrew TTS — Testing Hebrew voice...")
            audio, rate = asyncio.run(_synthesize_chunk("שלום, זה בדיקת קול בעברית"))
            if audio is not None:
                global audio_data, sample_rate, play_pos
                audio_data  = audio
                sample_rate = rate
                play_pos    = 0
                start_playback()
            else:
                update_tray("red", "Hebrew TTS — Piper unreachable")
        threading.Thread(target=run, daemon=True).start()


    ttk.Button(win, text="▶  Test Hebrew Voice", style="Test.TButton",
               command=test_piper_he).pack(anchor="w", padx=16, pady=(2, 4))

    # ── Piper English ──────────────────────────────────────────────────────────
    section("Piper — English Voice  (Ctrl+Alt+H)")
    en_voice_var   = tk.StringVar(value=cfg.get("en_voice", ""))
    en_speed_var   = tk.DoubleVar(value=float(cfg.get("en_speed", 1.0)))
    en_speed_label = tk.StringVar(value=f"{cfg.get('en_speed', 1.0):.2f}x")
    voice_row("Voice:", en_voice_var, piper_voices_en)
    speed_row("Speed:", en_speed_var, en_speed_label)

    def test_piper_en():
        cfg["en_voice"] = en_voice_var.get()
        cfg["en_speed"] = round(en_speed_var.get(), 2)
        save_config(cfg)
        def run():
            update_tray("yellow", "Hebrew TTS — Testing English voice...")
            audio, rate = asyncio.run(_synthesize_chunk("Hello, this is an English voice test."))
            if audio is not None:
                global audio_data, sample_rate, play_pos
                audio_data  = audio
                sample_rate = rate
                play_pos    = 0
                start_playback()
            else:
                update_tray("red", "Hebrew TTS — Piper unreachable")
        threading.Thread(target=run, daemon=True).start()

    ttk.Button(win, text="▶  Test English Voice", style="Test.TButton",
               command=test_piper_en).pack(anchor="w", padx=16, pady=(2, 4))

    # ── Windows TTS ────────────────────────────────────────────────────────────
    section("Windows TTS  (Ctrl+Alt+W)")
    win_voice_var   = tk.StringVar(value=cfg.get("windows_voice", ""))
    win_speed_var   = tk.DoubleVar(value=float(cfg.get("windows_speed", 1.0)))
    win_speed_label = tk.StringVar(value=f"{cfg.get('windows_speed', 1.0):.2f}x")
    voice_row("Voice:", win_voice_var, windows_voices)
    ttk.Label(win, text="   ★ Add Natural voices: Settings → Accessibility → Narrator → Add voices",
              foreground="#6c7086", font=("Segoe UI", 8)).pack(anchor="w", padx=16)
    speed_row("Speed:", win_speed_var, win_speed_label)

    def test_windows():
        cfg["windows_voice"] = win_voice_var.get()
        cfg["windows_speed"] = round(win_speed_var.get(), 2)
        save_config(cfg)
        def run():
            global audio_data, play_pos
            update_tray("blue", "Hebrew TTS — Testing Windows voice...")
            success = fetch_windows("Hello, this is a Windows TTS voice test.")
            if success and audio_data is not None:
                play_pos = 0
                start_playback()
            else:
                update_tray("red", "Hebrew TTS — Windows TTS failed")
        threading.Thread(target=run, daemon=True).start()

    ttk.Button(win, text="▶  Test Windows Voice", style="Test.TButton",
               command=test_windows).pack(anchor="w", padx=16, pady=(2, 4))

    # ── Debug logging ──────────────────────────────────────────────────────────
    section("Logging")
    debug_var = tk.BooleanVar(value=cfg.get("debug_logging", False))
    f_log = ttk.Frame(win)
    f_log.pack(fill="x", padx=16, pady=4)
    ttk.Checkbutton(f_log, text="Enable debug logging  (writes to hebrew_tts.log)",
                    variable=debug_var).pack(anchor="w")
    ttk.Label(f_log,
              text="   Turn on only when troubleshooting — off keeps the app silent",
              foreground="#6c7086", font=("Segoe UI", 8)).pack(anchor="w")

    # ── Hotkeys ────────────────────────────────────────────────────────────────
    section("Hotkeys")
    hk = ttk.Frame(win)
    hk.pack(fill="x", padx=16, pady=6)
    ttk.Label(hk, text="Piper TTS:",    width=14).grid(row=0, column=0, sticky="w", pady=3)
    piper_hk_var = tk.StringVar(value=cfg.get("piper_hotkey", "ctrl+alt+h"))
    ttk.Entry(hk, textvariable=piper_hk_var, width=22).grid(row=0, column=1, sticky="w")
    ttk.Label(hk, text="Windows TTS:", width=14).grid(row=1, column=0, sticky="w", pady=3)
    win_hk_var = tk.StringVar(value=cfg.get("windows_hotkey", "ctrl+alt+w"))
    ttk.Entry(hk, textvariable=win_hk_var, width=22).grid(row=1, column=1, sticky="w")
    ttk.Label(hk, text="Format: ctrl+alt+h  /  ctrl+shift+f1  /  ctrl+alt+shift+h",
              foreground="#6c7086", font=("Segoe UI", 8)).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

    # ── Buttons ────────────────────────────────────────────────────────────────
    ttk.Frame(win, height=4).pack()
    tk.Frame(win, height=1, bg="#45475a").pack(fill="x", padx=16)
    bf = ttk.Frame(win)
    bf.pack(fill="x", padx=16, pady=12)

    def collect_all():
        cfg["he_voice"]      = he_voice_var.get()
        cfg["he_speed"]      = round(he_speed_var.get(), 2)
        cfg["en_voice"]      = en_voice_var.get()
        cfg["en_speed"]      = round(en_speed_var.get(), 2)
        cfg["windows_voice"] = win_voice_var.get()
        cfg["windows_speed"] = round(win_speed_var.get(), 2)
        cfg["piper_hotkey"]  = piper_hk_var.get().strip().lower()
        cfg["windows_hotkey"]= win_hk_var.get().strip().lower()
        cfg["debug_logging"] = debug_var.get()
        setup_logging(cfg["debug_logging"])

    def on_apply():
        collect_all()
        save_config(cfg)
        register_hotkeys()
        update_tray("green", "Hebrew TTS — Settings applied")

    def on_save():
        collect_all()
        save_config(cfg)
        register_hotkeys()
        update_tray("green", "Hebrew TTS — Settings saved")
        win.destroy()

    def on_cancel():
        win.destroy()

    ttk.Button(bf, text="Save & Close", command=on_save).pack(side="right", padx=(6, 0))
    ttk.Button(bf, text="Apply",        command=on_apply).pack(side="right", padx=(6, 0))
    ttk.Button(bf, text="Cancel",       command=on_cancel).pack(side="right")

    win.protocol("WM_DELETE_WINDOW", on_cancel)
    win.bind("<Destroy>", lambda e: _clear_settings_ref())
    win.mainloop()

def _clear_settings_ref():
    global settings_window
    settings_window = None

# ── Tray ───────────────────────────────────────────────────────────────────────
def on_settings(icon, menu_item):
    threading.Thread(target=open_settings, daemon=True).start()

def on_stop(icon, menu_item):
    stop_pipeline()

def on_quit(icon, menu_item):
    stop_playback()
    keyboard.unhook_all()
    icon.stop()

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    global tray_icon, windows_voices

    _LOGGER.info("Starting Hebrew TTS Reader")

    windows_voices = get_windows_voices()
    threading.Thread(target=fetch_piper_voices, daemon=True).start()
    register_hotkeys()

    tray_icon = pystray.Icon(
        name="HebrewTTS",
        icon=make_tray_image("green"),
        title="Hebrew TTS — Ready  |  Ctrl+Alt+H=Piper  Ctrl+Alt+W=Windows",
        menu=Menu(
            item("Settings / Voices / Speed", on_settings),
            Menu.SEPARATOR,
            item("Stop Audio",                on_stop),
            Menu.SEPARATOR,
            item("Quit",                      on_quit),
        ),
    )

    tray_icon.run()

if __name__ == "__main__":
    main()
