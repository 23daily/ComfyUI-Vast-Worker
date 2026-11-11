"""End-to-end suite: a real local ComfyUI with the plugin, against the Vast
simulator, which starts a second real ComfyUI as the "rented instance".

Nothing is rented and no model is needed; everything runs on this machine.

    ~/ComfyUI/.venv/bin/python tests/e2e/run_e2e.py --comfy-dir ~/ComfyUI

Takes several minutes: it waits out the real one-minute idle and watchdog
timers. Use --quick to skip those two scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
PROXY_ID = "vast_worker_proxy"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Suite:
    def __init__(self, args):
        self.args = args
        self.comfy_dir = os.path.abspath(os.path.expanduser(args.comfy_dir))
        venv_python = os.path.join(self.comfy_dir, ".venv", "bin", "python")
        self.python = args.python or (venv_python if os.path.exists(venv_python) else sys.executable)
        self.work = tempfile.mkdtemp(prefix="vw-e2e-")
        self.local_base = os.path.join(self.work, "local")
        self.sim_port = free_port()
        self.local_port = free_port()
        self.sim_url = f"http://127.0.0.1:{self.sim_port}"
        self.local_url = f"http://127.0.0.1:{self.local_port}"
        self.sim_proc: subprocess.Popen | None = None
        self.local_proc: subprocess.Popen | None = None
        self.session: aiohttp.ClientSession | None = None
        self.results: list[tuple[str, bool, str]] = []
        self.events: list[dict] = []
        self.client_id = "e2e-" + uuid.uuid4().hex[:8]
        self._ws_task: asyncio.Task | None = None

    # -- bookkeeping ----------------------------------------------------
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""), flush=True)
        return bool(ok)

    # -- environment ------------------------------------------------------
    def prepare_dirs(self) -> None:
        custom = os.path.join(self.local_base, "custom_nodes")
        for sub in ("custom_nodes", "input", "output", "temp", "user", "models/loras"):
            os.makedirs(os.path.join(self.local_base, sub), exist_ok=True)
        os.symlink(ROOT, os.path.join(custom, os.path.basename(ROOT)))
        shutil.copy(os.path.join(FIXTURES, "vw_test_nodes.py"), custom)

        # A node pack that is a git clone, so the instance installs it by cloning.
        origin = os.path.join(self.work, "origin", "vw_git_pack.git")
        seed = os.path.join(self.work, "seed")
        shutil.copytree(os.path.join(FIXTURES, "vw_git_pack"), seed)
        git_env = dict(os.environ, GIT_AUTHOR_NAME="e2e", GIT_AUTHOR_EMAIL="e2e@example.com",
                       GIT_COMMITTER_NAME="e2e", GIT_COMMITTER_EMAIL="e2e@example.com")

        def git(*cmd, cwd=None):
            subprocess.run(["git", *cmd], cwd=cwd, env=git_env, check=True, capture_output=True)

        git("init", "-q", "-b", "main", seed)
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "fixture", cwd=seed)
        os.makedirs(os.path.dirname(origin))
        git("clone", "-q", "--bare", seed, origin)
        git("clone", "-q", "file://" + origin, os.path.join(custom, "vw_git_pack"))

    def start_sim(self) -> None:
        log = open(os.path.join(self.work, "sim.log"), "ab")
        self.sim_proc = subprocess.Popen(
            [self.python, os.path.join(ROOT, "tests", "sim", "vast_sim.py"), "--port", str(self.sim_port),
             "--comfy-dir", self.comfy_dir, "--python", self.python, "--work-dir", os.path.join(self.work, "sim")],
            stdout=log, stderr=subprocess.STDOUT)

    def start_local(self) -> None:
        env = dict(os.environ)
        env.update({
            "VAST_WORKER_API_BASE": self.sim_url,
            "VAST_API_KEY": "sim-key",
            "VAST_WORKER_POLL_SECONDS": "0.5",
            # The playground keeps the real 20 s tick so the sidebar behaves as it does in use.
            "VAST_WORKER_TICK_SECONDS": "20" if self.args.serve else "1",
            "VAST_WORKER_ALLOW_FILE_GIT": "1",
            "VAST_WORKER_CIVITAI_BASE": self.sim_url,
        })
        env.pop("VAST_WORKER_SETTINGS_JSON", None)
        log = open(os.path.join(self.work, "local.log"), "ab")
        self.local_proc = subprocess.Popen(
            [self.python, os.path.join(self.comfy_dir, "main.py"), "--base-directory", self.local_base,
             "--port", str(self.local_port), "--listen", "127.0.0.1", "--cpu", "--disable-auto-launch"],
            cwd=self.comfy_dir, env=env, stdout=log, stderr=subprocess.STDOUT)

    def stop_local(self, hard: bool = False) -> None:
        if self.local_proc and self.local_proc.poll() is None:
            if hard:
                self.local_proc.kill()
            else:
                self.local_proc.terminate()
            try:
                self.local_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.local_proc.kill()
        self.local_proc = None

    async def wait_http(self, url: str, timeout: float = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status < 500:
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(f"timed out waiting for {url}")

    # -- HTTP helpers -----------------------------------------------------
    async def local(self, method: str, path: str, body=None):
        async with self.session.request(method, self.local_url + path, json=body) as resp:
            text = await resp.text()
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text

    async def sim(self, method: str, path: str, body=None):
        async with self.session.request(method, self.sim_url + path, json=body) as resp:
            return await resp.json()

    async def status(self) -> dict:
        return (await self.local("GET", "/vast_worker/status"))[1]

    async def queue(self, prompt: dict, workflow: dict | None = None) -> str:
        body = {"prompt": prompt, "client_id": self.client_id}
        if workflow is not None:
            body["extra_data"] = {"extra_pnginfo": {"workflow": workflow}}
        code, data = await self.local("POST", "/prompt", body)
        if code != 200:
            raise RuntimeError(f"queue failed: {code} {data}")
        return data["prompt_id"]

    async def history(self, prompt_id: str, timeout: float = 240) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, data = await self.local("GET", f"/history/{prompt_id}")
            if isinstance(data, dict) and prompt_id in data:
                return data[prompt_id]
            await asyncio.sleep(0.4)
        raise RuntimeError(f"prompt {prompt_id} did not finish in {timeout}s")

    async def run(self, prompt: dict, workflow: dict | None = None, timeout: float = 240) -> dict:
        return await self.history(await self.queue(prompt, workflow), timeout)

    async def wait_for(self, predicate, timeout: float, interval: float = 0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(interval)
        return None

    async def listen(self) -> None:
        """Collect websocket events from the local ComfyUI, like a browser tab."""
        while True:
            try:
                async with self.session.ws_connect(
                        f"{self.local_url.replace('http', 'ws')}/ws?clientId={self.client_id}") as ws:
                    await ws.send_json({"type": "feature_flags", "data": {"supports_preview_metadata": True}})
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.events.append(json.loads(msg.data))
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self.events.append({"type": "__binary__", "size": len(msg.data)})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.5)

    def events_of(self, kind: str, prompt_id: str | None = None) -> list[dict]:
        out = []
        for event in self.events:
            if event.get("type") != kind:
                continue
            data = event.get("data") or {}
            if prompt_id and isinstance(data, dict) and data.get("prompt_id") not in (None, prompt_id):
                continue
            out.append(data)
        return out

    @staticmethod
    def outcome(entry: dict) -> str:
        status = entry.get("status") or {}
        kinds = [m[0] for m in status.get("messages", [])]
        if "execution_interrupted" in kinds:
            return "interrupted"
        return status.get("status_str", "?")

    @staticmethod
    def error_text(entry: dict) -> str:
        for kind, data in (entry.get("status") or {}).get("messages", []):
            if kind == "execution_error":
                return str(data.get("exception_message", ""))
        return ""

    # -- prompts ----------------------------------------------------------
    @staticmethod
    def p_image(color: int, prefix: str = "vw") -> dict:
        return {
            "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": color}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": prefix}},
        }

    # -- scenarios --------------------------------------------------------
    async def configure(self) -> None:
        code, data = await self.local("POST", "/vast_worker/settings", {
            "connection": {"scheme": "http"},
            "instance": {"image": "sim/comfy:test"},
            "lifecycle": {"confirm_rental": False, "idle_minutes": 1, "watchdog_minutes": 1,
                          "boot_timeout_minutes": 5},
        })
        self.check("settings saved", code == 200 and data["settings"]["connection"]["scheme"] == "http", str(data))
        st = await self.status()
        self.check("starts in local mode", st["mode"] == "local")
        self.check("API key comes from the environment and is not exposed",
                   st["secrets"]["vast_api_key"] == "env" and "sim-key" not in json.dumps(st))
        code, data = await self.local("POST", "/vast_worker/verify_key")
        self.check("API key verified against the Vast API", code == 200 and data.get("credit") == 12.34, str(data))
        code, data = await self.local("POST", "/vast_worker/offers")
        self.check("offer search returns priced offers", code == 200 and len(data["offers"]) >= 2
                   and data["offers"][0]["dph_total"] <= data["offers"][1]["dph_total"], str(data)[:200])

    async def s_search_conditions(self) -> None:
        print("\n[search conditions: price, GPU model, country]", flush=True)

        async def offers(patch: dict) -> list[dict]:
            await self.local("POST", "/vast_worker/settings", {"gpu": patch})
            code, data = await self.local("POST", "/vast_worker/offers")
            return data["offers"] if code == 200 else []

        base = {"countries": [], "exclude_countries": ["CN"], "gpu_names": [], "max_dph": 1.0}
        found = await offers(base)
        self.check("cards too old for the image are never offered (Tesla P40 is the cheapest)",
                   [o["gpu_name"] for o in found] == ["RTX 3090", "RTX 4090", "RTX 5090"], str([o["gpu_name"] for o in found]))
        found = await offers({**base, "countries": ["se"]})
        self.check("country allow-list keeps only that country",
                   [o["geolocation"] for o in found] == ["Sweden, SE"], str(found))
        found = await offers({**base, "exclude_countries": ["US"]})
        self.check("country block-list removes that country",
                   sorted(o["geolocation"] for o in found) == ["Quebec, CA", "Sweden, SE"], str(found))
        found = await offers({**base, "countries": ["US", "CA"], "exclude_countries": ["US"]})
        self.check("a blocked country is dropped from the allow-list",
                   [o["geolocation"] for o in found] == ["Quebec, CA"], str(found))
        found = await offers({**base, "gpu_names": ["RTX 5090", "RTX 4090"]})
        self.check("GPU model filter", sorted(o["gpu_name"] for o in found) == ["RTX 4090", "RTX 5090"], str(found))
        found = await offers({**base, "gpu_names": ["50"]})
        self.check("part of a GPU name is enough", [o["gpu_name"] for o in found] == ["RTX 5090"], str(found))
        found = await offers({**base, "gpu_names": ["rtx40", "3090"]})
        self.check("name matching ignores case and spaces",
                   [o["gpu_name"] for o in found] == ["RTX 3090", "RTX 4090"], str(found))
        found = await offers({**base, "gpu_names": ["P40"]})
        self.check("a name match cannot bring back a card that is too old", found == [], str(found))
        found = await offers({**base, "max_dph": 0.25})
        self.check("price limit", [o["gpu_name"] for o in found] == ["RTX 3090"], str(found))
        await offers(base)

        code, market = await self.local("GET", "/vast_worker/market")
        gpus = {g["name"]: g for g in market.get("gpus", [])} if code == 200 else {}
        countries = {c["code"]: c for c in market.get("countries", [])} if code == 200 else {}
        self.check("market lists GPU models with their lowest price",
                   gpus.get("RTX 3090", {}).get("min_dph") == 0.22 and gpus.get("RTX 3090", {}).get("vram_gb") == 24, str(gpus)[:300])
        self.check("market lists countries with offer counts",
                   countries.get("US", {}).get("offers") == 2 and set(countries) == {"US", "SE", "CA"}, str(countries))

    async def s_browse_and_rent(self) -> None:
        print("\n[machine browser: try filters without saving, rent the machine you pick]", flush=True)
        await self.local("POST", "/vast_worker/settings", {"lifecycle": {"confirm_rental": True}})
        code, data = await self.local("POST", "/vast_worker/offers", {"gpu": {"countries": ["SE"]}, "limit": 100})
        self.check("search with trial filters", code == 200 and [o["id"] for o in data["offers"]] == [9001]
                   and data["total"] == 1, str(data)[:200])
        self.check("trial filters are not saved", (await self.status())["settings"]["gpu"]["countries"] == [])
        code, data = await self.local("POST", "/vast_worker/offers", {"sort": "performance", "limit": 100})
        self.check("results carry the fields the browser shows",
                   code == 200 and len(data["offers"]) == 3 and all(k in data["offers"][0] for k in
                   ("gpu_name", "gpu_ram_gb", "dph_total", "geolocation", "inet_up", "inet_down", "reliability", "cuda_max_good")),
                   str(data)[:300])
        code, data = await self.local("POST", "/vast_worker/start", {"offer_id": 424242})
        self.check("an offer that is not in the results is refused", code == 400, f"{code} {data}")
        code, _ = await self.local("POST", "/vast_worker/start", {"offer_id": 9001})

        async def ready():
            st = await self.status()
            return st if st["phase"] == "ready" else None

        st = await self.wait_for(ready, 120)
        self.check("the picked machine is rented and made ready, with no second confirmation",
                   code == 200 and bool(st) and st["instance"]["gpu_name"] == "RTX 4090"
                   and st["instance"]["dph_total"] == 0.31 and not st["confirmations"], str(st and st.get("instance")))
        code, data = await self.local("POST", "/vast_worker/start", {"offer_id": 9001})
        self.check("a second machine is refused while one is running", code == 400, f"{code} {data}")
        state = await self.sim("GET", "/_sim/state")
        self.check("exactly one machine was rented", len(state["instances"]) == 1 and state["calls"].count("create") == 1)
        await self.local("POST", "/vast_worker/destroy")
        state = await self.sim("GET", "/_sim/state")
        self.check("and it is destroyed again", not state["instances"])
        await self.local("POST", "/vast_worker/settings", {"lifecycle": {"confirm_rental": False}})

    async def s_destroy_while_starting(self) -> None:
        print("\n[Destroy works while a machine is still starting]", flush=True)
        await self.local("POST", "/vast_worker/start", {})

        async def starting():
            st = await self.status()
            state = await self.sim("GET", "/_sim/state")
            return st if st["phase"] in ("booting", "installing") and state["instances"] else None

        st = await self.wait_for(starting, 30, 0.2)
        self.check("a machine is starting", bool(st), str(st and st["phase"]))
        began = time.time()
        code, _ = await self.local("POST", "/vast_worker/destroy")
        took = time.time() - began
        state = await self.sim("GET", "/_sim/state")
        self.check("Destroy answers within seconds instead of waiting for the boot", code == 200 and took < 8, f"{code} {took:.1f}s")
        self.check("the half-started machine is destroyed on Vast", not state["instances"], str(state["instances"]))
        await asyncio.sleep(4)
        st = await self.status()
        state = await self.sim("GET", "/_sim/state")
        self.check("and nothing comes back afterwards",
                   st["instance"] is None and st["phase"] == "none" and not state["instances"], f"{st['phase']} {st['instance']}")

    async def s_local_mode(self) -> None:
        print("\n[local mode is untouched]", flush=True)
        entry = await self.run(self.p_image(0x112233, "local"))
        self.check("local run succeeds", self.outcome(entry) == "success")
        self.check("local run keeps the original node ids", "2" in entry.get("outputs", {}))
        self.check("nothing was rented", not (await self.sim("GET", "/_sim/state"))["instances"])

    async def s_basic(self) -> None:
        print("\n[first Vast run: rent, boot, install agent, execute]", flush=True)
        code, _ = await self.local("POST", "/vast_worker/mode", {"mode": "vast"})
        self.check("switched to Vast mode", code == 200)
        prompt_id = await self.queue(self.p_image(0x336699))
        entry = await self.history(prompt_id, 300)
        self.check("Vast run succeeds", self.outcome(entry) == "success", self.error_text(entry))
        images = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("images") or []
        self.check("history lists the output image", len(images) == 1, str(entry.get("outputs")))
        if images:
            path = os.path.join(self.local_base, "output", images[0].get("subfolder", ""), images[0]["filename"])
            self.check("output file was downloaded to the local output folder",
                       os.path.isfile(path) and os.path.getsize(path) > 0, path)
        state = await self.sim("GET", "/_sim/state")
        self.check("exactly one instance rented", len(state["instances"]) == 1, str(state["instances"]))
        inst = state["instances"][0]
        self.first_instance = inst
        self.check("cheapest offer chosen", inst["gpu_name"] == "RTX 3090", inst["gpu_name"])
        self.check("create request carries image, launch mode and label",
                   inst["payload"].get("image") == "sim/comfy:test" and inst["payload"].get("runtype") == "ssh_direct"
                   and inst["payload"].get("onstart") == "entrypoint.sh"
                   and str(inst["payload"].get("label", "")).startswith("comfyui-vast-worker-"), str(inst["payload"]))
        self.check("instance got an access token and watchdog setting",
                   "WEB_PASSWORD" in inst["env_keys"] and "VAST_WORKER_WATCHDOG_MINUTES" in inst["env_keys"])
        self.check("agent installed through the portal exactly once", inst["provisions"] == 1, str(inst["provisions"]))
        agent_file = os.path.join(inst["comfy_base"], "custom_nodes", "vast_worker_agent.py")
        with open(os.path.join(ROOT, "agent", "vast_worker_agent.py"), "rb") as f:
            source = f.read()
        self.check("agent file on the instance matches the local copy",
                   os.path.isfile(agent_file) and open(agent_file, "rb").read() == source)
        self.check("remote node progress relayed to the browser",
                   any(d.get("node") in ("1", "2") for d in self.events_of("executing", prompt_id)),
                   str(self.events_of("executing", prompt_id))[:300])
        executed = [d for d in self.events_of("executed", prompt_id) if d.get("node") == "2"]
        self.check("remote node output relayed with a local file name",
                   bool(executed) and executed[0]["output"]["images"][0]["filename"] == images[0]["filename"]
                   if images else False)
        st = await self.status()
        self.check("status shows a ready instance with a price",
                   st["phase"] == "ready" and st["instance"]["dph_total"] == 0.22, str(st.get("instance")))
        async with self.session.get(f"http://127.0.0.1:{inst['edge_port']}/system_stats") as resp:
            self.check("instance rejects requests without the token", resp.status == 401, str(resp.status))

    async def s_collision(self) -> None:
        print("\n[output names never overwrite local files]", flush=True)
        out = os.path.join(self.local_base, "output")
        existing = os.path.join(out, "vw_00002_.png")
        with open(existing, "wb") as f:
            f.write(b"precious local file")
        entry = await self.run(self.p_image(0x993366))
        images = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("images") or []
        self.check("second Vast run succeeds", self.outcome(entry) == "success", self.error_text(entry))
        self.check("existing local file untouched", open(existing, "rb").read() == b"precious local file")
        self.check("new output saved under a free name",
                   bool(images) and images[0]["filename"] == "vw_00003_.png"
                   and os.path.isfile(os.path.join(out, "vw_00003_.png")), str(images))
        state = await self.sim("GET", "/_sim/state")
        self.check("instance reused, not rented again", len(state["instances"]) == 1 and state["calls"].count("create") == 3)

    async def s_input_upload(self) -> None:
        print("\n[local input files are uploaded]", flush=True)
        from PIL import Image

        Image.new("RGB", (32, 32), (10, 200, 30)).save(os.path.join(self.local_base, "input", "e2e_in.png"))
        prompt = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "e2e_in.png"}},
            "2": {"class_type": "ImageInvert", "inputs": {"image": ["1", 0]}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "inv"}},
        }
        entry = await self.run(prompt)
        self.check("run with a local input image succeeds", self.outcome(entry) == "success", self.error_text(entry))
        images = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("images") or []
        ok = False
        if images:
            pixel = Image.open(os.path.join(self.local_base, "output", images[0]["filename"])).convert("RGB").getpixel((5, 5))
            ok = abs(pixel[0] - 245) <= 2 and abs(pixel[1] - 55) <= 2 and abs(pixel[2] - 225) <= 2
        self.check("result is the inverted input image", ok, str(images))

    async def s_nodes_and_models(self) -> None:
        print("\n[custom node sync by upload + model download on the instance]", flush=True)
        url = f"{self.sim_url}/files/tiny_lora.safetensors?size=5000"
        code, data = await self.local("POST", "/vast_worker/models",
                                      {"url": url, "directory": "loras", "name": "tiny_lora.safetensors"})
        self.check("model added to the remote library", code == 200 and len(data["models"]) == 1, str(data))
        _, info = await self.local("GET", "/object_info/VWTestModelInfo")
        options = info["VWTestModelInfo"]["input"]["required"]["lora_name"][0]
        self.check("remote-only model appears in the local dropdown (Vast mode)", "tiny_lora.safetensors" in options, str(options))
        self.check("model file is not on this machine",
                   not os.path.exists(os.path.join(self.local_base, "models", "loras", "tiny_lora.safetensors")))
        before = (await self.sim("GET", "/_sim/state"))["instances"][0]["restarts"]
        entry = await self.run({"1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": "tiny_lora.safetensors"}}})
        self.check("run needing a custom node and a model succeeds", self.outcome(entry) == "success", self.error_text(entry))
        text = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")
        self.check("node ran on the instance against the downloaded model", text == ["tiny_lora.safetensors:5000"], str(text))
        inst = (await self.sim("GET", "/_sim/state"))["instances"][0]
        self.check("instance ComfyUI restarted once to load the node", inst["restarts"] == before + 1, f"{before}->{inst['restarts']}")
        self.check("single-file node installed on the instance",
                   os.path.isfile(os.path.join(inst["comfy_base"], "custom_nodes", "vw_test_nodes.py")))
        self.check("model downloaded into the instance's loras folder",
                   os.path.getsize(os.path.join(inst["comfy_base"], "models", "loras", "tiny_lora.safetensors")) == 5000)

        url2 = f"{self.sim_url}/redirect/redir.safetensors?size=3000"
        await self.local("POST", "/vast_worker/models", {"url": url2, "directory": "loras", "name": "sub/redir.safetensors"})
        entry = await self.run({"1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": "sub/redir.safetensors"}}})
        text = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")
        self.check("download follows redirects and honours subfolders", text == ["sub/redir.safetensors:3000"],
                   f"{text} {self.error_text(entry)}")
        after = (await self.sim("GET", "/_sim/state"))["instances"][0]["restarts"]
        self.check("no restart when nothing new has to be installed", after == inst["restarts"])

    async def s_git_pack(self) -> None:
        print("\n[custom node sync by git clone]", flush=True)
        entry = await self.run({"1": {"class_type": "VWGitEcho", "inputs": {"text": "hi"}}})
        self.check("run needing a git-based node pack succeeds", self.outcome(entry) == "success", self.error_text(entry))
        text = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")
        self.check("node pack code ran on the instance", text == ["git-pack says: HI"], str(text))
        inst = (await self.sim("GET", "/_sim/state"))["instances"][0]
        self.check("node pack was cloned (has .git) on the instance",
                   os.path.isdir(os.path.join(inst["comfy_base"], "custom_nodes", "vw_git_pack", ".git")))

    async def s_workflow_hint(self) -> None:
        print("\n[model links embedded in the workflow]", flush=True)
        workflow = {"nodes": [{"id": 1, "type": "VWTestModelInfo", "properties": {"models": [
            {"name": "hinted.safetensors", "url": f"{self.sim_url}/files/hinted.safetensors?size=1234", "directory": "loras"}]}}]}
        entry = await self.run({"1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": "hinted.safetensors"}}}, workflow)
        text = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")
        self.check("model downloaded from the link inside the workflow", text == ["hinted.safetensors:1234"],
                   f"{text} {self.error_text(entry)}")
        _, data = await self.local("GET", "/vast_worker/models")
        self.check("that link is remembered in the library",
                   any(m["name"] == "hinted.safetensors" and m["source"] == "workflow" for m in data["models"]))

    async def s_local_files(self) -> None:
        print("\n[local model files with no download link are uploaded]", flush=True)
        loras = os.path.join(self.local_base, "models", "loras")
        inst = (await self.sim("GET", "/_sim/state"))["instances"][0]
        remote_loras = os.path.join(inst["comfy_base"], "models", "loras")

        def make(name: str, size: int) -> None:
            path = os.path.join(loras, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(os.urandom(min(size, 4096)))
                f.truncate(size)

        def prompt(name: str) -> dict:
            return {"1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": name}}}

        async def pending():
            confirmations = (await self.status())["confirmations"]
            return confirmations[0] if confirmations else None

        def text(entry: dict):
            return (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")

        make("mine/trained.safetensors", 3000)
        entry = await self.run(prompt("mine/trained.safetensors"))
        self.check("a local-only LoRA is uploaded without asking", text(entry) == ["mine/trained.safetensors:3000"],
                   f"{text(entry)} {self.error_text(entry)}")
        uploaded = os.path.join(remote_loras, "mine", "trained.safetensors")
        self.check("uploaded file is byte-identical on the instance",
                   os.path.isfile(uploaded)
                   and open(uploaded, "rb").read() == open(os.path.join(loras, "mine", "trained.safetensors"), "rb").read())
        _, data = await self.local("GET", "/vast_worker/models")
        self.check("uploaded files are not added to the link library",
                   not any(m["name"] == "mine/trained.safetensors" for m in data["models"]))

        big = 120 * 1024 * 1024  # above the 100 MB limit of ComfyUI's own upload route
        make("big_local.safetensors", big)
        entry = await self.run(prompt("big_local.safetensors"))
        self.check("a 120 MB file uploads (streamed, past ComfyUI's 100 MB upload limit)",
                   text(entry) == [f"big_local.safetensors:{big}"], f"{text(entry)} {self.error_text(entry)}")
        self.check("no partial file is left behind", not os.path.exists(os.path.join(remote_loras, "big_local.safetensors.part")))

        await self.local("POST", "/vast_worker/settings", {"upload": {"local_models": "auto", "confirm_over_mb": 1}})
        make("over_limit.safetensors", 2 * 1024 * 1024)
        prompt_id = await self.queue(prompt("over_limit.safetensors"))
        confirmation = await self.wait_for(pending, 60)
        self.check("a file over the size limit asks first, offering the local copy",
                   bool(confirmation) and confirmation["kind"] == "models"
                   and confirmation["models"][0]["local_size"] == 2 * 1024 * 1024, str(confirmation))
        if confirmation:
            await self.local("POST", "/vast_worker/confirm", {"id": confirmation["id"], "approved": True, "models": [
                {"name": "over_limit.safetensors", "directory": "loras", "upload": True}]})
        entry = await self.history(prompt_id, 120)
        self.check("approving uploads it", text(entry) == [f"over_limit.safetensors:{2 * 1024 * 1024}"],
                   f"{text(entry)} {self.error_text(entry)}")

        await self.local("POST", "/vast_worker/settings", {"upload": {"local_models": "ask", "confirm_over_mb": 2000}})
        make("ask_me.safetensors", 1500)
        prompt_id = await self.queue(prompt("ask_me.safetensors"))
        confirmation = await self.wait_for(pending, 60)
        self.check("'ask' mode asks even for small files",
                   bool(confirmation) and confirmation["models"][0]["local_size"] == 1500, str(confirmation))
        if confirmation:
            await self.local("POST", "/vast_worker/confirm", {"id": confirmation["id"], "approved": True, "models": [
                {"name": "ask_me.safetensors", "directory": "loras",
                 "url": f"{self.sim_url}/files/ask_me.safetensors?size=999"}]})
        entry = await self.history(prompt_id, 120)
        self.check("a link given in the dialog wins over the local copy", text(entry) == ["ask_me.safetensors:999"],
                   f"{text(entry)} {self.error_text(entry)}")

        await self.local("POST", "/vast_worker/settings", {"upload": {"local_models": "off"}})
        make("never.safetensors", 1200)
        prompt_id = await self.queue(prompt("never.safetensors"))
        confirmation = await self.wait_for(pending, 60)
        self.check("'off' mode never offers the local copy",
                   bool(confirmation) and confirmation["models"][0]["local_size"] is None, str(confirmation))
        if confirmation:
            await self.local("POST", "/vast_worker/confirm", {"id": confirmation["id"], "approved": False})
        entry = await self.history(prompt_id, 60)
        self.check("and nothing was uploaded", self.outcome(entry) == "interrupted"
                   and not os.path.exists(os.path.join(remote_loras, "never.safetensors")))
        await self.local("POST", "/vast_worker/settings", {"upload": {"local_models": "auto", "confirm_over_mb": 2000}})

        from PIL import Image

        os.makedirs(os.path.join(self.local_base, "input", "refs"), exist_ok=True)
        Image.new("RGB", (16, 16), (250, 10, 10)).save(os.path.join(self.local_base, "input", "refs", "ref.png"))
        entry = await self.run({
            "1": {"class_type": "LoadImage", "inputs": {"image": "refs/ref.png"}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "ref"}},
        })
        self.check("a reference image in an input subfolder is uploaded",
                   self.outcome(entry) == "success"
                   and os.path.isfile(os.path.join(inst["comfy_base"], "input", "refs", "ref.png")), self.error_text(entry))

    async def s_download_before_upload(self) -> None:
        print("\n[a local model is downloaded when Civitai has it; upload is the fallback]", flush=True)
        import hashlib

        loras = os.path.join(self.local_base, "models", "loras")
        inst = (await self.sim("GET", "/_sim/state"))["instances"][0]
        remote_loras = os.path.join(inst["comfy_base"], "models", "loras")

        def make(name: str, size: int) -> str:
            data = os.urandom(size)
            with open(os.path.join(loras, name), "wb") as f:
                f.write(data)
            return hashlib.sha256(data).hexdigest()

        def prompt(name: str) -> dict:
            return {"1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": name}}}

        def text(entry: dict):
            return (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")

        def listing(sha: str, name: str, size: int) -> dict:
            # The matching file is listed second, with its hash in upper case, like the real API.
            return {sha: {"id": 77, "name": "v1.0", "model": {"name": "Test LoRA"},
                          "downloadUrl": f"{self.sim_url}/files/wrong_file.bin?size=11",
                          "files": [{"hashes": {"SHA256": "0" * 64}, "downloadUrl": f"{self.sim_url}/files/other.bin?size=12"},
                                    {"hashes": {"SHA256": sha.upper()}, "downloadUrl": f"{self.sim_url}/files/{name}?size={size}"}]}}

        sha = make("on_civitai.safetensors", 3000)
        await self.sim("POST", "/_sim/config", {"civitai": listing(sha, "on_civitai.safetensors", 4321)})
        entry = await self.run(prompt("on_civitai.safetensors"))
        self.check("a local model that Civitai recognises is downloaded on the instance, not uploaded",
                   text(entry) == ["on_civitai.safetensors:4321"], f"{text(entry)} {self.error_text(entry)}")
        state = await self.sim("GET", "/_sim/state")
        self.check("only the file's hash was sent to Civitai", sha in state["civitai_queries"])
        _, data = await self.local("GET", "/vast_worker/models")
        self.check("the link found on Civitai is remembered in the library",
                   any(m["name"] == "on_civitai.safetensors" and m["source"] == "civitai" for m in data["models"]))

        make("broken_link.safetensors", 2500)
        await self.local("POST", "/vast_worker/models", {
            "url": f"{self.sim_url}/no-such-file", "directory": "loras", "name": "broken_link.safetensors"})
        entry = await self.run(prompt("broken_link.safetensors"))
        self.check("when the download fails, the local copy is uploaded instead",
                   text(entry) == ["broken_link.safetensors:2500"], f"{text(entry)} {self.error_text(entry)}")
        events = " | ".join(e["message"] for e in (await self.status())["events"])
        self.check("and the log says why", "uploading your local copy instead" in events, events[-300:])

        await self.local("POST", "/vast_worker/models", {
            "url": f"{self.sim_url}/no-such-file", "directory": "loras", "name": "only_remote.safetensors"})
        entry = await self.run(prompt("only_remote.safetensors"))
        self.check("a failed download with no local copy is an error that names the model",
                   self.outcome(entry) == "error" and "only_remote.safetensors" in self.error_text(entry), self.error_text(entry)[:300])

        await self.local("POST", "/vast_worker/settings", {"upload": {"civitai_lookup": False}})
        sha = make("lookup_off.safetensors", 2000)
        await self.sim("POST", "/_sim/config", {"civitai": listing(sha, "lookup_off.safetensors", 999)})
        entry = await self.run(prompt("lookup_off.safetensors"))
        state = await self.sim("GET", "/_sim/state")
        self.check("with the lookup switched off the file is uploaded and Civitai is not asked",
                   text(entry) == ["lookup_off.safetensors:2000"] and sha not in state["civitai_queries"],
                   f"{text(entry)} {self.error_text(entry)}")
        await self.local("POST", "/vast_worker/settings", {"upload": {"civitai_lookup": True}})
        self.check("no stray partial files on the instance",
                   not [f for f in os.listdir(remote_loras) if f.endswith(".part")])

    async def s_disk_space(self) -> None:
        print("\n[a model that cannot fit is refused before anything is downloaded]", flush=True)
        inst = (await self.sim("GET", "/_sim/state"))["instances"][0]
        remote_loras = os.path.join(inst["comfy_base"], "models", "loras")
        huge = 10 ** 15  # one petabyte: larger than any disk this test runs on
        await self.local("POST", "/vast_worker/models", {
            "url": f"{self.sim_url}/sparse/huge.safetensors?size={huge}", "directory": "loras", "name": "huge.safetensors"})
        await self.local("POST", "/vast_worker/models", {
            "url": f"{self.sim_url}/files/small_next_to_huge.safetensors?size=2000", "directory": "loras",
            "name": "small_next_to_huge.safetensors"})
        began = time.time()
        entry = await self.run({
            "1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": "huge.safetensors"}},
            "2": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": "small_next_to_huge.safetensors"}},
        })
        text = self.error_text(entry)
        self.check("the job fails with a disk-space message that says what to do",
                   self.outcome(entry) == "error" and "Not enough disk space" in text and "Machine setup" in text, text[:300])
        self.check("it fails fast, without downloading", time.time() - began < 20, f"{time.time() - began:.1f}s")
        leftovers = [f for f in os.listdir(remote_loras) if f.startswith(("huge", "small_next_to_huge"))]
        self.check("nothing from that batch was written to the instance", leftovers == [], str(leftovers))
        await self.local("POST", "/vast_worker/models/remove", {"directory": "loras", "name": "huge.safetensors"})

        async def disk_known():
            st = await self.status()
            return st if (st.get("instance") or {}).get("disk_free_bytes") else None

        st = await self.wait_for(disk_known, 15)
        self.check("the sidebar status reports the instance's free disk space",
                   bool(st) and st["instance"]["disk_free_bytes"] > 0 and st["instance"]["disk_gb"] == st["settings"]["instance"]["disk_gb"],
                   str(st and st["instance"]))

    async def s_missing_link(self) -> None:
        print("\n[asks for a download link when none is known]", flush=True)
        prompt = {"1": {"class_type": "VWTestModelInfo", "inputs": {"lora_name": "mystery.safetensors"}}}

        async def pending():
            confirmations = (await self.status())["confirmations"]
            return confirmations[0] if confirmations else None

        prompt_id = await self.queue(prompt)
        confirmation = await self.wait_for(pending, 60)
        self.check("a models confirmation is raised",
                   bool(confirmation) and confirmation["kind"] == "models"
                   and confirmation["models"][0]["name"] == "mystery.safetensors"
                   and confirmation["models"][0]["directory"] == "loras", str(confirmation))
        await self.local("POST", "/vast_worker/confirm", {"id": confirmation["id"], "approved": False})
        entry = await self.history(prompt_id, 60)
        self.check("declining ends the job as interrupted, not as an error", self.outcome(entry) == "interrupted", str(entry.get("status")))

        prompt_id = await self.queue(prompt)
        confirmation = await self.wait_for(pending, 60)
        await self.local("POST", "/vast_worker/confirm", {"id": confirmation["id"], "approved": True, "models": [
            {"name": "mystery.safetensors", "directory": "loras", "url": f"{self.sim_url}/files/mystery.safetensors?size=777"}]})
        entry = await self.history(prompt_id, 120)
        text = (entry.get("outputs", {}).get(PROXY_ID) or {}).get("text")
        self.check("supplying a link lets the job finish", text == ["mystery.safetensors:777"], f"{text} {self.error_text(entry)}")

    async def s_remote_error(self) -> None:
        print("\n[errors on the instance are reported locally]", flush=True)
        prompt = {
            "1": {"class_type": "EmptyImage", "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 1}},
            "2": {"class_type": "VWTestFail", "inputs": {"image": ["1", 0]}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "fail"}},
        }
        entry = await self.run(prompt)
        text = self.error_text(entry)
        self.check("job is marked failed", self.outcome(entry) == "error", str(entry.get("status"))[:200])
        self.check("error names the failing remote node and message",
                   "VWTestFail" in text and "boom from the remote side" in text, text[:300])

        entry = await self.run({"1": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "x"}}})
        text = self.error_text(entry)
        self.check("remote validation errors are reported", self.outcome(entry) == "error" and "validation" in text.lower(), text[:300])

    async def s_interrupt(self) -> None:
        print("\n[cancelling a job stops it on the instance]", flush=True)
        prompt = {
            "1": {"class_type": "EmptyImage", "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 2}},
            "2": {"class_type": "VWTestSleep", "inputs": {"image": ["1", 0], "seconds": 60.0}},
            "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "sleep"}},
        }
        prompt_id = await self.queue(prompt)

        async def sleeping():
            return any(d.get("node") == "2" for d in self.events_of("executing", prompt_id))

        self.check("remote node reported as executing", bool(await self.wait_for(sleeping, 60)))
        progressed = await self.wait_for(
            lambda: asyncio.sleep(0, any("2" in (d.get("nodes") or {}) for d in self.events_of("progress_state", prompt_id))), 20)
        self.check("remote sampler-style progress relayed", bool(progressed))
        started = time.time()
        await self.local("POST", "/interrupt", {})
        entry = await self.history(prompt_id, 30)
        self.check("job ends as interrupted within seconds",
                   self.outcome(entry) == "interrupted" and time.time() - started < 15, f"{self.outcome(entry)} {time.time() - started:.1f}s")
        inst = (await self.sim("GET", "/_sim/state"))["instances"][0]

        async def remote_idle():
            async with self.session.get(f"http://127.0.0.1:{inst['comfy_port']}/queue") as resp:
                data = await resp.json()
                return not data["queue_running"] and not data["queue_pending"]

        self.check("instance stopped working on it", bool(await self.wait_for(remote_idle, 20)))

    async def s_dropdown_local(self) -> None:
        print("\n[library entries are hidden from dropdowns in local mode]", flush=True)
        await self.local("POST", "/vast_worker/mode", {"mode": "local"})
        _, info = await self.local("GET", "/object_info/VWTestModelInfo")
        options = info["VWTestModelInfo"]["input"]["required"]["lora_name"][0]
        self.check("remote-only model not offered in local mode", "tiny_lora.safetensors" not in options, str(options))
        await self.local("POST", "/vast_worker/mode", {"mode": "vast"})

    async def s_confirm_and_retry(self) -> None:
        print("\n[rental confirmation, and falling back when a machine fails to start]", flush=True)
        code, _ = await self.local("POST", "/vast_worker/destroy")
        self.check("manual destroy works", code == 200)
        state = await self.sim("GET", "/_sim/state")
        self.check("instance is gone on the Vast side", not state["instances"] and state["destroyed"][-1]["by"] == "user")
        await self.local("POST", "/vast_worker/settings", {"lifecycle": {"confirm_rental": True}})
        await self.sim("POST", "/_sim/config", {"fail_offer_ids": [9002]})

        prompt_id = await self.queue(self.p_image(0x224466, "retry"))

        async def pending():
            confirmations = (await self.status())["confirmations"]
            return confirmations[0] if confirmations else None

        confirmation = await self.wait_for(pending, 60)
        self.check("a rental confirmation lists offers cheapest first",
                   bool(confirmation) and confirmation["kind"] == "rent" and confirmation["offers"][0]["id"] == 9002
                   and confirmation["image"] == "sim/comfy:test", str(confirmation)[:300])
        self.check("nothing is rented before the user confirms",
                   (await self.sim("GET", "/_sim/state"))["calls"].count("create") == 3)
        await self.local("POST", "/vast_worker/confirm", {"id": confirmation["id"], "approved": True, "offer_id": 9002})
        entry = await self.history(prompt_id, 300)
        self.check("job succeeds on the next offer", self.outcome(entry) == "success", self.error_text(entry))
        state = await self.sim("GET", "/_sim/state")
        self.check("the machine that failed to start was destroyed", any(d["id"] == 1004 for d in state["destroyed"]), str(state["destroyed"]))
        self.check("running on the fallback machine", len(state["instances"]) == 1 and state["instances"][0]["gpu_name"] == "RTX 4090",
                   str([i["gpu_name"] for i in state["instances"]]))
        await self.sim("POST", "/_sim/config", {"fail_offer_ids": []})
        await self.local("POST", "/vast_worker/settings", {"lifecycle": {"confirm_rental": False}})

    async def s_leftovers(self) -> None:
        print("\n[instances left behind by a lost session are found]", flush=True)
        await self.sim("POST", "/_sim/config", {"fail_offer_ids": [9003]})  # these never start a ComfyUI
        auth = {"Authorization": "Bearer sim-key"}

        async def rent(label):
            async with self.session.put(f"{self.sim_url}/api/v0/asks/9003/", headers=auth,
                                        json={"image": "x", "label": label, "env": {}}) as resp:
                return (await resp.json())["new_contract"]

        lost = await rent("comfyui-vast-worker-lost1234")
        unrelated = await rent("someone-elses-machine")
        tracked = (await self.status())["instance"]["id"]
        await self.local("POST", "/vast_worker/verify_key")  # also rescans the account

        async def found():
            others = (await self.status())["others"]
            return others or None

        others = await self.wait_for(found, 20) or []
        self.check("only this plugin's untracked instance is reported",
                   [o["id"] for o in others] == [lost], f"{others} tracked={tracked}")
        code, _ = await self.local("POST", "/vast_worker/destroy_other", {"id": unrelated})
        self.check("refuses to destroy an instance it did not rent", code == 400, str(code))
        code, _ = await self.local("POST", "/vast_worker/destroy_other", {"id": lost})
        ids = [i["id"] for i in (await self.sim("GET", "/_sim/state"))["instances"]]
        self.check("leftover destroyed, other machines untouched",
                   code == 200 and lost not in ids and unrelated in ids and tracked in ids, f"{code} {ids}")
        async with self.session.delete(f"{self.sim_url}/api/v0/instances/{unrelated}/", headers=auth):
            pass
        await self.sim("POST", "/_sim/config", {"fail_offer_ids": []})

    async def s_adopt(self) -> None:
        print("\n[restarting local ComfyUI keeps track of the rented instance]", flush=True)
        before = await self.sim("GET", "/_sim/state")
        instance_id = before["instances"][0]["id"]
        self._ws_task.cancel()
        self.stop_local()
        self.start_local()
        await self.wait_http(self.local_url + "/system_stats")
        self._ws_task = asyncio.ensure_future(self.listen())

        async def ready():
            st = await self.status()
            return st if st["phase"] == "ready" else None

        st = await self.wait_for(ready, 90)
        self.check("instance re-adopted after restart", bool(st) and st["instance"]["id"] == instance_id, str(st and st.get("instance")))
        self.check("mode falls back to local after a restart", bool(st) and st["mode"] == "local")
        await self.local("POST", "/vast_worker/mode", {"mode": "vast"})
        entry = await self.run(self.p_image(0x556677, "adopt"))
        self.check("job runs on the adopted instance", self.outcome(entry) == "success", self.error_text(entry))
        after = await self.sim("GET", "/_sim/state")
        self.check("no second rental", after["calls"].count("create") == before["calls"].count("create"))

    async def s_idle(self) -> None:
        print("\n[idle instances are destroyed] (waits about a minute)", flush=True)
        async def gone():
            return not (await self.sim("GET", "/_sim/state"))["instances"]

        self.check("instance destroyed after the idle limit", bool(await self.wait_for(gone, 110, 2)))
        state = await self.sim("GET", "/_sim/state")
        self.check("it was destroyed by the controller", state["destroyed"][-1]["by"] == "user", str(state["destroyed"][-1]))
        st = await self.status()
        self.check("status shows no instance", st["instance"] is None and st["phase"] == "none", f"{st['phase']} {st['instance']}")

    async def s_watchdog(self) -> None:
        print("\n[instance destroys itself when the controller disappears] (waits over a minute)", flush=True)
        entry = await self.run(self.p_image(0x778899, "wd"), timeout=300)
        self.check("fresh instance rented for the watchdog test", self.outcome(entry) == "success", self.error_text(entry))
        state = await self.sim("GET", "/_sim/state")
        instance_id = state["instances"][0]["id"]
        self._ws_task.cancel()
        self.stop_local(hard=True)

        async def gone():
            return not (await self.sim("GET", "/_sim/state"))["instances"]

        self.check("instance removed itself after losing the controller", bool(await self.wait_for(gone, 130, 2)))
        state = await self.sim("GET", "/_sim/state")
        last = state["destroyed"][-1]
        self.check("self-destroy used the instance's own key", last["id"] == instance_id and last["by"] == "instance", str(last))

    # -- demo mode --------------------------------------------------------
    async def serve(self) -> int:
        """Start the simulator and a local ComfyUI, then wait: a playground
        for trying the plugin in a browser without a Vast.ai account."""
        self.prepare_dirs()
        self.session = aiohttp.ClientSession()
        try:
            self.start_sim()
            await self.wait_http(self.sim_url + "/_sim/state", 30)
            self.start_local()
            await self.wait_http(self.local_url + "/system_stats")
            await self.local("POST", "/vast_worker/settings", {
                "connection": {"scheme": "http"}, "instance": {"image": "sim/comfy:test"},
                "lifecycle": {"idle_minutes": 5, "watchdog_minutes": 5}})
            print(f"ComfyUI with the plugin: {self.local_url}", flush=True)
            print(f"Vast simulator:          {self.sim_url}/_sim/state", flush=True)
            print(f"Example model link:      {self.sim_url}/files/demo.safetensors?size=20000000", flush=True)
            print("Press Ctrl-C to stop.", flush=True)
            while self.local_proc and self.local_proc.poll() is None:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.session.close()
            self.stop_local()
            if self.sim_proc and self.sim_proc.poll() is None:
                self.sim_proc.send_signal(signal.SIGTERM)
            shutil.rmtree(self.work, ignore_errors=True)
        return 0

    # -- driver -----------------------------------------------------------
    async def main(self) -> int:
        if self.args.serve:
            return await self.serve()
        print(f"work dir: {self.work}", flush=True)
        self.prepare_dirs()
        self.session = aiohttp.ClientSession()
        try:
            self.start_sim()
            await self.wait_http(self.sim_url + "/_sim/state", 30)
            self.start_local()
            await self.wait_http(self.local_url + "/system_stats")
            self._ws_task = asyncio.ensure_future(self.listen())
            await asyncio.sleep(1.5)

            scenarios = [self.configure, self.s_search_conditions, self.s_browse_and_rent, self.s_destroy_while_starting,
                         self.s_local_mode, self.s_basic, self.s_collision,
                         self.s_input_upload, self.s_nodes_and_models, self.s_git_pack, self.s_workflow_hint,
                         self.s_local_files, self.s_download_before_upload, self.s_disk_space, self.s_missing_link,
                         self.s_remote_error, self.s_interrupt, self.s_dropdown_local, self.s_confirm_and_retry,
                         self.s_leftovers, self.s_adopt]
            if not self.args.quick:
                scenarios += [self.s_idle, self.s_watchdog]
            if self.args.only:
                wanted = set(self.args.only.split(","))
                scenarios = [s for s in scenarios if s.__name__ in wanted or s.__name__ in ("configure", "s_basic")]
            for scenario in scenarios:
                try:
                    await scenario()
                except Exception as exc:
                    self.check(f"{scenario.__name__} raised {type(exc).__name__}", False, str(exc)[:300])
                    traceback.print_exc()
        finally:
            if self._ws_task:
                self._ws_task.cancel()
            await self.session.close()
            self.stop_local()
            if self.sim_proc and self.sim_proc.poll() is None:
                self.sim_proc.send_signal(signal.SIGTERM)
                try:
                    self.sim_proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.sim_proc.kill()

        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)} passed, {len(failed)} failed", flush=True)
        for name, _, detail in failed:
            print(f"  FAILED: {name}  {detail}", flush=True)
        if failed or self.args.keep:
            print(f"logs kept in {self.work}", flush=True)
        else:
            shutil.rmtree(self.work, ignore_errors=True)
        return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--comfy-dir", default="~/ComfyUI", help="path to a ComfyUI checkout")
    parser.add_argument("--python", default=None, help="Python that can run that ComfyUI (default: its .venv)")
    parser.add_argument("--quick", action="store_true", help="skip the idle and watchdog scenarios")
    parser.add_argument("--only", default=None, help="comma-separated scenario names to run")
    parser.add_argument("--keep", action="store_true", help="keep the work directory and logs")
    parser.add_argument("--serve", action="store_true",
                        help="do not run tests: start the simulator and ComfyUI and leave them running")
    parser.add_argument("--local-port", type=int, default=None, help="port for the local ComfyUI (with --serve)")
    args = parser.parse_args()
    suite = Suite(args)
    if args.local_port:
        suite.local_port = args.local_port
        suite.local_url = f"http://127.0.0.1:{args.local_port}"
    try:
        sys.exit(asyncio.run(suite.main()))
    except KeyboardInterrupt:
        suite.stop_local()
        if suite.sim_proc and suite.sim_proc.poll() is None:
            suite.sim_proc.terminate()
        shutil.rmtree(suite.work, ignore_errors=True)


if __name__ == "__main__":
    main()
