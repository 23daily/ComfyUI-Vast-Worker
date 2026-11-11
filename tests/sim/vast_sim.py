"""A local stand-in for Vast.ai, for end-to-end tests without renting anything.

It imitates the three things the plugin talks to:

* the Vast REST API (search offers, create / show / list / destroy instances)
* the instance edge: a reverse proxy that enforces the bearer token, like
  Caddy does on Vast's images, in front of a real ComfyUI child process
* the Instance Portal's ``POST /capabilities/provision``, executing the
  manifest's ``write_files`` and ``post_commands`` (with a fake
  ``supervisorctl`` that restarts the child)

"Renting" an instance starts a second ComfyUI on this machine with an empty
base directory laid out like the Vast image (``$WORKSPACE/ComfyUI``).

    python tests/sim/vast_sim.py --port 18400 --comfy-dir ~/ComfyUI --work-dir /tmp/sim
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time

import aiohttp
from aiohttp import web

OFFERS = [
    {"id": 9001, "machine_id": 501, "gpu_name": "RTX 4090", "num_gpus": 1, "gpu_ram": 24564,
     "dph_total": 0.31, "reliability2": 0.995, "inet_down": 900, "geolocation": "Sweden, SE",
     "cuda_max_good": 12.9, "compute_cap": 890, "cpu_cores_effective": 16, "cpu_ram": 64000, "disk_space": 400},
    {"id": 9002, "machine_id": 502, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24576,
     "dph_total": 0.22, "reliability2": 0.991, "inet_down": 500, "geolocation": "Texas, US",
     "cuda_max_good": 12.8, "compute_cap": 860, "cpu_cores_effective": 12, "cpu_ram": 48000, "disk_space": 300},
    {"id": 9003, "machine_id": 503, "gpu_name": "RTX 5090", "num_gpus": 1, "gpu_ram": 32607,
     "dph_total": 0.55, "reliability2": 0.999, "inet_down": 2000, "geolocation": "Quebec, CA",
     "cuda_max_good": 13.0, "compute_cap": 1200, "cpu_cores_effective": 24, "cpu_ram": 128000, "disk_space": 800},
    # Pascal-era card: cheapest of all, but the image's PyTorch cannot use it.
    {"id": 9004, "machine_id": 504, "gpu_name": "Tesla P40", "num_gpus": 1, "gpu_ram": 24576,
     "dph_total": 0.12, "reliability2": 0.993, "inet_down": 700, "geolocation": "California, US",
     "cuda_max_good": 12.8, "compute_cap": 610, "cpu_cores_effective": 8, "cpu_ram": 32000, "disk_space": 200},
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def expand(value, env: dict):
    """Imitate the provisioner's ${VAR} / ${VAR:-default} expansion."""
    if isinstance(value, str):
        def repl(match):
            name, default = match.group(1), match.group(3)
            return env.get(name) or (default if default is not None else "")
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}", repl, value)
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


