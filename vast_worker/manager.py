"""Lifecycle of the rented instance: rent, boot, install the agent, keep
alive, destroy when idle.

One instance at a time. Its record is persisted, so a ComfyUI restart never
loses track of a machine that is still billing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from collections import deque
from typing import Any

from . import INSTANCE_LABEL_PREFIX, __version__, config
from .bootstrap import agent_version, build_bootstrap_manifest
from .library import ModelLibrary
from .profile import COMFY_PORT, PORTAL_PORT, build_create_payload, resolve_image
from .remote import RemoteClient, RemoteError, RemoteUnavailable
from .util import redact
from .vast_api import VastAPI, VastAPIError, find_offers, parse_port_map, summarize_offer

log = logging.getLogger("vast_worker")

MODES = ("local", "vast")
DEAD_STATUSES = ("exited", "unknown", "offline")


class WorkerError(Exception):
    """A problem to show the user as-is."""


class Cancelled(Exception):
    """The user declined a confirmation."""


class BootError(Exception):
    """An instance failed to come up; another offer may still work."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class InstanceManager:
    def __init__(self):
        self.mode = "local"
        self.phase = "none"  # none | searching | confirming | renting | booting | installing | ready | destroying | error
        self.message = ""
        self.record: dict | None = None
        self.remote: RemoteClient | None = None
        self.busy = 0
        self.last_activity = time.time()
        self.activity: dict | None = None
        self.others: list[dict] = []
        self.events: deque = deque(maxlen=200)
        self.library = ModelLibrary()
        self.uploaded: dict[str, tuple[float, int]] = {}  # input files already on the instance
        self._lock: asyncio.Lock | None = None
        self._confirms: dict[str, tuple[asyncio.Future, dict]] = {}
        self._broadcast_pending = False
        self._heartbeat_failures = 0
        self.job_started: float | None = None
        self.disk_free: int | None = None  # free bytes on the instance, from its last heartbeat
        self._op_task: asyncio.Task | None = None  # the boot/reconnect in progress, if any
        self._aborting = False
        self._tasks: list[asyncio.Task] = []
        self._poll = _env_float("VAST_WORKER_POLL_SECONDS", 5.0)
        self._tick = _env_float("VAST_WORKER_TICK_SECONDS", 20.0)

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _server(self):
        try:
            from server import PromptServer

            return getattr(PromptServer, "instance", None)
        except Exception:
            return None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once when ComfyUI loads the plugin."""
        self._tasks.append(loop.create_task(self._startup()))
        self._tasks.append(loop.create_task(self._housekeeping()))

    def log(self, message: str, level: str = "info") -> None:
        message = redact(message, config.get_secret("vast_api_key"), (self.record or {}).get("token"))
        self.events.append({"time": time.time(), "level": level, "message": message})
        getattr(log, "warning" if level in ("warn", "error") else "info")("[vast-worker] %s", message)
        self.broadcast()

    def _set(self, phase: str, message: str = "") -> None:
        changed = phase != self.phase
        message_changed = message != self.message
        self.phase, self.message = phase, message
        if changed and message:
            self.events.append({"time": time.time(), "level": "info", "message": message})
            log.info("[vast-worker] %s", message)
        elif message_changed and message:
            log.debug("[vast-worker] %s", message)
        self.broadcast()

    def set_activity(self, phase: str | None, message: str = "", items: list | None = None, detail: str = "") -> None:
        self.activity = ({"phase": phase, "message": message, "items": items or [], "detail": detail,
                          "job_seconds": round(time.time() - self.job_started) if self.job_started else None}
                         if phase else None)
        self.broadcast()

    def broadcast(self) -> None:
        """Push a status snapshot to every connected browser (coalesced)."""
        server = self._server()
        if server is None or self._broadcast_pending:
            return
        self._broadcast_pending = True

        def send() -> None:
            self._broadcast_pending = False
            try:
                server.send_sync("vast_worker.status", self.snapshot())
            except Exception:
                pass

        try:
            server.loop.call_soon_threadsafe(lambda: server.loop.call_later(0.2, send))
        except Exception:
            self._broadcast_pending = False

    def touch(self) -> None:
        self.last_activity = time.time()

    def begin_job(self) -> None:
        if self.busy == 0:
            self.job_started = time.time()
        self.busy += 1
        self.touch()
        self.broadcast()

    def end_job(self) -> None:
        self.busy = max(0, self.busy - 1)
        if self.busy == 0:
            self.job_started = None
        self.touch()
        self.activity = None
        self.broadcast()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        if mode != self.mode:
            self.mode = mode
            self.log("Run target: Vast.ai" if mode == "vast" else "Run target: this computer")

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def snapshot(self, full: bool = False) -> dict:
        settings = config.load_settings()
        instance = None
        if self.record:
            rec = self.record
            created = rec.get("created_at") or time.time()
            uptime = max(0.0, time.time() - created)
            dph = float(rec.get("dph_total") or 0)
            instance = {
                "id": rec.get("id"),
                "label": rec.get("label"),
                "gpu_name": (rec.get("offer") or {}).get("gpu_name"),
                "num_gpus": (rec.get("offer") or {}).get("num_gpus"),
                "gpu_ram_gb": (rec.get("offer") or {}).get("gpu_ram_gb"),
                "geolocation": (rec.get("offer") or {}).get("geolocation"),
                "dph_total": dph,
                "image": rec.get("image"),
                "created_at": created,
                "uptime_seconds": uptime,
                "cost_estimate": round(dph * uptime / 3600.0, 4),
                "disk_gb": rec.get("disk_gb"),
                "disk_free_bytes": self.disk_free,
                "idle_seconds": 0 if self.busy else max(0.0, time.time() - self.last_activity),
                "idle_limit_seconds": settings["lifecycle"]["idle_minutes"] * 60,
            }
        snap: dict[str, Any] = {
            "version": __version__,
            "mode": self.mode,
            "phase": self.phase,
            "message": self.message,
            "busy": self.busy,
            "instance": instance,
            # The job clock is read at snapshot time, so it keeps ticking
            # during long steps that do not report progress themselves.
            "activity": ({**self.activity,
                          "job_seconds": round(time.time() - self.job_started) if self.job_started else None}
                         if self.activity else None),
            "others": self.others,
            "secrets": config.secrets_status(),
            "confirmations": [{"id": cid, **payload} for cid, (_, payload) in self._confirms.items()],
            "events": list(self.events)[-60:],
        }
        if full:
            snap["settings"] = settings
            snap["models"] = self.library.list()
        return snap

    # ------------------------------------------------------------------
    # confirmations (answered from the browser)
    # ------------------------------------------------------------------
    async def request_confirmation(self, payload: dict, timeout: float = 900) -> dict:
        cid = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._confirms[cid] = (future, payload)
        server = self._server()
        if server is not None:
            server.send_sync("vast_worker.confirm", {"id": cid, **payload})
        self.broadcast()
        try:
            answer = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise Cancelled("no answer to the confirmation dialog") from None
        finally:
            self._confirms.pop(cid, None)
            if server is not None:
                server.send_sync("vast_worker.confirm_closed", {"id": cid})
            self.broadcast()
        if not answer.get("approved"):
            raise Cancelled("declined")
        return answer

    def resolve_confirmation(self, cid: str, answer: dict) -> bool:
        entry = self._confirms.get(cid)
        if entry is None or entry[0].done():
            return False
        entry[0].set_result(answer)
        return True

    # ------------------------------------------------------------------
    # Vast API access
    # ------------------------------------------------------------------
    def _api(self) -> VastAPI:
        key = config.get_secret("vast_api_key")
        if not key:
            raise WorkerError("No Vast.ai API key is set. Add one in the Vast Worker sidebar.")
        return VastAPI(key)

    def _client_for(self, record: dict) -> RemoteClient:
        settings = config.load_settings()
        return RemoteClient(
            record["comfy_base"], record.get("portal_base"), record["token"],
            verify_tls=settings["connection"]["verify_tls"])

    def _save(self) -> None:
        config.save_state({"instance": self.record} if self.record else {})

    async def _forget(self) -> None:
        if self.remote is not None:
            await self.remote.close()
        self.remote = None
        self.record = None
        self.disk_free = None
        self.uploaded = {}
        self._heartbeat_failures = 0
        self._save()

    # ------------------------------------------------------------------
    # getting an instance
    # ------------------------------------------------------------------
    async def ensure_ready(self, offer: dict | None = None) -> RemoteClient:
        """Return a client for a ready instance, renting one if necessary.

        ``offer`` rents that specific machine (the user picked it in the
        machine browser); it is refused while another instance exists.
        """
        return await self._exclusive(lambda: self._ensure(offer))

    async def _exclusive(self, start):
        """Run a slow operation under the lock, as a task Destroy can cancel.

        Booting a machine takes minutes. Without this, a Destroy request
        would sit behind the lock until the boot had finished.
        """
        async with self._get_lock():
            task = asyncio.ensure_future(start())
            self._op_task = task
            try:
                return await task
            except asyncio.CancelledError:
                if self._aborting:
                    raise Cancelled("stopped by Destroy") from None
                raise
            finally:
                self._op_task = None
                self._aborting = False

    async def _ensure(self, offer: dict | None) -> RemoteClient:
        if offer is not None:
            if self.record:
                raise WorkerError("An instance is already running. Destroy it before renting another machine.")
            remote = await self._rent(offer)
            self.touch()
            return remote
        if self.record and self.remote and self.phase == "ready":
            if await self._agent_alive():
                self.touch()
                return self.remote
            self.log("The instance stopped responding; checking it with Vast.ai…", "warn")
        if self.record:
            if await self._adopt():
                self.touch()
                assert self.remote is not None
                return self.remote
        remote = await self._rent()
        self.touch()
        return remote

    async def _ping(self) -> dict:
        """Ping the agent, remembering how much disk the instance has left."""
        assert self.remote is not None
        info = await self.remote.agent_ping()
        if isinstance(info.get("disk_free_bytes"), (int, float)):
            self.disk_free = int(info["disk_free_bytes"])
        return info

    async def _agent_alive(self, attempts: int = 3) -> bool:
        assert self.remote is not None
        for i in range(attempts):
            try:
                info = await self._ping()
                if info.get("agent_version") == agent_version():
                    return True
                return False  # an older agent: re-install through _adopt
            except RemoteError:
                if i < attempts - 1:
                    await asyncio.sleep(2)
        return False

    def _endpoints(self, inst: dict) -> tuple[str, str] | None:
        ports = parse_port_map(inst)
        ip = str(inst.get("public_ipaddr") or "").strip()
        if not ip or COMFY_PORT not in ports or PORTAL_PORT not in ports:
            return None
        scheme = config.load_settings()["connection"]["scheme"]
        return (f"{scheme}://{ip}:{ports[COMFY_PORT]}", f"{scheme}://{ip}:{ports[PORTAL_PORT]}")

    async def _adopt(self) -> bool:
        """Try to reuse the instance in ``self.record``. False = it is gone."""
        assert self.record is not None
        record = self.record
        settings = config.load_settings()
        async with self._api() as api:
            inst = await api.show_instance(record["id"])
            if inst is None:
                self.log(f"Instance {record['id']} no longer exists.")
                await self._forget()
                self._set("none", "")
                return False
            if inst.get("actual_status") != "running":
                self.log(f"Instance {record['id']} is {inst.get('actual_status')}; destroying it.", "warn")
                await self._destroy_record(api)
                return False
            endpoints = self._endpoints(inst)
            if endpoints is None:
                self.log(f"Instance {record['id']} has no reachable ports; destroying it.", "warn")
                await self._destroy_record(api)
                return False
            record["comfy_base"], record["portal_base"] = endpoints
            if self.remote is not None:
                await self.remote.close()
            self.remote = self._client_for(record)
            self._save()
            deadline = time.time() + min(300, settings["lifecycle"]["boot_timeout_minutes"] * 60)
            try:
                self._set("booting", "Reconnecting to the instance…")
                await self._wait_comfy(api, deadline)
                await self._ensure_agent(deadline)
            except (BootError, RemoteError) as exc:
                self.log(f"Could not reconnect to instance {record['id']}: {exc}. Destroying it.", "warn")
                await self._destroy_record(api)
                return False
        self._set("ready", f"Instance {record['id']} is ready.")
        return True

    async def _rent(self, picked_offer: dict | None = None) -> RemoteClient:
        settings = config.load_settings()
        async with self._api() as api:
            if picked_offer is not None:
                offers = [picked_offer]
            else:
                self._set("searching", "Searching Vast.ai for a matching GPU…")
                try:
                    offers = await find_offers(api, settings["gpu"], settings["instance"]["disk_gb"])
                except VastAPIError as exc:
                    self._set("error", str(exc))
                    raise WorkerError(str(exc)) from exc
            ceiling = settings["gpu"]["max_dph"]
            if not offers:
                wanted = ", ".join(settings["gpu"]["gpu_names"])
                message = ("No Vast.ai offer matches your search conditions "
                           f"({'GPU ' + wanted + ', ' if wanted else ''}"
                           f"at least {settings['gpu']['min_vram_gb']} GB VRAM, at most "
                           f"${ceiling:.2f}/h). Raise the price limit or loosen the conditions.")
                self._set("error", message)
                raise WorkerError(message)

            import_version = None
            try:
                import comfyui_version

                import_version = comfyui_version.__version__
            except Exception:
                pass
            image = await resolve_image(settings["instance"]["image"], import_version)

            chosen = offers[0]
            # A machine picked in the browser was already confirmed there.
            if picked_offer is None and settings["lifecycle"]["confirm_rental"]:
                self._set("confirming", "Waiting for you to confirm the rental…")
                try:
                    answer = await self.request_confirmation({
                        "kind": "rent",
                        "offers": [summarize_offer(o) for o in offers[:6]],
                        "image": image,
                        "disk_gb": settings["instance"]["disk_gb"],
                        "idle_minutes": settings["lifecycle"]["idle_minutes"],
                    })
                except Cancelled:
                    self._set("none", "")
                    raise
                picked = [o for o in offers if o.get("id") == answer.get("offer_id")]
                chosen = picked[0] if picked else chosen

            ordered = [chosen] + [o for o in offers if o is not chosen]
            failures: list[str] = []
            for offer in ordered[: settings["lifecycle"]["max_rent_attempts"]]:
                try:
                    return await self._launch(api, offer, image, settings)
                except (BootError, VastAPIError, RemoteError) as exc:
                    failures.append(f"{offer.get('gpu_name')} (offer {offer.get('id')}): {exc}")
                    self.log(f"That machine did not start: {exc}", "warn")
            message = "Could not start a Vast.ai instance. " + " | ".join(failures)
            self._set("error", message)
            raise WorkerError(message)

    async def _launch(self, api: VastAPI, offer: dict, image: str, settings: dict) -> RemoteClient:
        token = secrets.token_urlsafe(32)
        label = f"{INSTANCE_LABEL_PREFIX}-{uuid.uuid4().hex[:8]}"
        summary = summarize_offer(offer)
        price = float(offer.get("dph_total") or 0)
        self._set("renting", f"Renting {summary['gpu_name']} at ${price:.3f}/h…")
        # The rental request must run to completion even if Destroy cancels
        # us mid-flight: otherwise a machine could be created that nobody tracks.
        creating = asyncio.ensure_future(
            api.create_instance(offer["id"], build_create_payload(settings, image, token, label)))
        try:
            instance_id = await asyncio.shield(creating)
        except asyncio.CancelledError:
            try:
                await api.destroy_instance(await creating)
            except VastAPIError:
                pass
            raise
        self.record = {
            "id": instance_id, "label": label, "token": token, "offer": summary, "image": image,
            "dph_total": price, "created_at": time.time(), "comfy_base": None, "portal_base": None,
            "disk_gb": settings["instance"]["disk_gb"],
        }
        self._save()  # from here on the machine is billing: never lose its id
        deadline = time.time() + settings["lifecycle"]["boot_timeout_minutes"] * 60
        try:
            await self._wait_running(api, deadline)
            await self._wait_comfy(api, deadline)
            await self._ensure_agent(deadline)
        except BaseException:
            # Includes cancellation: a half-booted machine must not be left billing.
            await self._destroy_record(api, quiet=True)
            raise
        self._set("ready", f"Instance {instance_id} is ready ({summary['gpu_name']}, ${price:.3f}/h).")
        assert self.remote is not None
        return self.remote

    async def _wait_running(self, api: VastAPI, deadline: float) -> None:
        assert self.record is not None
        missing = 0
        while True:
            inst = await api.show_instance(self.record["id"])
            if inst is None:
                # One empty answer can be a hiccup of the API; three in a row is real.
                missing += 1
                if missing >= 3:
                    raise BootError("the instance disappeared while starting (the host may have withdrawn it)")
                await asyncio.sleep(self._poll)
                continue
            missing = 0
            status = inst.get("actual_status")
            detail = str(inst.get("status_msg") or "").strip().splitlines()
            detail_text = detail[-1][:140] if detail else ""
            if status in DEAD_STATUSES:
                raise BootError(f"the instance failed to start ({status}) {detail_text}".strip())
            if status == "running":
                endpoints = self._endpoints(inst)
                if endpoints is not None:
                    self.record["comfy_base"], self.record["portal_base"] = endpoints
                    if inst.get("dph_total"):
                        self.record["dph_total"] = float(inst["dph_total"])
                    self._save()
                    self.remote = self._client_for(self.record)
                    return
            self._set("booting", f"Starting the instance ({status or 'scheduling'})… {detail_text}".strip())
            if time.time() > deadline:
                raise BootError("timed out waiting for the instance to start")
            await asyncio.sleep(self._poll)

    async def _wait_comfy(self, api: VastAPI, deadline: float) -> None:
        assert self.record is not None and self.remote is not None
        checks = 0
        last_error = "no response"
        while True:
            try:
                await self.remote.system_stats()
                return
            except RemoteUnavailable as exc:
                last_error = str(exc)
            except RemoteError as exc:
                if exc.status in (401, 403):
                    raise BootError("the instance rejected the access token") from exc
                last_error = f"HTTP {exc.status}: {exc}"
            self._set("booting", "Waiting for ComfyUI to start on the instance…")
            if time.time() > deadline:
                raise BootError(f"timed out waiting for ComfyUI on the instance (last error: {last_error})")
            checks += 1
            if checks % 6 == 0:
                inst = await api.show_instance(self.record["id"])
                if inst is None or inst.get("actual_status") in DEAD_STATUSES:
                    raise BootError("the instance stopped while ComfyUI was starting")
            await asyncio.sleep(self._poll)

    async def _ensure_agent(self, deadline: float) -> None:
        assert self.remote is not None
        wanted = agent_version()
        try:
            if (await self._ping()).get("agent_version") == wanted:
                return
        except RemoteError:
            pass
        self._set("installing", "Installing the worker agent on the instance…")
        last_error: Exception | None = None
        for _ in range(5):
            try:
                await self.remote.portal_provision(build_bootstrap_manifest())
                last_error = None
                break
            except RemoteError as exc:
                last_error = exc
                if exc.status == 404:
                    raise BootError(
                        "this image's Instance Portal has no provisioning API; "
                        "use a recent vastai/comfy image (set Docker image to auto)") from exc
                await asyncio.sleep(self._poll)
        if last_error is not None:
            raise BootError(f"could not reach the instance portal to install the agent: {last_error}")
        limit = min(deadline, time.time() + 300)
        while time.time() < limit:
            await asyncio.sleep(min(self._poll, 2.0))
            try:
                if (await self._ping()).get("agent_version") == wanted:
                    return
            except RemoteError:
                continue
        raise BootError("the worker agent did not come up after installation")

    # ------------------------------------------------------------------
    # destroying
    # ------------------------------------------------------------------
    async def _destroy_record(self, api: VastAPI, quiet: bool = False) -> None:
        if not self.record:
            return
        instance_id = self.record["id"]
        if not quiet:
            self._set("destroying", f"Destroying instance {instance_id}…")
        try:
            await api.destroy_instance(instance_id)
        except VastAPIError as exc:
            message = (f"Could not destroy instance {instance_id}: {exc}. "
                       "It may still be billing — destroy it in the Vast.ai console.")
            self._set("error", message)
            raise WorkerError(message) from exc
        await self._forget()
        if not quiet:
            self._set("none", "")
            self.log(f"Instance {instance_id} destroyed.")

    async def destroy(self, reason: str = "requested") -> None:
        task = self._op_task
        aborted = False
        if task is not None and not task.done():
            # A machine is still starting. Stop that first: the launch code
            # destroys its half-booted machine when it is cancelled.
            aborted = True
            self._aborting = True
            self.log(f"Stopping the start-up and destroying the instance ({reason}).")
            task.cancel()
            await asyncio.wait([task], timeout=90)
        async with self._get_lock():
            if not self.record:
                if aborted and self.phase != "error":
                    self._set("none", "")
                    self.log("The instance was destroyed before it finished starting.")
                return
            self.log(f"Destroying the instance ({reason}).")
            async with self._api() as api:
                await self._destroy_record(api)

    async def destroy_other(self, instance_id: int) -> None:
        """Destroy a leftover instance carrying this plugin's label."""
        if not any(o.get("id") == instance_id for o in self.others):
            raise WorkerError("That instance is not one of this plugin's leftovers.")
        async with self._api() as api:
            await api.destroy_instance(instance_id)
        self.log(f"Leftover instance {instance_id} destroyed.")
        await self.refresh_others()

    async def refresh_others(self) -> None:
        """Find instances with our label that this session is not tracking."""
        try:
            async with self._api() as api:
                instances = await api.show_instances()
        except (WorkerError, VastAPIError):
            return
        mine = (self.record or {}).get("id")
        self.others = [
            {"id": i.get("id"), "label": i.get("label"), "gpu_name": i.get("gpu_name"),
             "dph_total": i.get("dph_total"), "actual_status": i.get("actual_status")}
            for i in instances
            if str(i.get("label") or "").startswith(INSTANCE_LABEL_PREFIX) and i.get("id") != mine
        ]
        self.broadcast()

    # ------------------------------------------------------------------
    # background work
    # ------------------------------------------------------------------
    async def _startup(self) -> None:
        await asyncio.sleep(1.0)
        record = config.load_state().get("instance")
        if isinstance(record, dict) and record.get("id"):
            self.record = record
            self._set("booting", f"Found instance {record['id']} from a previous session; reconnecting…")
            try:
                await self._exclusive(self._adopt)
            except Cancelled:
                pass  # destroyed from the sidebar while reconnecting
            except WorkerError as exc:
                self._set("error", f"Instance {record['id']} may still be running: {exc}")
            except Exception as exc:
                self._set("error", f"Could not check instance {record['id']}: {exc}")
        await self.refresh_others()
        if self.others:
            self.log(f"{len(self.others)} other instance(s) rented by this plugin are still on your account.", "warn")

    async def _housekeeping(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(self._tick)
            ticks += 1
            try:
                settings = config.load_settings()
                if self.phase == "ready" and self.remote is not None:
                    try:
                        beat = await self.remote.agent_heartbeat(settings["lifecycle"]["watchdog_minutes"] * 60)
                        if isinstance(beat.get("disk_free_bytes"), (int, float)):
                            self.disk_free = int(beat["disk_free_bytes"])
                        self._heartbeat_failures = 0
                    except RemoteError:
                        self._heartbeat_failures += 1
                    if self.busy == 0:
                        idle = time.time() - self.last_activity
                        if idle > settings["lifecycle"]["idle_minutes"] * 60:
                            await self.destroy(f"idle for {int(idle // 60)} min")
                            continue
                    if self._heartbeat_failures >= 3 and self.record and not self._get_lock().locked():
                        async with self._api() as api:
                            if await api.show_instance(self.record["id"]) is None:
                                self.log("The instance no longer exists on Vast.ai.", "warn")
                                await self._forget()
                                self._set("none", "")
                if ticks % 15 == 0 and config.get_secret("vast_api_key"):
                    await self.refresh_others()
                if self.record:
                    self.broadcast()  # keeps uptime / cost fresh in the sidebar
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[vast-worker] housekeeping error: %s", exc)


MANAGER = InstanceManager()
