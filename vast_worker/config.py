"""Settings, secrets and persisted state for the controller.

Everything is stored under ComfyUI's *system user* directory
(``user/__vast_worker``), which ComfyUI never serves over HTTP. Secrets are
write-only from the browser's point of view: routes report whether a key is
set, never its value.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from .util import deep_merge

log = logging.getLogger("vast_worker")

DEFAULT_SETTINGS: dict[str, Any] = {
    "gpu": {
        "min_vram_gb": 24,
        "gpu_names": [],  # empty = any GPU model
        "num_gpus": 1,
        "max_dph": 1.0,  # USD per hour, hard ceiling for offers
        "min_reliability": 0.98,
        "min_inet_down_mbps": 200,
        "min_cuda": 12.4,
        # CUDA compute capability x100. 750 = Turing (RTX 20 series) or newer.
        # The PyTorch build in Vast's ComfyUI image no longer runs on Pascal
        # and older cards (Tesla P40, GTX 10 series), which are often the
        # cheapest offers.
        "min_compute_cap": 750,
        "verified_only": True,
        "countries": [],  # ISO codes; empty = anywhere
        "exclude_countries": ["CN"],  # Hugging Face is unreachable from some regions
    },
    "instance": {
        "image": "auto",  # auto = vastai/comfy tag matching the local ComfyUI version
        "disk_gb": 80,
        "runtype": "ssh_direct",
        "onstart": "entrypoint.sh",
        "extra_env": {},
    },
    "lifecycle": {
        "idle_minutes": 15,
        "watchdog_minutes": 10,
        "boot_timeout_minutes": 25,
        "confirm_rental": True,
        "max_rent_attempts": 2,
    },
    "connection": {
        "scheme": "https",
        "verify_tls": False,
    },
    "upload": {
        # What to do with a model file the workflow uses that exists on this
        # computer but has no download link: "auto" uploads it, "ask" shows a
        # dialog first, "off" never uploads (a link is required).
        "local_models": "auto",
        # In "auto" mode, files larger than this still ask first. 0 = never ask.
        "confirm_over_mb": 2000,
        # Before uploading a local model, ask Civitai (by file hash) whether it
        # has that exact file, and download it on the instance if so.
        "civitai_lookup": True,
    },
}

UPLOAD_POLICIES = ("auto", "ask", "off")

SECRET_KEYS = ("vast_api_key", "hf_token", "civitai_token")
_SECRET_ENV = {
    "vast_api_key": "VAST_API_KEY",
    "hf_token": "HF_TOKEN",
    "civitai_token": "CIVITAI_TOKEN",
}

_lock = threading.RLock()


def get_data_dir() -> str:
    """Directory holding config, secrets, state and the model library."""
    override = os.environ.get("VAST_WORKER_DATA_DIR")
    if override:
        path = override
    else:
        import folder_paths  # provided by ComfyUI

        if hasattr(folder_paths, "get_system_user_directory"):
            path = folder_paths.get_system_user_directory("vast_worker")
        else:  # older ComfyUI: no protected system directory
            path = os.path.join(folder_paths.get_user_directory(), "__vast_worker")
    os.makedirs(path, exist_ok=True)
    return path


def _path(name: str) -> str:
    return os.path.join(get_data_dir(), name)


def _read_json(name: str, default: Any) -> Any:
    try:
        with open(_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as exc:  # corrupted file: keep going with defaults
        log.warning("[vast-worker] could not read %s: %s", name, exc)
        return default


def _write_json(name: str, data: Any, private: bool = False) -> None:
    path = _path(name)
    tmp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def _coerce(default: Any, value: Any) -> Any:
    """Coerce ``value`` to the type of ``default``; fall back to the default."""
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int):
            return int(float(value))
        if isinstance(default, float):
            return float(value)
        if isinstance(default, str):
            return str(value)
        if isinstance(default, list):
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",")]
            return [str(v) for v in value if str(v).strip()]
        if isinstance(default, dict):
            return {str(k): str(v) for k, v in dict(value).items()}
    except Exception:
        return default
    return value


def sanitize_settings(raw: dict | None) -> dict:
    """Keep only known keys, coerced to the right types and sane ranges."""
    out: dict[str, Any] = {}
    raw = raw or {}
    for section, defaults in DEFAULT_SETTINGS.items():
        src = raw.get(section) if isinstance(raw.get(section), dict) else {}
        out[section] = {
            key: _coerce(default, src[key]) if key in src else default
            for key, default in defaults.items()
        }
    gpu, life, inst, conn = out["gpu"], out["lifecycle"], out["instance"], out["connection"]
    gpu["min_vram_gb"] = max(0, gpu["min_vram_gb"])
    gpu["num_gpus"] = max(1, gpu["num_gpus"])
    gpu["max_dph"] = max(0.01, gpu["max_dph"])
    gpu["min_reliability"] = min(1.0, max(0.0, gpu["min_reliability"]))
    gpu["countries"] = sorted({c.strip().upper() for c in gpu["countries"] if c.strip()})
    gpu["exclude_countries"] = sorted({c.strip().upper() for c in gpu["exclude_countries"] if c.strip()})
    gpu["gpu_names"] = [n.strip() for n in gpu["gpu_names"] if n.strip()]
    upload = out["upload"]
    if upload["local_models"] not in UPLOAD_POLICIES:
        upload["local_models"] = "auto"
    upload["confirm_over_mb"] = max(0, upload["confirm_over_mb"])
    inst["disk_gb"] = max(20, inst["disk_gb"])
    life["idle_minutes"] = max(1, life["idle_minutes"])
    life["watchdog_minutes"] = max(0, life["watchdog_minutes"])
    life["boot_timeout_minutes"] = max(3, life["boot_timeout_minutes"])
    life["max_rent_attempts"] = min(5, max(1, life["max_rent_attempts"]))
    if conn["scheme"] not in ("https", "http"):
        conn["scheme"] = "https"
    return out


def load_settings() -> dict:
    with _lock:
        stored = _read_json("config.json", {})
        merged = deep_merge(DEFAULT_SETTINGS, stored if isinstance(stored, dict) else {})
        env_override = os.environ.get("VAST_WORKER_SETTINGS_JSON")
        if env_override:
            try:
                merged = deep_merge(merged, json.loads(env_override))
            except Exception as exc:
                log.warning("[vast-worker] ignoring VAST_WORKER_SETTINGS_JSON: %s", exc)
        return sanitize_settings(merged)


def save_settings(patch: dict) -> dict:
    """Merge ``patch`` into the stored settings and return the result."""
    with _lock:
        stored = _read_json("config.json", {})
        merged = deep_merge(DEFAULT_SETTINGS, stored if isinstance(stored, dict) else {})
        merged = sanitize_settings(deep_merge(merged, patch or {}))
        _write_json("config.json", merged)
        return load_settings()


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------

def get_secret(name: str) -> str | None:
    """Stored value first, then the matching environment variable."""
    if name not in SECRET_KEYS:
        raise KeyError(name)
    with _lock:
        stored = _read_json("secrets.json", {})
    value = stored.get(name) if isinstance(stored, dict) else None
    if value:
        return str(value)
    return os.environ.get(_SECRET_ENV[name]) or None


def set_secrets(patch: dict) -> None:
    """Store secrets. A value of "" or None removes that secret."""
    with _lock:
        stored = _read_json("secrets.json", {})
        if not isinstance(stored, dict):
            stored = {}
        for key, value in (patch or {}).items():
            if key not in SECRET_KEYS:
                continue
            if value is None or str(value).strip() == "":
                stored.pop(key, None)
            else:
                stored[key] = str(value).strip()
        _write_json("secrets.json", stored, private=True)


def secrets_status() -> dict:
    """Where each secret comes from ("stored", "env" or None). Never the value."""
    with _lock:
        stored = _read_json("secrets.json", {})
    out = {}
    for key in SECRET_KEYS:
        if isinstance(stored, dict) and stored.get(key):
            out[key] = "stored"
        elif os.environ.get(_SECRET_ENV[key]):
            out[key] = "env"
        else:
            out[key] = None
    return out


# --------------------------------------------------------------------------
# instance state (survives a ComfyUI restart so a rented machine is never lost)
# --------------------------------------------------------------------------

def load_state() -> dict:
    with _lock:
        data = _read_json("state.json", {})
        return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    with _lock:
        _write_json("state.json", state or {}, private=True)
