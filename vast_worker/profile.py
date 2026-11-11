"""How a Vast.ai instance is launched: image, ports, environment.

The defaults mirror Vast's official ComfyUI template (image ``vastai/comfy``,
built from https://github.com/vast-ai/base-image). That image ships:

* Caddy in front of every proxied port, with token authentication. Setting
  ``WEB_PASSWORD`` at launch makes that value the bearer token.
* ComfyUI on internal port 18188, exposed (through Caddy) on 8188.
* The Instance Portal API on 1111, used once to install the worker agent.
"""

from __future__ import annotations

import logging
import re

import aiohttp

log = logging.getLogger("vast_worker")

COMFY_PORT = 8188
PORTAL_PORT = 1111
IMAGE_REPO = "vastai/comfy"
IMAGE_VARIANT = "cuda-12.9-py312"

PORTAL_CONFIG = (
    "localhost:1111:11111:/:Instance Portal"
    "|localhost:8188:18188:/:ComfyUI"
)


def image_tag_for(comfy_version: str) -> str:
    version = comfy_version.strip().lstrip("v")
    return f"{IMAGE_REPO}:v{version}-{IMAGE_VARIANT}"


async def resolve_image(setting: str, comfy_version: str | None) -> str:
    """Pick the Docker image for a new instance.

    ``setting`` other than "auto" is used as-is. For "auto" the tag matching
    the local ComfyUI version is preferred, so local node definitions match the
    remote; if Docker Hub has no such tag the newest one is used instead.
    """
    if setting and setting.strip().lower() != "auto":
        return setting.strip()

    wanted = image_tag_for(comfy_version) if comfy_version else None
    hub = f"https://hub.docker.com/v2/repositories/{IMAGE_REPO}/tags"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if wanted:
                tag = wanted.split(":", 1)[1]
                async with session.get(f"{hub}/{tag}") as resp:
                    if resp.status == 200:
                        return wanted
            params = {"page_size": "50", "ordering": "last_updated", "name": IMAGE_VARIANT}
            async with session.get(hub, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("results", []):
                        name = str(item.get("name", ""))
                        if re.match(r"^v\d+\.\d+\.\d+-" + re.escape(IMAGE_VARIANT) + "$", name):
                            if wanted and name != wanted.split(":", 1)[1]:
                                log.warning(
                                    "[vast-worker] no %s image for ComfyUI %s; using %s",
                                    IMAGE_REPO, comfy_version, name,
                                )
                            return f"{IMAGE_REPO}:{name}"
    except Exception as exc:  # Docker Hub unreachable: fall through to best guess
        log.warning("[vast-worker] could not query Docker Hub for image tags: %s", exc)
    if wanted:
        return wanted
    raise RuntimeError("could not determine a ComfyUI image; set instance.image explicitly")


def build_create_payload(settings: dict, image: str, token: str, label: str) -> dict:
    """Request body for ``PUT /api/v0/asks/{offer_id}/``."""
    inst = settings["instance"]
    conn = settings["connection"]
    life = settings["lifecycle"]
    env: dict[str, str] = {
        f"-p {PORTAL_PORT}:{PORTAL_PORT}": "1",
        f"-p {COMFY_PORT}:{COMFY_PORT}": "1",
        "OPEN_BUTTON_PORT": str(PORTAL_PORT),
        "OPEN_BUTTON_TOKEN": "1",
        "DATA_DIRECTORY": "/workspace/",
        "PORTAL_CONFIG": PORTAL_CONFIG,
        # Bearer token for everything behind Caddy on this instance.
        "WEB_PASSWORD": token,
        # Second check inside the agent, in case the edge is ever misconfigured.
        "VAST_WORKER_TOKEN": token,
        "VAST_WORKER_WATCHDOG_MINUTES": str(life["watchdog_minutes"]),
    }
    if conn["scheme"] == "https":
        env["ENABLE_HTTPS"] = "true"
    for key, value in (inst.get("extra_env") or {}).items():
        env[str(key)] = str(value)

    payload: dict = {
        "image": image,
        "disk": int(inst["disk_gb"]),
        "runtype": inst["runtype"],
        "label": label,
        "env": env,
        "cancel_unavail": True,
    }
    if inst["runtype"] != "args" and inst.get("onstart"):
        payload["onstart"] = inst["onstart"]
    return payload
