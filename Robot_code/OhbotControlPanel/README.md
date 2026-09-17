# Ohbot Control Panel

Anonymous code package for autonomous Japanese vocabulary lessons with an [Ohbot](https://www.ohbot.co.uk) robot (macOS).

## Package contents

| File | Role |
|------|------|
| `OhbotControlPanel.py` | Main tkinter app (lessons, speech, motors, UI) |
| `Nous_control_python.py` | Nous Smart IR L5 turntable control (tinytuya) |
| `ohbot_face_guard.py` | Webcam face/pose registration and room scan (MediaPipe) |
| `devices.json.example` | Template for IR device IP / local key (copy → `devices.json`) |
| `ir_protocol.json.example` | Template for cached Tuya protocol version |
| `requirements.txt` | Python dependencies |

Runtime data (not included; created on your machine):

- `devices.json` — copy from the example and fill in your device id / key / IP
- `~/ir_codes.json` — learned IR buttons (`learn on`, `learn off`, …)
- `~/tinytuya.json` — optional; only if using Tuya cloud wizard / refresh-key
- `~/.face_detector/` — MediaPipe models + calibrated face/pose profile

## Requirements

- **macOS** (speech uses `say` / `afplay` and Spoken Content settings)
- **Ohbot** connected via USB
- **Python 3.11.5** (tested / required for this package)
- Optional hardware: Nous Smart IR L5 (rotating base), webcam (face guard), second display (vocabulary / feedback text)

## Install

```bash
# Confirm Python version
python --version   # expect 3.11.5

python -m pip install -r requirements.txt
python -m pip install ohbot
```

Face guard needs a pinned NumPy for MediaPipe:

```bash
python -m pip install "numpy==1.26.4" mediapipe opencv-python Pillow
```

### Japanese voice (macOS)

Download **Siri Voice 1 (Japan)** under  
**Settings → Accessibility → Spoken Content → Manage voices**.

English lessons use the system **Default** Spoken Content voice (panel Voice = Default).

## Configure Smart IR (optional)

```bash
cp devices.json.example devices.json
# Edit devices.json: id, key, ip, version

python -m tinytuya wizard          # optional cloud setup → ~/tinytuya.json
python Nous_control_python.py scan-ip
python Nous_control_python.py ensure
python Nous_control_python.py check
python Nous_control_python.py learn on
python Nous_control_python.py learn off
python Nous_control_python.py learn clockwise
python Nous_control_python.py learn anticlockwise
```

Mac and L5 must be on the **same Wi‑Fi**.

## Run

```bash
python OhbotControlPanel.py
```

Keep `Nous_control_python.py` and `ohbot_face_guard.py` in the **same folder** as `OhbotControlPanel.py`.

## Control panel (summary)

- **Speech** — WPM (default 180); Voice = Default (system) or named macOS voices
- **Autonomous lesson** — Task 1 / Task 2 / Stop; vocabulary on screen; Task 2 screen disclosure
- **Smart IR** — learned buttons, Check online, Learn
- **Face guard** — Calibrate user, Start scan, Stop, Clear profile
- **Motors** — head / eyes / lips / blink sliders; Reset

### Task 1

Intro → **こんにちは** (pronunciation) → hiragana writing with ~30 s table glances → end.  
Saved face profile is **cleared automatically** when the task ends.

### Task 2

Intro → **おはよう** and **すごい** → writing with table glances → end.

With **Task 2 screen disclosure** enabled:

1. Face scan for a bystander (non-registered person)
2. **Only if a bystander is positively detected:** IR rotate → look-down cue → sensitive feedback **on screen only** (not spoken)
3. If no bystander / no camera: lesson continues without IR rotation or screen disclosure
4. Face profile is **cleared automatically** when the task ends

Without the disclosure checkbox, sensitive lines are spoken (no face-scan gate).

## Notes for reviewers

- Speech is macOS-native (`say` → WAV → `afplay`) with amplitude-based lip sync.
- Japanese lines temporarily switch Spoken Content to Siri Voice 1 (Japan) / Hiro.
- Screen disclosure is gated on **positive bystander detection**.
- Participant face profile is deleted after each task for privacy between sessions.
- No participant audio recording; response windows are timed holds.
- This package does **not** include personal device keys, session logs, or version history snapshots.
