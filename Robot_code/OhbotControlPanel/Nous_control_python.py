import json
import os
import sys
import threading
import time

import tinytuya
from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

_EXAMPLES_DEVICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")
_HOME_DEVICES = os.path.expanduser("~/devices.json")
DEVICES_FILE = _EXAMPLES_DEVICES if os.path.isfile(_EXAMPLES_DEVICES) else _HOME_DEVICES
CODES_FILE = os.path.expanduser("~/ir_codes.json")
CONFIG_FILE = os.path.expanduser("~/tinytuya.json")
PROTOCOL_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(DEVICES_FILE)), "ir_protocol.json")
VERSIONS = (3.5, 3.4, 3.3)

# Nous Smart IR L5 hardware (what tinytuya scan finds on your network)
DEVICE_ID = "YOUR_DEVICE_ID"
# "DIY" on iot.tuya.com is a remote profile inside the L5 app — not a second device.
# Python talks to the hardware above; save buttons locally in ~/ir_codes.json.

IP = "192.168.1.100"
VERSION = 3.5
# Nous L5 uses control_type 1 (DPS 201/202)
CONTROL_TYPE = 1

# 904 on v3.3 = wrong protocol (not a successful send). None = success in tinytuya.
IR_SEND_HARD_FAIL = frozenset({"914", "901", "905", "904"})
OFFLINE_ERRS = frozenset({"901", "905"})


def _local_ipv4():
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        addr = sock.getsockname()[0]
        sock.close()
        return addr
    except OSError:
        return None


def _wifi_subnet_hint(saved_ip):
    local = _local_ipv4()
    if not local or not saved_ip:
        return ""
    if str(local).rsplit(".", 1)[0] == str(saved_ip).rsplit(".", 1)[0]:
        return ""
    return (
        f"  Note: this Mac is on {local}, devices.json has {saved_ip} — "
        "same Wi‑Fi recommended; run scan-ip if the L5 moved."
    )


def parse_device_version(value, default=VERSION):
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def ir_result_ok(result, allow_soft=True):
    """True when tinytuya reports success after a waited ``set_value`` send."""
    if isinstance(result, dict):
        err = result.get("Err")
        if err is None:
            return True
        return str(err) not in IR_SEND_HARD_FAIL
    # ``None`` is the normal OK response for L5 IR sends (v3.5) after a waited send.
    return result is None


def ir_result_note(result):
    if result is None or not isinstance(result, dict):
        return ""
    err = result.get("Err")
    if err is None:
        return ""
    return f" ({result.get('Error', err)})"


