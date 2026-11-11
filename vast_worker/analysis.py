"""Works out what a prompt needs on the remote side.

Given an API-format prompt (and, when available, the editor workflow that
produced it) this finds the model files it references, the node classes it
uses and the local input files that must be uploaded first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .library import ModelLibrary, _canonical_folder, local_filename_list, probe_model_inputs
from .util import is_model_filename

# Fallback when a node's inputs cannot be probed: conventional input names.
INPUT_NAME_FOLDERS = {
    "ckpt_name": "checkpoints",
    "lora_name": "loras",
    "vae_name": "vae",
    "unet_name": "diffusion_models",
    "clip_name": "text_encoders",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "clip_name3": "text_encoders",
    "clip_name4": "text_encoders",
    "control_net_name": "controlnet",
    "style_model_name": "style_models",
    "gligen_name": "gligen",
    "hypernetwork_name": "hypernetworks",
    "photomaker_model_name": "photomaker",
}


@dataclass
class ModelRef:
    name: str
    directory: str | None
    url: str | None
    node_id: str
    class_type: str
    input_name: str

    def key(self) -> tuple[str | None, str]:
        return (self.directory, self.name)


def iter_workflow_nodes(workflow: Any) -> Iterable[dict]:
    """All nodes of an editor workflow, including those inside subgraphs."""
    if not isinstance(workflow, dict):
        return
    for node in workflow.get("nodes") or []:
        if isinstance(node, dict):
            yield node
    definitions = workflow.get("definitions")
    if isinstance(definitions, dict):
        for sub in definitions.get("subgraphs") or []:
            if isinstance(sub, dict):
                for node in sub.get("nodes") or []:
                    if isinstance(node, dict):
                        yield node


def workflow_model_hints(workflow: Any) -> dict[str, dict]:
    """Model download info embedded in a workflow: name -> {url, directory}.

    Workflow templates record this on loader nodes as ``properties.models``
    (and older ones as a top-level ``models`` list).
    """
    hints: dict[str, dict] = {}

    def take(items: Any) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name, url = item.get("name"), item.get("url")
            if isinstance(name, str) and isinstance(url, str) and url.startswith(("http://", "https://")):
                directory = item.get("directory")
                hints.setdefault(name, {
                    "url": url,
                    "directory": _canonical_folder(directory) if isinstance(directory, str) and directory else None,
                })

    if isinstance(workflow, dict):
        take(workflow.get("models"))
    for node in iter_workflow_nodes(workflow):
        props = node.get("properties")
        if isinstance(props, dict):
            take(props.get("models"))
    return hints


def _folder_holding(name: str, folders: Iterable[str]) -> str | None:
    """The model folder that has ``name`` locally, if exactly one does."""
    found = [folder for folder in folders if name in local_filename_list(folder)]
    return _canonical_folder(found[0]) if len(found) == 1 else None


def extract_model_refs(
    prompt: dict,
    workflow: Any,
    library: ModelLibrary,
    node_classes: dict | None = None,
    model_folders: Iterable[str] | None = None,
) -> list[ModelRef]:
    """Every model file the prompt references, with folder and URL if known."""
    hints = workflow_model_hints(workflow)
    if node_classes is None:
        import nodes

        node_classes = nodes.NODE_CLASS_MAPPINGS
    if model_folders is None:
        import folder_paths

        model_folders = [f for f in folder_paths.folder_names_and_paths if f not in ("custom_nodes", "configs")]
    model_folders = list(model_folders)

    refs: dict[tuple[str | None, str], ModelRef] = {}
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        probed: dict[str, str] | None = None
        for input_name, value in inputs.items():
            if not is_model_filename(value):
                continue
            if probed is None:
                node_cls = node_classes.get(class_type)
                probed = probe_model_inputs(class_type, node_cls) if node_cls is not None else {}
            hint = hints.get(value) or hints.get(os.path.basename(value)) or {}
            directory = (
                probed.get(input_name)
                or hint.get("directory")
                or INPUT_NAME_FOLDERS.get(input_name)
                or _folder_holding(value, model_folders)
            )
            entry = library.find(value, directory)
            if directory is None and entry:
                directory = entry["directory"]
            url = hint.get("url") or (entry["url"] if entry else None)
            ref = ModelRef(value, directory, url, str(node_id), class_type, str(input_name))
            refs.setdefault(ref.key(), ref)
    return list(refs.values())


def extract_class_types(prompt: dict) -> list[str]:
    return sorted({str(n["class_type"]) for n in prompt.values() if isinstance(n, dict) and n.get("class_type")})


def extract_input_files(prompt: dict, resolver: Callable[[str], tuple[str, str | None]] | None = None) -> list[dict]:
    """Local files the prompt reads from the input (or temp/output) folder.

    Covers LoadImage-style inputs, including the ``name [input]`` annotation
    and files in subfolders (for example masks saved under ``clipspace``).
    """
    import folder_paths

    resolve = resolver or folder_paths.annotated_filepath
    bases = {
        os.path.abspath(folder_paths.get_input_directory()): "input",
        os.path.abspath(folder_paths.get_temp_directory()): "temp",
        os.path.abspath(folder_paths.get_output_directory()): "output",
    }
    found: dict[str, dict] = {}
    for node in prompt.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        for value in node["inputs"].values():
            if not isinstance(value, str) or not value or len(value) > 400 or "\n" in value:
                continue
            if is_model_filename(value):
                continue
            try:
                name, base = resolve(value)
            except Exception:
                continue
            base = os.path.abspath(base or folder_paths.get_input_directory())
            kind = bases.get(base)
            if kind is None or not name or os.path.isabs(name):
                continue
            full = os.path.abspath(os.path.join(base, name))
            if not full.startswith(base + os.sep) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            subfolder, filename = os.path.split(rel)
            found.setdefault(full, {
                "local_path": full,
                "name": filename,
                "subfolder": subfolder,
                "type": kind,
            })
    return list(found.values())
