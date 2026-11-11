"""Minimal async client for the Vast.ai REST API.

Only the calls the plugin needs: search offers, create / show / destroy an
instance, list instances and verify the API key. Documented at
https://docs.vast.ai/api-reference/introduction
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import quote

import aiohttp

DEFAULT_API_BASE = "https://console.vast.ai"


def api_base() -> str:
    return (os.environ.get("VAST_WORKER_API_BASE") or DEFAULT_API_BASE).rstrip("/")


class VastAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Vast.ai API error {status}: {message}")
        self.status = status
        self.message = message


class VastAPI:
    def __init__(self, api_key: str, base_url: str | None = None, session: aiohttp.ClientSession | None = None):
        if not api_key:
            raise VastAPIError(401, "no Vast.ai API key configured")
        self._key = api_key
        self._base = (base_url or api_base()).rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "VastAPI":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
            self._owns_session = True
        return self._session

    async def _request(self, method: str, path: str, body: Any = None, retries: int = 4) -> Any:
        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        delay = 1.5
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self._get_session().request(method, url, json=body, headers=headers) as resp:
                    text = await resp.text()
                    # The API has per-endpoint rate limits and sends no Retry-After.
                    if resp.status == 429 or resp.status >= 500:
                        last_error = VastAPIError(resp.status, text[:300])
                        if attempt < retries:
                            await asyncio.sleep(delay)
                            delay *= 2
                            continue
                        raise last_error
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {"raw": text}
                    if resp.status >= 400:
                        message = ""
                        if isinstance(data, dict):
                            message = str(data.get("msg") or data.get("error") or data.get("detail") or "")
                        raise VastAPIError(resp.status, message or text[:300])
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise VastAPIError(0, f"could not reach Vast.ai: {exc}") from exc
        raise VastAPIError(0, str(last_error))

    # ------------------------------------------------------------------
    async def whoami(self) -> dict:
        """Current user; used to verify the key and show the credit balance."""
        data = await self._request("GET", "/api/v0/users/current/")
        return data if isinstance(data, dict) else {}

    async def search_offers(self, query: dict) -> list[dict]:
        data = await self._request("POST", "/api/v0/bundles/", query)
        offers = data.get("offers", []) if isinstance(data, dict) else []
        return offers if isinstance(offers, list) else []

    async def create_instance(self, offer_id: int, payload: dict) -> int:
        data = await self._request("PUT", f"/api/v0/asks/{int(offer_id)}/", payload, retries=1)
        if not isinstance(data, dict) or not data.get("success", True):
            raise VastAPIError(400, str(data)[:300])
        # The new instance id is returned as "new_contract", not "id".
        instance_id = data.get("new_contract") or data.get("id")
        if not instance_id:
            raise VastAPIError(400, f"create instance returned no id: {str(data)[:300]}")
        return int(instance_id)

    async def show_instance(self, instance_id: int) -> dict | None:
        """Instance details, or None when it no longer exists."""
        try:
            data = await self._request("GET", f"/api/v0/instances/{int(instance_id)}/")
        except VastAPIError as exc:
            if exc.status == 404:
                return None
            raise
        inst = data.get("instances") if isinstance(data, dict) else None
        if isinstance(inst, list):
            inst = inst[0] if inst else None
        return inst if isinstance(inst, dict) and inst else None

    async def show_instances(self) -> list[dict]:
        """Every instance on the account.

        Uses the v1 endpoint: Vast retired ``GET /api/v0/instances/`` (it now
        answers 410). v1 pages by cursor, 25 instances at a time.
        """
        found: list[dict] = []
        token: str | None = None
        for _ in range(40):
            path = "/api/v1/instances/?limit=25"
            if token:
                path += "&after_token=" + quote(str(token), safe="")
            data = await self._request("GET", path)
            page = data.get("instances") if isinstance(data, dict) else None
            if not isinstance(page, list) or not page:
                break
            found.extend(i for i in page if isinstance(i, dict))
            token = data.get("next_token")
            if not token:
                break
        return found

    async def destroy_instance(self, instance_id: int) -> bool:
        try:
            await self._request("DELETE", f"/api/v0/instances/{int(instance_id)}/")
        except VastAPIError as exc:
            if exc.status == 404:  # already gone
                return True
            raise
        return True


# ----------------------------------------------------------------------
# helpers that do not need a network connection
# ----------------------------------------------------------------------

def build_offer_query(gpu: dict, disk_gb: int, limit: int = 20) -> dict:
    """Translate the plugin's GPU settings into a Vast offer search."""
    query: dict[str, Any] = {
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "num_gpus": {"eq": int(gpu.get("num_gpus", 1))},
        "gpu_ram": {"gte": float(gpu.get("min_vram_gb", 0)) * 1000},  # REST API uses MB
        "reliability": {"gte": float(gpu.get("min_reliability", 0))},
        "dph_total": {"lte": float(gpu.get("max_dph", 1.0))},
        "inet_down": {"gte": float(gpu.get("min_inet_down_mbps", 0))},
        "disk_space": {"gte": float(disk_gb)},
        "cuda_max_good": {"gte": float(gpu.get("min_cuda", 0))},
        "direct_port_count": {"gte": 4},
        "type": "ondemand",
        "order": [["dph_total", "asc"]],
        "allocated_storage": float(disk_gb),
        "limit": int(limit),
    }
    if int(gpu.get("min_compute_cap", 0) or 0) > 0:
        query["compute_cap"] = {"gte": int(gpu["min_compute_cap"])}
    if gpu.get("verified_only", True):
        query["verified"] = {"eq": True}
    if any(n for n in gpu.get("gpu_names", [])):
        # GPU names are matched here, not by Vast (which only matches exact
        # names), so ask for a long list and let find_offers() narrow it down.
        query["limit"] = max(query["limit"], 1000)
    excluded = [c for c in gpu.get("exclude_countries", []) if c]
    allowed = [c for c in gpu.get("countries", []) if c and c not in excluded]
    if gpu.get("countries"):
        # An allow-list wins; excluded countries are simply left out of it.
        query["geolocation"] = {"in": allowed}
    elif excluded:
        query["geolocation"] = {"notin": excluded}
    return query