class Instance:
    def __init__(self, sim: "Sim", instance_id: int, offer: dict, payload: dict):
        self.sim = sim
        self.id = instance_id
        self.offer = offer
        self.payload = payload
        self.env = {k: v for k, v in (payload.get("env") or {}).items() if not k.startswith("-p ")}
        self.label = payload.get("label")
        self.token = self.env.get("WEB_PASSWORD", "")
        self.container_key = f"container-key-{instance_id}"
        self.workspace = os.path.join(sim.work_dir, f"inst-{instance_id}", "workspace")
        self.comfy_base = os.path.join(self.workspace, "ComfyUI")
        self.comfy_port = free_port()
        self.edge_port = free_port()
        self.portal_port = free_port()
        self.created = time.time()
        self.status = "loading"
        self.status_msg = "pulling image"
        self.proc: subprocess.Popen | None = None
        self.runners: list[web.AppRunner] = []
        self.restarts = 0
        self.provisions: list[dict] = []
        self.log_path = os.path.join(sim.work_dir, f"inst-{instance_id}", "comfyui.log")
        for sub in ("custom_nodes", "models", "input", "output", "temp", "user"):
            os.makedirs(os.path.join(self.comfy_base, sub), exist_ok=True)

    # -- child ComfyUI -------------------------------------------------
    def spawn(self) -> None:
        env = dict(os.environ)
        for key in ("VAST_API_KEY", "VAST_WORKER_SETTINGS_JSON", "VAST_WORKER_DATA_DIR"):
            env.pop(key, None)
        env.update(self.env)
        env.update({
            "WORKSPACE": self.workspace,
            "CONTAINER_ID": str(self.id),
            "CONTAINER_API_KEY": self.container_key,
            "VAST_WORKER_API_BASE": self.sim.base_url,
            "SIM_INSTANCE_ID": str(self.id),
            "PATH": self.sim.fake_bin + os.pathsep + env.get("PATH", ""),
        })
        cmd = [self.sim.python, os.path.join(self.sim.comfy_dir, "main.py"),
               "--base-directory", self.comfy_base, "--port", str(self.comfy_port),
               "--listen", "127.0.0.1", "--cpu", "--disable-auto-launch"]
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(cmd, cwd=self.sim.comfy_dir, env=env, stdout=log, stderr=subprocess.STDOUT)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self) -> None:
        self.restarts += 1
        self.kill()
        self.spawn()

    # -- edge proxy and portal ------------------------------------------
    def _authorized(self, request: web.Request) -> bool:
        return bool(self.token) and request.headers.get("Authorization") == f"Bearer {self.token}"

    async def _edge(self, request: web.Request):
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        target = f"http://127.0.0.1:{self.comfy_port}{request.rel_url}"
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length", "connection", "upgrade", "sec-websocket-key",
                                        "sec-websocket-version", "sec-websocket-extensions")}
        session = self.sim.session
        if request.headers.get("Upgrade", "").lower() == "websocket":
            client = web.WebSocketResponse(max_msg_size=0)
            await client.prepare(request)
            try:
                upstream = await session.ws_connect(target.replace("http://", "ws://", 1), max_msg_size=0)
            except aiohttp.ClientError:
                await client.close(code=1013)
                return client

            async def pump(src, dst):
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break

            tasks = [asyncio.ensure_future(pump(client, upstream)), asyncio.ensure_future(pump(upstream, client))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await upstream.close()
            await client.close()
            return client
        try:
            # Stream the body through, like a real reverse proxy: nothing is
            # buffered, so large uploads behave as they would behind Caddy.
            body = request.content if request.body_exists else None
            if body is not None and request.content_length is not None:
                headers["Content-Length"] = str(request.content_length)
            async with session.request(request.method, target, headers=headers, data=body,
                                       allow_redirects=False) as resp:
                out = web.StreamResponse(status=resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                        out.headers[key] = value
                await out.prepare(request)
                async for chunk in resp.content.iter_chunked(1 << 16):
                    await out.write(chunk)
                await out.write_eof()
                return out
        except aiohttp.ClientError:
            return web.Response(status=502, text="upstream not ready")

    async def _provision(self, request: web.Request):
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        body = await request.json()
        text = body.get("inline_yaml")
        if not text:
            return web.json_response({"detail": "Provide inline_yaml"}, status=400)
        try:
            import yaml

            manifest = yaml.safe_load(text)
        except ImportError:
            manifest = json.loads(text)
        self.provisions.append(manifest)
        asyncio.ensure_future(self._run_provisioner(manifest))
        return web.json_response({"status": "started", "detail": "Provisioner launched in the background",
                                  "log_file": "/var/log/portal/provisioning.log"})

    async def _run_provisioner(self, manifest: dict) -> None:
        env = dict(os.environ)
        env.update({"WORKSPACE": self.workspace, "SIM_INSTANCE_ID": str(self.id),
                    "PATH": self.sim.fake_bin + os.pathsep + env.get("PATH", "")})
        manifest = expand(manifest, env)
        root = os.path.abspath(os.path.join(self.sim.work_dir, f"inst-{self.id}"))
        for item in manifest.get("write_files") or []:
            path = os.path.abspath(item["path"])
            if not path.startswith(root + os.sep):
                print(f"[sim] refusing to write outside the instance: {path}", flush=True)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(item.get("content", ""))
            os.chmod(path, int(str(item.get("permissions", "0644")), 8))
        for command in manifest.get("post_commands") or []:
            proc = await asyncio.create_subprocess_exec("bash", "-c", command, env=env, cwd=self.workspace)
            code = await proc.wait()
            if code != 0:
                print(f"[sim] post_command failed ({code}): {command[:120]}", flush=True)
                return

    async def start_servers(self) -> None:
        edge = web.Application(client_max_size=1024 ** 3)
        edge.router.add_route("*", "/{tail:.*}", self._edge)
        portal = web.Application(client_max_size=64 * 1024 ** 2)
        portal.router.add_post("/capabilities/provision", self._provision)
        for app, port in ((edge, self.edge_port), (portal, self.portal_port)):
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            self.runners.append(runner)

    async def stop_servers(self) -> None:
        for runner in self.runners:
            await runner.cleanup()
        self.runners = []

    def describe(self) -> dict:
        info = {
            "id": self.id, "label": self.label, "actual_status": self.status, "intended_status": "running",
            "status_msg": self.status_msg, "gpu_name": self.offer["gpu_name"], "num_gpus": 1,
            "dph_total": self.offer["dph_total"], "public_ipaddr": " 127.0.0.1 ", "start_date": self.created,
            "image_uuid": self.payload.get("image"),
        }
        if self.status == "running":
            info["ports"] = {
                "8188/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(self.edge_port)}],
                "1111/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(self.portal_port)}],
                "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40022"}],
            }
        return info


