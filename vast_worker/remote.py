"""HTTP / WebSocket client for one rented instance.

Talks to three things on the instance, all behind the same bearer token:
the remote ComfyUI API, the worker agent routes it hosts, and (once, for
bootstrap) the Instance Portal API.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable
from urllib.parse import urlencode

import aiohttp

from .util import safe_join

AGENT_PREFIX = "/vast_worker_agent"


class RemoteError(Exception):
    """The instance answered, but with an error."""

    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RemoteUnavailable(RemoteError):
    """The instance could not be reached at all."""


class RemoteClient:
    def __init__(self, comfy_base: str, portal_base: str | None, token: str, verify_tls: bool = False):
        self.comfy_base = comfy_base.rstrip("/")
        self.portal_base = portal_base.rstrip("/") if portal_base else None
        self._token = token
        self._ssl: Any = None if verify_tls else False
        self._session: aiohttp.ClientSession | None = None

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "X-Vast-Worker-Token": self._token}

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    async def _json(self, method: str, url: str, *, body: Any = None, timeout: float = 30) -> Any:
        try:
            async with self.session().request(
                method, url, json=body, ssl=self._ssl, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status in (502, 503, 504):
                    raise RemoteUnavailable(f"instance not ready (HTTP {resp.status})", resp.status)
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": (await resp.text())[:500]}
                if resp.status >= 400:
                    message = ""
                    if isinstance(data, dict):
                        message = str(data.get("error") or data.get("detail") or data.get("raw") or "")
                    raise RemoteError(message or f"HTTP {resp.status}", resp.status, data)
                return data
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            raise RemoteUnavailable(f"cannot reach the instance: {exc or type(exc).__name__}") from exc

    async def comfy(self, method: str, path: str, *, body: Any = None, timeout: float = 30) -> Any:
        return await self._json(method, f"{self.comfy_base}{path}", body=body, timeout=timeout)

    async def agent(self, method: str, path: str, *, body: Any = None, timeout: float = 30) -> Any:
        return await self._json(method, f"{self.comfy_base}{AGENT_PREFIX}{path}", body=body, timeout=timeout)

    # ------------------------------------------------------------------
    # ComfyUI API
    # ------------------------------------------------------------------
    async def system_stats(self) -> dict:
        return await self.comfy("GET", "/system_stats", timeout=10)

    async def queue_prompt(self, body: dict) -> dict:
        return await self.comfy("POST", "/prompt", body=body, timeout=60)

    async def interrupt(self, prompt_id: str | None = None) -> None:
        body = {"prompt_id": prompt_id} if prompt_id else {}
        await self.comfy("POST", "/interrupt", body=body, timeout=15)

    async def delete_queued(self, prompt_id: str) -> None:
        await self.comfy("POST", "/queue", body={"delete": [prompt_id]}, timeout=15)

    async def upload(self, local_path: str, kind: str, path: str, directory: str = "",
                     progress: Callable[[int], None] | None = None, attempts: int = 4) -> int:
        """Stream a local file to the instance through the agent; returns its size.

        ``kind`` is "model" (with ``directory``, a model folder), "input",
        "temp", "output" or "package". The body is sent raw, so file size is
        not limited, and an interrupted upload resumes from where it stopped.
        ``progress`` is called with the number of bytes sent so far.
        """
        size = os.path.getsize(local_path)
        query = {"kind": kind, "path": path, "directory": directory}
        url = f"{self.comfy_base}{AGENT_PREFIX}/upload"
        name = os.path.basename(path)
        last_error = "upload did not complete"
        for attempt in range(attempts):
            try:
                status = await self.agent("GET", "/upload_status?" + urlencode(query), timeout=30)
                offset = int(status.get("bytes") or 0)
                if offset >= size:
                    offset = 0  # a stale partial file from something else: start over

                async def body(start: int = offset):
                    sent = start
                    with open(local_path, "rb") as f:
                        f.seek(start)
                        while True:
                            chunk = f.read(1 << 20)
                            if not chunk:
                                break
                            yield chunk
                            sent += len(chunk)
                            if progress:
                                progress(sent)

                params = dict(query, offset=str(offset), size=str(size))
                async with self.session().put(
                    url, params=params, data=body(), ssl=self._ssl,
                    headers={"Content-Length": str(size - offset), "Content-Type": "application/octet-stream"},
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600),
                ) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
                    if resp.status == 409:
                        last_error = "the instance has a different partial file"
                        continue
                    if resp.status >= 400:
                        raise RemoteError(
                            f"upload of {name} failed: {data.get('error') or 'HTTP ' + str(resp.status)}", resp.status)
                    if data.get("complete"):
                        if progress:
                            progress(size)
                        return size
                    last_error = f"only {data.get('bytes')} of {size} bytes arrived"
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError) as exc:
                last_error = str(exc) or type(exc).__name__
            if attempt < attempts - 1:
                await asyncio.sleep(2)
        raise RemoteUnavailable(f"upload of {name} failed: {last_error}")

    async def download_view(self, filename: str, subfolder: str, kind: str, dest_dir: str, dest_name: str) -> str:
        """Stream a remote output/temp file into ``dest_dir``; returns its path."""
        target = safe_join(dest_dir, dest_name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        params = {"filename": filename, "subfolder": subfolder or "", "type": kind}
        tmp = target + ".part"
        try:
            async with self.session().get(
                f"{self.comfy_base}/view", params=params, ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300),
            ) as resp:
                if resp.status >= 400:
                    raise RemoteError(f"could not fetch {filename}: HTTP {resp.status}", resp.status)
                with open(tmp, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            raise RemoteUnavailable(f"download of {filename} failed: {exc}") from exc
        os.replace(tmp, target)
        return target

    def ws_connect(self, client_id: str):
        base = self.comfy_base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return self.session().ws_connect(
            f"{base}/ws?clientId={client_id}", ssl=self._ssl, heartbeat=30,
            max_msg_size=256 * 1024 * 1024, timeout=30,
        )

    # ------------------------------------------------------------------
    # Instance Portal (bootstrap only)
    # ------------------------------------------------------------------
    async def portal_provision(self, inline_yaml: str) -> dict:
        if not self.portal_base:
            raise RemoteError("this instance has no portal endpoint")
        return await self._json(
            "POST", f"{self.portal_base}/capabilities/provision", body={"inline_yaml": inline_yaml}, timeout=60)

    # ------------------------------------------------------------------
    # worker agent
    # ------------------------------------------------------------------
    async def agent_ping(self) -> dict:
        return await self.agent("GET", "/ping", timeout=10)

    async def agent_heartbeat(self, ttl_seconds: float) -> dict:
        return await self.agent("POST", "/heartbeat", body={"ttl_seconds": ttl_seconds}, timeout=10)

    async def agent_check(self, models: list[dict], class_types: list[str]) -> dict:
        return await self.agent("POST", "/check", body={"models": models, "class_types": class_types}, timeout=60)

    async def agent_download(self, items: list[dict], hf_token: str | None, civitai_token: str | None) -> str:
        body = {"items": items, "hf_token": hf_token or "", "civitai_token": civitai_token or ""}
        return (await self.agent("POST", "/download", body=body))["job_id"]

    async def agent_install_nodes(self, packages: list[dict]) -> str:
        return (await self.agent("POST", "/install_nodes", body={"packages": packages}))["job_id"]

    async def agent_job(self, job_id: str) -> dict:
        return await self.agent("GET", f"/jobs/{job_id}", timeout=15)

    async def agent_restart(self) -> None:
        await self.agent("POST", "/restart", timeout=15)
