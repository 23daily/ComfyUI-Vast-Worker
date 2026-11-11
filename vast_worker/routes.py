"""HTTP routes the sidebar talks to (served by the local ComfyUI)."""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from . import config
from .library import filename_from_url
from .manager import MANAGER, Cancelled, WorkerError
from .vast_api import (VastAPI, VastAPIError, build_market_query, find_offers, summarize_market,
                       summarize_offer)

log = logging.getLogger("vast_worker")
PREFIX = "/vast_worker"
_market_cache: dict = {"data": None, "time": 0.0}
_offer_cache: dict = {}  # offers of the last search, by id


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _wrap(handler):
    async def wrapped(request: web.Request):
        try:
            return await handler(request)
        except (WorkerError, ValueError) as exc:
            return _json_error(str(exc))
        except VastAPIError as exc:
            return _json_error(str(exc), 502)
        except Exception as exc:
            log.exception("[vast-worker] route failed")
            return _json_error(f"{type(exc).__name__}: {exc}", 500)

    return wrapped


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def register(routes) -> None:
    @routes.get(f"{PREFIX}/status")
    @_wrap
    async def status(request):
        return web.json_response(MANAGER.snapshot(full=True))

    @routes.post(f"{PREFIX}/mode")
    @_wrap
    async def set_mode(request):
        body = await _body(request)
        MANAGER.set_mode(str(body.get("mode")))
        return web.json_response(MANAGER.snapshot())

    @routes.post(f"{PREFIX}/settings")
    @_wrap
    async def set_settings(request):
        settings = config.save_settings(await _body(request))
        MANAGER.broadcast()
        return web.json_response({"settings": settings})

    @routes.post(f"{PREFIX}/secrets")
    @_wrap
    async def set_secrets(request):
        # Write-only: values are stored and never sent back to the browser.
        config.set_secrets(await _body(request))
        MANAGER.broadcast()
        return web.json_response({"secrets": config.secrets_status()})

    @routes.post(f"{PREFIX}/verify_key")
    @_wrap
    async def verify_key(request):
        key = config.get_secret("vast_api_key")
        if not key:
            raise WorkerError("No Vast.ai API key is set.")
        async with VastAPI(key) as api:
            user = await api.whoami()
        asyncio.ensure_future(MANAGER.refresh_others())
        return web.json_response({
            "ok": True,
            "credit": user.get("credit"),
            "balance": user.get("balance"),
            "email_set": bool(user.get("email")),
        })

    @routes.post(f"{PREFIX}/offers")
    @_wrap
    async def offers(request):
        """Search offers. A body of ``{"gpu": {...}}`` tries conditions without saving them."""
        body = await _body(request)
        settings = config.load_settings()
        if isinstance(body.get("gpu"), dict):
            settings = config.sanitize_settings({**settings, "gpu": {**settings["gpu"], **body["gpu"]}})
        key = config.get_secret("vast_api_key")
        if not key:
            raise WorkerError("No Vast.ai API key is set.")
        limit = max(1, min(int(body.get("limit") or 10), 200))
        async with VastAPI(key) as api:
            found = await find_offers(api, settings["gpu"], settings["instance"]["disk_gb"],
                                      limit=max(limit, 100), sort=str(body.get("sort") or "price"))
        _offer_cache.clear()
        _offer_cache.update({o["id"]: o for o in found})
        return web.json_response({"offers": [summarize_offer(o) for o in found[:limit]], "total": len(found)})

    @routes.get(f"{PREFIX}/market")
    @_wrap
    async def market(request):
        """GPU models and countries currently on offer, for the settings pickers."""
        now = time.time()
        if _market_cache["data"] is None or now - _market_cache["time"] > 600:
            key = config.get_secret("vast_api_key")
            if not key:
                raise WorkerError("No Vast.ai API key is set.")
            verified = config.load_settings()["gpu"]["verified_only"]
            async with VastAPI(key) as api:
                found = await api.search_offers(build_market_query(verified))
            _market_cache.update(data=summarize_market(found), time=now)
        return web.json_response(_market_cache["data"])

    @routes.post(f"{PREFIX}/start")
    @_wrap
    async def start(request):
        """Rent and boot an instance now, ahead of the first job.

        With ``offer_id`` (from the last search) that exact machine is rented.
        """
        body = await _body(request)
        offer = None
        if body.get("offer_id") is not None:
            offer = _offer_cache.get(int(body["offer_id"]))
            if offer is None:
                raise WorkerError("That offer is no longer in the search results. Search again.")
            if MANAGER.record:
                raise WorkerError("An instance is already running. Destroy it before renting another machine.")

        async def run():
            try:
                await MANAGER.ensure_ready(offer)
            except Cancelled:
                pass  # the user pressed Destroy, or declined the rental
            except Exception as exc:
                MANAGER.log(f"Could not start an instance: {exc}", "error")

        asyncio.ensure_future(run())
        return web.json_response({"ok": True})

    @routes.post(f"{PREFIX}/destroy")
    @_wrap
    async def destroy(request):
        await MANAGER.destroy("requested from the sidebar")
        return web.json_response(MANAGER.snapshot())

    @routes.post(f"{PREFIX}/destroy_other")
    @_wrap
    async def destroy_other(request):
        body = await _body(request)
        await MANAGER.destroy_other(int(body.get("id")))
        return web.json_response(MANAGER.snapshot())

    @routes.post(f"{PREFIX}/confirm")
    @_wrap
    async def confirm(request):
        body = await _body(request)
        ok = MANAGER.resolve_confirmation(str(body.get("id")), body)
        return web.json_response({"ok": ok})

    @routes.get(f"{PREFIX}/models")
    @_wrap
    async def list_models(request):
        import folder_paths

        folders = sorted(f for f in folder_paths.folder_names_and_paths if f not in ("custom_nodes", "configs"))
        return web.json_response({"models": MANAGER.library.list(), "folders": folders})

    @routes.post(f"{PREFIX}/models")
    @_wrap
    async def add_model(request):
        body = await _body(request)
        url = str(body.get("url") or "").strip()
        name = str(body.get("name") or "").strip() or filename_from_url(url)
        if not name:
            raise ValueError("Could not tell the file name from that link; please type one.")
        entry = MANAGER.library.add(name, str(body.get("directory") or ""), url)
        return web.json_response({"entry": entry, "models": MANAGER.library.list()})

    @routes.post(f"{PREFIX}/models/remove")
    @_wrap
    async def remove_model(request):
        body = await _body(request)
        MANAGER.library.remove(str(body.get("directory") or ""), str(body.get("name") or ""))
        return web.json_response({"models": MANAGER.library.list()})
