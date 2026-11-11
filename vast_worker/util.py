"""Small helpers shared by the controller modules."""

from __future__ import annotations

import copy
import os
import re
from typing import Any

MODEL_EXTENSIONS = (
    ".safetensors",
    ".sft",
    ".ckpt",
    ".pt",
    ".pth",
    ".bin",
    ".gguf",
    ".onnx",
)


def deep_merge(base: dict, override: dict) -> dict:
    """Return a copy of ``base`` with ``override`` merged into it recursively."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def safe_relative_path(path: str) -> str:
    """Normalise an untrusted relative path; raise ValueError if it escapes.

    Accepts forward or back slashes. Returns a path using the OS separator
    that is guaranteed to stay below whatever base it is later joined to.
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    cleaned = path.replace("\\", "/").strip()
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ValueError(f"absolute paths are not allowed: {path!r}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"path escapes its base directory: {path!r}")
    return os.path.join(*parts) if parts else ""


def safe_join(base: str, *paths: str) -> str:
    """Join untrusted relative paths under ``base``; raise if the result escapes."""
    rel = [safe_relative_path(p) for p in paths if p]
    full = os.path.abspath(os.path.join(base, *rel))
    base_abs = os.path.abspath(base)
    if full != base_abs and not full.startswith(base_abs + os.sep):
        raise ValueError(f"path escapes its base directory: {paths!r}")
    return full


def is_model_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) < 512
        and "\n" not in value
        and value.lower().endswith(MODEL_EXTENSIONS)
    )


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def redact(text: str, *secrets: str | None) -> str:
    """Remove secret values from text that is about to be logged or shown."""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    return out
