"""Redirects a queued prompt to Vast.ai.

When Vast mode is on, the prompt handler below swaps the whole submitted
graph for a single ``VastRemoteExecute`` node that carries the original graph
as data. ComfyUI then validates and queues that one node as usual, so the job
shows up in the local queue and history, can be cancelled, and never needs
the models to exist locally.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging

from .executor import RemoteExecutionError, RemoteInterrupted, run_remote_job
from .manager import MANAGER, Cancelled, WorkerError
from .remote import RemoteError

log = logging.getLogger("vast_worker")

PROXY_CLASS = "VastRemoteExecute"
PROXY_NODE_ID = "vast_worker_proxy"
# extra_data is copied into the proxy payload, which ends up in history:
# login tokens for API nodes must not be written there.
_SENSITIVE = ("auth_token_comfy_org", "api_key_comfy_org")


def _wants_vast(json_data: dict) -> bool:
    extra = json_data.get("extra_data")
    override = extra.get("vast_worker") if isinstance(extra, dict) else None
    if isinstance(override, dict) and override.get("mode") in ("local", "vast"):
        return override["mode"] == "vast"
    return MANAGER.mode == "vast"


def on_prompt(json_data: dict) -> dict:
    """ComfyUI prompt handler: runs before validation of every queued prompt."""
    prompt = json_data.get("prompt")
    if not isinstance(prompt, dict) or not prompt or not _wants_vast(json_data):
        return json_data
    if any(isinstance(n, dict) and n.get("class_type") == PROXY_CLASS for n in prompt.values()):
        return json_data  # already wrapped (for example re-queued from history)

    extra = json_data.get("extra_data") if isinstance(json_data.get("extra_data"), dict) else {}
    payload = {
        "version": 1,
        "prompt": prompt,
        "extra_data": {k: v for k, v in extra.items()
                       if k not in _SENSITIVE and k not in ("extra_pnginfo", "vast_worker")},
        "partial_execution_targets": json_data.pop("partial_execution_targets", None),
    }
    json_data["prompt"] = {
        PROXY_NODE_ID: {"class_type": PROXY_CLASS, "inputs": {"payload": json.dumps(payload)}},
    }
    return json_data


class VastRemoteExecute:
    """Internal node. It is created automatically when Vast mode is on."""

    CATEGORY = "Vast Worker"
    DESCRIPTION = ("Internal node used by ComfyUI-Vast-Worker to run a whole workflow on a rented "
                   "Vast.ai GPU. You do not need to add it yourself.")
    FUNCTION = "run"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"payload": ("STRING", {"multiline": True, "default": ""})},
            "hidden": {"extra_pnginfo": "EXTRA_PNGINFO"},
        }

    def run(self, payload: str, extra_pnginfo=None):
        import comfy.model_management as mm
        from comfy_execution.utils import get_executing_context
        from server import PromptServer

        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or not isinstance(data.get("prompt"), dict):
                raise ValueError("no prompt inside")
        except ValueError as exc:
            raise RuntimeError(f"Vast Worker: invalid payload ({exc})") from exc

        server = PromptServer.instance
        context = get_executing_context()
        prompt_id = context.prompt_id if context else server.last_prompt_id
        future = asyncio.run_coroutine_threadsafe(
            run_remote_job(MANAGER, data, prompt_id, server.client_id, extra_pnginfo), server.loop)
        try:
            while True:
                try:
                    outputs = future.result(timeout=0.25)
                    break
                except concurrent.futures.TimeoutError:
                    if mm.processing_interrupted():
                        future.cancel()
                        raise mm.InterruptProcessingException() from None
        except (Cancelled, RemoteInterrupted):
            raise mm.InterruptProcessingException() from None
        except concurrent.futures.CancelledError:
            raise mm.InterruptProcessingException() from None
        except (WorkerError, RemoteExecutionError) as exc:
            MANAGER.log(str(exc).splitlines()[0], "error")
            raise RuntimeError(f"Vast Worker: {exc}") from None
        except RemoteError as exc:
            MANAGER.log(f"Lost contact with the instance: {exc}", "error")
            raise RuntimeError(f"Vast Worker: lost contact with the instance: {exc}") from None
        except Exception as exc:
            MANAGER.log(f"{type(exc).__name__}: {exc}", "error")
            raise RuntimeError(f"Vast Worker: {type(exc).__name__}: {exc}") from exc
        return {"ui": outputs}


NODE_CLASS_MAPPINGS = {PROXY_CLASS: VastRemoteExecute}
NODE_DISPLAY_NAME_MAPPINGS = {PROXY_CLASS: "Vast Remote Execute (internal)"}
