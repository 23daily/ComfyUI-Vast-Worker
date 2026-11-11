"""Finds the local custom node package behind a node class and describes how
the remote instance can install the same thing.

Preference order: clone the same git commit on the instance; otherwise pack
the local source into an archive and upload it.
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
from dataclasses import dataclass

from .util import MODEL_EXTENSIONS

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea", ".vscode"}
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_MODEL_FILE_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
OWN_PACKAGE = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class LocalPackage:
    name: str  # folder name, or file name for single-file nodes
    path: str
    single_file: bool


def local_package_for(class_type: str, node_classes: dict | None = None) -> LocalPackage | None:
    """The custom node package that defines ``class_type`` locally, if any."""
    import folder_paths

    if node_classes is None:
        import nodes

        node_classes = nodes.NODE_CLASS_MAPPINGS
    node_cls = node_classes.get(class_type)
    module = getattr(node_cls, "RELATIVE_PYTHON_MODULE", "") if node_cls is not None else ""
    if not module.startswith("custom_nodes."):
        return None  # core node, or unknown
    name = module.split(".", 1)[1]
    if name == OWN_PACKAGE:
        return None
    for base in folder_paths.get_folder_paths("custom_nodes"):
        folder = os.path.join(base, name)
        if os.path.isdir(folder):
            return LocalPackage(name, folder, False)
        if os.path.isfile(folder + ".py"):
            return LocalPackage(name + ".py", folder + ".py", True)
    return None


def _git(path: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def _public_url(url: str) -> str | None:
    """Turn a git remote into something an instance without SSH keys can clone."""
    url = url.strip()
    match = re.match(r"^git@([^:]+):(.+)$", url)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    match = re.match(r"^ssh://git@([^/]+)/(.+)$", url)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    if url.startswith(("https://", "http://")):
        # Never forward credentials embedded in a remote URL.
        return re.sub(r"^(https?://)[^/@]+@", r"\1", url)
    if url.startswith("file://") and os.environ.get("VAST_WORKER_ALLOW_FILE_GIT") == "1":
        return url  # tests only
    return None


def git_spec(pkg: LocalPackage) -> dict | None:
    """Install-by-clone description, or None when the package is not a clone."""
    if pkg.single_file or not os.path.exists(os.path.join(pkg.path, ".git")):
        return None
    remote = _git(pkg.path, "config", "--get", "remote.origin.url")
    commit = _git(pkg.path, "rev-parse", "HEAD")
    url = _public_url(remote) if remote else None
    if not url or not commit:
        return None
    return {"type": "git", "name": pkg.name, "url": url, "commit": commit}


def build_archive(pkg: LocalPackage, out_path: str) -> list[str]:
    """Pack a package folder into ``out_path`` (tar.gz). Returns skipped files.

    Version-control data, caches and large binaries are left out: weights a
    node pack downloads for itself are fetched again on the instance instead
    of being uploaded from a home connection.
    """
    skipped: list[str] = []
    total = 0
    with tarfile.open(out_path, "w:gz") as tar:
        for root, dirs, files in os.walk(pkg.path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for filename in sorted(files):
                full = os.path.join(root, filename)
                rel = os.path.relpath(full, pkg.path)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                size = os.path.getsize(full)
                is_model = filename.lower().endswith(MODEL_EXTENSIONS)
                if size > MAX_FILE_BYTES or (is_model and size > MAX_MODEL_FILE_BYTES):
                    skipped.append(rel)
                    continue
                total += size
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError(
                        f"custom node package {pkg.name} is too large to upload "
                        f"(over {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB of source files)")
                tar.add(full, arcname=rel, recursive=False)
    return skipped
