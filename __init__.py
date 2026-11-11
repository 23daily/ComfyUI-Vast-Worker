"""ComfyUI-Vast-Worker: run workflows on a rented Vast.ai GPU.

Local ComfyUI stays the editor. When "Run on Vast.ai" is on, each queued
workflow is executed on an instance this plugin rents, provisions and
destroys through the Vast.ai API.
"""

import logging

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

log = logging.getLogger("vast_worker")

try:
    from server import PromptServer

    _server = getattr(PromptServer, "instance", None)
except Exception:  # imported outside ComfyUI (tests, tooling)
    _server = None

if _server is not None:
    from .vast_worker import routes as _routes
    from .vast_worker.library import install_folder_paths_patch
    from .vast_worker.manager import MANAGER
    from .vast_worker.proxy_node import NODE_CLASS_MAPPINGS as _NODES
    from .vast_worker.proxy_node import NODE_DISPLAY_NAME_MAPPINGS as _NAMES
    from .vast_worker.proxy_node import on_prompt

    NODE_CLASS_MAPPINGS.update(_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_NAMES)

    install_folder_paths_patch(MANAGER.library, lambda: MANAGER.mode == "vast")
    _routes.register(_server.routes)
    _server.add_on_prompt_handler(on_prompt)
    MANAGER.start(_server.loop)
    log.info("[vast-worker] loaded; workflows run locally until Vast mode is switched on")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