class Sim:
    def __init__(self, port: int, comfy_dir: str, python: str, work_dir: str):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.comfy_dir = os.path.abspath(os.path.expanduser(comfy_dir))
        self.python = python
        self.work_dir = os.path.abspath(work_dir)
        self.instances: dict[int, Instance] = {}
        self.destroyed: list[dict] = []
        self.next_id = 1001
        self.fail_offer_ids: set[int] = set()
        self.page_size = 2  # small, so the suite exercises pagination
        self.session: aiohttp.ClientSession | None = None
        self.calls: list[str] = []
        self.queries: list[dict] = []
        self.civitai: dict[str, dict] = {}
        self.civitai_queries: list[str] = []
        os.makedirs(self.work_dir, exist_ok=True)
        self.fake_bin = os.path.join(self.work_dir, "fakebin")
        os.makedirs(self.fake_bin, exist_ok=True)
        script = os.path.join(self.fake_bin, "supervisorctl")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n"
                    'if [ "$1" = "restart" ]; then\n'
                    f'  exec curl -fsS -X POST "{self.base_url}/_sim/restart/$SIM_INSTANCE_ID"\n'
                    "fi\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # -- helpers --------------------------------------------------------
    def _key(self, request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def _user(self, request: web.Request) -> bool:
        return self._key(request) == "sim-key"

    async def _boot(self, inst: Instance) -> None:
        await asyncio.sleep(1.0)
        if inst.id not in self.instances:
            return
        if inst.offer["id"] in self.fail_offer_ids:
            inst.status, inst.status_msg = "exited", "Error response from daemon: failed to start container"
            return
        await inst.start_servers()
        inst.spawn()
        inst.status, inst.status_msg = "running", "success, running"

    async def _remove(self, inst: Instance, how: str, by: str) -> None:
        self.instances.pop(inst.id, None)
        inst.kill()
        await inst.stop_servers()
        self.destroyed.append({"id": inst.id, "how": how, "by": by, "time": time.time(),
                               "restarts": inst.restarts, "label": inst.label})

    # -- Vast API -------------------------------------------------------
    async def whoami(self, request):
        if not self._user(request):
            return web.json_response({"success": False, "msg": "Invalid API key"}, status=401)
        return web.json_response({"id": 1, "email": "sim@example.com", "credit": 12.34})

    async def search(self, request):
        self.calls.append("search")
        if not self._user(request):
            return web.json_response({"success": False, "msg": "Invalid API key"}, status=401)
        query = await request.json()
        self.queries.append(query)
        ceiling = (query.get("dph_total") or {}).get("lte", 1e9)
        vram = (query.get("gpu_ram") or {}).get("gte", 0)
        cap = (query.get("compute_cap") or {}).get("gte", 0)
        names = (query.get("gpu_name") or {}).get("in")
        geo = query.get("geolocation") or {}
        offers = []
        for offer in OFFERS:
            country = offer["geolocation"].rsplit(",", 1)[-1].strip()
            if offer["dph_total"] > ceiling or offer["gpu_ram"] < vram or offer["compute_cap"] < cap:
                continue
            if names is not None and offer["gpu_name"] not in names:
                continue
            if "in" in geo and country not in geo["in"]:
                continue
            if "notin" in geo and country in geo["notin"]:
                continue
            offers.append(offer)
        offers.sort(key=lambda o: o["dph_total"])
        return web.json_response({"offers": offers})

    async def create(self, request):
        self.calls.append("create")
        if not self._user(request):
            return web.json_response({"success": False, "msg": "Invalid API key"}, status=401)
        offer_id = int(request.match_info["offer_id"])
        offer = next((o for o in OFFERS if o["id"] == offer_id), None)
        if offer is None:
            return web.json_response({"success": False, "msg": "no such ask"}, status=404)
        payload = await request.json()
        inst = Instance(self, self.next_id, offer, payload)
        self.next_id += 1
        self.instances[inst.id] = inst
        asyncio.ensure_future(self._boot(inst))
        return web.json_response({"success": True, "new_contract": inst.id})

    async def show(self, request):
        inst = self.instances.get(int(request.match_info["instance_id"]))
        key = self._key(request)
        if not (self._user(request) or (inst and key == inst.container_key)):
            return web.json_response({"success": False, "msg": "Invalid API key"}, status=401)
        # The real API answers 200 with "instances": null for an unknown id.
        return web.json_response({"instances": inst.describe() if inst else None})

    async def list_instances_v0(self, request):
        # The real service retired this endpoint; it answers exactly like this.
        return web.Response(status=410, text="/api/v0/instances/ is deprecated. Use /api/v1/instances/ instead.")

    async def list_instances(self, request):
        """v1 list: cursor pagination, at most 25 per page."""
        if not self._user(request):
            return web.json_response({"success": False, "msg": "Invalid API key"}, status=401)
        limit = int(request.query.get("limit", "25"))
        limit = 5 if limit <= 0 else min(limit, self.page_size)
        everything = sorted(self.instances.values(), key=lambda i: i.id)
        after = request.query.get("after_token")
        if after:
            everything = [i for i in everything if i.id > int(after)]
        page = everything[:limit]
        more = len(everything) > limit
        return web.json_response({
            "instances": [i.describe() for i in page],
            "instances_found": len(page),
            "total_instances": len(self.instances),
            "next_token": str(page[-1].id) if more and page else None,
        })

    async def destroy(self, request):
        self.calls.append("destroy")
        inst = self.instances.get(int(request.match_info["instance_id"]))
        key = self._key(request)
        if inst is None:
            return web.json_response({"success": False, "msg": "no such instance"}, status=404)
        if self._user(request):
            await self._remove(inst, "destroy", "user")
        elif key == inst.container_key:
            await self._remove(inst, "destroy", "instance")
        else:
            return web.json_response({"success": False, "msg": "Invalid API key"}, status=401)
        return web.json_response({"success": True})

    async def manage(self, request):
        inst = self.instances.get(int(request.match_info["instance_id"]))
        key = self._key(request)
        if inst is None or not (self._user(request) or key == inst.container_key):
            return web.json_response({"success": False}, status=401)
        body = await request.json()
        if body.get("state") == "stopped":
            await self._remove(inst, "stop", "instance" if key == inst.container_key else "user")
        return web.json_response({"success": True})

    # -- test fixtures and control --------------------------------------
    async def fixture_file(self, request):
        """Deterministic bytes: /files/<name>?size=N (supports Range)."""
        size = int(request.query.get("size", "4096"))
        name = request.match_info["name"]
        data = (name.encode() * (size // max(1, len(name)) + 1))[:size]
        rng = request.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            if start >= size:
                return web.Response(status=416)
            return web.Response(status=206, body=data[start:], headers={
                "Content-Range": f"bytes {start}-{size - 1}/{size}", "Content-Type": "application/octet-stream"})
        return web.Response(body=data, headers={"Content-Type": "application/octet-stream"})

    async def fixture_sparse(self, request):
        """Claims to be a file of any size without holding its bytes: /sparse/<name>?size=N."""
        size = int(request.query.get("size", "0"))
        return web.Response(status=206, body=b"x", headers={
            "Content-Range": f"bytes 0-0/{size}", "Content-Type": "application/octet-stream"})

    async def civitai_by_hash(self, request):
        """Stand-in for Civitai's lookup of a model version by file hash."""
        sha = request.match_info["sha"].lower()
        self.civitai_queries.append(sha)
        found = self.civitai.get(sha)
        if found is None:
            return web.json_response({"error": "Model not found"}, status=404)
        return web.json_response(found)

    async def fixture_redirect(self, request):
        raise web.HTTPFound(f"/files/{request.match_info['name']}?{request.query_string}")

    async def control_restart(self, request):
        inst = self.instances.get(int(request.match_info["instance_id"]))
        if inst is None:
            return web.Response(status=404)
        inst.restart()
        return web.json_response({"ok": True})

    async def control_state(self, request):
        return web.json_response({
            "instances": [{**i.describe(), "restarts": i.restarts, "edge_port": i.edge_port,
                           "comfy_port": i.comfy_port, "comfy_base": i.comfy_base,
                           "env_keys": sorted(i.env), "payload": {k: v for k, v in i.payload.items() if k != "env"},
                           "provisions": len(i.provisions)} for i in self.instances.values()],
            "destroyed": self.destroyed,
            "calls": self.calls,
            "last_query": self.queries[-1] if self.queries else None,
            "civitai_queries": self.civitai_queries,
        })

    async def control_config(self, request):
        body = await request.json()
        if "fail_offer_ids" in body:
            self.fail_offer_ids = {int(x) for x in body["fail_offer_ids"]}
        if "civitai" in body:
            self.civitai.update({k.lower(): v for k, v in body["civitai"].items()})
        return web.json_response({"ok": True})

    def build_app(self) -> web.Application:
        app = web.Application()
        r = app.router
        r.add_get("/api/v0/users/current/", self.whoami)
        r.add_post("/api/v0/bundles/", self.search)
        r.add_put("/api/v0/asks/{offer_id}/", self.create)
        r.add_get("/api/v0/instances/", self.list_instances_v0)
        r.add_get("/api/v1/instances/", self.list_instances)
        r.add_get("/api/v1/instances", self.list_instances)
        r.add_get("/api/v0/instances/{instance_id}/", self.show)
        r.add_delete("/api/v0/instances/{instance_id}/", self.destroy)
        r.add_put("/api/v0/instances/{instance_id}/", self.manage)
        r.add_get("/files/{name}", self.fixture_file)
        r.add_get("/redirect/{name}", self.fixture_redirect)
        r.add_get("/sparse/{name}", self.fixture_sparse)
        r.add_get("/api/v1/model-versions/by-hash/{sha}", self.civitai_by_hash)
        r.add_post("/_sim/restart/{instance_id}", self.control_restart)
        r.add_get("/_sim/state", self.control_state)
        r.add_post("/_sim/config", self.control_config)

        async def on_startup(app):
            self.session = aiohttp.ClientSession(auto_decompress=False)

        async def on_cleanup(app):
            for inst in list(self.instances.values()):
                inst.kill()
                await inst.stop_servers()
            if self.session:
                await self.session.close()

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18400)
    parser.add_argument("--comfy-dir", default="~/ComfyUI")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args()
    sim = Sim(args.port, args.comfy_dir, args.python, args.work_dir)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    web.run_app(sim.build_app(), host="127.0.0.1", port=args.port, print=lambda *_: None)


if __name__ == "__main__":
    main()