def load_protocol_cache(device_id):
    """Last protocol version that succeeded for this device (avoids re-scanning every send)."""
    if not os.path.isfile(PROTOCOL_CACHE_FILE):
        return None
    try:
        with open(PROTOCOL_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(device_id)
        if not entry:
            return None
        return parse_device_version(entry.get("version"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_protocol_cache(device_id, version):
    data = {}
    if os.path.isfile(PROTOCOL_CACHE_FILE):
        try:
            with open(PROTOCOL_CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    data[device_id] = {"version": float(version)}
    try:
        with open(PROTOCOL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def clear_protocol_cache(device_id):
    if not os.path.isfile(PROTOCOL_CACHE_FILE):
        return
    try:
        with open(PROTOCOL_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data.pop(device_id, None)
        with open(PROTOCOL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def resolve_send_version(device_id, device_version):
    cached = load_protocol_cache(device_id)
    if cached is not None:
        return cached
    return parse_device_version(device_version, VERSION)


def load_device(device_id, verbose=True):
    if not os.path.isfile(DEVICES_FILE):
        raise SystemExit("Missing ~/devices.json. Run: python -m tinytuya wizard")

    with open(DEVICES_FILE, encoding="utf-8") as f:
        devices = json.load(f)

    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        raise SystemExit(
            f"Device {device_id} not in ~/devices.json.\n"
            f"Run wizard for it:\n"
            f"  python -m tinytuya wizard -key YOUR_KEY -secret YOUR_SECRET "
            f"-region eu -device {device_id} -yes"
        )
    ip = device.get("ip") or IP
    if not ip:
        raise SystemExit("IP missing. Run: python -m tinytuya scan")

    if verbose:
        print(f"Device: {device.get('name', device_id)} @ {ip}")
    device_version = parse_device_version(device.get("version"), VERSION)
    return device["id"], ip, device["key"], device_version


def connect_ir(device_id, ip, local_key, control_type, version=VERSION, persist=False):
    ir = IRRemoteControlDevice(
        device_id,
        ip,
        local_key,
        version=version,
        control_type=control_type,
        persist=persist,
    )
    ir.set_socketPersistent(persist)
    ir.set_socketRetryLimit(1)
    ir.set_socketTimeout(3)
    ir.set_sendWait(0.3)
    return ir


def send_ir_code(ir, code, control_type, nowait=False):
    """Send learned IR; use ``set_value`` with wait so 914 key errors are not masked."""
    wait_s = 0.05 if nowait else 0.8
    ir.set_sendWait(wait_s)
    if control_type == 1:
        command = {
            IRRemoteControlDevice.NSDP_CONTROL: "send_ir",
            IRRemoteControlDevice.NSDP_TYPE: 0,
            IRRemoteControlDevice.NSDP_HEAD: "",
            IRRemoteControlDevice.NSDP_KEY1: "1" + code,
        }
        return ir.set_value(
            IRRemoteControlDevice.DP_SEND_IR,
            json.dumps(command),
            nowait=nowait,
        )

    command = {
        IRRemoteControlDevice.DP_MODE: "send_ir",
        IRRemoteControlDevice.DP_CODE_TYPE: 0,
        IRRemoteControlDevice.DP_KEY_STUDY: code,
    }
    return ir.set_multiple_values(command, nowait=nowait)


def load_codes():
    if not os.path.isfile(CODES_FILE):
        return {}
    with open(CODES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_codes(codes):
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, indent=2)
    print(f"Saved to {CODES_FILE}")


def learn_button(device_id, ip, local_key, name):
    print(f"\nLearning '{name}' — point your remote at the L5 and press the button...")
    print("(Close Smart Life / Nous app first.)\n")

    for control_type in dict.fromkeys((CONTROL_TYPE, 2, 1)):
        print(f"Trying IR protocol type {control_type}...")
        ir = connect_ir(device_id, ip, local_key, control_type)
        code = ir.receive_button(timeout=30)
        if code and isinstance(code, str):
            codes = load_codes()
            codes[name] = {"code": code, "control_type": control_type}
            save_codes(codes)
            print(f"Learned '{name}' OK (control_type={control_type})")
            return True
        print(f"Type {control_type} failed:", code)

    print("Learn failed. Hold remote 5-10 cm from blaster, use fresh batteries, retry.")
    return False


def get_code_entry(codes, name):
    entry = codes[name]
    if isinstance(entry, str):
        return entry, CONTROL_TYPE
    return entry["code"], entry.get("control_type", CONTROL_TYPE)


def normalize_code_entry(codes, name):
    """Ensure every learned button has ``code`` + ``control_type`` metadata."""
    entry = codes.get(name)
    if entry is None:
        return None
    if isinstance(entry, str):
        return {"code": entry, "control_type": CONTROL_TYPE}
    if isinstance(entry, dict) and "code" in entry:
        entry.setdefault("control_type", CONTROL_TYPE)
        return entry
    return None


def update_devices_json(device_id, key, ip=None, name=None, version=None):
    if os.path.isfile(DEVICES_FILE):
        with open(DEVICES_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    else:
        saved = []

    old = next((d for d in saved if d.get("id") == device_id), {})
    entry = {
        **old,
        "id": device_id,
        "key": key,
        "ip": ip or old.get("ip") or IP,
        "version": str(version) if version is not None else (old.get("version") or str(VERSION)),
        "name": name or old.get("name") or "Smart IR",
    }

    updated = [entry if d.get("id") == device_id else d for d in saved]
    if not any(d.get("id") == device_id for d in updated):
        updated.append(entry)

    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=4)

    mirror_device_entry_to_home(device_id)
    print(f"Updated {DEVICES_FILE}")
    print(f"  ip:  {entry['ip']}")
    print(f"  key: {entry['key']}")
    return entry["ip"], entry["key"]


def mirror_device_entry_to_home(device_id):
    """Keep ~/devices.json in sync when examples/devices.json is the active source."""
    if not os.path.isfile(_EXAMPLES_DEVICES):
        return
    if os.path.abspath(DEVICES_FILE) != os.path.abspath(_EXAMPLES_DEVICES):
        return
    with open(_EXAMPLES_DEVICES, encoding="utf-8") as f:
        src_list = json.load(f)
    entry = next((d for d in src_list if d.get("id") == device_id), None)
    if not entry:
        return
    if os.path.isfile(_HOME_DEVICES):
        with open(_HOME_DEVICES, encoding="utf-8") as f:
            home = json.load(f)
    else:
        home = []
    updated = [dict(entry) if d.get("id") == device_id else d for d in home]
    if not any(d.get("id") == device_id for d in updated):
        updated.append(dict(entry))
    with open(_HOME_DEVICES, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=4)


def update_device_protocol_version(device_id, version):
    if not os.path.isfile(DEVICES_FILE):
        return
    with open(DEVICES_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    for device in saved:
        if device.get("id") == device_id:
            device["version"] = str(version)
            break
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=4)
    mirror_device_entry_to_home(device_id)


def _format_tuya_cloud_failure(devices):
    """Turn tinytuya Cloud ``getdevices`` error into a readable message + fix hints."""
    text = str(devices)
    lines = [f"Could not fetch devices: {devices}"]
    if "28841002" in text or "subscription has expired" in text.lower():
        lines.append(
            "\nTuya IoT Core subscription has expired (paid renew not required if you have a local key)."
            "\n  A) Free extension: iot.tuya.com → Cloud → IoT Core → View details → apply to extend (individual devs)."
            "\n  B) New free developer account + link Smart Life app, then: python -m tinytuya wizard"
            "\n  C) Extract local key from Smart Life app (no cloud):"
            "\n       https://github.com/jasonacox/tinytuya/discussions/687#3-my-tuya-iot-cloud-trial-has-expired--subscription-issues"
            "\n     Then: python Nous_control_python.py set-key YOUR_LOCAL_KEY [ip]"
            "\n  D) Same Wi‑Fi, then: python Nous_control_python.py scan-ip && python Nous_control_python.py check"
        )
    elif "914" in text:
        lines.append("Local device key expired — refresh-key needs a working Tuya cloud API.")
    return "\n".join(lines)


def refresh_key_from_cloud(device_id):
    if not os.path.isfile(CONFIG_FILE):
        raise SystemExit(
            "Missing ~/tinytuya.json. Run:\n"
            "  python -m tinytuya wizard -key YOUR_ID -secret YOUR_SECRET -region eu -yes"
        )

    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)

    cloud = tinytuya.Cloud(
        apiKey=config["apiKey"],
        apiSecret=config["apiSecret"],
        apiRegion=config["apiRegion"],
        apiDeviceID=device_id,
    )
    if cloud.error:
        raise SystemExit(f"Tuya cloud error: {cloud.error}")

    devices = cloud.getdevices(verbose=False)
    if not isinstance(devices, list):
        raise SystemExit(_format_tuya_cloud_failure(devices))

    match = next((d for d in devices if d.get("id") == device_id), None)
    if not match or not match.get("key"):
        raise SystemExit(f"Device {device_id} not found in Tuya cloud.")

    return update_devices_json(device_id, match["key"], match.get("ip"), match.get("name"))


def refresh_key(device_id):
    print("Fetching new key from Tuya cloud...")
    try:
        ip, key = refresh_key_from_cloud(device_id)
        invalidate_ir_ready()
        return ip, key
    except SystemExit as exc:
        print(exc)
        raise


_ir_ready_lock = threading.Lock()
_ir_ready_state = {"ok": False, "monotonic_at": 0.0, "info": None}
IR_READY_TTL_S = 300.0


def _link_status_kind(result):
    if not isinstance(result, dict):
        return "ok"
    err = str(result.get("Err", ""))
    if err == "914":
        return "914"
    if err in ("901", "905"):
        return "offline"
    if err in IR_SEND_HARD_FAIL:
        return "fail"
    return "ok"


def probe_ir_link(device_id, ip, local_key, version, control_type=CONTROL_TYPE):
    ir = connect_ir(device_id, ip, local_key, control_type, version=version)
    return ir.status()


def find_working_protocol(device_id, ip, local_key, device_version=None, control_type=CONTROL_TYPE):
    cached = load_protocol_cache(device_id)
    device_version = parse_device_version(device_version, VERSION)
    candidates = tuple(dict.fromkeys((cached, device_version, *VERSIONS)))
    last = None
    saw_914 = False
    for ver in candidates:
        if ver is None:
            continue
        result = probe_ir_link(device_id, ip, local_key, ver, control_type)
        last = result
        kind = _link_status_kind(result)
        if kind == "ok":
            save_protocol_cache(device_id, ver)
            update_device_protocol_version(device_id, ver)
            return ver, result, None
        if kind == "914":
            saw_914 = True
        elif kind == "offline":
            return None, result, "offline"
    return None, last, "914" if saw_914 else "fail"


def require_ir_ready(device_id=DEVICE_ID, verbose=False):
    """Return ensure_ir_ready info; print a hint when the L5 is not reachable."""
    info = ensure_ir_ready(device_id, verbose=verbose)
    if not info.get("ok"):
        msg = info.get("message") or "IR L5 not ready"
        print(msg)
        hint = _wifi_subnet_hint(info.get("ip"))
        if hint:
            print(hint)
        if info.get("key_rejected"):
            print(
                "  Local key rejected (914). Cloud refresh unavailable? Run:\n"
                "  python Nous_control_python.py set-key YOUR_LOCAL_KEY [ip]"
            )
        elif info.get("ip"):
            print("  Try: python Nous_control_python.py scan-ip")
        print("  Then: python Nous_control_python.py ensure")
    return info


def scan_and_update_ip(device_id=DEVICE_ID):
    """Find the L5 on the LAN and update examples/devices.json with the current IP."""
    device_id, old_ip, local_key, device_version = load_device(device_id)
    print(f"Scanning LAN for {device_id} (was @ {old_ip})...")
    try:
        found = tinytuya.deviceScan(verbose=False)
    except Exception as exc:
        raise SystemExit(f"Scan failed: {exc}") from exc
    if not isinstance(found, dict):
        raise SystemExit(f"Unexpected scan result: {found}")

    for addr, dev in found.items():
        dev_id = dev.get("gwId") or dev.get("id")
        if dev_id != device_id:
            continue
        new_ip = str(addr).split(":")[0].strip()
        if not new_ip:
            continue
        ver = parse_device_version(dev.get("version"), device_version)
        update_devices_json(device_id, local_key, new_ip, version=ver)
        invalidate_ir_ready()
        print(f"Found Smart IR @ {new_ip} (protocol v{ver})")
        return new_ip

    raise SystemExit(
        f"Device {device_id} not found on LAN.\n"
        "Check the L5 is powered on and on the same Wi‑Fi as this computer."
    )


def invalidate_ir_ready():
    with _ir_ready_lock:
        _ir_ready_state["ok"] = False
        _ir_ready_state["monotonic_at"] = 0.0
        _ir_ready_state["info"] = None


def ensure_ir_ready(device_id=DEVICE_ID, force=False, verbose=False):
    """Probe L5 link, pick protocol version, refresh key on 914, sync devices.json."""
    now = time.monotonic()
    with _ir_ready_lock:
        if (
            not force
            and _ir_ready_state["ok"]
            and _ir_ready_state["info"]
            and (now - _ir_ready_state["monotonic_at"]) < IR_READY_TTL_S
        ):
            return dict(_ir_ready_state["info"])

    info = {
        "ok": False,
        "device_id": device_id,
        "ip": None,
        "version": None,
        "key_refreshed": False,
        "key_rejected": False,
        "message": "",
    }

    try:
        device_id, ip, local_key, device_version = load_device(device_id, verbose=verbose)
    except SystemExit as exc:
        info["message"] = str(exc)
        with _ir_ready_lock:
            _ir_ready_state.update(ok=False, monotonic_at=now, info=dict(info))
        return info

    info["ip"] = ip
    key_refreshed = False
    ver, result, err_kind = find_working_protocol(device_id, ip, local_key, device_version)

    if ver is None and err_kind == "914":
        info["key_rejected"] = True
        if verbose:
            print("IR: local key rejected (914) — trying Tuya cloud refresh...")
        try:
            clear_protocol_cache(device_id)
            ip, local_key = refresh_key_from_cloud(device_id)
            key_refreshed = True
            ver, result, err_kind = find_working_protocol(device_id, ip, local_key, device_version)
            if ver is not None:
                info["key_rejected"] = False
        except SystemExit as exc:
            info["message"] = (
                f"IR L5 @ {ip} reachable but local key rejected (914). "
                f"Cloud refresh failed: {exc}"
            )
            invalidate_ir_ready()
            return info

    info["key_refreshed"] = key_refreshed
    if ver is not None:
        info["ok"] = True
        info["version"] = ver
        msg = f"IR L5 ready @ {ip} (protocol v{ver})"
        if key_refreshed:
            msg += " — key refreshed"
        info["message"] = msg
        if verbose:
            print(f"IR: {msg}")
    elif err_kind == "offline":
        info["message"] = (
            f"IR L5 offline @ {ip} — check Wi‑Fi or run: python -m tinytuya scan"
        )
        if verbose:
            print(f"IR: {info['message']}")
            hint = _wifi_subnet_hint(ip)
            if hint:
                print(hint.strip())
    elif err_kind == "914" or info.get("key_rejected"):
        info["key_rejected"] = True
        info["message"] = (
            f"IR L5 @ {ip} reachable but local key rejected (914) — run set-key with a fresh key"
        )
        if verbose:
            print(f"IR: {info['message']} (last: {result})")
    else:
        info["message"] = (
            f"IR L5 not responding @ {ip} — check ~/tinytuya.json or run refresh-key"
        )
        if verbose:
            print(f"IR: {info['message']} (last: {result})")

    with _ir_ready_lock:
        _ir_ready_state["ok"] = info["ok"]
        _ir_ready_state["monotonic_at"] = time.monotonic()
        _ir_ready_state["info"] = dict(info)
    return info


def try_send(device_id, ip, local_key, code, saved_type, device_version=None, retried_key=False):
    device_version = parse_device_version(device_version, VERSION)
    cached_version = load_protocol_cache(device_id)
    control_types = tuple(dict.fromkeys((saved_type, CONTROL_TYPE, 2, 1)))
    last_result = None
    failures = []

    def _attempt(version, control_type, quiet=False):
        nonlocal last_result
        ir = connect_ir(device_id, ip, local_key, control_type, version=version)
        last_result = send_ir_code(ir, code, control_type)
        if ir_result_ok(last_result):
            save_protocol_cache(device_id, version)
            note = ir_result_note(last_result)
            print(f"Sent via v{version} control_type {control_type}{note}")
            return True
        err = last_result.get("Err") if isinstance(last_result, dict) else None
        if str(err) in OFFLINE_ERRS:
            if not quiet:
                print(f"v{version} offline @ {ip}:", last_result)
            return "offline"
        if str(err) in IR_SEND_HARD_FAIL:
            failures.append((version, control_type, last_result))
            if not quiet:
                print(f"v{version} control_type {control_type} failed:", last_result)
        return False

    hit = None
    if cached_version is not None:
        hit = _attempt(cached_version, saved_type, quiet=True)
        if hit == "offline":
            return False, last_result
        if hit:
            return True, last_result

    if cached_version is None:
        hit = _attempt(device_version, saved_type, quiet=True)
        if hit == "offline":
            return False, last_result
        if hit:
            return True, last_result

    if cached_version is not None:
        clear_protocol_cache(device_id)

    for version in VERSIONS:
        for control_type in control_types:
            if cached_version is not None and version == cached_version and control_type == saved_type:
                continue
            if version == device_version and control_type == saved_type:
                continue
            hit = _attempt(version, control_type, quiet=True)
            if hit == "offline":
                return False, last_result
            if hit:
                return True, last_result

    for version, control_type, result in failures[:3]:
        print(f"v{version} control_type {control_type} failed:", result)
    if len(failures) > 3:
        print(f"... and {len(failures) - 3} more protocol attempts failed")

    if (
        not retried_key
        and isinstance(last_result, dict)
        and str(last_result.get("Err")) == "914"
    ):
        print("\nKey expired — auto-refreshing from Tuya cloud...")
        ip, local_key = refresh_key(device_id)
        clear_protocol_cache(device_id)
        return try_send(
            device_id,
            ip,
            local_key,
            code,
            saved_type,
            device_version=device_version,
            retried_key=True,
        )

    return False, last_result


class _IRSendBase:
    """Send learned IR buttons by name or as attributes (loaded from ~/ir_codes.json).

    Examples::

        IR_send_base("on")      # string name
        IR_send_base.on()       # attribute (same as above if learned as ``on``)
        IR_send_base.names()    # list all learned button names
    """

    def __call__(self, name):
        if not require_ir_ready(verbose=False).get("ok"):
            return False
        device_id, ip, local_key, device_version = load_device(DEVICE_ID, verbose=False)
        send_saved(device_id, ip, local_key, name, device_version=device_version)
        return True

    def sequence(self, names, gap_s=0.0, sleep_fn=None):
        """Send multiple buttons over one connection; ``gap_s`` waits between each."""
        ensure_ir_ready(verbose=False)
        device_id, ip, local_key, device_version = load_device(DEVICE_ID, verbose=False)
        return send_saved_sequence(
            device_id, ip, local_key, names, gap_s=gap_s, sleep_fn=sleep_fn, device_version=device_version
        )

    def sequence_fast(self, names, gap_s=0.0, sleep_fn=None):
        """One connection, learned protocol only — for tight rotate→pause→off sequences."""
        ensure_ir_ready(verbose=False)
        device_id, ip, local_key, device_version = load_device(DEVICE_ID, verbose=False)
        return send_saved_sequence_fast(
            device_id,
            ip,
            local_key,
            names,
            gap_s=gap_s,
            sleep_fn=sleep_fn,
            device_version=device_version,
        )

    def send_fast(self, name, nowait=False):
        """Single button on learned protocol only (no version scan)."""
        ensure_ir_ready(verbose=False)
        device_id, ip, local_key, device_version = load_device(DEVICE_ID, verbose=False)
        return send_saved_fast(
            device_id, ip, local_key, name, nowait=nowait, device_version=device_version
        )

    def names(self):
        """All learned button names (updates when you learn/delete buttons)."""
        return list(load_codes().keys())

    def _resolve_button_name(self, attr):
        codes = load_codes()
        if attr in codes:
            return attr
        for key in codes:
            safe = key.replace("-", "_").replace(" ", "_")
            if safe == attr:
                return key
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        resolved = self._resolve_button_name(name)
        if resolved is None:
            learned = self.names()
            raise AttributeError(
                f"No IR button '{name}'. Learned buttons: {learned or '(none — run learn <name>)'}"
            )

        def _send():
            self(resolved)

        _send.__name__ = name
        _send.__doc__ = f"Send learned IR button '{resolved}'."
        return _send

    def __dir__(self):
        return sorted(set(super().__dir__() + ["names"] + [n.replace("-", "_").replace(" ", "_") for n in self.names()]))

    def __repr__(self):
        learned = self.names()
        if learned:
            return f"<IR_send_base learned: {', '.join(learned)}>"
        return "<IR_send_base learned: (none)>"


IR_send_base = _IRSendBase()


def send_saved_fast(device_id, ip, local_key, name, nowait=False, device_version=None, retried_key=False):
    """Send one learned button without scanning all protocol versions."""
    if not retried_key:
        ready = require_ir_ready(device_id, verbose=False)
        if not ready.get("ok"):
            return False
        device_id, ip, local_key, device_version = load_device(device_id, verbose=False)
    codes = load_codes()
    if name not in codes:
        print(f"No code named '{name}'. Saved codes: {list(codes.keys())}")
        return False
    code, control_type = get_code_entry(codes, name)
    device_version = resolve_send_version(device_id, device_version)
    ir = connect_ir(device_id, ip, local_key, control_type, version=device_version, persist=False)
    ir.set_sendWait(0.05)
    ir.set_socketTimeout(2)
    print(f"Sending '{name}'...")
    result = send_ir_code(ir, code, control_type, nowait=nowait)
    if ir_result_ok(result):
        save_protocol_cache(device_id, device_version)
        note = ir_result_note(result)
        if note:
            print(f"Sent '{name}' OK{note}")
        return True
    err = result.get("Err") if isinstance(result, dict) else None
    if str(err) == "914":
        clear_protocol_cache(device_id)
        invalidate_ir_ready()
    if not retried_key and str(err) == "914":
        print(f"Send '{name}' failed (914) — refreshing key from Tuya cloud...")
        ip, local_key = refresh_key(device_id)
        return send_saved_fast(
            device_id,
            ip,
            local_key,
            name,
            nowait=nowait,
            device_version=device_version,
            retried_key=True,
        )
    print(f"Send '{name}' failed:", result)
    if isinstance(result, dict) and str(result.get("Err")) in OFFLINE_ERRS:
        print(f"L5 offline @ {ip} — run: python Nous_control_python.py scan-ip")
    return False


def send_saved_sequence_fast(
    device_id, ip, local_key, names, gap_s=0.0, sleep_fn=None, device_version=None, retried_key=False
):
    """Send multiple buttons on one persistent link (no per-button version scan)."""
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return True

    codes = load_codes()
    for name in names:
        if name not in codes:
            print(f"No code named '{name}'. Saved codes: {list(codes.keys())}")
            return False

    device_version = resolve_send_version(device_id, device_version)
    _, saved_type = get_code_entry(codes, names[0])
    ir = connect_ir(device_id, ip, local_key, saved_type, version=device_version, persist=True)
    ir.set_sendWait(0.05)
    ir.set_socketTimeout(2)
    gap = max(0.0, float(0.0 if gap_s is None else gap_s))

    for i, name in enumerate(names):
        code, control_type = get_code_entry(codes, name)
        print(f"Sending '{name}'...")
        result = send_ir_code(ir, code, control_type, nowait=True)
        if not ir_result_ok(result):
            err = result.get("Err") if isinstance(result, dict) else None
            if not retried_key and str(err) == "914":
                print("Sequence failed (914) — refreshing key from Tuya cloud...")
                ip, local_key = refresh_key(device_id)
                return send_saved_sequence_fast(
                    device_id,
                    ip,
                    local_key,
                    names,
                    gap_s=gap_s,
                    sleep_fn=sleep_fn,
                    device_version=device_version,
                    retried_key=True,
                )
            print(f"Send '{name}' failed:", result)
            return False
        if i < len(names) - 1 and gap > 0:
            if sleep_fn is not None:
                if not sleep_fn(gap):
                    return False
            else:
                time.sleep(gap)
    return True


def send_saved_sequence(device_id, ip, local_key, names, gap_s=0.0, sleep_fn=None, device_version=None):
    """Send multiple learned buttons with one device connection."""
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return True

    codes = load_codes()
    for name in names:
        if name not in codes:
            print(f"No code named '{name}'. Saved codes: {list(codes.keys())}")
            return False

    gap = max(0.0, float(0.0 if gap_s is None else gap_s))

    for i, name in enumerate(names):
        code, saved_type = get_code_entry(codes, name)
        print(f"Sending '{name}'...")
        print("Aim the L5 at your appliance — line of sight required.\n")
        ok, last_result = try_send(device_id, ip, local_key, code, saved_type, device_version)
        if not ok:
            print("Send failed:", last_result)
            return False
        if i < len(names) - 1 and gap > 0:
            if sleep_fn is not None:
                if not sleep_fn(gap):
                    return False
            else:
                time.sleep(gap)
    return True


def send_saved(device_id, ip, local_key, name, retried=False, device_version=None):
    if not retried:
        ready = require_ir_ready(device_id, verbose=False)
        if not ready.get("ok"):
            return False
        device_id, ip, local_key, device_version = load_device(device_id, verbose=False)
    codes = load_codes()
    if name not in codes:
        print(f"No code named '{name}'. Saved codes: {list(codes.keys())}")
        return

    code, saved_type = get_code_entry(codes, name)
    print(f"Sending '{name}'...")
    print("Aim the L5 at your appliance — line of sight required.\n")

    ok, last_result = try_send(
        device_id, ip, local_key, code, saved_type, device_version=device_version
    )
    if ok:
        return True

    if not retried and isinstance(last_result, dict) and str(last_result.get("Err")) == "914":
        print("\nKey expired — auto-refreshing from Tuya cloud...")
        ip, local_key = refresh_key(device_id)
        send_saved(device_id, ip, local_key, name, retried=True, device_version=device_version)
        return False

    err = last_result.get("Err") if isinstance(last_result, dict) else None
    if str(err) in OFFLINE_ERRS:
        print(f"Send failed: L5 offline @ {ip} — run: python Nous_control_python.py scan-ip")
    else:
        print("Send failed:", last_result)
    return False


def check_device(device_id=DEVICE_ID, ip=None, local_key=None, retried=False, device_version=None):
    del ip, local_key, retried, device_version
    info = ensure_ir_ready(device_id, force=True, verbose=False)
    addr = info.get("ip") or IP
    if info["ok"]:
        print(f"ONLINE — {device_id} @ {addr} (protocol v{info['version']})")
        if info["key_refreshed"]:
            print("Key was refreshed from Tuya cloud.")
        print("Status 900 on L5 is normal (device does not expose full status).")
        return
    print(info["message"])


def list_codes():
    codes = load_codes()
    if not codes:
        print("No saved codes yet. Run: python Nous_control_python.py learn <name>")
        return
    for name in codes:
        print(f"  {name}")


def delete_codes(names):
    codes = load_codes()
    missing = [n for n in names if n not in codes]
    for name in names:
        codes.pop(name, None)
    save_codes(codes)
    for name in names:
        if name not in missing:
            print(f"Deleted '{name}'")
    for name in missing:
        print(f"Not found: '{name}'")


def set_local_key(device_id, key, ip=None):
    """Paste a local key from app extraction / a new wizard run (no cloud needed after)."""
    key = str(key or "").strip()
    if not key:
        raise SystemExit("Usage: python Nous_control_python.py set-key <local_key> [ip]")
    clear_protocol_cache(device_id)
    invalidate_ir_ready()
    ip_out, _ = update_devices_json(device_id, key, ip=ip)
    print(f"Local key saved for {device_id} @ {ip_out}")
    print("Run: python Nous_control_python.py scan-ip   # if IP may have changed")
    print("Run: python Nous_control_python.py check")


def main():
    if len(sys.argv) < 2:
        print(
            "Smart IR control\n\n"
            "  python Nous_control_python.py learn <name>   # learn a remote button\n"
            "  python Nous_control_python.py send <name>    # send button (power = toggle on/off)\n"
            "  python Nous_control_python.py list           # list saved buttons\n"
            "  python Nous_control_python.py delete <name>  # delete one or more buttons\n"
            "  python Nous_control_python.py check          # is the L5 online?\n"
            "  python Nous_control_python.py ensure         # auto-fix key + protocol\n"
            "  python Nous_control_python.py scan-ip        # find L5 on LAN, update IP\n"
            "  python Nous_control_python.py refresh-key    # fetch new key from Tuya cloud\n"
            "  python Nous_control_python.py set-key <key> [ip]  # paste local key (no cloud)\n"
            "\n"
            "In Python code:\n"
            "  from Nous_control_python import IR_send_base\n"
            "  IR_send_base.on()         # send learned button 'on'\n"
            "  IR_send_base('power')     # or use string name\n"
            "  IR_send_base.names()      # list all learned buttons\n"
            "  print(IR_send_base)       # shows learned button names\n"
            "\n"
            "Hardware: Nous Smart IR L5 (set DEVICE_ID in this file or devices.json)\n"
            "'DIY' in the app is a remote profile on this device, not a separate ID.\n"
        )
        return

    command = sys.argv[1].lower()

    if command == "list":
        list_codes()
        return
    if command == "delete":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python Nous_control_python.py delete <name> [name2 ...]")
        delete_codes(sys.argv[2:])
        return
    if command == "refresh-key":
        refresh_key(DEVICE_ID)
        return
    if command == "set-key":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python Nous_control_python.py set-key <local_key> [ip]")
        set_local_key(DEVICE_ID, sys.argv[2], ip=sys.argv[3] if len(sys.argv) > 3 else None)
        return
    if command == "ensure":
        info = ensure_ir_ready(DEVICE_ID, force=True, verbose=True)
        if not info["ok"]:
            raise SystemExit(info["message"])
        return
    if command == "scan-ip":
        scan_and_update_ip(DEVICE_ID)
        info = ensure_ir_ready(DEVICE_ID, force=True, verbose=True)
        if not info["ok"]:
            raise SystemExit(info["message"])
        return

    device_id, ip, local_key, device_version = load_device(DEVICE_ID)

    if command == "check":
        check_device(device_id)
        return

    if command == "learn":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python Nous_control_python.py learn <name>")
        learn_button(device_id, ip, local_key, sys.argv[2])
    elif command == "send":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python Nous_control_python.py send <name>")
        IR_send_base(sys.argv[2])
    else:
        raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    #IR_send_base.on()
    main()   