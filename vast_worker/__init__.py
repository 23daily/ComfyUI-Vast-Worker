"""ComfyUI-Vast-Worker controller package.

Modules here run inside the *local* ComfyUI process. The code that runs on
the rented Vast.ai instance lives in ``agent/vast_worker_agent.py``.
"""

__version__ = "0.1.0"

# Label prefix put on every instance this plugin rents, so leftovers can be found.
INSTANCE_LABEL_PREFIX = "comfyui-vast-worker"
