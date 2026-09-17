#!/usr/bin/env python3
"""Ohbot Control Panel — autonomous Japanese vocabulary lessons with Ohbot.

Companion modules (same folder): Nous_control_python.py, ohbot_face_guard.py.
"""

from tkinter import *
from tkinter import ttk, messagebox
from ohbot import ohbot
import threading
import time
import re
import os
import random
import subprocess
import sys
import struct
import wave
import traceback
import atexit
import signal
import plistlib

# Japanese: Siri Voice 1 (Japan) via macOS Spoken Content + say without -v.
MAC_SIRI_JA_SYSTEM_VOICE = "__MAC_SIRI_JA_SYSTEM_VOICE__"

def _ensure_companion_modules_on_path():
    """Add directory with the newest Nous_control_python (has IR_send_base)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    seen = set()

    def add(path):
        path = os.path.abspath(path)
        if path not in seen:
            seen.add(path)
            candidates.append(path)

    add(script_dir)
    d = script_dir
    for _ in range(6):
        for sub in (
            "examples",
            os.path.join("ohbot-python", "examples"),
            os.path.join("ohbot-python", "examples"),
        ):
            add(os.path.join(d, sub))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent

    def _nous_module_score(dirpath):
        fp = os.path.join(dirpath, "Nous_control_python.py")
        if not os.path.isfile(fp):
            return None
        score = 0
        try:
            with open(fp, encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
            if "IR_send_base" in text:
                score += 100
            if "ensure_ir_ready" in text:
                score += 20
            if "ohbot-python" in dirpath and dirpath.endswith("examples"):
                score += 30
        except OSError:
            score = 1
        if dirpath == script_dir:
            score -= 5
        return score

    ranked = []
    for path in candidates:
        score = _nous_module_score(path)
        if score is not None:
            ranked.append((score, path))
    if not ranked:
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        return script_dir

    ranked.sort(key=lambda item: (-item[0], item[1]))
    best = ranked[0][1]
    for path in candidates:
        if path != best and os.path.isfile(os.path.join(path, "Nous_control_python.py")):
            try:
                sys.path.remove(path)
            except ValueError:
                pass
    if best not in sys.path:
        sys.path.insert(0, best)
    return best


_IR_COMPANION_DIR = _ensure_companion_modules_on_path()

try:
    from Nous_control_python import (
        DEVICE_ID as _IR_DEVICE_ID,
        IR_send_base as _IR_SEND_BASE,
        check_device as _ir_check_device,
        ensure_ir_ready as _ir_ensure_ready,
        learn_button as _ir_learn_button,
        list_codes as _ir_list_codes,
        load_device as _ir_load_device,
    )

    _IR_CONTROL_AVAILABLE = True
    print(
        f"[IR] loaded from {_IR_COMPANION_DIR} — buttons: {_IR_SEND_BASE.names()}"
    )
except Exception as _ir_import_exc:
    _IR_CONTROL_AVAILABLE = False
    _IR_SEND_BASE = None
    print(f"[IR] import failed ({_IR_COMPANION_DIR}): {_ir_import_exc}")

try:
    from ohbot_face_guard import FaceGuardService

    _FACE_GUARD_AVAILABLE = True
except Exception as _face_guard_import_exc:
    _FACE_GUARD_AVAILABLE = False
    FaceGuardService = None
    _face_guard_import_exc  # noqa: F841 — kept for status message

# Ctrl+C / kill often skip tkinter on_closing and atexit — send IR off synchronously here.
_IR_SHUTDOWN_APP = None


def _emergency_ir_off(button="off"):
    """Send ``off`` only when tracked base state is on; safe from signal handlers."""
    if not _IR_CONTROL_AVAILABLE or _IR_SEND_BASE is None:
        return False
    app = _IR_SHUTDOWN_APP
    if app is not None and not getattr(app, "ir_send_off_on_stop", True):
        return False
    btn = str(button or "off").strip()
    if app is not None:
        with app._ir_power_lock:
            if not app._ir_base_is_on:
                print("[IR] shutdown skipped: base is already off")
                return False
    try:
        names = _IR_SEND_BASE.names()
    except Exception as exc:
        print(f"[IR] shutdown failed (list codes): {exc}")
        return False
    if btn not in names:
        print(f"[IR] shutdown skipped: no button '{btn}' in ~/ir_codes.json")
        return False
    try:
        print(f"[IR] sending shutdown: {btn} ...")
        _IR_SEND_BASE(btn)
        if app is not None:
            with app._ir_power_lock:
                app._ir_base_is_on = False
        print(f"[IR] shutdown sent: {btn}")
        return True
    except Exception as exc:
        print(f"[IR] shutdown failed: {exc}")
        return False


def _ir_process_atexit_shutdown():
    btn = "off"
    app = _IR_SHUTDOWN_APP
    if app is not None:
        btn = str(getattr(app, "ir_shutdown_button", "off") or "off").strip()
    _emergency_ir_off(btn)


atexit.register(_ir_process_atexit_shutdown)


class OhbotControlPanel:
    def __init__(self, root):
        self.root = root
        self.root.title("Ohbot Control Panel")
        self.root.geometry("900x700")
        self.root.resizable(True, True)

        # Status variable
        self.connected = False
        self.is_speaking = False

        # Session state (Guided Vocabulary Learning)
        self.session_active = False
        self.response_event = threading.Event()
        self.stop_event = threading.Event()
        self.autonomy_thread = None
        self._next_blink_at = time.time() + random.uniform(2.5, 5.5)
        self._continuous_blink_thread = None
        self._continuous_blink_stop = threading.Event()
        self._continuous_blink_active = False
        self.continuous_blink_min_interval_s = 2.0
        self.continuous_blink_max_interval_s = 5.5
        self.continuous_blink_step_delay_s = 0.02
        # How far lids close each blink: 0.7 = 70% of open→shut travel (not fully closed).
        self.continuous_blink_close_fraction = 0.70
        # How long the robot pauses (in its current pose) at points where it
        # gives the participant time to respond. No microphone is involved —
        # the robot just holds position for this many seconds.
        self.default_wait_s = 1
        self.little_movement_smooth_steps = 12
        self.little_movement_step_delay_s = 0.024
        self.little_movement_motor_speed = 4
        self.little_movement_hold_s = 0.08
        # Glance at table materials: nod/eye-tilt delta from current pose (not motor_defaults).
        self.table_look_nod_delta = 3.0
        self.table_look_eye_tilt_delta = 3.0
        # Writing task: hold at table while participant writes (seconds).
        self.writing_table_glance_s = 30
        self.task2_look_down_cue_text = "Hey, look down here at your paper."
        self.task2_look_down_cue_hold_s = 2.0
        # turn_head_full_*: approximate degrees from neutral head pose (motor_defaults).
        # Tune if the physical angle feels off.
        self.head_turn_nudge_degrees = 30.0
        # Treat ~this many degrees from centre (motor 5) to one mechanical stop ≈ 5 units.
        self.head_turn_half_range_degrees = 45.0
        # Head shake left↔right for negation ("no").
        self.negation_shake_duration_s = 2.0
        self.negation_shake_cycles = 2
        self.negation_shake_delta_units = 1.5
        self.negation_shake_smooth_steps = 8
        # Task 2 only: background left↔right look sweep (see start/stop_task2_continuous_look_sweep).
        self._task2_look_sweep_thread = None
        self._task2_look_sweep_stop = threading.Event()
        self._task2_look_sweep_active = False
        self.task2_look_sweep_hold_s = 2.0
        self.task2_look_sweep_delta_units = 1.5
        self.task2_look_sweep_smooth_steps = 12
        self.task2_look_sweep_step_delay_s = 0.03
        self.task2_look_sweep_motor_speed = 3
        self.task2_screen_disclosure_var = None
        self.lesson_vocab_screen_disclosure_var = None
        self.task2_disclosure_window = None
        self._disclosure_text_frame = None
        self.task2_disclosure_ja_label = None
        self.task2_disclosure_romaji_label = None
        self.task2_disclosure_meaning_label = None
        # How long Task 2 feedback text stays on the disclosure window (screen-only mode).
        self.task2_screen_disclosure_display_s = 8.0
        self.lesson_vocab_disclosure_font_size = 100
        self.lesson_vocab_single_word_font_size = 200
        self.lesson_vocab_disclosure_wraplength = 1700
        self.task2_feedback_disclosure_font_size = 100
        self.task2_feedback_disclosure_wraplength = 1700
        # 0 = auto from font size + window width; else max chars per wrapped line
        self.task2_feedback_disclosure_chars_per_line = 0
        # Vertical position on disclosure window (0.5 = middle; higher = lower on screen).
        self.disclosure_text_rely = 0.65
        # Single-word Japanese (writing task): centre the large word on screen.
        self.lesson_vocab_single_word_rely = 0.50

        # Voice selection (loaded lazily so UI never blocks)
        self.voice_options = ["Default"]
        # Default: macOS system voice (Settings → Accessibility → Spoken Content); say omits -v.
        self.mac_system_voice_labels = ("Default",)
        # Default UI voice selection on startup (macOS `say` voice name).
        self.voice_var_value = "Default"
        self.voice_combo = None
        self._auto_ja_voice_name = None
        self._known_voice_availability = {}
        # Separate speech rate for Japanese macOS `say` (words-per-minute).
        # Lower = slower; higher = shorter utterance (used for stressed repeat demos).
        self.japanese_rate_wpm = 110
        self.japanese_rate_wpm_stressed_repeat = 75
        # Louder stressed repeat: multiply wav samples (>1) and afplay level (0–1).
        self.japanese_volume_gain_stressed_repeat = 3.0
        self.japanese_afplay_volume_stressed_repeat = 1.0
        # Pitch: macOS Japanese voices (Otoya/Kyoko) are often higher than Daniel (Enhanced).
        # Negative semitones lower Japanese toward English pitch; tune by ear (e.g. -2 to -4).
        self.japanese_pitch_semitones = -3.0
        self.english_pitch_semitones = 0.0
        # Japanese lesson speech: Siri Voice 1 (Japan) = Hiro (~400 MB neural premium).
        self.japanese_siri_voice_label = "Siri Voice 1 (Japan)"
        self.japanese_siri_voice_ids = (
            "com.apple.ttsbundle.gryphon-neural_Hiro_ja-JP_premium",
            "com.apple.siri.natural.Hiro",
            "com.apple.speech.synthesis.voice.custom.siri.hiro.premium",
        )
        self.japanese_siri_tts_language = "ja"
        self.mac_english_tts_language = "en"
        self._active_mac_ja_siri_voice_id = None
        # Default English speaking rate for macOS ``say`` (words per minute).
        self.default_speech_wpm = 180
        # Legacy: Otoya/Kyoko only used if Siri Hiro activation fails.
        self.prefer_japanese_male_voice = True
        # Prevent overlapping speech from background thread / UI actions.
        self._speech_lock = threading.Lock()
        # Return lips to rest during speech after every N words (audio keeps playing; 0 = off).
        self.lips_reset_every_n_words = 4
        self.lips_reset_hold_s = 0.2
        # Lip-sync motion around rest pose (motor_defaults 4/5); raise scale for wider mouth.
        self.lipsync_top_rest = 0.0
        self.lipsync_bottom_rest = 6.0
        self.lipsync_amplitude_scale = 0.2
        self.lipsync_visemes_per_sec = 14
        # Wait after afplay starts before lip-sync (afplay/CoreAudio startup; lips were ahead of sound).
        self.lipsync_afplay_start_delay_s = 0.25
        self._active_lip_reset_viseme_indices = []

        # Nous Smart IR L5 (optional — requires tinytuya + ~/devices.json)
        self._ir_available = _IR_CONTROL_AVAILABLE
        self.IR_send_base = _IR_SEND_BASE
        self.ir_buttons_frame = None
        self.ir_learn_name_var = None
        self.ir_send_interval_s = 0.35  # pause between back-to-back IR sends
        self.task2_ir_base_settle_s = 1  # wait after rotation IR for base to move
        self.ir_send_off_on_stop = True  # send shutdown IR when Stop / cancel / exit
        self.ir_shutdown_button = "off"  # only this button turns base off
        self.ir_base_on_commands = (
            "power",
            "anticlockwise",
            "on",
            "clockwise",
            "speed_0",
            "speed_1",
            "speed_2",
        )
        self._ir_base_is_on = False
        self._ir_power_lock = threading.Lock()
        self._closing = False
        global _IR_SHUTDOWN_APP
        _IR_SHUTDOWN_APP = self

        # Face guard (calibration + scan for other people)
        self._face_guard_available = _FACE_GUARD_AVAILABLE
        self._face_guard_stop = threading.Event()
        self._face_guard_thread = None
        self._face_guard_service = None
        self.face_guard_status_label = None
        self._face_guard_preview_window = None
        self._face_guard_preview_label = None
        self._face_guard_preview_photo = None
        self._face_guard_preview_max_width = 640
        self.face_guard_show_preview = False
        self._face_guard_ready = threading.Event()
        self.task2_face_scan_face_hold_s = 0.5
        self.task2_face_scan_settled_frames = 6
        self.task2_face_scan_center_tolerance = 0.12
        self.task2_face_scan_head_delta = 0.25
        self.task2_face_scan_max_s = 8.0
        # Task 2: sensitive on-screen disclosure only after positive bystander detect.
        self._task2_bystander_detected = False

        # Initialize Ohbot
        self.init_ohbot()
        self._ohbot_say_raw = ohbot.say
        self._install_soft_lipsync_profile()
        # Library _moveSpeech ends with move(TOPLIP,5)/move(BOTTOMLIP,5); that pulls lips
        # toward 5 before our reset — looks like "mouth goes up then resets" when rest top ≠ 5.
        self._install_lip_sync_without_library_neutral_pose()
        self._install_safe_speech_wrapper()

        # Create the GUI
        self.create_ui()
        self.root.after(0, lambda: self._show_task2_disclosure(""))
        if self._ir_available:
            threading.Thread(
                target=self._bootstrap_ir_connection,
                daemon=True,
                name="ir-bootstrap",
            ).start()

    def _bootstrap_ir_connection(self):
        """On startup: refresh key if expired, probe protocol, sync devices.json."""
        try:
            info = _ir_ensure_ready(verbose=True)
            msg = info.get("message") or (
                "IR L5 ready" if info.get("ok") else "IR setup failed — see terminal"
            )
            self.root.after(0, lambda m=msg: self._set_info(m))
            self.root.after(0, self._refresh_ir_buttons)
        except Exception as exc:
            self.root.after(
                0, lambda e=exc: self._set_info(f"IR bootstrap error: {e}")
            )

    def init_ohbot(self):
        """Initialize Ohbot connection"""
        try:
            ohbot.reset()
            self.connected = True
            print("✓ Ohbot initialized successfully")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to initialize Ohbot: {e}")
            self.connected = False

    def _get_voice_list(self):
        """Return list of available voice names (macOS) with a 'Default' option.

        Returns all installed macOS voices (excluding novelty/effect voices).
        """
        if sys.platform == "darwin":
            try:
                # Exclude macOS novelty / musical effect voices
                exclude_names = {
                    "Bells",
                    "Boing",
                    "Bubbles",
                    "Cellos",
                    "Deranged",
                    "GoodNews",
                    "BadNews",
                    "Organ",
                    "Trinoids",
                    "Whisper",
                    "Zarvox",
                    "Bahh",
                    "Hysterical",
                    "Albert",
                }
                out = subprocess.run(
                    ["say", "-v", "?"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if out.returncode == 0 and out.stdout:
                    names = []
                    for line in out.stdout.strip().splitlines():
                        # Format: "<Name...> <Locale> # <Comment>"
                        # Name can contain spaces, e.g. "Otoya (Enhanced)".
                        left = line.split("#")[0].strip()
                        if not left:
                            continue
                        parts = left.split()
                        if len(parts) >= 2:
                            name = " ".join(parts[:-1]).strip()
                            if name in exclude_names:
                                continue
                            names.append(name)
                    if names:
                        return ["Default"] + sorted(set(names))
            except Exception:
                pass
        return ["Default"]

    def _load_voices_async(self):
        """Load voice list without blocking UI, then update combobox."""
        def run():
            voices = self._get_voice_list()

            def apply():
                try:
                    self.voice_options = voices
                    if self.voice_combo is not None:
                        self.voice_combo["values"] = voices
                        current = self.voice_var.get()
                        if current not in voices:
                            preferred = self.voice_var_value or "Default"
                            for fallback in (
                                preferred,
                                "Default",
                                "Jamie (Premium)",
                                "Jamie (Enhanced)",
                                "Daniel (Enhanced)",
                            ):
                                if fallback in voices:
                                    preferred = fallback
                                    break
                            else:
                                preferred = "Default"
                            self.voice_var.set(
                                preferred if preferred in voices else "Default"
                            )
                except Exception:
                    pass

            self.root.after(0, apply)

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()

    def _read_mac_system_tts_language(self):
        try:
            r = subprocess.run(
                ["defaults", "read", "com.apple.speech.voice.prefs", "SystemTTSLanguage"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                return str(r.stdout or "").strip()
        except Exception:
            pass
        return None

    def _set_mac_system_tts_language(self, language):
        lang = str(language or "").strip()
        if not lang:
            return
        try:
            subprocess.run(
                [
                    "defaults",
                    "write",
                    "com.apple.speech.voice.prefs",
                    "SystemTTSLanguage",
                    "-string",
                    lang,
                ],
                check=False,
                timeout=5,
            )
        except Exception:
            pass

    def _set_mac_spoken_content_voice(self, voice_id, language):
        """Set Spoken Content voice for ``language`` (undocumented; required for Siri voices)."""
        voice_id = str(voice_id or "").strip()
        lang = str(language or "").strip()
        if not voice_id or not lang:
            return False
        pref = os.path.expanduser("~/Library/Preferences/com.apple.Accessibility.plist")
        try:
            with open(pref, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            data = {}
        sel = data.get("SpokenContentDefaultVoiceSelectionsByLanguage")
        new_sel = []
        updated = False
        if isinstance(sel, list):
            i = 0
            while i < len(sel):
                item = sel[i]
                if isinstance(item, str):
                    bound = item
                    entry = sel[i + 1] if i + 1 < len(sel) else {}
                    i += 2
                    if bound == lang and isinstance(entry, dict):
                        d = dict(entry)
                        d["voiceId"] = voice_id
                        d["boundLanguage"] = lang
                        d.setdefault("_type", "Speech.VoiceSelection")
                        d.setdefault("_version", 0)
                        new_sel.extend([bound, d])
                        updated = True
                    else:
                        new_sel.extend([bound, entry] if isinstance(entry, dict) else [bound])
                else:
                    i += 1
        if not updated:
            new_sel.extend([
                lang,
                {
                    "_type": "Speech.VoiceSelection",
                    "_version": 0,
                    "boundLanguage": lang,
                    "voiceId": voice_id,
                },
            ])
        data["SpokenContentDefaultVoiceSelectionsByLanguage"] = new_sel
        try:
            with open(pref, "wb") as f:
                plistlib.dump(data, f)
            self._set_mac_system_tts_language(lang)
            return True
        except Exception:
            return False

    def _ensure_mac_siri_ja_voice(self):
        """Activate Siri Voice 1 (Japan) / Hiro for ``say`` without ``-v``."""
        if sys.platform != "darwin":
            return False
        voice_ids = tuple(getattr(self, "japanese_siri_voice_ids", ()) or ())
        active = getattr(self, "_active_mac_ja_siri_voice_id", None)
        if active and active in voice_ids:
            self._set_mac_system_tts_language(
                getattr(self, "japanese_siri_tts_language", "ja")
            )
            return True
        lang = str(getattr(self, "japanese_siri_tts_language", "ja") or "ja")
        for voice_id in voice_ids:
            if self._set_mac_spoken_content_voice(voice_id, lang):
                self._active_mac_ja_siri_voice_id = voice_id
                return True
        return False

    def _append_mac_say_voice_args(self, cmd, voice_name):
        """Append ``-v`` or activate Siri system voice before ``say``."""
        if voice_name == MAC_SIRI_JA_SYSTEM_VOICE:
            self._ensure_mac_siri_ja_voice()
            return
        if voice_name is None:
            self._set_mac_system_tts_language(
                getattr(self, "mac_english_tts_language", "en")
            )
            return
        if voice_name:
            cmd += ["-v", str(voice_name)]

    def _mac_say_voice_name(self, selected=None):
        """Voice for ``say -v``; None = macOS system default (no ``-v``)."""
        if selected is None:
            try:
                selected = self.voice_var.get() if hasattr(self, "voice_var") else self.voice_var_value
            except Exception:
                selected = self.voice_var_value
        label = str(selected or "").strip()
        if not label or label in getattr(self, "mac_system_voice_labels", ("Default",)):
            return None
        return label

    def on_voice_change(self):
        """Apply selected voice to Ohbot speech."""
        try:
            selected = self.voice_var.get()
            if not selected or selected in getattr(self, "mac_system_voice_labels", ("Default",)):
                ohbot.setVoice("")
                self._set_info(
                    "Voice: macOS system default "
                    "(Settings → Accessibility → Spoken Content)"
                )
            else:
                ohbot.setVoice(selected)
                self._set_info(f"Voice set to {selected}")
        except Exception as e:
            self._set_info(f"Voice error: {e}")

    def _apply_selected_voice(self):
        """Apply voice_var to Ohbot (safe to call repeatedly)."""
        try:
            selected = self.voice_var.get() if hasattr(self, "voice_var") else self.voice_var_value
            if not selected or selected in getattr(self, "mac_system_voice_labels", ("Default",)):
                ohbot.setVoice("")
                return
            # If a voice isn't available, fall back gracefully (prevents weird retries/repeats).
            available = set(self.voice_options or [])
            fallback_map = {
                "Jamie (Premium)": "Jamie (Enhanced)",
                "Jamie (Enhanced)": "Jamie",
                "Daniel (Enhanced)": "Daniel",
                "Otoya (Enhanced)": "Otoya",
                "Kyoko (Enhanced)": "Kyoko",
            }
            voice_name = selected
            if voice_name not in available and voice_name in fallback_map and fallback_map[voice_name] in available:
                voice_name = fallback_map[voice_name]
                try:
                    self.voice_var.set(voice_name)
                except Exception:
                    pass
            ohbot.setVoice(voice_name)
        except Exception:
            pass

    def _normalize_speech_text(self, text):
        txt = str(text or "").strip()
        if not txt:
            return ""
        # Normalize punctuation that can cause awkward pauses/stalls in some voices.
        txt = txt.replace("’", "'").replace("“", "\"").replace("”", "\"")
        txt = txt.replace("...", ". ")
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt

    def _speech_unit_count(self, text):
        """Word count for spaced text; glyph count for unspaced Japanese."""
        txt = self._normalize_speech_text(text)
        if not txt:
            return 0
        if self._text_contains_japanese(txt) and not re.search(r"\s", txt):
            return len([c for c in txt if not c.isspace()])
        return len(txt.split())

    def _compute_lip_reset_viseme_indices(self, text, num_visemes):
        """Viseme frame indices (0..num_visemes-1) where lips close; aligned to word boundaries."""
        n = int(getattr(self, "lips_reset_every_n_words", 3) or 3)
        nv = int(num_visemes)
        if n <= 0 or nv <= 1:
            return []
        units = self._speech_unit_count(text)
        if units <= n:
            return []
        indices = []
        for boundary in range(n, units, n):
            idx = int((boundary / float(units)) * nv)
            idx = min(nv - 1, max(0, idx))
            if not indices or idx > indices[-1]:
                indices.append(idx)
        return indices

    def _text_contains_japanese(self, text: str) -> bool:
        s = str(text or "")
        # Hiragana: 3040–309F, Katakana: 30A0–30FF, CJK: 4E00–9FFF
        return bool(re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", s))

    def _get_mac_voice_locales(self):
        """Return dict of macOS voice name -> locale (cached)."""
        if sys.platform != "darwin":
            return {}
        cached = getattr(self, "_mac_voice_locales_cache", None)
        if isinstance(cached, dict) and cached:
            return cached
        try:
            out = subprocess.run(
                ["say", "-v", "?"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            locales = {}
            if out.returncode == 0 and out.stdout:
                for line in out.stdout.strip().splitlines():
                    left = line.split("#")[0].strip()
                    if not left:
                        continue
                    parts = left.split()
                    if len(parts) >= 2:
                        locale = parts[-1]
                        name = " ".join(parts[:-1]).strip()
                        locales[name] = locale
            self._mac_voice_locales_cache = locales
            return locales
        except Exception:
            self._mac_voice_locales_cache = {}
            return {}

    def _ensure_japanese_voice_selected_if_needed(self, text: str) -> bool:
        """If text contains Japanese and user hasn't explicitly chosen a voice, try a Japanese macOS voice.

        Returns True if we applied an automatic Japanese voice (so caller can restore later).
        """
        if sys.platform != "darwin":
            return False
        if not self._text_contains_japanese(text):
            return False
        # If user explicitly selected a voice, respect it.
        try:
            selected = self.voice_var.get() if hasattr(self, "voice_var") else self.voice_var_value
        except Exception:
            selected = self.voice_var_value
        if selected and selected != "Default":
            return False
        # Cache the first available Japanese voice name we find.
        # Use the installed voice list (say -v ?) rather than probing with "say -v <name> <text>",
        # which can cause an extra utterance.
        if self._auto_ja_voice_name is None:
            self._get_japanese_voice_name()
        if self._auto_ja_voice_name == MAC_SIRI_JA_SYSTEM_VOICE:
            try:
                ohbot.setVoice("")
                return True
            except Exception:
                pass
        elif self._auto_ja_voice_name:
            try:
                ohbot.setVoice(self._auto_ja_voice_name)
                return True
            except Exception:
                pass
        else:
            # Make it obvious why Japanese isn't being used.
            try:
                self._set_info(
                    "Japanese voice not found — download Siri Voice 1 (Japan) in "
                    "Settings → Accessibility → Spoken Content"
                )
            except Exception:
                pass
        return False

    def _reset_lips_to_initialized(self):
        """Return lips to panel default positions (same as motor sliders at startup)."""
        if not self.connected:
            return
        try:
            defaults = getattr(self, "motor_defaults", None) or {4: 0, 5: 6}
            top = float(defaults.get(4, 0))
            bottom = float(defaults.get(5, 6))
            ohbot.move(ohbot.TOPLIP, top)
            ohbot.move(ohbot.BOTTOMLIP, bottom)
            ohbot.lipTopPos = top
            ohbot.lipBottomPos = bottom
        except Exception:
            pass

    def _wait_lip_sync_thread_finished(self):
        """ohbot.say(untilDone=True) does not join the lip-sync thread; it often returns first.

        Without a short grace period, _moveSpeech can still run after say() returns and
        overwrite lip positions before we apply the panel rest pose.
        """
        if not self.connected:
            return
        try:
            for _ in range(5):
                ohbot.wait(ohbot.WAITLONG)
        except Exception:
            time.sleep(0.35)

    def _speak_reliably(self, text, pause_between_chunks_s=0.1):
        if not self.connected:
            return
        cleaned = self._normalize_speech_text(text)
        if not cleaned:
            return
        # Voice selection is handled by the caller (e.g., _session_say vs _session_say_japanese).
        chunks = [c.strip() for c in re.split(r"(?<=[\.\!\?\;\:])\s+", cleaned) if c.strip()]
        if not chunks:
            chunks = [cleaned]
        for chunk in chunks:
            if self.stop_event.is_set():
                return
            try:
                est_s = max(0.5, len(chunk.split()) * 60.0 / 160.0)
                est_visemes = max(10, int(est_s * 10))
                self._active_lip_reset_viseme_indices = self._compute_lip_reset_viseme_indices(chunk, est_visemes)
                say_kw = dict(untilDone=True, lipSync=True)
                if sys.platform == "darwin":
                    lip_delay = float(
                        getattr(self, "lipsync_afplay_start_delay_s", 0.15) or 0.15
                    )
                    if lip_delay > 0:
                        # ohbot: negative soundDelay delays lip movement (sound was starting late).
                        say_kw["soundDelay"] = -min(0.5, lip_delay)
                self._ohbot_say_raw(chunk, **say_kw)
            except Exception as e:
                try:
                    self._set_info(f"Speech error: {e}")
                except Exception:
                    pass
            finally:
                self._active_lip_reset_viseme_indices = []
            self._wait_lip_sync_thread_finished()
            self._reset_lips_to_initialized()
            time.sleep(max(0.0, float(pause_between_chunks_s)))

    def _get_japanese_voice_name(self):
        """Siri Voice 1 (Japan) / Hiro via macOS system voice (``say`` without ``-v``)."""
        if sys.platform != "darwin":
            return None
        if self._auto_ja_voice_name:
            return self._auto_ja_voice_name
        self._auto_ja_voice_name = MAC_SIRI_JA_SYSTEM_VOICE
        return MAC_SIRI_JA_SYSTEM_VOICE

    def _install_safe_speech_wrapper(self):
        try:
            raw = self._ohbot_say_raw

            def safe_say(text, *args, **kwargs):
                # If a call uses non-standard args, use original behavior.
                if args or kwargs:
                    return raw(text, *args, **kwargs)
                return self._speak_reliably(text)

            ohbot.say = safe_say
        except Exception:
            pass

    def _install_soft_lipsync_profile(self):
        """Map wav volume (0–10) to lip positions around rest; tune via lipsync_amplitude_scale."""
        try:
            panel = self

            def _soft_top(val):
                base = float(getattr(panel, "lipsync_top_rest", 0.0) or 0.0)
                scale = float(getattr(panel, "lipsync_amplitude_scale", 0.45) or 0.45)
                v = max(0.0, min(10.0, float(val)))
                return max(0.0, min(10.0, base + v * scale))

            def _soft_bottom(val):
                base = float(getattr(panel, "lipsync_bottom_rest", 6.0) or 6.0)
                scale = float(getattr(panel, "lipsync_amplitude_scale", 0.45) or 0.45)
                v = max(0.0, min(10.0, float(val)))
                return max(0.0, min(10.0, base + v * scale))

            ohbot._phonememapTop = _soft_top
            ohbot._phonememapBottom = _soft_bottom
        except Exception:
            pass

    def _install_lip_sync_without_library_neutral_pose(self):
        """Same viseme loop as ohbot._moveSpeech but omit final move to (5,5).

        Stock code ends with move(TOPLIP,5)/move(BOTTOMLIP,5). If your rest pose uses a
        low top lip (e.g. 0), that final step looks like the mouth jumps up before reset.
        """
        try:
            panel = self

            def _move_speech_no_final_55(phonemes, times, doMove):
                # Some synthesizers/voices can produce empty phoneme timing arrays for
                # very short utterances. The stock implementation assumes at least one
                # timestamp; guard to prevent thread crashes.
                if not times or not phonemes:
                    return
                startTime = time.time()
                timeNow = 0.0
                totalTime = times[len(times) - 1]
                currentX = -1
                reset_indices = list(getattr(panel, "_active_lip_reset_viseme_indices", None) or [])
                reset_idx = 0
                hold_s = float(getattr(panel, "lips_reset_hold_s", 0.09) or 0.09)
                rest_until = 0.0
                while timeNow < totalTime:
                    timeNow = time.time() - startTime
                    if time.time() < rest_until:
                        ohbot.wait(ohbot.WAITLONG)
                        continue
                    limit = min(len(times), len(phonemes))
                    for x in range(0, limit):
                        if timeNow > times[x] and x > currentX:
                            while reset_idx < len(reset_indices) and x >= reset_indices[reset_idx]:
                                if doMove:
                                    panel._reset_lips_to_initialized()
                                    rest_until = time.time() + max(0.05, hold_s)
                                reset_idx += 1
                            if time.time() < rest_until:
                                currentX = x
                                break
                            if str(ohbot.synthesizer).upper() == "FESTIVAL":
                                ohbot.lipTopPos = ohbot._phonememapTopFest(phonemes[x])
                                ohbot.lipBottomPos = ohbot._phonememapBottomFest(phonemes[x])
                            else:
                                ohbot.lipTopPos = ohbot._phonememapTop(phonemes[x])
                                ohbot.lipBottomPos = ohbot._phonememapBottom(phonemes[x])
                            if doMove:
                                ohbot.move(ohbot.TOPLIP, ohbot.lipTopPos, 10)
                                ohbot.move(ohbot.BOTTOMLIP, ohbot.lipBottomPos, 10)
                            currentX = x
                    ohbot.wait(ohbot.WAITLONG)

            ohbot._moveSpeech = _move_speech_no_final_55
        except Exception:
            pass

    def create_ui(self):
        """Create the user interface"""
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(N, S, E, W))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=0)  # status
        main_frame.rowconfigure(1, weight=0)  # speech controls (wpm)
        main_frame.rowconfigure(2, weight=1)  # guided vocab
        main_frame.rowconfigure(3, weight=0)  # smart IR
        main_frame.rowconfigure(4, weight=0)  # face guard
        main_frame.rowconfigure(5, weight=1)  # motor sliders

        # === HEADER ===
        header_frame = ttk.LabelFrame(main_frame, text="Status", padding="10")
        header_frame.grid(row=0, column=0, sticky=(E, W), pady=10)
        header_frame.columnconfigure(0, weight=1)

        status_text = "✓ Connected" if self.connected else "✗ Disconnected"
        self.status_label = ttk.Label(
            header_frame,
            text=status_text,
            font=("Arial", 12, "bold"),
            foreground="green" if self.connected else "red",
        )
        self.status_label.pack(side=LEFT)
        ttk.Button(header_frame, text="Reset", command=self.reset_to_defaults).pack(side=RIGHT, padx=5)

        # === SPEECH CONTROLS ===
        speech_frame = ttk.LabelFrame(main_frame, text="Speech", padding="15")
        speech_frame.grid(row=1, column=0, sticky=(N, S, E, W), padx=5, pady=5)
        speech_frame.columnconfigure(0, weight=1)

        self.info_label = ttk.Label(speech_frame, text="Ready", font=("Arial", 9))
        self.info_label.grid(row=0, column=0, sticky=W)

        ttk.Label(speech_frame, text="Speaking Speed (WPM):").grid(row=1, column=0, sticky=W, pady=5)
        self.speech_speed_var = IntVar(value=getattr(self, "default_speech_wpm", 180))
        speech_speed_slider = ttk.Scale(
            speech_frame,
            from_=50,
            to=300,
            orient=HORIZONTAL,
            variable=self.speech_speed_var,
            command=lambda v: self.update_speech_speed(int(float(v))),
        )
        speech_speed_slider.grid(row=1, column=1, sticky=(E, W), padx=5)

        # === VOICE CONTROL ===
        ttk.Label(speech_frame, text="Voice:").grid(row=2, column=0, sticky=W, pady=5)
        self.voice_var = StringVar(value=self.voice_var_value)
        self.voice_combo = ttk.Combobox(
            speech_frame,
            textvariable=self.voice_var,
            values=self.voice_options,
            state="readonly",
            width=22,
        )
        self.voice_combo.grid(row=2, column=1, sticky=W, padx=5)
        self.voice_combo.bind("<<ComboboxSelected>>", lambda e: self.on_voice_change())
        self._load_voices_async()

        # === AUTONOMOUS LESSON ===
        session_frame = ttk.LabelFrame(main_frame, text="Autonomous lesson", padding="15")
        session_frame.grid(row=2, column=0, sticky=(N, S, E, W), padx=5, pady=5)
        session_frame.columnconfigure(0, weight=1)

        autonomy_row = ttk.Frame(session_frame)
        autonomy_row.grid(row=0, column=0, sticky=(E, W), pady=4)

        ttk.Label(autonomy_row, text="Tasks:").grid(row=0, column=0, sticky=W)
        ttk.Button(autonomy_row, text="Task 1 start", command=self.start_task_1).grid(row=0, column=1, sticky=W, padx=6)
        ttk.Button(autonomy_row, text="Task 2 start", command=self.start_task_2).grid(row=0, column=2, sticky=W, padx=6)
        ttk.Button(autonomy_row, text="Stop", command=self.stop_autonomous_session).grid(row=0, column=3, sticky=W, padx=6)
        self.task2_screen_disclosure_var = BooleanVar(value=False)
        self.lesson_vocab_screen_disclosure_var = BooleanVar(value=True)
        ttk.Checkbutton(
            autonomy_row,
            text="Task 1 vocabulary on screen",
            variable=self.lesson_vocab_screen_disclosure_var,
        ).grid(row=0, column=4, sticky=W, padx=6)
        ttk.Checkbutton(
            autonomy_row,
            text="Task 2 screen disclosure (feedback lines)",
            variable=self.task2_screen_disclosure_var,
        ).grid(row=0, column=5, sticky=W, padx=6)
        ttk.Button(
            autonomy_row,
            text="Task 2 look start",
            command=self.start_task2_continuous_look_sweep,
        ).grid(row=0, column=6, sticky=W, padx=4)
        ttk.Button(
            autonomy_row,
            text="Task 2 look stop",
            command=self.stop_task2_continuous_look_sweep,
        ).grid(row=0, column=7, sticky=W, padx=4)

        # === SMART IR (Nous L5) ===
        ir_frame = ttk.LabelFrame(main_frame, text="Smart IR (Nous L5)", padding="10")
        ir_frame.grid(row=3, column=0, sticky=(E, W), padx=5, pady=5)
        ir_frame.columnconfigure(1, weight=1)

        self.ir_buttons_frame = ttk.Frame(ir_frame)
        self.ir_buttons_frame.grid(row=0, column=0, columnspan=4, sticky=(E, W), pady=4)

        ir_tools = ttk.Frame(ir_frame)
        ir_tools.grid(row=1, column=0, columnspan=4, sticky=(E, W), pady=4)
        ttk.Button(ir_tools, text="Refresh list", command=self._refresh_ir_buttons).pack(side=LEFT, padx=4)
        ttk.Button(ir_tools, text="Check online", command=self.ir_check).pack(side=LEFT, padx=4)
        ttk.Label(ir_tools, text="Learn name:").pack(side=LEFT, padx=(12, 4))
        self.ir_learn_name_var = StringVar(value="")
        ttk.Entry(ir_tools, textvariable=self.ir_learn_name_var, width=14).pack(side=LEFT, padx=2)
        ttk.Button(ir_tools, text="Learn", command=self.ir_learn).pack(side=LEFT, padx=4)

        if not self._ir_available:
            ttk.Label(
                ir_frame,
                text="IR unavailable — pip install tinytuya and set up ~/devices.json",
                foreground="gray",
            ).grid(row=2, column=0, columnspan=4, sticky=W)
        self._refresh_ir_buttons()

        # === FACE GUARD (calibration + other-person scan) ===
        face_guard_frame = ttk.LabelFrame(
            main_frame, text="Face guard (camera scan)", padding="10"
        )
        face_guard_frame.grid(row=4, column=0, sticky=(E, W), padx=5, pady=5)
        face_guard_frame.columnconfigure(1, weight=1)

        face_guard_row = ttk.Frame(face_guard_frame)
        face_guard_row.grid(row=0, column=0, columnspan=2, sticky=(E, W), pady=4)
        ttk.Button(
            face_guard_row,
            text="Calibrate user",
            command=self.start_face_calibration,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            face_guard_row,
            text="Start scan",
            command=self.start_face_detection,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            face_guard_row,
            text="Stop",
            command=self.stop_face_guard,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            face_guard_row,
            text="Clear profile",
            command=self.clear_face_guard_profile,
        ).pack(side=LEFT, padx=4)

        profile_hint = self._face_guard_profile_status_text()
        self.face_guard_status_label = ttk.Label(
            face_guard_frame,
            text=profile_hint if self._face_guard_available else "Face guard unavailable",
            font=("Arial", 9),
            wraplength=820,
        )
        self.face_guard_status_label.grid(row=1, column=0, columnspan=2, sticky=W, pady=(4, 0))
        if not self._face_guard_available:
            ttk.Label(
                face_guard_frame,
                text="pip install numpy==1.26.4 mediapipe opencv-python",
                foreground="gray",
            ).grid(row=2, column=0, columnspan=2, sticky=W)

        # === MOTOR CONTROLS WITH SLIDERS ===
        motor_control_frame = ttk.LabelFrame(main_frame, text="Motor Controls with Sliders", padding="15")
        motor_control_frame.grid(row=5, column=0, sticky=(N, S, E, W), padx=5, pady=5)
        motor_control_frame.columnconfigure(0, weight=1)

        self.motor_sliders = {}
        self.motor_defaults = {
            0: 5.5,
            1: 5,
            2: 5.5,
            6: 5.5,
            4: 0,
            5: 6,
            3: 10,
            7: 4,
        }
        motors = [
            ("Head Nod", 0),
            ("Head Turn", 1),
            ("Eye Turn", 2),
            ("Eye Tilt", 6),
            ("Top Lip", 4),
            ("Bottom Lip", 5),
            ("Lid Blink", 3),
            ("Head Roll", 7),
        ]
        for i, (motor_name, motor_index) in enumerate(motors):
            motor_frame = ttk.Frame(motor_control_frame)
            motor_frame.grid(row=i, column=0, sticky=(E, W), pady=5)
            motor_frame.columnconfigure(1, weight=1)
            ttk.Label(motor_frame, text=motor_name).grid(row=0, column=0, sticky=W)
            slider = ttk.Scale(
                motor_frame,
                from_=0,
                to=10,
                orient=HORIZONTAL,
                command=lambda value, idx=motor_index: self.move_motor_on_scroll(idx, value),
            )
            slider.grid(row=0, column=1, sticky=(E, W), padx=5)
            slider.set(self.motor_defaults.get(motor_index, 0))
            self.motor_sliders[motor_index] = slider

    def _face_guard_profile_status_text(self):
        if not self._face_guard_available:
            return "Face guard unavailable"
        if FaceGuardService.has_saved_profile():
            return f"Profile saved: {FaceGuardService.profile_path()}"
        return "No saved profile"

    def _refresh_face_guard_profile_status(self):
        self._set_face_guard_status(self._face_guard_profile_status_text())

    def _set_face_guard_status(self, text):
        if self.face_guard_status_label is not None:
            self.root.after(
                0, lambda t=str(text): self.face_guard_status_label.config(text=t)
            )

    def _face_guard_preview_geometry(self):
        """Place camera preview beside the control panel — not on the disclosure screen."""
        try:
            self.root.update_idletasks()
            rx = int(self.root.winfo_rootx())
            ry = int(self.root.winfo_rooty())
            rw = int(self.root.winfo_width() or 420)
            return f"680x520+{rx + rw + 16}+{max(0, ry)}"
        except Exception:
            return "680x520+80+80"

    def _open_face_guard_preview(self):
        if (
            self._face_guard_preview_window is not None
            and self._face_guard_preview_window.winfo_exists()
        ):
            self._face_guard_preview_window.geometry(self._face_guard_preview_geometry())
            self._face_guard_preview_window.deiconify()
            self._face_guard_preview_window.lift()
            return
        win = Toplevel(self.root)
        win.title("Face guard camera")
        win.geometry(self._face_guard_preview_geometry())
        win.configure(bg="black")
        lbl = Label(win, bg="black")
        lbl.pack(fill=BOTH, expand=True)
        win.protocol("WM_DELETE_WINDOW", self._hide_face_guard_preview)
        self._face_guard_preview_window = win
        self._face_guard_preview_label = lbl
        win.lift()

    def _hide_face_guard_preview(self):
        if self._face_guard_preview_window is not None:
            try:
                if self._face_guard_preview_window.winfo_exists():
                    self._face_guard_preview_window.withdraw()
            except Exception:
                pass

    def _close_face_guard_preview(self):
        self._face_guard_preview_photo = None
        if self._face_guard_preview_window is not None:
            try:
                if self._face_guard_preview_window.winfo_exists():
                    self._face_guard_preview_window.destroy()
            except Exception:
                pass
        self._face_guard_preview_window = None
        self._face_guard_preview_label = None

    def _face_guard_on_frame(self, frame_bgr):
        """Called from face-guard worker thread — marshal preview to tk main thread."""
        if not getattr(self, "face_guard_show_preview", False):
            return
        try:
            frame_copy = frame_bgr.copy()
        except Exception:
            return
        self.root.after(0, lambda img=frame_copy: self._face_guard_update_preview(img))

    def _face_guard_update_preview(self, frame_bgr):
        if self._face_guard_preview_label is None:
            self._open_face_guard_preview()
        if self._face_guard_preview_label is None:
            return
        try:
            import cv2

            frame = frame_bgr
            height, width = frame.shape[:2]
            max_w = int(self._face_guard_preview_max_width or 640)
            if width > max_w > 0:
                scale = max_w / float(width)
                frame = cv2.resize(
                    frame,
                    (int(width * scale), int(height * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            from PIL import Image, ImageTk

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(image=Image.fromarray(rgb))
            self._face_guard_preview_photo = photo
            self._face_guard_preview_label.config(image=photo)
        except ImportError:
            self._set_face_guard_status(
                "Camera preview needs pillow: pip install pillow"
            )
        except Exception:
            pass

    def _get_face_guard_service(self):
        on_frame = (
            self._face_guard_on_frame
            if getattr(self, "face_guard_show_preview", False)
            else None
        )
        if self._face_guard_service is None:
            self._face_guard_service = FaceGuardService(
                stop_event=self._face_guard_stop,
                on_status=self._set_face_guard_status,
                on_frame=on_frame,
                ready_event=self._face_guard_ready,
            )
        else:
            self._face_guard_service.on_frame = on_frame
            self._face_guard_service.ready_event = self._face_guard_ready
        return self._face_guard_service

    def _face_guard_thread_running(self):
        thread = getattr(self, "_face_guard_thread", None)
        return thread is not None and thread.is_alive()

    def _begin_face_guard_thread(self, target, info_msg, allow_during_lesson=False):
        if self._face_guard_thread_running():
            self._set_face_guard_status("Face guard already running")
            return False
        if (
            not allow_during_lesson
            and self.autonomy_thread
            and self.autonomy_thread.is_alive()
        ):
            messagebox.showwarning(
                "Face guard",
                "Stop the autonomous lesson first — both use head motors.",
            )
            return False
        self._face_guard_stop.clear()
        self._face_guard_ready.clear()
        if getattr(self, "face_guard_show_preview", False):
            self.root.after(0, self._open_face_guard_preview)
        self._face_guard_thread = threading.Thread(target=target, daemon=True)
        self._face_guard_thread.start()
        self._set_face_guard_status(info_msg)
        return True

    def _start_face_guard_scan(self, from_lesson=False, status_msg=None):
        """Start background face-guard detection (optional during Task 2 lesson)."""
        if not self._face_guard_available:
            return False
        if self._face_guard_thread_running():
            return True
        if not FaceGuardService.has_saved_profile():
            if from_lesson:
                print("[TASK2] face scan skipped — run Calibrate user first")
                return False
            messagebox.showwarning(
                "Face guard",
                "No saved profile. Run Calibrate user first.",
            )
            return False

        def _worker():
            try:
                service = self._get_face_guard_service()
                service.run_detection(show_window=False)
            except Exception as exc:
                self._set_face_guard_status(f"Detection error: {exc}")
                try:
                    print("[FACE_GUARD_DETECT_ERROR]", exc)
                    print(traceback.format_exc())
                except Exception:
                    pass
            finally:
                self.root.after(0, self._close_face_guard_preview)

        ok = self._begin_face_guard_thread(
            _worker,
            status_msg
            or "Scanning — returns to centre when complete",
            allow_during_lesson=from_lesson,
        )
        return ok

    def start_task2_face_scan(self):
        """Task 2 — scan for another person; sets ``_task2_bystander_detected``."""
        self._task2_bystander_detected = False
        if not self._task2_screen_disclosure_enabled():
            return True
        if not self._face_guard_available:
            print("[TASK2] face scan skipped — face guard unavailable")
            return True
        if self._face_guard_thread_running():
            print("[TASK2] face scan blocked — face guard already running")
            return False
        if not FaceGuardService.has_saved_profile():
            print("[TASK2] face scan skipped — run Calibrate user first")
            return False
        if self.stop_event.is_set():
            return False

        self._face_guard_stop.clear()
        self._face_guard_ready.clear()
        self._set_face_guard_status(
            "Task 2 — scanning until another person detected, then returning to rest..."
        )
        hold_s = float(getattr(self, "task2_face_scan_face_hold_s", 1.0) or 1.0)
        settled_frames = int(getattr(self, "task2_face_scan_settled_frames", 10) or 10)
        center_tol = getattr(self, "task2_face_scan_center_tolerance", 0.12)
        head_delta = getattr(self, "task2_face_scan_head_delta", 0.08)
        max_facing_s = float(getattr(self, "task2_face_scan_max_s", 12.0) or 12.0)
        rest_snap = self._snapshot_head_eye_pose()
        rest_turn = float(rest_snap.get(ohbot.HEADTURN, self.motor_defaults.get(1, 5)))
        rest_nod = float(rest_snap.get(ohbot.HEADNOD, self.motor_defaults.get(0, 5.5)))
        service = None

        try:
            service = self._get_face_guard_service()
            result = service.run_detection(
                show_window=False,
                task2_one_shot=True,
                extra_stop_event=self.stop_event,
                task2_facing_hold_s=hold_s,
                task2_facing_settled_frames=settled_frames,
                task2_facing_center_tolerance=center_tol,
                task2_facing_head_delta=head_delta,
                task2_facing_max_s=max_facing_s,
                rest_turn=rest_turn,
                rest_nod=rest_nod,
            )
            if isinstance(result, tuple):
                ok, other_detected = result
            else:
                ok, other_detected = bool(result), False
            self._task2_bystander_detected = bool(other_detected)
            if self.stop_event.is_set() or self._face_guard_stop.is_set():
                return False
            if not ok:
                print("[TASK2] face scan failed")
                return False
            if self._task2_bystander_detected:
                print("[TASK2] face scan complete — bystander DETECTED (disclosure allowed)")
            else:
                print("[TASK2] face scan complete — no bystander (skip screen disclosure)")
            return True
        except Exception as exc:
            self._task2_bystander_detected = False
            self._set_face_guard_status(f"Task 2 face scan error: {exc}")
            try:
                print("[TASK2_FACE_SCAN_ERROR]", exc)
                print(traceback.format_exc())
            except Exception:
                pass
            return False
        finally:
            self.root.after(0, self._close_face_guard_preview)
            if rest_snap and self.connected:
                try:
                    self._restore_head_eye_pose(rest_snap, smooth=True)
                except Exception:
                    pass

    def stop_task2_face_scan(self):
        """Task 2 — stop face scan after sensitive disclosure block."""
        self.stop_face_guard()

    def _clear_face_guard_profile_after_task(self):
        """Delete calibrated profile after a lesson ends (no confirmation dialog)."""
        if not self._face_guard_available:
            return
        try:
            service = getattr(self, "_face_guard_service", None)
            if service is not None:
                ok, message = service.forget_profile()
            else:
                ok, message = FaceGuardService.clear_saved_profile()
            print(f"[FACE_GUARD] after task: {message}")
            self.root.after(0, self._refresh_face_guard_profile_status)
        except Exception as exc:
            print(f"[FACE_GUARD] after-task clear failed: {exc}")

    def start_face_calibration(self):
        if not self._face_guard_available:
            messagebox.showerror(
                "Face guard",
                "Face guard unavailable — install mediapipe, opencv-python, numpy==1.26.4",
            )
            return
        if not self.connected:
            messagebox.showerror("Face guard", "Ohbot is not connected.")
            return

        def _worker():
            try:
                service = self._get_face_guard_service()
                service.calibrate(show_window=False)
            except Exception as exc:
                self._set_face_guard_status(f"Calibration error: {exc}")
                try:
                    print("[FACE_GUARD_CALIB_ERROR]", exc)
                    print(traceback.format_exc())
                except Exception:
                    pass
            finally:
                self.root.after(0, self._close_face_guard_preview)
                self.root.after(0, self._refresh_face_guard_profile_status)

        self._begin_face_guard_thread(
            _worker, "Calibrating — look at camera"
        )

    def clear_face_guard_profile(self):
        """Remove all saved face-guard calibration data."""
        if not self._face_guard_available:
            messagebox.showerror(
                "Face guard",
                "Face guard unavailable — install mediapipe, opencv-python, numpy==1.26.4",
            )
            return
        if not messagebox.askyesno(
            "Clear profile",
            "Remove the saved face scan profile?\n\n"
            "You will need to run Calibrate user again before scanning.",
        ):
            return

        if self._face_guard_thread_running():
            self.stop_face_guard()

        service = getattr(self, "_face_guard_service", None)
        if service is not None:
            ok, message = service.forget_profile()
        else:
            ok, message = FaceGuardService.clear_saved_profile()

        if ok:
            self._refresh_face_guard_profile_status()
            messagebox.showinfo("Clear profile", message)
        else:
            messagebox.showerror("Clear profile", message)

    def start_face_detection(self):
        if not self._face_guard_available:
            messagebox.showerror(
                "Face guard",
                "Face guard unavailable — install mediapipe, opencv-python, numpy==1.26.4",
            )
            return
        if not self.connected:
            messagebox.showerror("Face guard", "Ohbot is not connected.")
            return
        self._start_face_guard_scan(from_lesson=False)

    def stop_face_guard(self, status_msg=None):
        """Stop camera/scan threads. Does NOT delete the saved user profile."""
        self._face_guard_stop.set()
        self._face_guard_ready.clear()
        thread = getattr(self, "_face_guard_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=8.0)
            except Exception:
                pass
        self._face_guard_thread = None
        service = getattr(self, "_face_guard_service", None)
        if service is not None:
            try:
                # Close camera/models only — keep in-memory + on-disk profile.
                service.close(destroy_windows=True)
            except Exception:
                pass
        try:
            self.root.after(0, self._close_face_guard_preview)
        except Exception:
            self._close_face_guard_preview()
        # Always restore profile status so Task 2 end does not look like a wipe.
        if status_msg:
            self._set_face_guard_status(status_msg)
        try:
            self.root.after(0, self._refresh_face_guard_profile_status)
        except Exception:
            self._refresh_face_guard_profile_status()

    def _set_info(self, text):
        self.root.after(0, lambda: self.info_label.config(text=text))

    def _note_ir_buttons_sent(self, names):
        """Track base power: on-commands turn base on; only ``off`` turns it off."""
        on_cmds = frozenset(
            str(c).strip()
            for c in getattr(self, "ir_base_on_commands", ())
            if str(c).strip()
        )
        off_btn = str(getattr(self, "ir_shutdown_button", "off") or "off").strip()
        with self._ir_power_lock:
            for btn in names:
                b = str(btn).strip()
                if b == off_btn:
                    self._ir_base_is_on = False
                elif b in on_cmds:
                    self._ir_base_is_on = True

    def ir_send(self, name, also=None, interval_s=None, pause_after_s=0.0, together=None, blocking=False):
        """Send one or more learned IR buttons.

        With ``also=``, buttons fire at the same time by default (no interval).
        ``pause_after_s`` waits for the IR base (not Ohbot) after the send completes.
        ``blocking=True`` holds the caller until sends and any pause finish (honours Stop).

        Examples::

            self.ir_send("on")
            self.ir_send("on", also="speed")                    # both at once
            self.ir_send("on", also="speed", together=True)     # same
            self.ir_send(["on", "speed"], together=True)        # list, all at once
            self.ir_send("on", also="speed", interval_s=0.5)    # one after another
            self.ir_send("clockwise", pause_after_s=3.0, blocking=True)
        """
        if not self._ir_available or self.IR_send_base is None:
            self._set_info("IR control unavailable (tinytuya / ~/devices.json)")
            return

        if isinstance(name, (list, tuple)):
            names = [str(n).strip() for n in name if str(n).strip()]
        else:
            names = [str(name).strip()]
        if also:
            names.append(str(also).strip())
        if not names:
            return

        if together is None:
            # ``also`` without ``interval_s`` → send at the same time
            together = also is not None and interval_s is None

        gap = 0.0 if together else float(
            interval_s
            if interval_s is not None
            else getattr(self, "ir_send_interval_s", 0.35) or 0.35
        )
        gap = max(0.0, min(10.0, gap))
        pause = max(0.0, float(pause_after_s or 0.0))
        result = {"ok": True}

        def _worker():
            try:
                if together and len(names) > 1:
                    errors = []

                    def _send_one(btn):
                        try:
                            self.IR_send_base(btn)
                        except Exception as exc:
                            errors.append(exc)

                    threads = [
                        threading.Thread(target=_send_one, args=(btn,), daemon=True)
                        for btn in names
                    ]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(timeout=15.0)
                    if errors:
                        raise errors[0]
                else:
                    if len(names) > 1:
                        if not self.IR_send_base.sequence(
                            names, gap_s=gap, sleep_fn=self._sleep_interruptible
                        ):
                            result["ok"] = False
                            return
                    else:
                        self.IR_send_base(names[0])
                    if self.stop_event.is_set():
                        result["ok"] = False
                        return
                if pause > 0:
                    if not self._sleep_interruptible(pause):
                        result["ok"] = False
                        return
                self._note_ir_buttons_sent(names)
                label = " + ".join(names) if together else " → ".join(names)
                self.root.after(0, lambda t=label: self._set_info(f"IR sent: {t}"))
            except Exception as exc:
                result["ok"] = False
                self.root.after(0, lambda e=exc: self._set_info(f"IR error: {e}"))

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        if blocking:
            worker.join(timeout=120.0)
            return result["ok"] and not self.stop_event.is_set()
        return None

    def ir_send_shutdown(self, blocking=False):
        """Send ``off`` when tracked base is on (power/on/clockwise/speed_* sent this session)."""
        btn = str(getattr(self, "ir_shutdown_button", "off") or "off").strip()

        def _do():
            return _emergency_ir_off(btn)

        if blocking:
            return _do()
        threading.Thread(target=_do, daemon=True).start()
        return None

    def _ir_stop_base_if_on(self, blocking=False):
        """Stop-button rule: only send ``off`` if an on-command was sent earlier."""
        if not getattr(self, "ir_send_off_on_stop", True):
            return False
        with self._ir_power_lock:
            if not self._ir_base_is_on:
                print("[IR] stop: base already off — off not sent")
                return False
        return bool(self.ir_send_shutdown(blocking=blocking))

    _IR_BUTTON_ORDER = (
        "power",
        "on",
        "off",
        "clockwise",
        "anticlockwise",
        "speed_0",
        "speed_1",
        "speed_2",
    )

    def _sorted_ir_button_names(self, names):
        order = {n: i for i, n in enumerate(self._IR_BUTTON_ORDER)}

        def key(name):
            return (order.get(name, len(order)), str(name).lower())

        return sorted(names, key=key)

    def _refresh_ir_buttons(self):
        if self.ir_buttons_frame is None:
            return
        for child in self.ir_buttons_frame.winfo_children():
            child.destroy()
        if not self._ir_available or self.IR_send_base is None:
            ttk.Label(
                self.ir_buttons_frame,
                text="(IR not loaded — run from examples/ or click Refresh list after fixing import)",
            ).pack(side=LEFT)
            return
        names = self._sorted_ir_button_names(self.IR_send_base.names())
        if not names:
            ttk.Label(
                self.ir_buttons_frame,
                text="No buttons learned — use Learn or: python Nous_control_python.py learn <name>",
            ).pack(side=LEFT)
            return
        for name in names:
            ttk.Button(
                self.ir_buttons_frame,
                text=name,
                command=lambda n=name: self.ir_send(n),
            ).pack(side=LEFT, padx=4, pady=2)

    def ir_check(self):
        if not self._ir_available:
            self._set_info("IR control unavailable")
            return

        def _worker():
            try:
                device_id, ip, local_key, _device_version = _ir_load_device(_IR_DEVICE_ID)
                _ir_check_device(device_id, ip, local_key)
                self.root.after(0, lambda: self._set_info("IR check finished — see terminal"))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._set_info(f"IR check error: {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def ir_learn(self):
        if not self._ir_available:
            self._set_info("IR control unavailable")
            return
        name = (self.ir_learn_name_var.get() if self.ir_learn_name_var else "").strip()
        if not name:
            messagebox.showwarning("Smart IR", "Enter a name for the button (e.g. on, power).")
            return
        self._set_info(f"Learning IR '{name}' — press remote at L5 (see terminal)")

        def _worker():
            try:
                device_id, ip, local_key, _device_version = _ir_load_device(_IR_DEVICE_ID)
                ok = _ir_learn_button(device_id, ip, local_key, name)
                self.root.after(0, self._refresh_ir_buttons)
                if ok:
                    self.root.after(0, lambda n=name: self._set_info(f"IR learned: {n}"))
                else:
                    self.root.after(0, lambda: self._set_info("IR learn failed"))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._set_info(f"IR learn error: {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _lesson_vocab_disclosure_font(self):
        size = int(getattr(self, "lesson_vocab_disclosure_font_size", 120) or 120)
        if sys.platform == "darwin":
            return ("Hiragino Sans", size, "bold")
        return ("Arial", size, "bold")

    def _lesson_vocab_single_word_font(self):
        """Larger font when only the Japanese word is shown (e.g. writing task)."""
        size = int(getattr(self, "lesson_vocab_single_word_font_size", 200) or 200)
        if sys.platform == "darwin":
            return ("Hiragino Sans", size, "bold")
        return ("Arial", size, "bold")

    def _task2_feedback_disclosure_font(self):
        size = int(getattr(self, "task2_feedback_disclosure_font_size", 52) or 52)
        return ("Arial", size, "bold")

    def _disclosure_wraplength_px(self, configured_attr="task2_feedback_disclosure_wraplength"):
        wrap = int(getattr(self, configured_attr, 1700) or 1700)
        win = getattr(self, "task2_disclosure_window", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.update_idletasks()
                    w = int(win.winfo_width() or 1920)
                    wrap = min(wrap, max(480, w - 140))
            except Exception:
                pass
        return wrap

    def _vocab_disclosure_max_chars(self, font_size):
        wrap = self._disclosure_wraplength_px("lesson_vocab_disclosure_wraplength")
        return max(6, int(wrap / (0.62 * max(24, int(font_size)))))

    def _wrap_vocab_disclosure_line(self, text, font_size):
        """Wrap romaji / meaning so large vocab font stays on screen."""
        text = str(text or "").strip()
        if not text:
            return ""
        text = text.replace("/", " / ")
        max_chars = self._vocab_disclosure_max_chars(font_size)
        if len(text) <= max_chars:
            return text

        if "-" in text:
            tokens = []
            for i, seg in enumerate(text.split("-")):
                tokens.append(seg if i == 0 else "-" + seg)
            lines = []
            current = []
            cur_len = 0
            for tok in tokens:
                if current and cur_len + len(tok) > max_chars:
                    lines.append("".join(current))
                    current = [tok]
                    cur_len = len(tok)
                else:
                    current.append(tok)
                    cur_len += len(tok)
            if current:
                lines.append("".join(current))
            return "\n".join(lines)

        words = text.split()
        if len(words) > 1:
            lines = []
            current = []
            for word in words:
                trial = " ".join(current + [word])
                if current and len(trial) > max_chars:
                    lines.append(" ".join(current))
                    current = [word]
                else:
                    current.append(word)
            if current:
                lines.append(" ".join(current))
            return "\n".join(lines)

        # Long unbroken string (e.g. Japanese): hard-wrap by character count.
        chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
        return "\n".join(chunks)

    def _set_disclosure_label(self, label, text, font=None, wraplength=None):
        if label is None:
            return
        kw = dict(
            text=text,
            justify=CENTER,
            anchor=CENTER,
            wraplength=int(wraplength or 0),
        )
        if font is not None:
            kw["font"] = font
        label.config(**kw)

    def _task2_feedback_chars_per_line(self):
        font_size = int(getattr(self, "task2_feedback_disclosure_font_size", 80) or 80)
        wrap = self._disclosure_wraplength_px("task2_feedback_disclosure_wraplength")
        auto = max(10, int(wrap / (0.58 * max(24, font_size))))
        manual = int(getattr(self, "task2_feedback_disclosure_chars_per_line", 0) or 0)
        if manual > 0:
            return min(manual, auto)
        return auto

    def _wrap_task2_feedback_lines(self, text):
        """Split feedback into lines that fit the disclosure window at the current font size."""
        text = " ".join(str(text or "").split())
        if not text:
            return []
        max_chars = self._task2_feedback_chars_per_line()
        words = text.split()
        lines = []
        current = []

        def _flush():
            if current:
                lines.append(" ".join(current))

        for word in words:
            if len(word) > max_chars:
                _flush()
                lines.append(word)
                current = []
                continue
            trial = " ".join(current + [word])
            if current and len(trial) > max_chars:
                _flush()
                current = [word]
            else:
                current.append(word)
        _flush()
        return lines

    def _position_disclosure_text_frame(self, single_word=False):
        """Place the text block lower on the disclosure screen (centred horizontally)."""
        frame = getattr(self, "_disclosure_text_frame", None)
        if frame is None:
            return
        if single_word:
            win = getattr(self, "task2_disclosure_window", None)
            try:
                if win is not None and win.winfo_exists():
                    win.update_idletasks()
            except Exception:
                pass
            rely = float(
                getattr(self, "lesson_vocab_single_word_rely", 0.50) or 0.50
            )
        else:
            rely = float(getattr(self, "disclosure_text_rely", 0.72) or 0.72)
        rely = max(0.35, min(0.9, rely))
        frame.place(relx=0.5, rely=rely, anchor=CENTER, relwidth=0.92)

    def _sync_vocab_disclosure_labels(self, show_romaji, show_meaning):
        """Single-word mode: hide empty romaji/meaning rows so rely applies to the Japanese line only."""
        line_pady = (0, 4)
        if self.task2_disclosure_ja_label is not None and not self.task2_disclosure_ja_label.winfo_ismapped():
            self.task2_disclosure_ja_label.pack(fill=X, pady=line_pady)
        for label, show in (
            (self.task2_disclosure_romaji_label, show_romaji),
            (self.task2_disclosure_meaning_label, show_meaning),
        ):
            if label is None:
                continue
            if show:
                if not label.winfo_ismapped():
                    label.pack(fill=X, pady=line_pady)
            else:
                label.pack_forget()

    def _ensure_disclosure_window(self):
        if self.task2_disclosure_window is not None and self.task2_disclosure_window.winfo_exists():
            return
        self.task2_disclosure_window = Toplevel(self.root)
        self.task2_disclosure_window.title("Lesson disclosure")
        self.task2_disclosure_window.geometry("1920x620")
        self.task2_disclosure_window.configure(bg="black")
        outer = Frame(self.task2_disclosure_window, bg="black")
        outer.pack(fill=BOTH, expand=True, padx=40, pady=16)
        frame = Frame(outer, bg="black")
        self._disclosure_text_frame = frame
        label_kw = dict(
            font=self._lesson_vocab_disclosure_font(),
            fg="white",
            bg="black",
            justify=CENTER,
            anchor=CENTER,
        )
        line_pady = (0, 4)
        self.task2_disclosure_ja_label = Label(frame, text="", **label_kw)
        self.task2_disclosure_ja_label.pack(fill=X, pady=line_pady)
        self.task2_disclosure_romaji_label = Label(frame, text="", **label_kw)
        self.task2_disclosure_romaji_label.pack(fill=X, pady=line_pady)
        self.task2_disclosure_meaning_label = Label(frame, text="", **label_kw)
        self.task2_disclosure_meaning_label.pack(fill=X, pady=line_pady)
        self._position_disclosure_text_frame()
        self.task2_disclosure_window.protocol("WM_DELETE_WINDOW", self._clear_task2_disclosure_text)

    def _raise_disclosure_window(self):
        if self.task2_disclosure_window is not None and self.task2_disclosure_window.winfo_exists():
            self.task2_disclosure_window.deiconify()
            self.task2_disclosure_window.lift()
            self.task2_disclosure_window.focus_force()

    def _disclosure_label_single_line(self, label, text, justify=CENTER, font=None):
        """Legacy helper — prefer ``_set_disclosure_label`` with wraplength."""
        wrap = self._disclosure_wraplength_px("task2_feedback_disclosure_wraplength")
        self._set_disclosure_label(label, text, font=font, wraplength=wrap if text else 0)

    def _show_task2_disclosure(self, text):
        """Task 2 sensitive feedback: large multi-line text, centred and width-limited."""
        self._ensure_disclosure_window()
        self._position_disclosure_text_frame()
        feedback_font = self._task2_feedback_disclosure_font()
        wrap = self._disclosure_wraplength_px("task2_feedback_disclosure_wraplength")
        lines = self._wrap_task2_feedback_lines(text)
        display = "\n".join(lines)
        self._set_disclosure_label(
            self.task2_disclosure_ja_label, display, font=feedback_font, wraplength=wrap
        )
        self._set_disclosure_label(self.task2_disclosure_romaji_label, "", font=feedback_font, wraplength=0)
        self._set_disclosure_label(self.task2_disclosure_meaning_label, "", font=feedback_font, wraplength=0)
        self._raise_disclosure_window()

    def _show_lesson_vocab_disclosure(self, japanese_text, romaji_text="", meaning_text=""):
        self._ensure_disclosure_window()
        ja = str(japanese_text or "").strip()
        romaji = str(romaji_text or "").strip()
        meaning = str(meaning_text or "").strip()
        ja_only = bool(ja) and not romaji and not meaning

        if ja_only:
            font_size = int(
                getattr(self, "lesson_vocab_single_word_font_size", 200) or 200
            )
            vocab_font = self._lesson_vocab_single_word_font()
        else:
            font_size = int(getattr(self, "lesson_vocab_disclosure_font_size", 120) or 120)
            vocab_font = self._lesson_vocab_disclosure_font()

        wrap = self._disclosure_wraplength_px("lesson_vocab_disclosure_wraplength")
        self._set_disclosure_label(
            self.task2_disclosure_ja_label,
            self._wrap_vocab_disclosure_line(ja, font_size),
            font=vocab_font,
            wraplength=wrap,
        )
        self._set_disclosure_label(
            self.task2_disclosure_romaji_label,
            self._wrap_vocab_disclosure_line(romaji, font_size) if romaji else "",
            font=vocab_font,
            wraplength=wrap if romaji else 0,
        )
        self._set_disclosure_label(
            self.task2_disclosure_meaning_label,
            self._wrap_vocab_disclosure_line(meaning, font_size) if meaning else "",
            font=vocab_font,
            wraplength=wrap if meaning else 0,
        )
        if ja_only:
            self._sync_vocab_disclosure_labels(False, False)
            self._position_disclosure_text_frame(single_word=True)
        else:
            self._sync_vocab_disclosure_labels(bool(romaji), bool(meaning))
            self._position_disclosure_text_frame()
        self._raise_disclosure_window()

    def _hide_task2_disclosure(self):
        if self.task2_disclosure_window is not None and self.task2_disclosure_window.winfo_exists():
            self.task2_disclosure_window.destroy()
        self.task2_disclosure_window = None
        self._disclosure_text_frame = None
        self.task2_disclosure_ja_label = None
        self.task2_disclosure_romaji_label = None
        self.task2_disclosure_meaning_label = None

    def _clear_task2_disclosure_text(self):
        if self.task2_disclosure_window is None or not self.task2_disclosure_window.winfo_exists():
            return
        if self.task2_disclosure_ja_label is not None:
            self.task2_disclosure_ja_label.config(text="")
        if self.task2_disclosure_romaji_label is not None:
            self.task2_disclosure_romaji_label.config(text="")
        if self.task2_disclosure_meaning_label is not None:
            self.task2_disclosure_meaning_label.config(text="")

    def _lesson_vocab_disclosure_enabled(self):
        return bool(
            self.lesson_vocab_screen_disclosure_var
            and self.lesson_vocab_screen_disclosure_var.get()
        )

    def _set_lesson_vocab_disclosure(self, japanese_text, romaji_text=None, meaning_text=None):
        """Show Japanese, romaji, and English meaning (top to bottom) until cleared."""
        if not self._lesson_vocab_disclosure_enabled():
            return
        ja = str(japanese_text or "").strip()
        romaji = "" if romaji_text is None else str(romaji_text).strip()
        meaning = "" if meaning_text is None else str(meaning_text).strip()
        if not ja:
            return
        self.root.after(
            0, lambda j=ja, r=romaji, m=meaning: self._show_lesson_vocab_disclosure(j, r, m)
        )

    def _clear_lesson_vocab_disclosure(self):
        if not self._lesson_vocab_disclosure_enabled():
            return
        self.root.after(0, self._clear_task2_disclosure_text)

    def _task2_screen_disclosure_enabled(self):
        return bool(self.task2_screen_disclosure_var and self.task2_screen_disclosure_var.get())

    def _task2_bystander_disclosure_active(self):
        """Screen-only sensitive feedback requires checkbox + positive bystander detect."""
        return (
            self._task2_screen_disclosure_enabled()
            and bool(getattr(self, "_task2_bystander_detected", False))
        )

    def _task2_ir_send(
        self,
        name,
        also=None,
        interval_s=None,
        pause_after_s=0.0,
        together=None,
        blocking=True,
    ):
        """Task 2 IR — only with screen disclosure + bystander; pause_after_s waits for base."""
        if not self._task2_bystander_disclosure_active():
            return True
        result = self.ir_send(
            name,
            also=also,
            interval_s=interval_s,
            pause_after_s=pause_after_s,
            together=together,
            blocking=blocking,
        )
        if blocking:
            return bool(result)
        return True

    def _task2_ir_base_settle_seconds(self):
        """Seconds to wait after rotation before off (0 is valid — do not use ``or``)."""
        v = getattr(self, "task2_ir_base_settle_s", 2.0)
        if v is None:
            v = 2.0
        return max(0.0, float(v))

    def _task2_ir_send_rotate_then_off(self, direction):
        """Rotate base, wait ``task2_ir_base_settle_s`` in lesson thread, then off."""
        if not self._task2_bystander_disclosure_active():
            if self._task2_screen_disclosure_enabled():
                print("[IR] task2 skipped — no bystander detected")
            return True
        if not self._ir_available or self.IR_send_base is None:
            print(
                "[IR] task2 skipped — IR not loaded (run from examples/ or install tinytuya + devices.json)"
            )
            return True
        if self.stop_event.is_set():
            return False
        direction = str(direction or "").strip()
        off_btn = str(getattr(self, "ir_shutdown_button", "off") or "off").strip()
        settle = self._task2_ir_base_settle_seconds()
        try:
            if not self.IR_send_base.send_fast(direction, nowait=False):
                return False
            self._note_ir_buttons_sent([direction])
            if settle > 0:
                print(f"[IR] base settle {settle:.1f}s before off...")
                if not self._sleep_interruptible(settle):
                    return False
            if not self.IR_send_base.send_fast(off_btn, nowait=False):
                return False
            self._note_ir_buttons_sent([off_btn])
            return not self.stop_event.is_set()
        except Exception as exc:
            print(f"[IR] task2 {direction} → {off_btn} failed: {exc}")
            self._set_info(f"IR error: {exc}")
            return False

    def _task2_say_with_optional_disclosure(self, text):
        """Screen-only sensitive line only if bystander was detected; else skip or speak."""
        if self._task2_screen_disclosure_enabled():
            if not getattr(self, "_task2_bystander_detected", False):
                print(
                    "[TASK2] skip screen disclosure — no bystander detected "
                    f"(line not shown): {text[:60]}..."
                )
                return True
            self.root.after(0, lambda t=text: self._show_task2_disclosure(t))
            # In disclosure mode, these lines are intentionally screen-only.
            hold = float(getattr(self, "task2_screen_disclosure_display_s", 10.0) or 10.0)
            hold = max(2.0, min(120.0, hold))
            ok = self._sleep_interruptible(hold)
            self.root.after(0, self._clear_task2_disclosure_text)
            return ok
        return self._session_say(text)

    def _task2_feedback_attention_pose(self):
        """Before Task 2 sensitive feedback: look left when lines are spoken; stay centred when screen-only disclosure."""
        if self._task2_screen_disclosure_enabled():
            self._do_little_movement(attention_side="center")
        else:
            self.attention_side_left(delta_units=1.5)

    def _say_lesson_line(self, text, optional_disclosure=False):
        """Speak a line; Task 2 can show sensitive feedback on screen instead when disclosure is enabled."""
        if optional_disclosure:
            #self._task2_feedback_attention_pose()
            return self._task2_say_with_optional_disclosure(text)
        return self._session_say(text)

    def _hold_and_wait(self, wait_s, extra_s=0):
        """Hold the current pose for `wait_s` (+ optional `extra_s`) seconds.

        Used at every script point where the robot pauses to give the
        participant time to respond. The robot stays where the last gesture
        left it — no microphone or audio capture is involved.

        - Head, eyes, body do NOT move during the wait.
        - Idle eyelid blinks continue so it doesn't look frozen.
        - Honors stop_event so Stop is responsive.

        Returns (text, status) for compatibility with previous call sites:
        always ("", "wait_only"), or ("", "stopped") if interrupted.
        """
        total = float(max(0.0, wait_s)) + float(max(0.0, extra_s))
        if total <= 0:
            return "", "wait_only"
        deadline = time.time() + total
        while time.time() < deadline:
            if self.stop_event.is_set():
                return "", "stopped"
            try:
                self._blink_idle_if_due()
            except Exception:
                pass
            remaining = deadline - time.time()
            time.sleep(min(0.2, max(0.0, remaining)))
        return "", "wait_only"

    def _blink_idle_if_due(self):
        """Eyelid blink only (no head motion), using same cadence as _do_little_movement."""
        if getattr(self, "_continuous_blink_active", False):
            return
        if not self.connected:
            return
        try:
            now = time.time()
            if now >= getattr(self, "_next_blink_at", 0):
                self._perform_blink_once(smooth=False)
                self._next_blink_at = now + random.uniform(2.5, 6.0)
        except Exception:
            pass

    def _lid_blink_closed_position(self, close_fraction=None):
        """Lid motor value at blink bottom; ``close_fraction`` 0.7 = 70% close, not full shut."""
        lid_open = float(self.motor_defaults.get(3, 10))
        frac = float(
            close_fraction
            if close_fraction is not None
            else getattr(self, "continuous_blink_close_fraction", 0.70) or 0.70
        )
        frac = min(1.0, max(0.0, frac))
        return int(round(max(0.0, lid_open * (1.0 - frac))))

    def _perform_blink_once(self, smooth=True, step_delay_s=None, close_fraction=None):
        """Close then open eyelids. ``close_fraction`` limits how far lids shut (default full for idle)."""
        if not self.connected:
            return False
        try:
            lid_open = int(round(float(self.motor_defaults.get(3, 10))))
            if close_fraction is None and not smooth:
                lid_closed = 0
            else:
                lid_closed = self._lid_blink_closed_position(close_fraction)
            lid_closed = min(lid_open, max(0, lid_closed))
            delay = float(
                step_delay_s
                if step_delay_s is not None
                else getattr(self, "continuous_blink_step_delay_s", 0.02) or 0.02
            )
            if smooth:
                for pos in range(lid_open, lid_closed - 1, -1):
                    if self._continuous_blink_stop.is_set() or self.stop_event.is_set():
                        return False
                    ohbot.move(ohbot.LIDBLINK, max(0, pos))
                    if not self._sleep_interruptible(delay):
                        return False
                for pos in range(lid_closed, lid_open + 1):
                    if self._continuous_blink_stop.is_set() or self.stop_event.is_set():
                        return False
                    ohbot.move(ohbot.LIDBLINK, min(10, pos))
                    if not self._sleep_interruptible(delay):
                        return False
            else:
                ohbot.move(ohbot.LIDBLINK, lid_closed)
                if not self._sleep_interruptible(random.uniform(0.06, 0.10)):
                    return False
                ohbot.move(ohbot.LIDBLINK, lid_open)
            return True
        except Exception:
            return False

    def _continuous_blink_worker(self):
        """Background loop: blink repeatedly until ``stop_continuous_blink()``."""
        while not self._continuous_blink_stop.is_set():
            if not self.connected:
                self._sleep_interruptible(0.2)
                continue
            self._perform_blink_once(
                smooth=True, close_fraction=getattr(self, "continuous_blink_close_fraction", 0.70)
            )
            lo = float(getattr(self, "continuous_blink_min_interval_s", 2.0) or 2.0)
            hi = float(getattr(self, "continuous_blink_max_interval_s", 5.5) or 5.5)
            pause_end = time.time() + random.uniform(lo, max(lo, hi))
            while time.time() < pause_end and not self._continuous_blink_stop.is_set():
                if not self._sleep_interruptible(0.1):
                    break
        try:
            if self.connected:
                ohbot.move(ohbot.LIDBLINK, float(self.motor_defaults.get(3, 10)))
        except Exception:
            pass
        self._continuous_blink_active = False

    def start_continuous_blink(self):
        """Start a daemon thread that blinks until ``stop_continuous_blink()``."""
        if not self.connected:
            return False
        t = getattr(self, "_continuous_blink_thread", None)
        if t is not None and t.is_alive():
            return True
        self._continuous_blink_stop.clear()
        self._continuous_blink_active = True
        self._continuous_blink_thread = threading.Thread(
            target=self._continuous_blink_worker, daemon=True
        )
        self._continuous_blink_thread.start()
        return True

    def stop_continuous_blink(self):
        """Stop the continuous blink thread and return lids to the open default."""
        self._continuous_blink_stop.set()
        t = getattr(self, "_continuous_blink_thread", None)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            try:
                t.join(timeout=8.0)
            except Exception:
                pass
        self._continuous_blink_thread = None
        self._continuous_blink_active = False
        try:
            if self.connected:
                ohbot.move(ohbot.LIDBLINK, float(self.motor_defaults.get(3, 10)))
        except Exception:
            pass

    def _idle_blink_during_hold(self, duration_s):
        """Blink periodically while holding a fixed pose (e.g. looking at the table while user writes)."""
        end = time.time() + max(0.0, float(duration_s))
        while time.time() < end and not self.stop_event.is_set():
            self._blink_idle_if_due()
            if not self._sleep_interruptible(0.15):
                return

    def _snapshot_head_eye_pose(self):
        """Current head/eye motor positions before a gesture (falls back to motor_defaults)."""
        snap = {}
        for motor_index in (ohbot.HEADNOD, ohbot.HEADTURN, ohbot.EYETURN, ohbot.EYETILT):
            try:
                p = float(ohbot.motorPos[motor_index])
                if p < 0 or p > 10:
                    raise ValueError("out of range")
            except Exception:
                p = float(self.motor_defaults.get(motor_index, 5))
            snap[motor_index] = p
        return snap

    @staticmethod
    def _smoothstep(t):
        """Ease-in-out (0–1) for gentler acceleration and deceleration."""
        t = max(0.0, min(1.0, float(t)))
        return t * t * (3.0 - 2.0 * t)

    def _smooth_head_eye_pose(self, start_pose, end_pose, steps=None, delay_s=None, motor_speed=None):
        """Interpolate head/eye motors smoothly from start_pose to end_pose."""
        if not self.connected or not start_pose or not end_pose:
            return False
        motors = (ohbot.HEADNOD, ohbot.HEADTURN, ohbot.EYETURN, ohbot.EYETILT)
        n = int(steps if steps is not None else getattr(self, "little_movement_smooth_steps", 12) or 12)
        n = max(3, min(30, n))
        delay = float(
            delay_s if delay_s is not None else getattr(self, "little_movement_step_delay_s", 0.024) or 0.024
        )
        spd = int(motor_speed if motor_speed is not None else getattr(self, "little_movement_motor_speed", 4) or 4)
        spd = max(1, min(10, spd))
        try:
            for i in range(1, n + 1):
                if self.stop_event.is_set():
                    return False
                if getattr(self, "_task2_look_sweep_active", False) and self._task2_look_sweep_stop.is_set():
                    return False
                t = self._smoothstep(i / float(n))
                for m in motors:
                    s = float(start_pose.get(m, self.motor_defaults.get(m, 5)))
                    e = float(end_pose.get(m, s))
                    pos = max(0.0, min(10.0, s + (e - s) * t))
                    ohbot.move(m, pos, spd)
                if not self._sleep_interruptible(delay):
                    return False
            for m in motors:
                pos = max(0.0, min(10.0, float(end_pose.get(m, self.motor_defaults.get(m, 5)))))
                ohbot.move(m, pos, spd)
                if hasattr(self, "motor_sliders") and m in self.motor_sliders:
                    mi, p = int(m), pos
                    self.root.after(0, lambda idx=mi, v=p: self.motor_sliders[idx].set(v))
            return True
        except Exception:
            return False

    def _restore_head_eye_pose(self, snap, smooth=True):
        """Return head/eye to a saved pose after a brief gesture."""
        if not self.connected or not snap:
            return
        if smooth:
            start = self._snapshot_head_eye_pose()
            self._smooth_head_eye_pose(start, snap)
            return
        try:
            for motor_index, pos in snap.items():
                ohbot.move(int(motor_index), float(pos))
                if hasattr(self, "motor_sliders") and int(motor_index) in self.motor_sliders:
                    mi, p = int(motor_index), float(pos)
                    self.root.after(0, lambda idx=mi, v=p: self.motor_sliders[idx].set(v))
        except Exception:
            pass

    def _do_little_movement(self, intensity="low", attention_side="center"):
        if not self.connected or self.stop_event.is_set():
            return
        prev_pose = self._snapshot_head_eye_pose()
        try:
            headturn = prev_pose.get(ohbot.HEADTURN, self.motor_defaults.get(1, 5))
            headnod = prev_pose.get(ohbot.HEADNOD, self.motor_defaults.get(0, 5))
            eyeturn = prev_pose.get(ohbot.EYETURN, self.motor_defaults.get(2, 5))
            eyeltilt = prev_pose.get(ohbot.EYETILT, self.motor_defaults.get(6, 5))

            # Larger HEADTURN / EYETURN = left on Ohbot; use signed deltas so "center" is balanced.
            if intensity == "low":
                yaw_choices = [-0.25, -0.15, -0.08, 0.0, 0.08, 0.15, 0.25]
                vert_choices = [-0.2, -0.12, -0.06, 0.0, 0.06, 0.12, 0.2]
            else:
                yaw_choices = [-0.8, -0.4, 0.0, 0.4, 0.8]
                vert_choices = [-0.6, -0.3, 0.0, 0.3, 0.6]

            if attention_side == "left":
                yaw_choices = [c for c in yaw_choices if c >= 0] or [0.1, 0.2, 0.3]
                vert_choices = [c for c in vert_choices if c >= 0] or [0.0, 0.2]
            elif attention_side == "right":
                yaw_choices = [c for c in yaw_choices if c <= 0] or [-0.1, -0.2, -0.3]
                vert_choices = [c for c in vert_choices if c <= 0] or [0.0, -0.2]

            yaw_delta = random.choice(yaw_choices)
            vert_delta = random.choice(vert_choices)
            vert_lift = 0.0 if attention_side == "center" else 0.4

            ht = max(0, min(10, headturn + yaw_delta))
            et = max(0, min(10, eyeturn + yaw_delta))
            hn = max(0, min(10, headnod + vert_lift + vert_delta))
            el = max(0, min(10, eyeltilt + vert_lift + vert_delta))

            target_pose = {
                ohbot.HEADNOD: hn,
                ohbot.HEADTURN: ht,
                ohbot.EYETURN: et,
                ohbot.EYETILT: el,
            }
            self._smooth_head_eye_pose(prev_pose, target_pose)

            self._blink_idle_if_due()

            if not self._sleep_interruptible(
                float(getattr(self, "little_movement_hold_s", 0.08) or 0.08)
            ):
                return
        except Exception:
            pass
        finally:
            self._restore_head_eye_pose(prev_pose)

    def _waiting_animation(self, duration_s=3.0):
        end = time.time() + max(0.0, duration_s)
        while time.time() < end and not self.stop_event.is_set():
            self._do_little_movement(intensity="low", attention_side="center")
            if not self._sleep_interruptible(0.2):
                return

    def _hold_left_attention(self):
        if not self.connected:
            return
        try:
            ohbot.move(ohbot.HEADTURN, self.motor_defaults.get(1, 5.5))
            ohbot.move(ohbot.EYETURN, self.motor_defaults.get(2, 5.5))
            ohbot.move(ohbot.HEADNOD, self.motor_defaults.get(0, 5.5))
            ohbot.move(ohbot.EYETILT, self.motor_defaults.get(6, 5.5))
        except Exception:
            pass

    def _head_turn_delta_units(self, degrees):
        """Motor-units delta from neutral for a given yaw nudge (0-10 head scale)."""
        span = float(getattr(self, "head_turn_half_range_degrees", 45.0) or 45.0)
        if span <= 0:
            span = 45.0
        return min(5.0, max(0.0, float(degrees) * (5.0 / span)))

    def _move_head_yaw_from_neutral(
        self, toward_left, degrees=None, sync_eyes=True, motor_speed=3, delta_units=None
    ):
        """Head yaw (+ optional eye yaw) from motor_defaults neutral. toward_left: larger HT = left."""
        if not self.connected:
            return
        try:
            spd = int(motor_speed)
            spd = max(1, min(10, spd))
            if delta_units is not None:
                delta = min(5.0, max(0.0, float(delta_units)))
            else:
                deg = float(self.head_turn_nudge_degrees if degrees is None else degrees)
                delta = self._head_turn_delta_units(deg)
            neutral_h = float(self.motor_defaults.get(1, 5.0))
            neutral_e = float(self.motor_defaults.get(2, 5.5))
            if toward_left:
                ht = min(10.0, neutral_h + delta)
                et = min(10.0, neutral_e + delta) if sync_eyes else neutral_e
            else:
                ht = max(0.0, neutral_h - delta)
                et = max(0.0, neutral_e - delta) if sync_eyes else neutral_e
            ohbot.move(ohbot.HEADTURN, ht, spd)
            if sync_eyes:
                ohbot.move(ohbot.EYETURN, et, spd)
            if hasattr(self, "motor_sliders"):
                if 1 in self.motor_sliders:
                    self.root.after(0, lambda pos=ht: self.motor_sliders[1].set(pos))
                if sync_eyes and 2 in self.motor_sliders:
                    self.root.after(0, lambda pos=et: self.motor_sliders[2].set(pos))
        except Exception:
            pass

    def attention_side_left(self, degrees=None, sync_eyes=True, motor_speed=3, delta_units=None):
        """Turn the face toward the participant's left (head + optional eyes).

        * ``degrees`` — yaw amount in degrees; default uses ``self.head_turn_nudge_degrees``.
        * ``sync_eyes`` — move eye turn with the head (default True).
        * ``motor_speed`` — Ohbot move speed 1–10 (default 3 = slow; try 6–8 if it feels sluggish).
        * ``delta_units`` — if set, raw 0–10 motor delta from neutral (overrides ``degrees``).
        """
        self._move_head_yaw_from_neutral(
            True,
            degrees=degrees,
            sync_eyes=sync_eyes,
            motor_speed=motor_speed,
            delta_units=delta_units,
        )

    def attention_side_right(self, degrees=None, sync_eyes=True, motor_speed=3, delta_units=None):
        """Turn the face toward the participant's right. Same parameters as ``attention_side_left``."""
        self._move_head_yaw_from_neutral(
            False,
            degrees=degrees,
            sync_eyes=sync_eyes,
            motor_speed=motor_speed,
            delta_units=delta_units,
        )

    def show_negation(
        self,
        duration_s=None,
        cycles=None,
        sync_eyes=True,
        restore=True,
        delta_units=None,
    ):
        """Shake head left and right (negation / \"no\") for a few seconds, then restore pose.

        * ``duration_s`` — total time for the shakes (default ``negation_shake_duration_s``).
        * ``cycles`` — how many left→right pairs (default ``negation_shake_cycles``).
        * ``delta_units`` — yaw amplitude on 0–10 motor scale (default ``negation_shake_delta_units``).
        """
        if not self.connected or self.stop_event.is_set():
            return False

        duration = float(
            duration_s
            if duration_s is not None
            else getattr(self, "negation_shake_duration_s", 2.0) or 2.0
        )
        duration = max(0.5, min(12.0, duration))
        n_cycles = int(
            cycles if cycles is not None else getattr(self, "negation_shake_cycles", 2) or 2
        )
        n_cycles = max(1, min(6, n_cycles))
        delta = float(
            delta_units
            if delta_units is not None
            else getattr(self, "negation_shake_delta_units", 1.5) or 1.5
        )
        delta = min(4.0, max(0.5, delta))
        steps = int(getattr(self, "negation_shake_smooth_steps", 8) or 8)
        steps = max(3, min(20, steps))

        start_pose = self._snapshot_head_eye_pose()
        center_ht = float(start_pose.get(ohbot.HEADTURN, self.motor_defaults.get(1, 5)))
        center_hn = float(start_pose.get(ohbot.HEADNOD, self.motor_defaults.get(0, 5)))
        center_et = float(start_pose.get(ohbot.EYETURN, self.motor_defaults.get(2, 5)))
        center_el = float(start_pose.get(ohbot.EYETILT, self.motor_defaults.get(6, 5)))

        def _yaw_pose(toward_left):
            if toward_left:
                ht = min(10.0, center_ht + delta)
                et = min(10.0, center_et + delta) if sync_eyes else center_et
            else:
                ht = max(0.0, center_ht - delta)
                et = max(0.0, center_et - delta) if sync_eyes else center_et
            return {
                ohbot.HEADNOD: center_hn,
                ohbot.HEADTURN: ht,
                ohbot.EYETURN: et,
                ohbot.EYETILT: center_el,
            }

        sway_count = n_cycles * 2
        delay = duration / float(max(1, sway_count * steps))

        current = dict(start_pose)
        try:
            for _ in range(n_cycles):
                for toward_left in (True, False):
                    if self.stop_event.is_set():
                        if restore:
                            self._restore_head_eye_pose(start_pose)
                        return False
                    target = _yaw_pose(toward_left)
                    if not self._smooth_head_eye_pose(
                        current, target, steps=steps, delay_s=delay
                    ):
                        if restore:
                            self._restore_head_eye_pose(start_pose)
                        return False
                    current = target
            if restore:
                self._restore_head_eye_pose(start_pose, smooth=True)
            return not self.stop_event.is_set()
        except Exception:
            if restore:
                self._restore_head_eye_pose(start_pose)
            return False

    def turn_head_full_left(self, sync_eyes=True, degrees=None):
        """Turn head ~30° toward the participant's left from neutral (not to the end stop).

        In this panel, larger motor values = more left. Eyes move by the same delta
        when sync_eyes is True. Override angle with ``degrees=`` or set
        ``self.head_turn_nudge_degrees`` for the default.
        """
        self._move_head_yaw_from_neutral(True, degrees=degrees, sync_eyes=sync_eyes, motor_speed=3)

    def turn_head_full_right(self, sync_eyes=True, degrees=None):
        """Turn head ~30° toward the participant's right from neutral (not to the end stop).

        Smaller motor values = more right. See ``turn_head_full_left`` for tuning.
        """
        self._move_head_yaw_from_neutral(False, degrees=degrees, sync_eyes=sync_eyes, motor_speed=3)

    def _neutral_head_eye_pose(self):
        return {
            ohbot.HEADNOD: float(self.motor_defaults.get(0, 5.5)),
            ohbot.HEADTURN: float(self.motor_defaults.get(1, 5.5)),
            ohbot.EYETURN: float(self.motor_defaults.get(2, 5.5)),
            ohbot.EYETILT: float(self.motor_defaults.get(6, 5.5)),
        }

    def _task2_look_sweep_target_pose(self, toward_left, delta_units=None, sync_eyes=True):
        delta = float(
            delta_units
            if delta_units is not None
            else getattr(self, "task2_look_sweep_delta_units", 1.5) or 1.5
        )
        delta = min(5.0, max(0.0, delta))
        pose = self._neutral_head_eye_pose()
        neutral_h = pose[ohbot.HEADTURN]
        neutral_e = pose[ohbot.EYETURN]
        if toward_left:
            pose[ohbot.HEADTURN] = min(10.0, neutral_h + delta)
            if sync_eyes:
                pose[ohbot.EYETURN] = min(10.0, neutral_e + delta)
        else:
            pose[ohbot.HEADTURN] = max(0.0, neutral_h - delta)
            if sync_eyes:
                pose[ohbot.EYETURN] = max(0.0, neutral_e - delta)
        return pose

    def _smooth_to_rest_head_eye_pose(self):
        """Smoothly return head/eyes to panel neutral (used after look sweep)."""
        if not self.connected:
            return False
        start = self._snapshot_head_eye_pose()
        end = self._neutral_head_eye_pose()
        steps = int(getattr(self, "task2_look_sweep_smooth_steps", 24) or 24)
        delay = float(getattr(self, "task2_look_sweep_step_delay_s", 0.03) or 0.03)
        spd = int(getattr(self, "task2_look_sweep_motor_speed", 3) or 3)
        return self._smooth_head_eye_pose(start, end, steps=steps, delay_s=delay, motor_speed=spd)

    def _task2_look_sweep_worker(self):
        """Task 2 daemon: smooth ease-in-out sweep left↔right (runs during speech as well)."""
        hold_s = float(getattr(self, "task2_look_sweep_hold_s", 2.0) or 2.0)
        hold_s = max(0.2, min(30.0, hold_s))
        delta = float(getattr(self, "task2_look_sweep_delta_units", 1.5) or 1.5)
        steps = int(getattr(self, "task2_look_sweep_smooth_steps", 24) or 24)
        steps = max(6, min(40, steps))
        delay = float(getattr(self, "task2_look_sweep_step_delay_s", 0.03) or 0.03)
        spd = int(getattr(self, "task2_look_sweep_motor_speed", 3) or 3)
        toward_left = True
        try:
            while not self._task2_look_sweep_stop.is_set():
                if not self.connected:
                    if not self._sleep_interruptible(0.2):
                        break
                    continue
                if self.stop_event.is_set():
                    break
                start_pose = self._snapshot_head_eye_pose()
                end_pose = self._task2_look_sweep_target_pose(toward_left, delta)
                self._smooth_head_eye_pose(
                    start_pose, end_pose, steps=steps, delay_s=delay, motor_speed=spd
                )
                toward_left = not toward_left
                end = time.time() + hold_s
                while time.time() < end:
                    if self._task2_look_sweep_stop.is_set() or self.stop_event.is_set():
                        break
                    if not self._sleep_interruptible(0.1):
                        break
        finally:
            try:
                if self.connected:
                    self._smooth_to_rest_head_eye_pose()
            except Exception:
                pass
            self._task2_look_sweep_active = False

    def start_task2_continuous_look_sweep(self):
        """Task 2 only: start background left↔right looking (call stop_task2_continuous_look_sweep to end)."""
        if not self.connected:
            return False
        t = getattr(self, "_task2_look_sweep_thread", None)
        if t is not None and t.is_alive():
            return True
        self._task2_look_sweep_stop.clear()
        self._task2_look_sweep_active = True
        self._task2_look_sweep_thread = threading.Thread(
            target=self._task2_look_sweep_worker, daemon=True
        )
        self._task2_look_sweep_thread.start()
        return True

    def stop_task2_continuous_look_sweep(self):
        """Task 2 only: stop background look sweep and return head/eyes to rest."""
        self._task2_look_sweep_stop.set()
        t = getattr(self, "_task2_look_sweep_thread", None)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            try:
                t.join(timeout=5.0)
            except Exception:
                pass
        self._task2_look_sweep_thread = None
        self._task2_look_sweep_active = False
        if self.connected:
            try:
                self._smooth_to_rest_head_eye_pose()
            except Exception:
                pass
        return True

    def start_task2_look_sweep(self):
        """Short alias for ``start_task2_continuous_look_sweep()``."""
        return self.start_task2_continuous_look_sweep()

    def stop_task2_look_sweep(self):
        """Short alias for ``stop_task2_continuous_look_sweep()`` — stops left↔right look and returns to rest."""
        return self.stop_task2_continuous_look_sweep()

    def _task2_look_down_and_say_cue(self, text=None, hold_s=None):
        """Task 2 + screen disclosure: speak the cue, then look down (verbal only, not on screen)."""
        if not self._task2_bystander_disclosure_active():
            return True
        if not self.connected or self.stop_event.is_set():
            return False

        line = str(
            text
            if text is not None
            else getattr(self, "task2_look_down_cue_text", "Hey, look down here at your paper.")
            or "Hey, look down here at your paper."
        )
        hold = float(
            hold_s
            if hold_s is not None
            else getattr(self, "task2_look_down_cue_hold_s", 1.0) or 1.0
        )
        hold = max(0.0, hold)

        start_pose = self._snapshot_head_eye_pose()
        nod_d = min(5.0, max(0.5, float(getattr(self, "table_look_nod_delta", 3.0) or 3.0)))
        tilt_d = min(
            5.0, max(0.5, float(getattr(self, "table_look_eye_tilt_delta", 3.0) or 3.0))
        )
        down_pose = dict(start_pose)
        down_pose[ohbot.HEADNOD] = max(
            0.0, min(10.0, float(start_pose[ohbot.HEADNOD]) - nod_d)
        )
        down_pose[ohbot.EYETILT] = max(
            0.0, min(10.0, float(start_pose[ohbot.EYETILT]) - tilt_d)
        )

        try:
            if not self._session_say(line):
                return False
            if not self._smooth_head_eye_pose(start_pose, down_pose):
                return False
            if hold > 0:
                self._idle_blink_during_hold(hold)
            if self.stop_event.is_set():
                self._restore_head_eye_pose(start_pose, smooth=True)
                return False
            self._restore_head_eye_pose(start_pose, smooth=True)
            return True
        except Exception:
            self._restore_head_eye_pose(start_pose, smooth=True)
            return False

    def _brief_look_at_table_and_restore(self, hold_s=0.6, nod_delta=None, eye_tilt_delta=None):
        """Glance down at the table from the current pose, hold, then restore exactly."""
        if not self.connected or self.stop_event.is_set():
            return False
        hold = max(0.0, float(hold_s))
        if hold >= 2.0:
            try:
                print(f"[TABLE_GLANCE] Looking at table for {hold:.0f}s...")
            except Exception:
                pass
        start_pose = self._snapshot_head_eye_pose()
        nod_d = float(
            nod_delta
            if nod_delta is not None
            else getattr(self, "table_look_nod_delta", 3.0) or 3.0
        )
        tilt_d = float(
            eye_tilt_delta
            if eye_tilt_delta is not None
            else getattr(self, "table_look_eye_tilt_delta", 3.0) or 3.0
        )
        nod_d = min(5.0, max(0.5, nod_d))
        tilt_d = min(5.0, max(0.5, tilt_d))

        down_pose = dict(start_pose)
        down_pose[ohbot.HEADNOD] = max(
            0.0, min(10.0, float(start_pose[ohbot.HEADNOD]) - nod_d)
        )
        down_pose[ohbot.EYETILT] = max(
            0.0, min(10.0, float(start_pose[ohbot.EYETILT]) - tilt_d)
        )

        try:
            if not self._smooth_head_eye_pose(start_pose, down_pose):
                return False
            self._idle_blink_during_hold(hold)
            if self.stop_event.is_set():
                self._restore_head_eye_pose(start_pose, smooth=True)
                return False
            self._restore_head_eye_pose(start_pose, smooth=True)
            if hold >= 2.0:
                try:
                    print("[TABLE_GLANCE] Done — continuing lesson")
                except Exception:
                    pass
            return True
        except Exception:
            self._restore_head_eye_pose(start_pose, smooth=True)
            return False

    @staticmethod
    def _text_looks_japanese(text):
        for ch in str(text):
            o = ord(ch)
            if 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF:
                return True
        return False

    def _session_say(
        self,
        text,
        rate_wpm=None,
        volume_gain=None,
        afplay_volume=None,
        japanese=None,
    ):
        """Speak one phrase; honor stop_event before/after.

        Optional kwargs (same as ``_session_say_japanese``):
          rate_wpm, volume_gain, afplay_volume — macOS wav + lip-sync tuning.
        Japanese voice is used when ``japanese=True``, text contains hiragana/katakana/kanji,
        or you pass stressed-repeat-style rate/volume overrides with Japanese text.
        """
        if self.stop_event.is_set():
            return False
        use_ja = japanese
        if use_ja is None:
            use_ja = self._text_looks_japanese(text)
        if use_ja:
            return self._session_say_japanese(
                text,
                rate_wpm=rate_wpm,
                volume_gain=volume_gain,
                afplay_volume=afplay_volume,
                pitch_semitones=None,
            )
        with self._speech_lock:
            try:
                print(f"[TASK_SAY_EN] {text}")
            except Exception:
                pass
            if sys.platform == "darwin":
                try:
                    selected = self.voice_var.get() if hasattr(self, "voice_var") else self.voice_var_value
                except Exception:
                    selected = self.voice_var_value
                voice_name = self._mac_say_voice_name(selected)
                if rate_wpm is not None:
                    wpm = int(rate_wpm)
                else:
                    try:
                        wpm = int(self.speech_speed_var.get()) if hasattr(self, "speech_speed_var") else int(
                            getattr(self, "default_speech_wpm", 180)
                        )
                    except Exception:
                        wpm = int(getattr(self, "default_speech_wpm", 180))
                vgain = float(volume_gain if volume_gain is not None else 1.0)
                avol = float(afplay_volume if afplay_volume is not None else 1.0)
                ok = self._speak_mac_voice_with_lipsync(
                    text,
                    voice_name=voice_name,
                    rate_wpm=wpm,
                    volume_gain=vgain,
                    afplay_volume=avol,
                )
                return bool(ok) and (not self.stop_event.is_set())

            ohbot.say(text)
            return not self.stop_event.is_set()

    def _is_japanese_mac_voice(self, voice_name):
        if not voice_name:
            return False
        if voice_name == MAC_SIRI_JA_SYSTEM_VOICE:
            return True
        v = str(voice_name).lower()
        return any(tok in v for tok in ("otoya", "kyoko", "ja_", "japanese"))

    def _pitch_shift_wav_file(self, wav_path, semitones):
        """Shift pitch in semitones while keeping duration (for matching Daniel vs Otoya)."""
        semitones = float(semitones)
        if abs(semitones) < 0.05:
            return True
        try:
            with wave.open(str(wav_path), "rb") as wf:
                nch, sw, rate, nframes, _, _ = wf.getparams()
                if sw != 2 or nframes <= 1:
                    return False
                raw = wf.readframes(nframes)
            count = len(raw) // 2
            fmt = "<" + "h" * count
            samples = list(struct.unpack(fmt, raw))
            factor = 2.0 ** (semitones / 12.0)
            n = len(samples)
            n_mid = max(2, int(round(n / factor)))

            def _interp(src, out_len):
                if out_len <= 1 or len(src) <= 1:
                    return list(src)
                out = []
                denom = max(1, out_len - 1)
                src_max = max(0, len(src) - 1)
                for i in range(out_len):
                    pos = i * src_max / denom
                    idx = int(pos)
                    frac = pos - idx
                    if idx >= len(src) - 1:
                        v = float(src[-1])
                    else:
                        v = float(src[idx]) * (1.0 - frac) + float(src[idx + 1]) * frac
                    out.append(int(max(-32768, min(32767, round(v)))))
                return out

            mid = _interp(samples, n_mid)
            shifted = _interp(mid, n)
            with wave.open(str(wav_path), "wb") as out:
                out.setnchannels(nch)
                out.setsampwidth(sw)
                out.setframerate(rate)
                out.writeframes(struct.pack(fmt, *shifted))
            return True
        except Exception:
            return False

    def _amplify_wav_file(self, wav_path, gain):
        """Boost 16-bit wav amplitude in place (for louder stressed demos)."""
        gain = float(gain)
        if gain <= 1.0001:
            return
        try:
            with wave.open(str(wav_path), "rb") as wf:
                nch, sw, rate, nframes, _, _ = wf.getparams()
                frames = wf.readframes(nframes)
            if sw != 2 or not frames:
                return
            count = len(frames) // 2
            fmt = "<" + "h" * count
            samples = struct.unpack(fmt, frames)
            boosted = [max(-32768, min(32767, int(s * gain))) for s in samples]
            with wave.open(str(wav_path), "wb") as out:
                out.setnchannels(nch)
                out.setsampwidth(sw)
                out.setframerate(rate)
                out.writeframes(struct.pack(fmt, *boosted))
        except Exception:
            pass

    def _afplay_cmd(self, wav_path, volume=1.0):
        vol = max(0.0, min(1.0, float(volume)))
        return ["afplay", "-v", str(vol), str(wav_path)]

    def _speak_mac_voice_with_lipsync(
        self,
        text,
        voice_name=None,
        rate_wpm=150,
        volume_gain=1.0,
        afplay_volume=1.0,
        pitch_semitones=None,
    ):
        """macOS-only: one continuous wav + lip-sync; lips reset every N words on a timeline."""
        return self._speak_mac_voice_with_lipsync_single(
            text,
            voice_name=voice_name,
            rate_wpm=rate_wpm,
            volume_gain=volume_gain,
            afplay_volume=afplay_volume,
            pitch_semitones=pitch_semitones,
        )

    def _speak_mac_voice_with_lipsync_single(
        self,
        text,
        voice_name=None,
        rate_wpm=150,
        volume_gain=1.0,
        afplay_volume=1.0,
        pitch_semitones=None,
    ):
        """One macOS wav + lip-sync utterance (full line audio, timed lip resets)."""
        if sys.platform != "darwin" or not self.connected:
            try:
                ohbot.say(text)
                return True
            except Exception:
                return False
        wav_path = getattr(ohbot, "speechAudioFile", "ohbotData/ohbotspeech.wav")
        spoken_text = str(text)
        tts_lang_before = self._read_mac_system_tts_language()
        tts_lang_changed = False
        try:
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except Exception:
                pass
            # 1) Generate wav for viseme timing
            gen_cmd = [
                "say",
                "-o",
                str(wav_path),
                "--file-format=RF64",
                "--data-format=LEI16@22050",
                "-r",
                str(int(rate_wpm)),
            ]
            if voice_name == MAC_SIRI_JA_SYSTEM_VOICE:
                tts_lang_changed = True
            elif voice_name is None:
                tts_lang_changed = True
            self._append_mac_say_voice_args(gen_cmd, voice_name)
            gen_cmd.append(spoken_text)
            subprocess.run(gen_cmd, timeout=15, check=True)
            if (not os.path.exists(wav_path)) or os.path.getsize(wav_path) < 128:
                raise RuntimeError("say did not generate a valid wav")

            if pitch_semitones is None:
                if self._is_japanese_mac_voice(voice_name):
                    pitch_semitones = float(getattr(self, "japanese_pitch_semitones", 0.0) or 0.0)
                else:
                    pitch_semitones = float(getattr(self, "english_pitch_semitones", 0.0) or 0.0)
            self._pitch_shift_wav_file(wav_path, pitch_semitones)

            self._amplify_wav_file(wav_path, volume_gain)

            wf = wave.open(wav_path, "rb")
            try:
                length = wf.getnframes()
                framerate = wf.getframerate()
                channels = wf.getnchannels()
                bytespersample = wf.getsampwidth()
                visemes_per_sec = int(getattr(self, "lipsync_visemes_per_sec", 14) or 14)
                visemes_per_sec = max(8, min(20, visemes_per_sec))
                chunk = int(framerate / visemes_per_sec)

                phonemes = []
                times = []
                ms = 0.0
                for _i in range(0, max(0, length - chunk), chunk):
                    vol = 0
                    buffer = wf.readframes(chunk)
                    bytesread = chunk * channels * bytespersample
                    index = 0
                    samples = int(bytesread / (channels * bytespersample)) if (channels * bytespersample) else 0
                    for _s in range(0, samples):
                        if index + 1 >= len(buffer):
                            break
                        vol += buffer[index]
                        vol += buffer[index + 1] * 256
                        index += bytespersample
                        if channels > 1:
                            if index + 1 >= len(buffer):
                                break
                            vol += buffer[index]
                            vol += buffer[index + 1] * 256
                            index += bytespersample
                    ms += (1000.0 / visemes_per_sec)
                    phonemes.append(float(vol))
                    times.append(float(ms) / 1000.0)

                mx = max(phonemes) if phonemes else 0.0
                if mx > 0:
                    phonemes = [v * 10.0 / mx for v in phonemes]
            finally:
                try:
                    wf.close()
                except Exception:
                    pass

            # 2) Play the wav via afplay, then start lip-sync once audio output has begun.
            if times:
                self._active_lip_reset_viseme_indices = self._compute_lip_reset_viseme_indices(
                    spoken_text, len(phonemes)
                )
                lip_delay = float(getattr(self, "lipsync_afplay_start_delay_s", 0.15) or 0.15)
                lip_delay = max(0.0, min(0.5, lip_delay))
                t_move = threading.Thread(
                    target=ohbot._moveSpeech, args=(phonemes, times, True), daemon=True
                )
                try:
                    p = subprocess.Popen(
                        self._afplay_cmd(wav_path, afplay_volume),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if lip_delay > 0:
                        time.sleep(lip_delay)
                    t_move.start()
                    end = time.time() + float(times[-1]) + 0.35
                    while time.time() < end and not self.stop_event.is_set():
                        if p.poll() is not None:
                            break
                        time.sleep(0.05)
                    if self.stop_event.is_set():
                        try:
                            p.terminate()
                        except Exception:
                            pass
                except Exception:
                    # Fallback: re-run say if afplay path fails for any reason.
                    play_cmd = ["say", "-r", str(int(rate_wpm))]
                    self._append_mac_say_voice_args(play_cmd, voice_name)
                    play_cmd.append(spoken_text)
                    subprocess.run(play_cmd, timeout=15)
            else:
                # If we couldn't compute timings, just play the wav directly.
                try:
                    subprocess.run(self._afplay_cmd(wav_path, afplay_volume), timeout=15)
                except Exception:
                    play_cmd = ["say", "-r", str(int(rate_wpm))]
                    self._append_mac_say_voice_args(play_cmd, voice_name)
                    play_cmd.append(spoken_text)
                    subprocess.run(play_cmd, timeout=15)
            return True
        except Exception as e:
            try:
                self._set_info(f"Speech error: {e}")
            except Exception:
                pass
            return False
        finally:
            if tts_lang_changed and tts_lang_before:
                self._set_mac_system_tts_language(tts_lang_before)
            elif tts_lang_changed:
                self._set_mac_system_tts_language(
                    getattr(self, "mac_english_tts_language", "en")
                )
            try:
                self._wait_lip_sync_thread_finished()
                self._reset_lips_to_initialized()
            except Exception:
                pass
            self._active_lip_reset_viseme_indices = []

    def _session_say_japanese(
        self, text, rate_wpm=None, volume_gain=None, afplay_volume=None, pitch_semitones=None
    ):
        """Speak one phrase using a Japanese voice (macOS) just for this utterance.

        ``rate_wpm`` overrides ``japanese_rate_wpm`` (higher = faster/shorter; good for stressed repeats).
        ``volume_gain`` multiplies wav level (>1 louder); ``afplay_volume`` is 0–1 for ``afplay -v``.
        ``pitch_semitones`` lowers/raises pitch to match Daniel (default ``japanese_pitch_semitones``).
        """
        if self.stop_event.is_set():
            return False
        with self._speech_lock:
            if sys.platform != "darwin":
                ohbot.say(text)
                return not self.stop_event.is_set()
            ja = self._get_japanese_voice_name()
            wpm = int(rate_wpm if rate_wpm is not None else self.japanese_rate_wpm)
            vgain = float(volume_gain if volume_gain is not None else 1.0)
            avol = float(afplay_volume if afplay_volume is not None else 1.0)
            pitch = float(
                pitch_semitones
                if pitch_semitones is not None
                else getattr(self, "japanese_pitch_semitones", 0.0) or 0.0
            )
            try:
                print(
                    f"[TASK_SAY_JA] voice={ja!r} rate={wpm} gain={vgain} vol={avol} "
                    f"pitch={pitch:+.1f}st text={text}"
                )
            except Exception:
                pass
            if not ja:
                self._set_info(
                    "Japanese voice not found — download Siri Voice 1 (Japan) in "
                    "Settings → Accessibility → Spoken Content"
                )
                return False
            if ja == MAC_SIRI_JA_SYSTEM_VOICE:
                ja_label = getattr(self, "japanese_siri_voice_label", "Siri Voice 1 (Japan)")
            else:
                ja_label = ja
            info_prefix = ""
            if ja != MAC_SIRI_JA_SYSTEM_VOICE and getattr(self, "prefer_japanese_male_voice", False) and not ja.startswith("Otoya"):
                info_prefix = f"Male Japanese voice not found; using {ja}. "

            self._set_info(f"{info_prefix}Japanese voice: {ja_label}")
            try:
                self._speak_mac_voice_with_lipsync(
                    text,
                    voice_name=ja,
                    rate_wpm=wpm,
                    volume_gain=vgain,
                    afplay_volume=avol,
                    pitch_semitones=pitch,
                )
            except Exception:
                # Fallback to original ohbot path.
                try:
                    ohbot.setVoice("" if ja == MAC_SIRI_JA_SYSTEM_VOICE else ja)
                    self._ohbot_say_raw(text, untilDone=True, lipSync=True)
                except Exception:
                    ohbot.say(text)
            finally:
                # Restore normal voice after speaking Japanese
                try:
                    self._apply_selected_voice()
                except Exception:
                    pass
                # Match default speech behavior: reset lips so mouth closes.
                try:
                    self._wait_lip_sync_thread_finished()
                    self._reset_lips_to_initialized()
                except Exception:
                    pass
            return not self.stop_event.is_set()

    def _sleep_interruptible(self, duration_s):
        deadline = time.time() + max(0.0, float(duration_s))
        while time.time() < deadline:
            if self.stop_event.is_set():
                return False
            time.sleep(min(0.05, deadline - time.time()))
        return True

    def _flush_motors_to_panel_defaults(self):
        """Snap motors to slider defaults — used when user hits Stop for immediate stillness."""
        if not self.connected:
            return
        try:
            md = getattr(self, "motor_defaults", None) or {}
            for motor_index, position in md.items():
                ohbot.move(int(motor_index), float(position))
        except Exception:
            pass

    def _begin_autonomous_thread(self, target, info_msg):
        if self.autonomy_thread and self.autonomy_thread.is_alive():
            self._set_info("A task is already running")
            return
        self.stop_event.clear()
        self.response_event.clear()
        self.start_continuous_blink()
        self.autonomy_thread = threading.Thread(target=target, daemon=True)
        self.autonomy_thread.start()
        self._set_info(info_msg)

    def start_task_1(self):
        self._begin_autonomous_thread(self._autonomous_task_1_thread, "Task 1 started")

    def start_task_2(self):
        self._begin_autonomous_thread(self._autonomous_task_2_thread, "Task 2 started")

    def stop_autonomous_session(self):
        self.stop_event.set()
        self.response_event.set()
        self.stop_continuous_blink()
        self.stop_task2_continuous_look_sweep()
        self.stop_face_guard()
        base_off_sent = self._ir_stop_base_if_on(blocking=True)
        threading.Thread(target=self._flush_motors_to_panel_defaults, daemon=True).start()
        if base_off_sent:
            self._set_info("Stopping task... base turned off.")
        else:
            self._set_info("Stopping task...")

    def _session_common_preamble(self):
        """Initial pose, attention, voice — shared by Task 1 and Task 2."""
        try:
            ohbot.move(ohbot.HEADTURN, self.motor_defaults.get(1, 5.5))
            ohbot.move(ohbot.EYETURN, self.motor_defaults.get(2, 5.5))
            ohbot.move(ohbot.HEADNOD, self.motor_defaults.get(0, 5.5))
            ohbot.move(ohbot.EYETILT, self.motor_defaults.get(6, 5.5))
            ohbot.move(ohbot.LIDBLINK, self.motor_defaults.get(3, 10))
            self._sleep_interruptible(0.2)
        except Exception:
            pass
        if self.stop_event.is_set():
            return False
        self._hold_left_attention()
        self._apply_selected_voice()
        self._do_little_movement(attention_side="center")
        return True

    def _autonomous_task_1_thread(self):
        try:
            time.sleep(10)
            
            if not self._session_say("Hi welcome to the Japanese language learning lesson."):
                    return

            self._do_little_movement(attention_side="center")
            if not self._session_say("I am your tutor."):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say("Today I will teach you Japanese words for everyday use."):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say("So are you ready?"):
                 return
            self._hold_and_wait(self.default_wait_s, min(3, self.default_wait_s))
            self._do_little_movement(attention_side="center")

            # # First word: こんにちは
            if not self._session_say("Good, let's begin. The first word you will learn is"):
                return
            self._do_little_movement(attention_side="center")

            self._set_lesson_vocab_disclosure("こんにちは", "Konnichiwa")
            if not self._session_say_japanese("こんにちは"):
                return
            self._do_little_movement(attention_side="center")
            self._set_lesson_vocab_disclosure("こんにちは", "Konnichiwa", "Hello")
            if not self._session_say("It means Hello."):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say("Try to pronounce it."):
                return
            self._hold_and_wait(self.default_wait_s, min(3, self.default_wait_s))

            # if not self._session_say("Try pronouncing it a little louder."):
            #     return
            # self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            if not self._session_say("You should put slightly more stress on the middle part of the word. Just repeat after me."):
                return
            self._set_lesson_vocab_disclosure("こんにちは", "kohn-nee-chee-wah")
            #self._do_little_movement(attention_side="center")

            if not self._session_say_japanese(
                "こんにちは",
                rate_wpm=int(self.japanese_rate_wpm_stressed_repeat),
                volume_gain=float(self.japanese_volume_gain_stressed_repeat),
                afplay_volume=float(self.japanese_afplay_volume_stressed_repeat),
            ):
                return

            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))
            self._do_little_movement(attention_side="center")
            self._clear_lesson_vocab_disclosure()

            # # Second word: ありがとう
            # if not self._session_say("Good Job, now I will teach you how to say Thank you in Japanese. Just repeat after me."):
            #     return
            # #self._do_little_movement(attention_side="center")

            # self._set_lesson_vocab_disclosure("ありがとう", "Arigatou", "Thank you")
            # if not self._session_say_japanese("ありがとう"):
            #     return
            # self._do_little_movement(attention_side="center")
            # self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            # if not self._session_say("Try again."):
            #     return
            # self._do_little_movement(attention_side="center")
            # self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            # if not self._session_say(
            #     "You should try to use a little bit deeper breath from your stomach area to pronounce it. Just follow me."
            # ):
            #     return
            # self._set_lesson_vocab_disclosure("ありがとう", "ah-ree-gah-toh")
            # #self._do_little_movement(attention_side="center")

            # if not self._session_say_japanese(
            #     "ありがとう",
            #     rate_wpm=int(self.japanese_rate_wpm_stressed_repeat),
            #     volume_gain=float(self.japanese_volume_gain_stressed_repeat),
            #     afplay_volume=float(self.japanese_afplay_volume_stressed_repeat),
            # ):
            #     return

            # self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))
            # self._do_little_movement(attention_side="center")
            # self._clear_lesson_vocab_disclosure()

            # if not self._session_say(
            #     "Your pronunciation is so unique, even Japanese people might need subtitles."
            # ):
            #     return
            # self._hold_and_wait(int(self.default_wait_s), min(1, int(self.default_wait_s)))
            # self._do_little_movement(attention_side="center")

            # # Writing practice
            if not self._session_say("Okay, now let's move on to the next part of the lesson."):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say(
                "I will ask you to write the Japanese words in hiragana characters."
            ):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say("You can use the paper infront of you to write."):
                return
            self._do_little_movement(attention_side="center")
            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            if not self._session_say("So shall we begin?"):
                return
            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))
            self._do_little_movement(attention_side="center")

            ## Write こんにちは
            if not self._session_say("Okay. Let's try to write the word you just learned in hiragana characters."):
                return
            self._do_little_movement(attention_side="center")
            self._set_lesson_vocab_disclosure("こんにちは")
            if not self._session_say_japanese("こんにちは"):
                return

            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )
            # if not self._session_say("Try writing it with bigger letters."):
            #     return
            # self._do_little_movement(attention_side="center")

            # self._brief_look_at_table_and_restore(hold_s=30)

            if not self._session_say(
                "Keep a little more space between the letters and try it again?"
            ):
                return
            self._do_little_movement(attention_side="center")
            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )

            if not self._session_say("Okay, this lesson is over for today. Thank you for joining!"):
                return
            self._do_little_movement(attention_side="center")

            if self.stop_event.is_set():
                return
            self._set_info("Task 1 complete")
        except Exception as e:
            self._set_info(f"Task 1 error: {e}")
            try:
                print("[TASK1_ERROR]", e)
                print(traceback.format_exc())
            except Exception:
                pass
        finally:
            self.session_active = False
            self.stop_continuous_blink()
            self._clear_lesson_vocab_disclosure()
            self._clear_face_guard_profile_after_task()
            if self.stop_event.is_set():
                self.ir_send_shutdown()
                self._set_info("Task 1 stopped")


    def _autonomous_task_2_thread(self):
        """Same lesson as Task 1 but new words; screen disclosure only if bystander detected."""
        self._task2_bystander_detected = False
        try:
            time.sleep(10)  
            if not self._session_say("Hi welcome again to the Japanese language learning lesson."):
                return

            # self._do_little_movement(attention_side="center")
            # if not self._session_say("I am your tutor."):
            #     return
            # self._do_little_movement(attention_side="center")

            # if not self._session_say("Today I will teach you Japanese words for everyday use."):
            #     return
            # self._do_little_movement(attention_side="center")

            if not self._session_say("So are you ready for today's lesson?"):
                 return
            self._hold_and_wait(self.default_wait_s, min(3, self.default_wait_s))
            self._do_little_movement(attention_side="center")
            
            if not self._session_say("Good, let's start with the word"):
                return
            self._do_little_movement(attention_side="center")

            self._set_lesson_vocab_disclosure("おはよう", "ohayo")
            if not self._session_say_japanese("おはよう"):
                return
            self._do_little_movement(attention_side="center")
            self._set_lesson_vocab_disclosure("おはよう", "ohayo", "Good Morning")
            if not self._session_say("It means Good Morning."):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say("Please try to pronounce it."):
                return
            self._hold_and_wait(self.default_wait_s, min(3, self.default_wait_s))

            if not self._session_say("Try pronouncing it a little louder."):
                return
            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            if not self._session_say("You should slightly emphasis on the middle part of the word. Just repeat after me."):
                return
            self._set_lesson_vocab_disclosure("おはよう", "oh-hah-yoh")
            #self._do_little_movement(attention_side="center")

            if not self._session_say_japanese(
                "おはよう",
                rate_wpm=int(self.japanese_rate_wpm_stressed_repeat),
                volume_gain=float(self.japanese_volume_gain_stressed_repeat),
                afplay_volume=float(self.japanese_afplay_volume_stressed_repeat),
            ):
                return

            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))
            self._do_little_movement(attention_side="center")

            self._do_little_movement(attention_side="center")

            # Second word: すごい
            self._clear_lesson_vocab_disclosure()
            if not self._session_say("Okay, now I will teach you how to say 'Amazing' in Japanese."):
                return
            #self._do_little_movement(attention_side="center")

            self._set_lesson_vocab_disclosure("すごい", "Sugoi", "Amazing")
            if not self._session_say_japanese("すごい"):
                return
            self._do_little_movement(attention_side="center")
            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            if not self._session_say("Try again with more confidence."):
                return
            self._do_little_movement(attention_side="center")
            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))

            if not self._session_say(
                "You should use a deeper breath from your stomach to pronounce it. Just follow me."
            ):
                return
            self._set_lesson_vocab_disclosure("すごい", "soo-goy")
            #self._do_little_movement(attention_side="center")

            if not self._session_say_japanese(
                "すごい",
                rate_wpm=int(self.japanese_rate_wpm_stressed_repeat),
                volume_gain=float(self.japanese_volume_gain_stressed_repeat),
                afplay_volume=float(self.japanese_afplay_volume_stressed_repeat),
            ):
                return

            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))
            self._do_little_movement(attention_side="center")
            self._clear_lesson_vocab_disclosure()

            ##self.stop_task2_look_sweep()
            ##self.attention_side_left(delta_units=1.5)
            ##self.show_negation(duration_s=1.0, cycles=3, delta_units=1) 
            if not self.start_task2_face_scan():
                return
            self.stop_task2_face_scan()
            if not self._task2_ir_send_rotate_then_off("anticlockwise"):
                return
            if not self._task2_look_down_and_say_cue("Hey, look down here."):
                return
            if not self._task2_say_with_optional_disclosure(
                "Wow, even I didn’t know that japanese pronunciation could be butchered like that."
            ):
                return
            #self._hold_and_wait(int(self.default_wait_s), min(1, int(self.default_wait_s)))
            # if not self._task2_say_with_optional_disclosure(
            #     "We will continue working on your pronunciation in the next session."
            # ):
            #     return
            self._clear_lesson_vocab_disclosure()
            if not self._task2_ir_send_rotate_then_off("clockwise"):
                #self.stop_task2_face_scan()
                return

            ##self._task2_feedback_attention_pose()
            #self._do_little_movement(attention_side="center")
            ##self._hold_and_wait(int(self.default_wait_s), min(1, int(self.default_wait_s)))

            ##self.start_task2_continuous_look_sweep()
            ## Writing practice
            if not self._session_say("Now, let's move on to the writing part."):
                return
            self._do_little_movement(attention_side="center")

            # if not self._session_say(
            #     "I will ask you to write the Japanese words you just learned in hiragana characters."
            # ):
            #     return
            # self._do_little_movement(attention_side="center")

            if not self._session_say("You can use the paper infront of you to write."):
                return
            self._do_little_movement(attention_side="center")

            if not self._session_say("So shall we begin?"):
                return
            self._hold_and_wait(int(self.default_wait_s), min(3, int(self.default_wait_s)))
            self._do_little_movement(attention_side="center")

            # Write おはよう
            if not self._session_say("Let's try to write this word."):
                return
            self._do_little_movement(attention_side="center")
            self._set_lesson_vocab_disclosure("おはよう")
            if not self._session_say_japanese("おはよう"):
                return
            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )

            if not self._session_say("Try writing it with bigger letters."):
                return
            self._do_little_movement(attention_side="center")

            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )

            if not self._session_say(
                "Keep a little more space between the letters and try writing it again?"
            ):
                return
            self._do_little_movement(attention_side="center")
            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )

            self._clear_lesson_vocab_disclosure()
            # Write すごい
            if not self._session_say("Okay, now let's write the next word."):
                return
            self._set_lesson_vocab_disclosure("すごい")
            if not self._session_say_japanese("すごい"):
                return
            self._do_little_movement(attention_side="center")

            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )

            if not self._session_say("Try writing again with equal spacing between the letters."):
                return
            self._do_little_movement(attention_side="center")

            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )

            if not self._session_say("Keep the letters aligned and try one more time."):
                return
            self._do_little_movement(attention_side="center")

            self._brief_look_at_table_and_restore(
                hold_s=float(getattr(self, "writing_table_glance_s", 30) or 30)
            )
            self._clear_lesson_vocab_disclosure()

            if not self.start_task2_face_scan():
                return
            self.stop_task2_face_scan()
            if not self._task2_ir_send_rotate_then_off("anticlockwise"):
                return
            if not self._task2_look_down_and_say_cue("Hey, look down here."):
                return
            if not self._task2_say_with_optional_disclosure(
                "Your handwriting is so unique, even Google Translator would say, 'No Language found'."):
                #self.stop_task2_face_scan()
                return
            self._clear_lesson_vocab_disclosure()
            
            #self._hold_and_wait(int(self.default_wait_s), min(1, int(self.default_wait_s)))
            #self._do_little_movement(attention_side="center")
    
            if not self._task2_ir_send_rotate_then_off("clockwise"):
                #self.stop_task2_face_scan()
                return

            #self._task2_feedback_attention_pose()
            # if not self._task2_say_with_optional_disclosure(
            #     "So we will continue working on your handwriting in the next session."
            # ):
                #return# 
            #self._do_little_movement(attention_side="center")
            # self._hold_and_wait(int(self.default_wait_s), min(1, int(self.default_wait_s)))
            #self.stop_task2_face_scan()

            #self.start_task2_continuous_look_sweep()
            if not self._session_say("This lesson is over for today. Thank you for joining!"):
                return
            self._do_little_movement(attention_side="center")
            #self.stop_task2_look_sweep()
            #self.hold_left_attention()
            if self.stop_event.is_set():
                return
            self._set_info("Task 2 complete")
        except Exception as e:
            self._set_info(f"Task 2 error: {e}")
            try:
                print("[TASK2_ERROR]", e)
                print(traceback.format_exc())
            except Exception:
                pass
        finally:
            self.session_active = False
            self.stop_continuous_blink()
            self.stop_task2_continuous_look_sweep()
            self.stop_task2_face_scan()
            self._clear_lesson_vocab_disclosure()
            self._clear_face_guard_profile_after_task()
            self._task2_bystander_detected = False
            try:
                self.root.after(0, self._refresh_face_guard_profile_status)
            except Exception:
                pass
            if self.stop_event.is_set():
                self.ir_send_shutdown()
                self._set_info("Task 2 stopped")

    def reset_to_defaults(self):
        if not self.connected:
            messagebox.showerror("Error", "Ohbot not connected")
            return
        # Stop autonomous lesson if running, then move hardware + sliders to startup defaults.
        self.stop_event.set()
        self.response_event.set()
        self.stop_continuous_blink()
        self.stop_face_guard()
        self.ir_send_shutdown()
        try:
            threading.Thread(target=self._reset_thread, daemon=True).start()
            self._set_info("Resetting to defaults...")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reset: {e}")

    def _reset_thread(self):
        """Apply motor_defaults to hardware. Waits for autonomy to exit so moves are not fighting say/lip-sync."""
        try:
            t = getattr(self, "autonomy_thread", None)
            if t is not None and t.is_alive():
                t.join(timeout=12.0)

            md = getattr(self, "motor_defaults", None) or {}
            order = (0, 1, 2, 3, 4, 5, 6, 7)
            for motor_index in order:
                if motor_index not in md:
                    continue
                position = float(md[motor_index])
                ohbot.move(motor_index, position, 10)
                try:
                    ohbot.wait(ohbot.WAITMEDIUM)
                except Exception:
                    time.sleep(0.05)
                if hasattr(self, "motor_sliders") and motor_index in self.motor_sliders:
                    self.root.after(0, lambda mi=motor_index, pos=position: self.motor_sliders[mi].set(pos))

            top = float(md.get(4, 0))
            bottom = float(md.get(5, 6))
            try:
                ohbot.lipTopPos = top
                ohbot.lipBottomPos = bottom
            except Exception:
                pass
            self._set_info("Reset complete")
        except Exception as e:
            self._set_info(f"Reset error: {e}")

    def move_motor_on_scroll(self, motor_index, value):
        if not self.connected:
            self.root.after(0, lambda: messagebox.showerror("Error", "Ohbot is not connected."))
            return
        try:
            ohbot.move(motor_index, float(value))
        except Exception as e:
            self._set_info(f"Error: {e}")

    def update_speech_speed(self, speed):
        try:
            ohbot.setSpeechSpeed(speed)
            self.info_label.config(text=f"Speaking speed set to {speed} WPM")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to set speaking speed: {e}")

    def on_closing(self):
        if self._closing:
            return
        self._closing = True
        self.stop_event.set()
        self.stop_face_guard()
        self.stop_task2_continuous_look_sweep()
        self.stop_continuous_blink()
        self.ir_send_shutdown(blocking=True)
        self._hide_task2_disclosure()
        if self.connected:
            try:
                ohbot.reset()
                ohbot.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    root = Tk()
    app = OhbotControlPanel(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)

    def _handle_interrupt(signum=None, frame=None):
        # Send IR immediately (works even if tkinter never gets to on_closing).
        btn = str(getattr(app, "ir_shutdown_button", "off") or "off").strip()
        _emergency_ir_off(btn)
        try:
            root.after(0, app.on_closing)
        except Exception:
            app.on_closing()

    signal.signal(signal.SIGINT, _handle_interrupt)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_interrupt)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nInterrupted — shutting down...")
        _handle_interrupt()

