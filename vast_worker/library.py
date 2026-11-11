"""The remote model library: which model files can be downloaded on Vast.ai.

Models never have to exist locally. The library remembers, per model file,
the folder it belongs to and the URL it can be downloaded from. While Vast
mode is on, library entries are merged into ComfyUI's model dropdowns so they
can be selected in loader nodes even though the files are not on this machine.
"""

from __future__ import annotations

import threading
import time
from typing import Callable
from urllib.parse import unquote, urlparse

from . import config
from .util import safe_relative_path

PROBE_PREFIX = "\x00vast-worker-probe:"
_tls = threading.local()


def _canonical_folder(name: str) -> str:
    """Fold legacy folder names (unet, clip) onto the current ones."""
    try:
        import folder_paths

        mapper = getattr(folder_paths, "map_legacy", None)
        if callable(mapper):
            return mapper(name)
    except Exception:
        pass
    return {"unet": "diffusion_models", "clip": "text_encoders"}.get(name, name)


def filename_from_url(url: str) -> str:
    """Best-effort file name for a download URL ("" when it has none)."""
    path = unquote(urlparse(url).path)
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name if "." in name else ""


class ModelLibrary:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], dict] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        data = config._read_json("models.json", [])
        for item in data if isinstance(data, list) else []:
            try:
                key = (_canonical_folder(str(item["directory"])), str(item["name"]))
                self._entries[key] = {
                    "name": key[1],
                    "directory": key[0],
                    "url": str(item["url"]),
                    "source": str(item.get("source", "user")),
                    "added": float(item.get("added", 0)),
                }
            except (KeyError, TypeError, ValueError):
                continue
        self._loaded = True

    def _save(self) -> None:
        # Private: a pasted download link can carry an access token in its query string.
        config._write_json("models.json", sorted(self._entries.values(), key=lambda e: (e["directory"], e["name"])),
                           private=True)

    # ------------------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            self._load()
            return sorted((dict(e) for e in self._entries.values()), key=lambda e: (e["directory"], e["name"].lower()))

    def add(self, name: str, directory: str, url: str, source: str = "user") -> dict:
        name = safe_relative_path(str(name).strip()).replace("\\", "/")
        directory = safe_relative_path(str(directory).strip()).replace("\\", "/")
        url = str(url).strip()
        if not name:
            raise ValueError("a model entry needs a file name")
        if not directory:
            raise ValueError("a model entry needs a folder (for example checkpoints or loras)")
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("the download URL must start with http:// or https://")
        entry = {"name": name, "directory": _canonical_folder(directory), "url": url,
                 "source": source, "added": time.time()}
        with self._lock:
            self._load()
            self._entries[(entry["directory"], name)] = entry
            self._save()
        return dict(entry)

    def remove(self, directory: str, name: str) -> bool:
        with self._lock:
            self._load()
            removed = self._entries.pop((_canonical_folder(directory), name), None) is not None
            if removed:
                self._save()
            return removed

    def find(self, name: str, directory: str | None = None) -> dict | None:
        with self._lock:
            self._load()
            if directory is not None:
                entry = self._entries.get((_canonical_folder(directory), name))
                return dict(entry) if entry else None
            matches = [e for e in self._entries.values() if e["name"] == name]
            return dict(matches[0]) if len(matches) == 1 else None

    def names_for(self, directory: str) -> list[str]:
        with self._lock:
            self._load()
            wanted = _canonical_folder(directory)
            return [name for (folder, name) in self._entries if folder == wanted]


# ----------------------------------------------------------------------
# folder_paths integration
# ----------------------------------------------------------------------

_original_get_filename_list = None


def install_folder_paths_patch(library: ModelLibrary, enabled: Callable[[], bool]) -> None:
    """Wrap ``folder_paths.get_filename_list``.

    * While ``enabled()`` is true, library entries are appended to the list so
      remote-only models show up in dropdowns.
    * While a probe is running on the calling thread, a marker is returned
      instead, which lets :func:`probe_model_inputs` learn which node input
      reads which model folder.
    """
    global _original_get_filename_list
    import folder_paths

    if _original_get_filename_list is not None:
        return
    _original_get_filename_list = folder_paths.get_filename_list

    def patched(folder_name: str):
        if getattr(_tls, "probing", False):
            return [PROBE_PREFIX + folder_name]
        names = _original_get_filename_list(folder_name)
        try:
            if enabled():
                extra = library.names_for(folder_name)
                if extra:
                    seen = set(names)
                    names = list(names) + [n for n in extra if n not in seen]
        except Exception:
            pass
        return names

    folder_paths.get_filename_list = patched


def local_filename_list(folder_name: str) -> list[str]:
    """The real local file list, without library entries."""
    try:
        getter = _original_get_filename_list
        if getter is None:
            import folder_paths

            getter = folder_paths.get_filename_list
        return list(getter(folder_name))
    except Exception:
        return []


_probe_cache: dict[str, dict[str, str]] = {}


def probe_model_inputs(class_type: str, node_cls) -> dict[str, str]:
    """Map input name -> model folder for one node class.

    Calls the node's ``INPUT_TYPES`` with the folder_paths patch in probe
    mode, then looks for the marker in each combo's option list.
    """
    if class_type in _probe_cache:
        return _probe_cache[class_type]
    result: dict[str, str] = {}
    if _original_get_filename_list is not None:
        _tls.probing = True
        try:
            types = node_cls.INPUT_TYPES()
        except Exception:
            types = {}
        finally:
            _tls.probing = False
        for section in ("required", "optional"):
            for name, spec in (types.get(section) or {}).items():
                if not isinstance(spec, (list, tuple)) or not spec:
                    continue
                options = spec[0]
                if not isinstance(options, list) and len(spec) > 1 and isinstance(spec[1], dict):
                    options = spec[1].get("options")
                if isinstance(options, (list, tuple)):
                    for option in options:
                        if isinstance(option, str) and option.startswith(PROBE_PREFIX):
                            result[name] = _canonical_folder(option[len(PROBE_PREFIX):])
                            break
    _probe_cache[class_type] = result
    return result
