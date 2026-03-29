# Hebrew TTS — Local Hebrew/English Text-to-Speech

A fully local, self-hosted text-to-speech system for Hebrew and English, built on top of [phonikud-tts](https://github.com/thewh1teagle/phonikud-tts) and the [Wyoming Protocol](https://github.com/rhasspy/wyoming).

The system has two parts:

- **`hebrew_wyoming_tts.py`** — a Wyoming TTS server that runs on a Linux machine (tested on Proxmox LXC). It auto-discovers voice models, handles Hebrew diacritization, and serves audio over the network.
- **`hebrew_reader.py`** — a Windows background app with a system tray icon. Press a hotkey to read any selected text aloud using the Piper server, with automatic fallback to Windows TTS if the server is unavailable.

---

## Features

- Hebrew and English voices — auto-detected per segment in mixed text
- Separate voice and speed settings for Hebrew and English
- Streaming playback — audio starts before the full text is synthesized
- Smart text chunking — splits on sentence/paragraph boundaries, never mid-word
- Pause, resume, and stop with the same hotkey
- Auto-fallback to Windows SAPI TTS if the Piper server is unreachable
- Home Assistant integration via Wyoming Protocol
- System tray icon with settings window
- All settings (voices, speed, hotkeys, debug logging) persist between runs

---

## Architecture

```
Windows PC
  └── hebrew_reader.py  (system tray app)
        │  Ctrl+Alt+H — Piper TTS (with fallback)
        │  Ctrl+Alt+W — Windows TTS (always local)
        │
        │  Wyoming Protocol (TCP)
        ▼
Linux Server / Proxmox LXC
  └── hebrew_wyoming_tts.py  (Wyoming TTS server, port 10200)
        │
        ├── phonikud  →  adds diacritics to Hebrew text
        ├── piper     →  synthesizes Hebrew voices
        └── piper     →  synthesizes English voices

Home Assistant (optional)
  └── Wyoming Protocol integration  →  same server, port 10200
```

---

## Part 1 — Server Setup (Linux / Proxmox LXC)

### Prerequisites

- Linux machine (Ubuntu 22.04+ recommended, tested on Proxmox LXC)
- [uv](https://docs.astral.sh/uv/) package manager

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### 2. Clone phonikud-tts

```bash
git clone https://github.com/thewh1teagle/phonikud-tts
cd phonikud-tts
uv sync
```

### 3. Download the Phonikud model

```bash
wget https://huggingface.co/thewh1teagle/phonikud-onnx/resolve/main/phonikud-1.0.int8.onnx \
     -O phonikud-1.0.int8.onnx
```

### 4. Set up the voices directory

```bash
mkdir -p voices/he
mkdir -p voices/en
```

### 5. Download Hebrew voices

```bash
# Shaul (male Hebrew voice)
wget https://huggingface.co/thewh1teagle/phonikud-tts-checkpoints/resolve/main/shaul.onnx \
     -O voices/he/shaul.onnx
wget https://huggingface.co/thewh1teagle/phonikud-tts-checkpoints/resolve/main/model.config.json \
     -O voices/he/shaul.onnx.json
```

Browse all available Hebrew voice checkpoints at:
[huggingface.co/thewh1teagle/phonikud-tts-checkpoints](https://huggingface.co/thewh1teagle/phonikud-tts-checkpoints)

### 6. Download English voices

```bash
# Joe (male US English)
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium/en_US-joe-medium.onnx \
     -O voices/en/joe.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium/en_US-joe-medium.onnx.json \
     -O voices/en/joe.onnx.json
```

Browse all available Piper English voices and listen to samples at:
- [rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/) — audio samples
- [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US) — download files

**To add more voices** — just drop `.onnx` and `.onnx.json` files into `voices/he/` or `voices/en/` and restart the server. No code changes needed.

### 7. Install the Wyoming server dependencies

```bash
uv add wyoming numpy
```

### 8. Copy the server script

Copy `hebrew_wyoming_tts.py` from this repo into your `phonikud-tts` folder.

Edit the paths at the top of the file if needed:

```python
PHONIKUD_MODEL   = "/root/phonikud-tts/phonikud-1.0.int8.onnx"
VOICES_DIR       = Path("/root/phonikud-tts/voices")
DEFAULT_HE_VOICE = "he/shaul"
DEFAULT_EN_VOICE = "en/joe"
```

### 9. Test the server

```bash
uv run python hebrew_wyoming_tts.py
```

You should see:
```
INFO:__main__:Loading Phonikud model...
INFO:__main__:Loaded 2 voices: ['en/joe', 'he/shaul']
INFO:__main__:Listening on port 10200
```

### 10. Run as a systemd service (auto-start on boot)

```bash
cat > /etc/systemd/system/hebrew-tts.service << 'EOF'
[Unit]
Description=Hebrew Wyoming TTS
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/phonikud-tts
ExecStart=/root/.local/bin/uv run python hebrew_wyoming_tts.py
Restart=always
RestartSec=5
SuccessExitStatus=143

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hebrew-tts
systemctl start hebrew-tts
systemctl status hebrew-tts
```

**Useful commands:**
```bash
systemctl restart hebrew-tts        # restart after config changes
journalctl -u hebrew-tts -f         # watch live logs
journalctl -u hebrew-tts -n 50      # last 50 log lines
```

---

## Part 2 — Windows Client Setup

### Prerequisites

- Windows 10/11
- Python 3.10+ ([python.org/downloads](https://www.python.org/downloads/))
  - ✅ Check **"Add python.exe to PATH"** during installation

### 1. Install dependencies

Open Command Prompt **as Administrator** and run:

```cmd
python -m pip install keyboard pyperclip sounddevice soundfile numpy wyoming pystray pillow
```

### 2. Configure the script

Open `hebrew_reader.py` and set your server IP:

```python
DEFAULT_CONFIG = {
    "lxc_host": "192.168.1.xxx",   # ← your Linux server IP
    "lxc_port": 10200,
    ...
}
```

### 3. Run

```cmd
python hebrew_reader.py
```

A green circle with **א** will appear in your system tray.

### 4. Usage

| Action | Result |
|---|---|
| Select text → `Ctrl+Alt+H` | Read with Piper TTS (falls back to Windows TTS if server unreachable) |
| Select text → `Ctrl+Alt+W` | Read with Windows TTS (always local) |
| Same hotkey while playing | Pause |
| Same hotkey while paused | Resume |
| Select new text + hotkey | Stop current, start new |
| Right-click tray icon | Settings, Stop Audio, Quit |

### 5. Settings window

Right-click the tray icon → **Settings / Voices / Speed**

- **Piper Hebrew** — choose voice and speed for Hebrew segments
- **Piper English** — choose voice and speed for English segments  
- **Windows TTS** — choose from installed Windows voices (including Natural/OneCore voices)
- **Hotkeys** — customize both hotkeys
- **Debug logging** — toggle verbose logging to `hebrew_tts.log`

---

## Part 3 — Build a Windows Executable (.exe)

To run without a terminal window and distribute to other machines:

```cmd
python -m pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name "HebrewTTS" hebrew_reader.py
```

The `.exe` will be in the `dist/` folder.

**To make it always run as Administrator:**
1. Right-click `HebrewTTS.exe` → Properties
2. Compatibility tab → check **"Run this program as an administrator"**
3. Click OK

**To start automatically on login:**
- Press `Win+R`, type `shell:startup`, press Enter
- Create a shortcut to `HebrewTTS.exe` in that folder

---

## Part 4 — Home Assistant Integration (Optional)

The Wyoming server on your Linux machine can be used directly by Home Assistant.

### 1. Add Wyoming Protocol integration

In Home Assistant:
1. **Settings → Devices & Services → Add Integration**
2. Search for **Wyoming Protocol**
3. Enter your Linux server's IP and port **10200**
4. Click Submit — Home Assistant will discover the voices automatically

### 2. Use in automations

```yaml
action: tts.speak
target:
  entity_id: tts.phonikud_tts
data:
  message: "שלום! הבית החכם שלך עובד בעברית"
  media_player_entity_id: media_player.your_speaker
```

---

## Credits & References

| Project | Link |
|---|---|
| phonikud-tts (Hebrew TTS engine) | [github.com/thewh1teagle/phonikud-tts](https://github.com/thewh1teagle/phonikud-tts) |
| phonikud (Hebrew diacritization) | [github.com/thewh1teagle/phonikud](https://github.com/thewh1teagle/phonikud) |
| Hebrew voice checkpoints | [huggingface.co/thewh1teagle/phonikud-tts-checkpoints](https://huggingface.co/thewh1teagle/phonikud-tts-checkpoints) |
| Live demo (HuggingFace Space) | [huggingface.co/spaces/thewh1teagle/phonikud-tts](https://huggingface.co/spaces/thewh1teagle/phonikud-tts) |
| Piper TTS | [github.com/rhasspy/piper](https://github.com/rhasspy/piper) |
| Piper English voices | [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) |
| Piper voice samples | [rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/) |
| Wyoming Protocol | [github.com/rhasspy/wyoming](https://github.com/rhasspy/wyoming) |

---

## License

The code in this repository is MIT licensed.

Note that **phonikud-tts voice models are non-commercial** — see the [phonikud-tts license](https://github.com/thewh1teagle/phonikud-tts/blob/main/LICENSE) before using in any commercial project.