def _squash(text: Any) -> str:
    return "".join(str(text or "").lower().split())


def filter_by_gpu(offers: list[dict], patterns: list[str]) -> list[dict]:
    """Keep the offers whose GPU the user asked for.

    A pattern that is the full name of a GPU on offer selects exactly that
    model ("RTX 4090" does not drag in "RTX 4090D"). Anything else is treated
    as part of a name ("4060" selects RTX 4060 and RTX 4060 Ti).
    """
    wanted = [_squash(p) for p in patterns if _squash(p)]
    if not wanted:
        return list(offers)
    full_names = {_squash(o.get("gpu_name")) for o in offers}
    exact = {p for p in wanted if p in full_names}
    partial = [p for p in wanted if p not in full_names]
    kept = []
    for offer in offers:
        name = _squash(offer.get("gpu_name"))
        if name in exact or any(p in name for p in partial):
            kept.append(offer)
    return kept


SORTS = {
    "price": lambda o: float(o.get("dph_total") or 0),
    "performance": lambda o: -float(o.get("dlperf") or 0),
    "value": lambda o: -float(o.get("dlperf_per_dphtotal") or 0),
}


async def find_offers(api: "VastAPI", gpu: dict, disk_gb: int, limit: int = 20, sort: str = "price") -> list[dict]:
    """Offers matching the user's search conditions (cheapest first by default)."""
    offers = await api.search_offers(build_offer_query(gpu, disk_gb, limit))
    ceiling = float(gpu.get("max_dph", 1e9))
    offers = [o for o in offers if o.get("id") and float(o.get("dph_total") or 0) <= ceiling]
    matched = filter_by_gpu(offers, list(gpu.get("gpu_names", [])))
    return sorted(matched, key=SORTS.get(sort, SORTS["price"]))


