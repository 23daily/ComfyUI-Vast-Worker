"""ComfyUI-Vast-Worker remote agent.

A single-file ComfyUI custom node that the controller installs on the rented
Vast.ai instance. It adds a few HTTP routes to the *remote* ComfyUI:

* download models straight from Hugging Face / Civitai onto the instance
* install custom node packages (git clone, or an uploaded archive)
* restart ComfyUI
* a dead-man's switch: if the controller stops sending heartbeats, the
  instance destroys itself so it cannot keep billing unattended

It must stay self-contained (standard library + aiohttp + ComfyUI itself),
because it is shipped to the instance as one file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp import web

import folder_paths
from server import PromptServer

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

AGENT_PROTOCOL = 1
PREFIX = "/vast_worker_agent"
log = logging.getLogger("vast_worker_agent")

try:
    with open(__file__, "rb") as _f:
        AGENT_VERSION = hashlib.sha256(_f.read()).hexdigest()[:16]
except OSError:
    AGENT_VERSION = "unknown"

BOOT_ID = uuid.uuid4().hex  # changes every time ComfyUI (re)starts
_TOKEN = os.environ.get("VAST_WORKER_TOKEN") or ""
_JOBS: dict[str, dict] = {}
_MAX_JOBS = 50


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _safe_rel(path: str) -> str:
    cleaned = str(path).replace("\\", "/").strip()
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ValueError(f"absolute path not allowed: {path!r}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"path escapes its base: {path!r}")
    return os.path.join(*parts) if parts else ""


def _safe_join(base: str, *paths: str) -> str:
    full = os.path.abspath(os.path.join(base, *[_safe_rel(p) for p in paths if p]))
    base_abs = os.path.abspath(base)
    if full != base_abs and not full.startswith(base_abs + os.sep):
        raise ValueError(f"path escapes its base: {paths!r}")
    return full


def _folder_key(directory: str) -> str:
    """Map legacy folder names (unet, clip) to the current ones."""
    mapper = getattr(folder_paths, "map_legacy", None)
    return mapper(directory) if callable(mapper) else directory


def _model_dir(directory: str) -> str:
    directory = _safe_rel(directory)
    key = _folder_key(directory)
    if key in folder_paths.folder_names_and_paths:
        return folder_paths.get_folder_paths(key)[0]
    return os.path.join(folder_paths.models_dir, directory)


def _model_path(directory: str, name: str) -> str:
    return _safe_join(_model_dir(directory), name)


def _model_exists(directory: str, name: str) -> bool:
    try:
        key = _folder_key(_safe_rel(directory))
        if key in folder_paths.folder_names_and_paths:
            found = folder_paths.get_full_path(key, name)
            if found and os.path.isfile(found) and os.path.getsize(found) > 0:
                return True
        path = _model_path(directory, name)
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception:
        return False


DISK_MARGIN = 1 << 30  # keep 1 GB free for ComfyUI's own temp and output files


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def _free_bytes(path: str | None = None) -> int:
    try:
        return shutil.disk_usage(path or folder_paths.models_dir).free
    except OSError:
        return 0


def _space_problem(needed: int, free: int, what: str) -> str | None:
    """A message for the user when ``needed`` bytes will not fit, else None."""
    if needed + DISK_MARGIN <= free:
        return None
    return (f"Not enough disk space on the instance for {what}: {_gb(needed)} needed, {_gb(free)} free. "
            "The disk size of a Vast.ai instance cannot be changed: destroy this instance, raise "
            "\"Disk (GB)\" under Machine setup, and rent again.")


def _new_job(kind: str) -> dict:
    if len(_JOBS) >= _MAX_JOBS:
        finished = [k for k, v in _JOBS.items() if v["state"] in ("done", "error")]
        for key in finished[: len(_JOBS) - _MAX_JOBS + 1]:
            _JOBS.pop(key, None)
    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "state": "running",
        "error": None,
        "items": [],
        "log": [],
        "started": time.time(),
    }
    _JOBS[job["id"]] = job
    return job


def _job_log(job: dict, line: str) -> None:
    job["log"].append(line.rstrip()[:500])
    del job["log"][:-200]
    log.info("[vast-worker-agent] %s", line.rstrip()[:300])


def _authorized(request: web.Request) -> bool:
    if not _TOKEN:
        return True  # the edge proxy (Caddy on Vast images) is the gate
    header = request.headers.get("X-Vast-Worker-Token") or ""
    bearer = request.headers.get("Authorization") or ""
    if bearer.lower().startswith("bearer "):
        bearer = bearer[7:]
    return header == _TOKEN or bearer == _TOKEN


def _guard(handler):
    async def wrapped(request: web.Request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # never leak a traceback page to the client
            log.exception("[vast-worker-agent] request failed")
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    return wrapped


# ----------------------------------------------------------------------
# dead-man's switch
# ----------------------------------------------------------------------

class Watchdog(threading.Thread):
    """Destroys this instance when the controller has been silent too long."""

    def __init__(self):
        super().__init__(daemon=True, name="vast-worker-watchdog")
        try:
            minutes = float(os.environ.get("VAST_WORKER_WATCHDOG_MINUTES", "10"))
        except ValueError:
            minutes = 10.0
        self.ttl = minutes * 60.0
        self.beat_file = os.path.join(tempfile.gettempdir(), "vast_worker_heartbeat")
        self.instance_id = os.environ.get("CONTAINER_ID") or ""
        self.instance_key = os.environ.get("CONTAINER_API_KEY") or ""
        self.api_base = (os.environ.get("VAST_WORKER_API_BASE") or "https://console.vast.ai").rstrip("/")
        self.fired = False
        if os.path.exists(self.beat_file):
            # ComfyUI was restarted: keep counting from the last real heartbeat.
            self.last_beat = os.path.getmtime(self.beat_file)
        else:
            self.last_beat = time.time()
            self._touch()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0 and bool(self.instance_id and self.instance_key)

    def _touch(self) -> None:
        try:
            with open(self.beat_file, "w") as f:
                f.write(str(self.last_beat))
        except OSError:
            pass

    def beat(self, ttl_seconds: float | None = None) -> None:
        self.last_beat = time.time()
        if ttl_seconds is not None and ttl_seconds >= 0:
            self.ttl = float(ttl_seconds)
        self._touch()

    def age(self) -> float:
        return time.time() - self.last_beat

    def _call(self, method: str, body: dict | None = None) -> bool:
        url = f"{self.api_base}/api/v0/instances/{self.instance_id}/"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.instance_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            log.warning("[vast-worker-agent] self-%s refused: HTTP %s", method, exc.code)
        except Exception as exc:
            log.warning("[vast-worker-agent] self-%s failed: %s", method, exc)
        return False

    def run(self) -> None:
        if not self.enabled:
            log.info("[vast-worker-agent] watchdog disabled (no instance credentials or ttl=0)")
            return
        log.info("[vast-worker-agent] watchdog armed: self-destroy after %.0fs without a heartbeat", self.ttl)
        while True:
            time.sleep(15 if self.ttl >= 60 else 1)
            if self.ttl <= 0 or self.age() < self.ttl:
                continue
            log.warning("[vast-worker-agent] no heartbeat for %.0fs: destroying this instance", self.age())
            # Destroy stops all billing. If the instance key may not destroy,
            # stopping at least ends GPU charges (storage is still billed).
            if self._call("DELETE") or self._call("PUT", {"state": "stopped"}):
                self.fired = True
                return
            time.sleep(60)


WATCHDOG = Watchdog()
WATCHDOG.start()


# ----------------------------------------------------------------------
# model downloads
# ----------------------------------------------------------------------

class DownloadError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def _auth_header(url: str, tokens: dict) -> dict:
    """Send a token only to the host it belongs to, over https."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return {}
    if tokens.get("hf_token") and (host == "huggingface.co" or host.endswith(".huggingface.co")):
        return {"Authorization": f"Bearer {tokens['hf_token']}"}
    if tokens.get("civitai_token") and (host == "civitai.com" or host.endswith(".civitai.com")):
        return {"Authorization": f"Bearer {tokens['civitai_token']}"}
    return {}


