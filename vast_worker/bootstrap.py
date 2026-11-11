"""Builds the one-off provisioning manifest that installs the worker agent.

Vast's base image exposes ``POST /capabilities/provision`` on the Instance
Portal. It accepts an inline provisioner manifest, which can write files and
run commands. We use it once per instance to drop the single-file agent into
the remote ``custom_nodes`` folder and restart ComfyUI. After that everything
goes through the agent's own routes.

The manifest is emitted as JSON, which is valid YAML, so no YAML library is
needed. The agent source travels base64-encoded because the provisioner
expands ``$VAR`` references in every manifest string.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

AGENT_FILENAME = "vast_worker_agent.py"
_AGENT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent", AGENT_FILENAME)

WORKSPACE = "${WORKSPACE:-/workspace}"


def agent_source() -> bytes:
    with open(_AGENT_PATH, "rb") as f:
        return f.read()


def agent_version() -> str:
    """Content hash of the agent; the controller re-installs when it differs."""
    return hashlib.sha256(agent_source()).hexdigest()[:16]


def build_bootstrap_manifest() -> str:
    payload = base64.b64encode(agent_source()).decode("ascii")
    staged = f"{WORKSPACE}/.vast_worker/agent.b64"
    target = f"{WORKSPACE}/ComfyUI/custom_nodes/{AGENT_FILENAME}"
    # The leading no-op makes every bootstrap unique, so the provisioner's
    # "same commands as last time, skip" check never swallows a re-install.
    nonce = f": vast-worker bootstrap {agent_version()} {int(time.time())}"
    manifest = {
        "version": 1,
        "write_files": [
            {"path": staged, "content": payload, "permissions": "0600"},
        ],
        "post_commands": [
            # Reads from stdin so it works with both GNU and BSD base64.
            f'{nonce}; base64 -d < "{staged}" > "{target}"',
            "supervisorctl restart comfyui",
        ],
    }
    return json.dumps(manifest)