def country_code(geolocation: Any) -> str:
    """ISO country code from Vast's "Region, CC" geolocation text."""
    text = str(geolocation or "").strip()
    code = text.rsplit(",", 1)[-1].strip().upper()
    return code if len(code) == 2 and code.isalpha() else ""


def build_market_query(verified_only: bool = True, limit: int = 3000) -> dict:
    """A broad search used only to list which GPU models and countries exist."""
    query: dict[str, Any] = {
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "num_gpus": {"eq": 1},
        "type": "ondemand",
        "order": [["dph_total", "asc"]],
        "limit": int(limit),
    }
    if verified_only:
        query["verified"] = {"eq": True}
    return query


def summarize_market(offers: list[dict]) -> dict:
    """GPU models and countries present in a list of offers, for the pickers."""
    gpus: dict[str, dict] = {}
    countries: dict[str, dict] = {}
    for offer in offers:
        price = float(offer.get("dph_total") or 0)
        name = str(offer.get("gpu_name") or "").strip()
        if name:
            row = gpus.setdefault(name, {"name": name, "offers": 0, "min_dph": price, "vram_gb": 0,
                                         "compute_cap": offer.get("compute_cap")})
            row["offers"] += 1
            row["min_dph"] = min(row["min_dph"], price)
            row["vram_gb"] = max(row["vram_gb"], round(float(offer.get("gpu_ram") or 0) / 1024))
        code = country_code(offer.get("geolocation"))
        if code:
            row = countries.setdefault(code, {"code": code, "offers": 0, "min_dph": price})
            row["offers"] += 1
            row["min_dph"] = min(row["min_dph"], price)
    return {
        "gpus": sorted(gpus.values(), key=lambda r: (-r["offers"], r["name"])),
        "countries": sorted(countries.values(), key=lambda r: (-r["offers"], r["code"])),
    }


def summarize_offer(offer: dict) -> dict:
    """The handful of offer fields the UI shows."""
    gpu_ram = offer.get("gpu_ram") or 0
    return {
        "id": offer.get("id"),
        "machine_id": offer.get("machine_id"),
        "gpu_name": offer.get("gpu_name"),
        "num_gpus": offer.get("num_gpus"),
        "gpu_ram_gb": round(float(gpu_ram) / 1024, 1) if gpu_ram else None,
        "dph_total": offer.get("dph_total"),
        "reliability": offer.get("reliability2", offer.get("reliability")),
        "inet_down": offer.get("inet_down"),
        "inet_up": offer.get("inet_up"),
        "dlperf": offer.get("dlperf"),
        "datacenter": offer.get("hosting_type") == 1,
        "geolocation": offer.get("geolocation"),
        "cuda_max_good": offer.get("cuda_max_good"),
        "compute_cap": offer.get("compute_cap"),
        "inet_down_cost": offer.get("inet_down_cost"),
        "cpu_cores": offer.get("cpu_cores_effective", offer.get("cpu_cores")),
        "cpu_ram_gb": round(float(offer.get("cpu_ram") or 0) / 1024, 1) if offer.get("cpu_ram") else None,
        "disk_space": offer.get("disk_space"),
    }


def parse_port_map(instance: dict) -> dict[int, int]:
    """Map internal container port -> external host port for a running instance.

    Vast returns Docker's port binding format, e.g.
    ``{"8188/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33526"}]}``.
    """
    out: dict[int, int] = {}
    ports = instance.get("ports")
    if not isinstance(ports, dict):
        return out
    for key, bindings in ports.items():
        try:
            internal = int(str(key).split("/")[0])
        except ValueError:
            continue
        if str(key).endswith("/udp"):
            continue
        if isinstance(bindings, list) and bindings:
            host_port = bindings[0].get("HostPort") if isinstance(bindings[0], dict) else None
            try:
                out[internal] = int(host_port)
            except (TypeError, ValueError):
                continue
    return out