async def _open(session: aiohttp.ClientSession, url: str, tokens: dict, offset: int, probe: bool = False):
    """GET with manual redirects, so tokens never follow a redirect to a CDN.

    ``probe`` asks for the first byte only, to learn the file size.
    """
    for _ in range(10):
        headers = {"User-Agent": "comfyui-vast-worker"}
        headers.update(_auth_header(url, tokens))
        if probe:
            headers["Range"] = "bytes=0-0"
        elif offset:
            headers["Range"] = f"bytes={offset}-"
        resp = await session.get(url, headers=headers, allow_redirects=False)
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.release()
            if not location:
                raise DownloadError("redirect without a Location header", fatal=True)
            url = urljoin(url, location)
            continue
        return resp
    raise DownloadError("too many redirects", fatal=True)


async def _download_one(session: aiohttp.ClientSession, item: dict, tokens: dict) -> None:
    dest = _model_path(item["directory"], item["name"])
    if _model_exists(item["directory"], item["name"]):
        item["state"] = "exists"
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    delay = 2.0
    for attempt in range(6):
        try:
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            resp = await _open(session, item["url"], tokens, offset)
            try:
                if resp.status in (401, 403):
                    host = urlparse(item["url"]).hostname or "the server"
                    raise DownloadError(
                        f"HTTP {resp.status} from {host}: an access token is required "
                        "(or the model licence has not been accepted)", fatal=True)
                if resp.status == 404:
                    raise DownloadError("HTTP 404: file not found at that URL", fatal=True)
                if resp.status == 416:  # range past the end: the part file is complete or stale
                    os.remove(part)
                    raise DownloadError("stale partial download, restarting")
                if resp.status not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status}")
                if offset and resp.status != 206:
                    offset = 0  # server ignored the range: start over
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if content_type.startswith("text/html"):
                    raise DownloadError("received a web page instead of a file (login required?)", fatal=True)
                length = resp.headers.get("Content-Length")
                item["total"] = offset + int(length) if length and length.isdigit() else None
                item["bytes"] = offset
                item["state"] = "downloading"
                with open(part, "ab" if offset else "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
                        item["bytes"] += len(chunk)
            finally:
                resp.release()
            if item.get("total") and item["bytes"] != item["total"]:
                raise DownloadError("connection closed before the file was complete")
            os.replace(part, dest)
            item["state"] = "done"
            return
        except DownloadError as exc:
            if exc.fatal or attempt == 5:
                raise
            item["state"] = "retrying"
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == 5:
                raise DownloadError(f"network error: {exc}") from exc
            item["state"] = "retrying"
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def _probe_size(session: aiohttp.ClientSession, item: dict, tokens: dict) -> int | None:
    """Size of a download in bytes, or None when the server does not say."""
    try:
        resp = await _open(session, item["url"], tokens, 0, probe=True)
    except Exception:
        return None
    try:
        content_range = resp.headers.get("Content-Range") or ""
        if resp.status == 206 and "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            return int(total) if total.isdigit() else None
        length = resp.headers.get("Content-Length")
        return int(length) if resp.status == 200 and length and length.isdigit() else None
    finally:
        resp.close()


async def _run_download(job: dict, tokens: dict, concurrency: int = 3) -> None:
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

    async def one(item: dict) -> None:
        async with sem:
            try:
                await _download_one(session, item, tokens)
            except Exception as exc:
                item["state"] = "error"
                item["error"] = str(exc)
                _job_log(job, f"download failed: {item['name']}: {exc}")

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Check that everything fits before the first byte is written:
            # filling the disk halfway through wastes minutes of rented time.
            pending = [i for i in job["items"] if not _model_exists(i["directory"], i["name"])]
            sizes = await asyncio.gather(*(_probe_size(session, i, tokens) for i in pending))
            for item, size in zip(pending, sizes):
                item["total"] = size
            problem = _space_problem(sum(s or 0 for s in sizes), _free_bytes(), "these models")
            if problem:
                _job_log(job, problem)
                job["error"] = problem
                job["state"] = "error"
                return
            await asyncio.gather(*(one(item) for item in job["items"]))
        failed = [i for i in job["items"] if i["state"] == "error"]
        if failed:
            job["error"] = "; ".join(f"{i['name']}: {i['error']}" for i in failed)
            job["state"] = "error"
        else:
            job["state"] = "done"
    except Exception as exc:
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["state"] = "error"


# ----------------------------------------------------------------------
# custom node installation
# ----------------------------------------------------------------------

async def _run(job: dict, cmd: list[str], cwd: str | None = None) -> int:
    _job_log(job, "$ " + " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout is not None
    async for raw in proc.stdout:
        _job_log(job, raw.decode("utf-8", "replace"))
    return await proc.wait()


def _extract_archive(archive: str, dest: str) -> None:
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with tarfile.open(archive, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # older Python: validate members by hand
            base = os.path.abspath(dest)
            for member in tar.getmembers():
                target = os.path.abspath(os.path.join(dest, member.name))
                if not (target == base or target.startswith(base + os.sep)):
                    raise ValueError(f"archive member escapes destination: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError(f"archive member type not allowed: {member.name}")
            tar.extractall(dest)


def _step(item: dict | None, text: str) -> None:
    """Record what an install is doing right now, for the progress display."""
    if item is not None:
        item["step"] = text
        item["step_started"] = time.time()


async def _install_requirements(job: dict, pkg_dir: str, item: dict | None = None) -> None:
    req = os.path.join(pkg_dir, "requirements.txt")
    if os.path.isfile(req):
        _step(item, "installing Python packages")
        # Never let a node pack replace the instance's CUDA build of torch.
        with open(req, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f if not re.match(r"^\s*(torch|torchvision|torchaudio|torchcodec)(\s|[<>=!~\[]|$)", ln)]
        filtered = os.path.join(pkg_dir, ".vast_worker_requirements.txt")
        with open(filtered, "w", encoding="utf-8") as f:
            f.writelines(lines)
        if shutil.which("uv"):
            cmd = ["uv", "pip", "install", "--python", sys.executable, "-r", filtered]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "-r", filtered]
        code = await _run(job, cmd, cwd=pkg_dir)
        if code != 0:
            raise RuntimeError(f"installing requirements failed (exit {code})")
    installer = os.path.join(pkg_dir, "install.py")
    if os.path.isfile(installer):
        _step(item, "running its install script")
        code = await _run(job, [sys.executable, "install.py"], cwd=pkg_dir)
        if code != 0:
            raise RuntimeError(f"install.py failed (exit {code})")


async def _install_one(job: dict, pkg: dict, item: dict | None = None) -> None:
    custom_dir = folder_paths.get_folder_paths("custom_nodes")[0]
    name = os.path.basename(_safe_rel(pkg["name"]))
    if not name or name.startswith("."):
        raise ValueError(f"invalid package name: {pkg.get('name')!r}")
    kind = pkg.get("type")

    if kind == "git":
        dest = os.path.join(custom_dir, name)
        if not os.path.isdir(dest):
            _step(item, "cloning the repository")
            code = await _run(job, ["git", "clone", "--recursive", pkg["url"], dest])
            if code != 0:
                raise RuntimeError(f"git clone failed (exit {code})")
        commit = pkg.get("commit")
        if commit:
            checkout = ["git", "-C", dest, "-c", "advice.detachedHead=false", "checkout", "--recurse-submodules", commit]
            code = await _run(job, checkout)
            if code != 0:
                await _run(job, ["git", "-C", dest, "fetch", "--all", "--tags"])
                code = await _run(job, checkout)
                if code != 0:
                    _job_log(job, f"could not check out {commit}; staying on the default branch")
        await _install_requirements(job, dest, item)
    elif kind == "upload":
        source = _safe_join(folder_paths.get_input_directory(), pkg["path"])
        if not os.path.isfile(source):
            raise FileNotFoundError(f"uploaded package not found: {pkg['path']}")
        if pkg.get("single_file"):
            if not name.endswith(".py"):
                raise ValueError("single-file packages must be .py files")
            shutil.copyfile(source, os.path.join(custom_dir, name))
        else:
            dest = os.path.join(custom_dir, name)
            _extract_archive(source, dest)
            await _install_requirements(job, dest, item)
        try:
            os.remove(source)
        except OSError:
            pass
    else:
        raise ValueError(f"unknown package type: {kind!r}")


async def _run_install(job: dict, packages: list[dict]) -> None:
    try:
        for pkg, item in zip(packages, job["items"]):
            item["state"] = "installing"
            try:
                await _install_one(job, pkg, item)
                item["state"] = "done"
                item["step"] = None
            except Exception as exc:
                item["state"] = "error"
                item["error"] = f"{type(exc).__name__}: {exc}"
                _job_log(job, f"install failed: {item['name']}: {exc}")
        failed = [i for i in job["items"] if i["state"] == "error"]
        if failed:
            job["error"] = "; ".join(f"{i['name']}: {i['error']}" for i in failed)
            job["state"] = "error"
        else:
            job["state"] = "done"
    except Exception as exc:
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["state"] = "error"


def _restart_comfyui() -> None:
    log.info("[vast-worker-agent] restarting ComfyUI")
    if shutil.which("supervisorctl"):
        subprocess.Popen(
            ["supervisorctl", "restart", "comfyui"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


# ----------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------

routes = PromptServer.instance.routes


@routes.get(f"{PREFIX}/ping")
@_guard
async def ping(request: web.Request):
    info: dict = {
        "ok": True,
        "agent_version": AGENT_VERSION,
        "protocol": AGENT_PROTOCOL,
        "boot_id": BOOT_ID,
        "heartbeat_age": round(WATCHDOG.age(), 1),
        "watchdog_seconds": WATCHDOG.ttl if WATCHDOG.enabled else 0,
    }
    info["disk_free_bytes"] = _free_bytes()
    try:
        import comfyui_version

        info["comfyui_version"] = comfyui_version.__version__
    except Exception:
        info["comfyui_version"] = None
    try:
        import torch

        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
            info["vram_bytes"] = torch.cuda.get_device_properties(0).total_memory
    except Exception:
        pass
    return web.json_response(info)


@routes.post(f"{PREFIX}/heartbeat")
@_guard
async def heartbeat(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    ttl = body.get("ttl_seconds")
    WATCHDOG.beat(float(ttl) if isinstance(ttl, (int, float)) else None)
    return web.json_response({"ok": True, "watchdog_seconds": WATCHDOG.ttl if WATCHDOG.enabled else 0,
                              "disk_free_bytes": _free_bytes()})


@routes.post(f"{PREFIX}/check")
@_guard
async def check(request: web.Request):
    """Report which of the given models and node classes this instance lacks."""
    import nodes

    body = await request.json()
    missing_models = [
        {"directory": m["directory"], "name": m["name"]}
        for m in body.get("models", [])
        if not _model_exists(str(m.get("directory", "")), str(m.get("name", "")))
    ]
    missing_classes = [c for c in body.get("class_types", []) if c not in nodes.NODE_CLASS_MAPPINGS]
    return web.json_response({"missing_models": missing_models, "missing_class_types": missing_classes})


@routes.post(f"{PREFIX}/download")
@_guard
async def download(request: web.Request):
    body = await request.json()
    items = []
    for raw in body.get("items", []):
        url = str(raw.get("url", ""))
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError(f"unsupported download URL: {url!r}")
        directory, name = str(raw.get("directory", "")), str(raw.get("name", ""))
        _model_path(directory, name)  # validates both parts
        if not name:
            raise ValueError("a download item needs a file name")
        items.append({"directory": directory, "name": name, "url": url,
                      "state": "queued", "bytes": 0, "total": None, "error": None})
    job = _new_job("download")
    job["items"] = items
    tokens = {"hf_token": body.get("hf_token") or "", "civitai_token": body.get("civitai_token") or ""}
    asyncio.ensure_future(_run_download(job, tokens))
    return web.json_response({"job_id": job["id"]})


@routes.post(f"{PREFIX}/install_nodes")
@_guard
async def install_nodes(request: web.Request):
    body = await request.json()
    packages = [p for p in body.get("packages", []) if isinstance(p, dict)]
    job = _new_job("install")
    job["items"] = [{"name": str(p.get("name", "")), "state": "queued", "error": None} for p in packages]
    asyncio.ensure_future(_run_install(job, packages))
    return web.json_response({"job_id": job["id"]})


@routes.get(f"{PREFIX}/jobs/{{job_id}}")
@_guard
async def job_status(request: web.Request):
    job = _JOBS.get(request.match_info["job_id"])
    if job is None:
        return web.json_response({"error": "unknown job"}, status=404)
    public = {k: v for k, v in job.items() if k != "log"}
    public["items"] = [{k: v for k, v in item.items() if k != "url"} for item in job["items"]]
    for item in public["items"]:  # elapsed time is computed here, so clock differences do not matter
        if item.get("step") and item.get("step_started"):
            item["step_seconds"] = max(0, round(time.time() - item["step_started"]))
    public["log"] = job["log"][-30:]
    return web.json_response(public)


def _upload_target(query) -> str:
    """Where an uploaded file goes. Every part of the path is validated."""
    kind, path = query.get("kind", ""), query.get("path", "")
    if not path:
        raise ValueError("an upload needs a path")
    if kind == "model":
        return _model_path(query.get("directory", ""), path)
    if kind in ("input", "temp", "output"):
        return _safe_join(folder_paths.get_directory_by_type(kind), path)
    if kind == "package":
        return _safe_join(folder_paths.get_input_directory(), "vast_worker", "pkg", path)
    raise ValueError(f"unknown upload kind: {kind!r}")


@routes.get(f"{PREFIX}/upload_status")
@_guard
async def upload_status(request: web.Request):
    """How much of an interrupted upload is already here, so it can resume."""
    part = _upload_target(request.query) + ".part"
    return web.json_response({"bytes": os.path.getsize(part) if os.path.exists(part) else 0})


@routes.put(f"{PREFIX}/upload")
@_guard
async def upload(request: web.Request):
    """Receive a file from the controller as a raw request body.

    The body is streamed to disk, so there is no size limit (ComfyUI's own
    upload route refuses anything over 100 MB by default). ``offset`` lets an
    interrupted upload continue where it stopped.
    """
    target = _upload_target(request.query)
    part = target + ".part"
    offset = int(request.query.get("offset", "0"))
    total = int(request.query.get("size", "-1"))
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if offset and offset != have:
        return web.json_response({"error": "offset does not match the partial file", "bytes": have}, status=409)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if total >= 0:
        problem = _space_problem(total - offset, _free_bytes(os.path.dirname(target)), os.path.basename(target))
        if problem:
            return web.json_response({"error": problem}, status=507)
    with open(part, "ab" if offset else "wb") as f:
        async for chunk in request.content.iter_chunked(1 << 20):
            f.write(chunk)
    size = os.path.getsize(part)
    if total >= 0 and size != total:
        return web.json_response({"complete": False, "bytes": size})
    os.replace(part, target)
    return web.json_response({"complete": True, "bytes": size})


@routes.post(f"{PREFIX}/restart")
@_guard
async def restart(request: web.Request):
    asyncio.get_event_loop().call_later(0.5, _restart_comfyui)
    return web.json_response({"ok": True})


log.info("[vast-worker-agent] ready (version %s)", AGENT_VERSION)
