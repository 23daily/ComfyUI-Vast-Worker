"""Finds where a local model file can be downloaded from on Civitai.

Civitai can look a model version up by the SHA-256 of one of its files. That
turns "this LoRA is on my disk" into a download link the rented instance can
fetch over its data-centre connection, which is far quicker than uploading
the file from home.

Only the hash is sent to Civitai, never the file.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from urllib.parse import urlparse

import aiohttp

from . import config

DEFAULT_BASE = "https://civitai.com"


def api_base() -> str:
    return (os.environ.get("VAST_WORKER_CIVITAI_BASE") or DEFAULT_BASE).rstrip("/")


def file_sha256(path: str) -> str:
    """SHA-256 of a file, cached by path, size and modification time.

    Hashing a multi-gigabyte checkpoint takes a while, so results are kept in
    the plugin's data directory and reused until the file changes.
    """
    stat = os.stat(path)
    cache = config._read_json("hashes.json", {})
    if not isinstance(cache, dict):
        cache = {}
    entry = cache.get(path)
    if isinstance(entry, dict) and entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime:
        return str(entry["sha256"])
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    cache[path] = {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": value}
    config._write_json("hashes.json", cache)
    return value


def pick_download(data: dict, sha256: str) -> dict | None:
    """The download link for our exact file out of a model-version response."""
    if not isinstance(data, dict):
        return None
    url = None
    for item in data.get("files") or []:
        hashes = item.get("hashes") if isinstance(item, dict) else None
        if isinstance(hashes, dict) and str(hashes.get("SHA256", "")).lower() == sha256.lower():
            url = item.get("downloadUrl")
            break
    url = url or data.get("downloadUrl")
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
        return None
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    title = " — ".join(str(part) for part in (model.get("name"), data.get("name")) if part)
    return {"url": url, "title": title or "a Civitai model", "version_id": data.get("id")}


async def find_download(path: str, token: str | None = None) -> dict | None:
    """Look a local file up on Civitai. Returns ``{url, title, version_id}`` or None."""
    sha256 = await asyncio.get_running_loop().run_in_executor(None, file_sha256, path)
    headers = {"User-Agent": "comfyui-vast-worker"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{api_base()}/api/v1/model-versions/by-hash/{sha256}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return None  # not a Civitai file (a LoRA you trained yourself, for example)
            if resp.status >= 400:
                raise RuntimeError(f"Civitai answered HTTP {resp.status}")
            data = await resp.json(content_type=None)
    return pick_download(data, sha256)
