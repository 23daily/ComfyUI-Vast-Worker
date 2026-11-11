"""Smoke test against the real Vast.ai. THIS RENTS A MACHINE AND COSTS MONEY
(typically a few cents: it runs for a few minutes and destroys the instance).

It exercises exactly the parts that the local simulator can only imitate:

1. the Vast REST API with your key (search, create, show, destroy)
2. the official ``vastai/comfy`` image: boot, port mapping, token check
3. installing the worker agent through the Instance Portal
4. running a tiny workflow and fetching its output
5. a model download on the instance (a small public file from Hugging Face)
6. optionally, the dead-man's switch (``--self-destroy``)

Usage (the key is read from the environment, never from the command line):

    export VAST_API_KEY=...
    python scripts/smoke_test.py --max-price 0.40

Needs only Python 3.10+ and aiohttp; ComfyUI does not have to be running.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SMOKE_FILE_URL = "https://huggingface.co/hf-internal-testing/tiny-random-gpt2/resolve/main/config.json"


def step(title: str) -> float:
    print(f"\n== {title}", flush=True)
    return time.time()


def done(started: float, note: str = "") -> None:
    print(f"   ok ({time.time() - started:.1f}s){' — ' + note if note else ''}", flush=True)


async def run(args) -> int:
    from vast_worker import config
    from vast_worker.manager import InstanceManager
    from vast_worker.vast_api import VastAPI, find_offers, summarize_offer

    if not config.get_secret("vast_api_key"):
        print("Set VAST_API_KEY in the environment first.")
        return 2

    settings = config.save_settings({
        "gpu": {"max_dph": args.max_price, "min_vram_gb": args.min_vram},
        "instance": {"image": args.image, "disk_gb": args.disk, "runtype": args.runtype},
        # One attempt only: this test rents exactly one machine.
        "lifecycle": {"confirm_rental": False, "watchdog_minutes": 10, "boot_timeout_minutes": args.boot_timeout,
                      "max_rent_attempts": 1},
        "connection": {"scheme": args.scheme},
    })

    t = step("Checking the API key and searching offers")
    async with VastAPI(config.get_secret("vast_api_key")) as api:
        user = await api.whoami()
        offers = await find_offers(api, settings["gpu"], settings["instance"]["disk_gb"])
    done(t, f"credit ${user.get('credit')}, {len(offers)} matching offers")
    if not offers:
        print("No offers match. Try a higher --max-price.")
        return 1
    def estimate(offer: dict) -> float:
        """Rough total for this test: 15 minutes of rent plus pulling a ~12 GB image."""
        return float(offer.get("dph_total") or 0) * 0.25 + 12 * float(offer.get("inet_down_cost") or 0)

    for offer in offers[:5]:
        s = summarize_offer(offer)
        print(f"   {s['gpu_name']:<14} {s['gpu_ram_gb']} GB  ${s['dph_total']:.3f}/h  "
              f"download ${float(offer.get('inet_down_cost') or 0):.4f}/GB  "
              f"est. total ${estimate(offer):.2f}  {s['geolocation']}")
    cheapest = summarize_offer(offers[0])
    print(f"\n   The test rents the cheapest one: {cheapest['gpu_name']} at ${cheapest['dph_total']:.3f}/h, "
          f"estimated total about ${estimate(offers[0]):.2f}.")
    if args.dry_run:
        print("   Dry run: nothing was rented.")
        return 0
    if not args.yes:
        answer = input(f"\nRent {cheapest['gpu_name']} at ${cheapest['dph_total']:.3f}/h for a few minutes? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled; nothing was rented.")
            return 0

    manager = InstanceManager()
    report: list[str] = []
    started_all = time.time()
    try:
        t = step("Renting, booting and installing the agent (this is the slow part)")
        remote = await manager.ensure_ready()
        boot_seconds = time.time() - t
        done(t, f"instance {manager.record['id']} at {manager.record['comfy_base']}")
        report.append(f"cold start: {boot_seconds:.0f}s")

        t = step("Token check: a request without the token must be refused")
        import aiohttp

        async with aiohttp.ClientSession() as bare:
            async with bare.get(f"{manager.record['comfy_base']}/system_stats", ssl=False) as resp:
                refused = resp.status in (401, 403)
        done(t, f"HTTP {resp.status}")
        report.append("unauthenticated access refused" if refused else
                      f"WARNING: unauthenticated request returned HTTP {resp.status} — the instance is open to anyone")

        t = step("Agent")
        info = await remote.agent_ping()
        done(t, f"ComfyUI {info.get('comfyui_version')}, GPU {info.get('gpu')}, "
                f"watchdog {info.get('watchdog_seconds')}s")
        report.append("watchdog armed" if info.get("watchdog_seconds") else
                      "WARNING: watchdog not armed (CONTAINER_ID / CONTAINER_API_KEY not visible to ComfyUI)")

        t = step("Running a workflow over the websocket")
        prompt_id = str(uuid.uuid4())
        prompt = {
            "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 255}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "vast_worker_smoke"}},
        }
        client_id = uuid.uuid4().hex
        images = []
        async with remote.ws_connect(client_id) as ws:
            await ws.send_json({"type": "feature_flags", "data": {}})
            await remote.queue_prompt({"prompt": prompt, "client_id": client_id, "prompt_id": prompt_id})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = msg.json()
                data = event.get("data") or {}
                if event.get("type") == "executed" and data.get("prompt_id") == prompt_id:
                    images = (data.get("output") or {}).get("images") or []
                if event.get("type") in ("execution_success", "execution_error") and data.get("prompt_id") == prompt_id:
                    if event["type"] == "execution_error":
                        raise RuntimeError(f"remote execution failed: {data.get('exception_message')}")
                    break
        with tempfile.TemporaryDirectory() as tmp:
            path = await remote.download_view(images[0]["filename"], images[0].get("subfolder", ""), "output", tmp, "out.png")
            size = os.path.getsize(path)
        done(t, f"output image fetched ({size} bytes)")
        report.append("workflow executed, output downloaded")

        t = step("Downloading a file on the instance from Hugging Face")
        job_id = await remote.agent_download(
            [{"directory": "loras", "name": "vast_worker_smoke/config.json", "url": SMOKE_FILE_URL}],
            config.get_secret("hf_token"), None)
        while True:
            job = await remote.agent_job(job_id)
            if job["state"] in ("done", "error"):
                break
            await asyncio.sleep(1)
        if job["state"] == "error":
            raise RuntimeError(f"download failed: {job['error']}")
        done(t)
        report.append("model download on the instance works")

        if args.upload_mb > 0:
            t = step(f"Uploading a {args.upload_mb} MB file from this computer "
                     "(ComfyUI's own upload route stops at 100 MB)")
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "upload.bin")
                with open(path, "wb") as f:
                    f.write(os.urandom(1 << 20))
                    f.truncate(args.upload_mb << 20)
                started = time.time()
                await remote.upload(path, "model", "vast_worker_smoke/upload.bin", "loras")
                seconds = max(time.time() - started, 0.001)
            check = await remote.agent_check([{"directory": "loras", "name": "vast_worker_smoke/upload.bin"}], [])
            if check.get("missing_models"):
                raise RuntimeError("the uploaded file is not on the instance")
            done(t, f"{args.upload_mb / seconds:.1f} MB/s")
            report.append(f"upload of a {args.upload_mb} MB local file works ({seconds:.0f}s)")

        if args.self_destroy:
            t = step("Dead-man's switch: stopping heartbeats and waiting for the instance to remove itself")
            for task in manager._tasks:
                task.cancel()
            await remote.agent_heartbeat(20)
            instance_id = manager.record["id"]
            outcome = "still running after 3 minutes"
            async with VastAPI(config.get_secret("vast_api_key")) as api:
                for _ in range(36):
                    await asyncio.sleep(5)
                    inst = await api.show_instance(instance_id)
                    if inst is None:
                        outcome = "destroyed itself"
                        break
                    if inst.get("actual_status") in ("exited", "stopped") or inst.get("intended_status") == "stopped":
                        outcome = "stopped itself (the instance key may not destroy; storage is still billed)"
                        break
            done(t, outcome)
            report.append(f"watchdog: {outcome}")
    except Exception as exc:
        report.append(f"FAILED: {type(exc).__name__}: {exc}")
        print(f"\n!! {type(exc).__name__}: {exc}", flush=True)
    finally:
        t = step("Destroying the instance")
        try:
            await manager.destroy("smoke test finished")
            done(t)
        except Exception as exc:
            print(f"   COULD NOT DESTROY: {exc}")
        record_id = (manager.record or {}).get("id")
        if record_id:
            print(f"\n!!! Instance {record_id} may still be running. Destroy it at https://cloud.vast.ai/instances/")
        else:
            try:
                async with VastAPI(config.get_secret("vast_api_key")) as api:
                    leftovers = [i for i in await api.show_instances()
                                 if str(i.get("label") or "").startswith("comfyui-vast-worker")]
                print("   no instances with this plugin's label remain on the account" if not leftovers else
                      f"\n!!! Still on your account: {[i.get('id') for i in leftovers]} — destroy them in the Vast.ai console.")
                if leftovers:
                    report.append("WARNING: instances with this plugin's label are still on the account")
            except Exception as exc:
                report.append(f"WARNING: could not list the account's instances to double-check: {exc}")
        for task in manager._tasks:
            task.cancel()

    print(f"\n== Report ({time.time() - started_all:.0f}s total)")
    for line in report:
        print(f"   - {line}")
    return 1 if any(line.startswith(("FAILED", "WARNING")) for line in report) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-price", type=float, default=0.40, help="price ceiling in $/hour (default 0.40)")
    parser.add_argument("--min-vram", type=int, default=12, help="minimum VRAM in GB (default 12)")
    parser.add_argument("--disk", type=int, default=40, help="disk size in GB (default 40)")
    parser.add_argument("--image", default="auto", help="Docker image (default: newest vastai/comfy)")
    parser.add_argument("--scheme", default="https", choices=["https", "http"])
    parser.add_argument("--runtype", default="ssh_direct", choices=["ssh_direct", "jupyter_direct", "args"],
                        help="Vast launch mode; try 'args' if the default does not boot the image")
    parser.add_argument("--boot-timeout", type=int, default=15, help="minutes to wait for the instance")
    parser.add_argument("--self-destroy", action="store_true", help="also test the dead-man's switch (adds ~1 minute)")
    parser.add_argument("--upload-mb", type=int, default=150,
                        help="size of the test upload from this computer in MB (default 150, 0 to skip)")
    parser.add_argument("--yes", action="store_true", help="do not ask before renting")
    parser.add_argument("--dry-run", action="store_true", help="list offers and the estimated cost, rent nothing")
    parser.add_argument("--use-cli-key", action="store_true",
                        help="if VAST_API_KEY is not set, use the key saved by the vastai CLI "
                             "(~/.config/vastai/vast_api_key)")
    args = parser.parse_args()

    if args.use_cli_key and not os.environ.get("VAST_API_KEY"):
        key_file = os.path.expanduser("~/.config/vastai/vast_api_key")
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                os.environ["VAST_API_KEY"] = f.read().strip()  # stays inside this process
        except OSError as exc:
            print(f"Could not read {key_file}: {exc}")
            sys.exit(2)

    logging.basicConfig(level=logging.INFO, format="   %(message)s")
    logging.getLogger("vast_worker").setLevel(logging.DEBUG)  # show every boot status line
    with tempfile.TemporaryDirectory(prefix="vw-smoke-") as data_dir:
        os.environ["VAST_WORKER_DATA_DIR"] = data_dir  # keep this run away from ComfyUI's own settings
        sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
