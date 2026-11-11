"""Runs one prompt on the rented instance and mirrors it into the local UI.

Order of work for a job:

1. make sure an instance is up (renting one if needed)
2. install custom node packages the instance lacks
3. download missing models on the instance
4. upload the local input files the prompt reads
5. queue the prompt remotely, relay progress and previews to the local
   browser, and pull every output file back into the local output folder
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid

import aiohttp

from . import civitai, config, nodesync
from .analysis import ModelRef, extract_class_types, extract_input_files, extract_model_refs
from .manager import Cancelled, InstanceManager, WorkerError
from .remote import RemoteClient, RemoteError, RemoteUnavailable
from .util import human_bytes, safe_relative_path

log = logging.getLogger("vast_worker")

# Remote events the local executor already emits itself, or that make no
# sense for a local browser.
SKIP_EVENTS = {
    "status", "execution_start", "execution_success", "execution_error",
    "execution_interrupted", "feature_flags", "logs",
}
# extra_data keys that must never leave this machine or that the remote sets itself.
DROP_EXTRA_DATA = {"auth_token_comfy_org", "api_key_comfy_org", "client_id", "create_time", "workflow_metadata"}


class RemoteExecutionError(Exception):
    """A node failed on the instance."""


class RemoteInterrupted(Exception):
    """The remote run was interrupted."""


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------

async def run_remote_job(manager: InstanceManager, payload: dict, prompt_id: str,
                         sid: str | None, extra_pnginfo: dict | None) -> dict:
    """Execute ``payload["prompt"]`` on Vast.ai; returns merged UI outputs."""
    manager.begin_job()
    try:
        return await _run(manager, payload, prompt_id, sid, extra_pnginfo)
    finally:
        manager.end_job()


async def _run(manager: InstanceManager, payload: dict, prompt_id: str,
               sid: str | None, extra_pnginfo: dict | None) -> dict:
    prompt = payload["prompt"]
    workflow = (extra_pnginfo or {}).get("workflow") if isinstance(extra_pnginfo, dict) else None

    manager.set_activity("preparing", "Analysing the workflow…")
    class_types = extract_class_types(prompt)
    model_refs = extract_model_refs(prompt, workflow, manager.library)
    input_files = extract_input_files(prompt)

    manager.set_activity("preparing", "Getting a machine ready…")
    remote = await manager.ensure_ready()
    manager.set_activity("preparing", "Checking what the instance already has…")

    known = [r for r in model_refs if r.directory]
    for ref in model_refs:
        if not ref.directory:
            manager.log(
                f"Could not tell which model folder '{ref.name}' belongs to "
                f"({ref.class_type}.{ref.input_name}); assuming it is already on the instance.", "warn")

    check = await remote.agent_check([{"directory": r.directory, "name": r.name} for r in known], class_types)
    if check.get("missing_class_types"):
        await _sync_nodes(manager, remote, check["missing_class_types"])
        check = await remote.agent_check([{"directory": r.directory, "name": r.name} for r in known], class_types)
        if check.get("missing_class_types"):
            raise WorkerError(
                "The instance still lacks these node types after installing custom nodes: "
                + ", ".join(check["missing_class_types"]))

    missing = {(m["directory"], m["name"]) for m in check.get("missing_models", [])}
    needed = [r for r in known if (r.directory, r.name) in missing]
    if needed:
        await _sync_models(manager, remote, needed)

    if input_files:
        await _upload_inputs(manager, remote, input_files)

    manager.set_activity("running", "Running on Vast.ai…")
    extra_data = {}
    if isinstance(extra_pnginfo, dict):
        extra_data["extra_pnginfo"] = extra_pnginfo
    for key, value in (payload.get("extra_data") or {}).items():
        if key not in DROP_EXTRA_DATA and key != "extra_pnginfo":
            extra_data[key] = value
    outputs = await _execute(manager, remote, prompt, prompt_id, sid, extra_data,
                             payload.get("partial_execution_targets"))
    manager.touch()
    return outputs


# ----------------------------------------------------------------------
# agent jobs
# ----------------------------------------------------------------------

async def _wait_job(manager: InstanceManager, remote: RemoteClient, job_id: str, phase: str, title: str) -> dict:
    """Poll an agent job to completion, mirroring its progress to the UI."""
    failures = 0
    while True:
        try:
            job = await remote.agent_job(job_id)
            failures = 0
        except RemoteUnavailable:
            failures += 1
            if failures > 10:
                raise
            await asyncio.sleep(2)
            continue
        items = job.get("items", [])
        done = sum(1 for i in items if i.get("state") in ("done", "exists"))
        total_bytes = sum(i.get("total") or 0 for i in items)
        got_bytes = sum(i.get("bytes") or 0 for i in items)
        detail = f"{title} ({done}/{len(items)})"
        if total_bytes:
            detail += f" — {human_bytes(got_bytes)} of {human_bytes(total_bytes)}"
        log_lines = [ln for ln in job.get("log", []) if ln.strip()]
        manager.set_activity(phase, detail, items, log_lines[-1][:200] if log_lines else "")
        if job.get("state") in ("done", "error"):
            return job
        await asyncio.sleep(1.0)


def _local_model_file(ref: ModelRef) -> str | None:
    """Path of the model on this computer, if it is here."""
    try:
        import folder_paths

        path = folder_paths.get_full_path(ref.directory, ref.name)
    except Exception:
        return None
    return path if path and os.path.isfile(path) else None


async def _upload_files(manager: InstanceManager, remote: RemoteClient, title: str, jobs: list[dict]) -> None:
    """Upload files one after another, showing per-file progress in the sidebar.

    Each job is ``{name, local_path, kind, path, directory}``.
    """
    items = [{"name": j["name"], "bytes": 0, "total": os.path.getsize(j["local_path"]),
              "state": "queued", "error": None} for j in jobs]
    for index, (job, item) in enumerate(zip(jobs, items), 1):
        item["state"] = "uploading"

        def progress(sent: int, item: dict = item, index: int = index) -> None:
            item["bytes"] = sent
            manager.touch()
            manager.set_activity("uploading", f"{title} ({index}/{len(jobs)})", items)

        progress(0)
        await remote.upload(job["local_path"], job["kind"], job["path"], job.get("directory", ""), progress)
        item["state"] = "done"
    manager.set_activity("uploading", f"{title} ({len(jobs)}/{len(jobs)})", items)


async def _upload_models(manager: InstanceManager, remote: RemoteClient, uploads: list[tuple[ModelRef, str]]) -> None:
    total = sum(os.path.getsize(path) for _, path in uploads)
    free = (await remote.agent_ping()).get("disk_free_bytes")
    if isinstance(free, (int, float)) and total + (1 << 30) > free:
        raise WorkerError(
            f"Not enough disk space on the instance to upload these models: {human_bytes(total)} needed, "
            f"{human_bytes(free)} free. The disk size of a Vast.ai instance cannot be changed: destroy this "
            "instance, raise \"Disk (GB)\" under Machine setup, and rent again.")
    manager.log(f"Uploading from this computer ({human_bytes(total)}): " + ", ".join(r.name for r, _ in uploads))
    await _upload_files(manager, remote, "Uploading models to Vast.ai", [
        {"name": ref.name, "local_path": path, "kind": "model", "path": ref.name, "directory": ref.directory}
        for ref, path in uploads])


async def _sync_models(manager: InstanceManager, remote: RemoteClient, needed: list[ModelRef]) -> None:
    """Get every missing model onto the instance, downloading wherever possible.

    Order of preference for each model:

    1. a known link (from the workflow or the model library): download there
    2. no link, but the file is here and Civitai recognises it: download there
    3. the file is here: upload it (subject to the upload settings)
    4. otherwise ask the user for a link

    A download that fails falls back to uploading the local copy, if there is one.
    """
    policy = config.load_settings()["upload"]
    limit = policy["confirm_over_mb"] * 1024 * 1024
    local_files = {ref.key(): _local_model_file(ref) for ref in needed}
    found_on_civitai: set = set()

    lookups = [r for r in needed if not r.url and local_files[r.key()]] if policy["civitai_lookup"] else []
    if lookups:
        manager.set_activity("preparing", "Looking for your local models on Civitai…")
        for ref in lookups:
            try:
                found = await civitai.find_download(local_files[ref.key()], config.get_secret("civitai_token"))
            except Exception as exc:
                manager.log(f"Could not ask Civitai about {ref.name}: {exc}", "warn")
                continue
            if found:
                ref.url = found["url"]
                found_on_civitai.add(ref.key())
                manager.log(f"{ref.name} is on Civitai ({found['title']}): downloading it on the instance instead of uploading.")

    def auto_upload_ok(local: str | None) -> bool:
        return bool(local) and policy["local_models"] == "auto" and (limit == 0 or os.path.getsize(local) <= limit)

    downloads = [r for r in needed if r.url]
    uploads: list[tuple[ModelRef, str]] = []
    questions: list[tuple[ModelRef, str | None]] = []
    for ref in needed:
        if ref.url:
            continue
        local = local_files[ref.key()] if policy["local_models"] != "off" else None
        if auto_upload_ok(local):
            uploads.append((ref, local))
        else:
            questions.append((ref, local))

    if questions:
        manager.set_activity("confirming", "Waiting for your answer about missing models…")
        answer = await manager.request_confirmation({
            "kind": "models",
            "models": [{"name": ref.name, "directory": ref.directory, "node": ref.class_type,
                        "local_size": os.path.getsize(local) if local else None}
                       for ref, local in questions],
        })
        replies = {(str(m.get("directory")), str(m.get("name"))): m
                   for m in answer.get("models", []) if isinstance(m, dict)}
        for ref, local in questions:
            reply = replies.get((str(ref.directory), ref.name)) or {}
            url = str(reply.get("url") or "").strip()
            if url:
                try:
                    manager.library.add(ref.name, ref.directory or "", url)
                except ValueError as exc:
                    raise WorkerError(f"{ref.name}: {exc}") from exc
                ref.url = url
                downloads.append(ref)
            elif reply.get("upload") and local:
                uploads.append((ref, local))
            else:
                raise WorkerError(f"No download link was given for model '{ref.name}'.")

    for ref in downloads:  # remember links learned from the workflow or from Civitai
        if ref.directory and manager.library.find(ref.name, ref.directory) is None:
            try:
                manager.library.add(ref.name, ref.directory, ref.url,
                                    source="civitai" if ref.key() in found_on_civitai else "workflow")
            except ValueError:
                pass

    job_id = None
    if downloads:
        manager.log("Downloading on the instance: " + ", ".join(r.name for r in downloads))
        job_id = await remote.agent_download(
            [{"directory": r.directory, "name": r.name, "url": r.url} for r in downloads],
            config.get_secret("hf_token"), config.get_secret("civitai_token"))
    if uploads:
        await _upload_models(manager, remote, uploads)
    if not job_id:
        return
    job = await _wait_job(manager, remote, job_id, "downloading", "Downloading models on Vast.ai")
    if job.get("state") != "error":
        return

    # Some downloads failed. Where the file is also on this computer, upload it instead.
    failed = {i.get("name"): i.get("error") for i in job.get("items", []) if i.get("state") == "error"}
    if not failed:
        raise WorkerError(str(job.get("error")))  # the whole batch was refused, for example for disk space
    fallback = [(r, local_files[r.key()]) for r in downloads
                if r.name in failed and auto_upload_ok(local_files[r.key()])]
    rescued = {r.name for r, _ in fallback}
    lost = [name for name in failed if name not in rescued]
    if lost:
        details = "; ".join(f"{name}: {failed[name]}" for name in lost)
        hint = ""
        if any(local_files[r.key()] for r in downloads if r.name in lost):
            hint = (" A copy is on this computer, but it was not uploaded because of your upload settings "
                    "(Behaviour in the sidebar).")
        raise WorkerError("Model download failed on the instance: " + details + hint)
    for ref, _ in fallback:
        manager.log(f"Download of {ref.name} failed ({failed[ref.name]}); uploading your local copy instead.", "warn")
    await _upload_models(manager, remote, fallback)


async def _upload_package(remote: RemoteClient, pkg: nodesync.LocalPackage, manager: InstanceManager) -> dict:
    """Upload a local custom node package; returns its install description."""
    if pkg.single_file:
        await remote.upload(pkg.path, "package", pkg.name)
        return {"type": "upload", "name": pkg.name, "path": f"vast_worker/pkg/{pkg.name}", "single_file": True}
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, f"{pkg.name}.tar.gz")
        skipped = await asyncio.get_running_loop().run_in_executor(None, nodesync.build_archive, pkg, archive)
        if skipped:
            manager.log(f"{pkg.name}: {len(skipped)} large file(s) were not uploaded "
                        f"(for example {skipped[0]}); the node pack must fetch them itself.", "warn")
        await remote.upload(archive, "package", f"{pkg.name}.tar.gz")
    return {"type": "upload", "name": pkg.name, "path": f"vast_worker/pkg/{pkg.name}.tar.gz", "single_file": False}


async def _sync_nodes(manager: InstanceManager, remote: RemoteClient, missing_classes: list[str]) -> None:
    packages: dict[str, nodesync.LocalPackage] = {}
    unknown: list[str] = []
    for class_type in missing_classes:
        pkg = nodesync.local_package_for(class_type)
        if pkg is None:
            unknown.append(class_type)
        else:
            packages.setdefault(pkg.name, pkg)
    if unknown:
        raise WorkerError(
            "These node types do not exist on the instance and do not come from a local custom node package: "
            + ", ".join(unknown) + ". The instance may run a different ComfyUI version.")

    manager.set_activity("installing", "Installing custom nodes on Vast.ai…")
    manager.log("Installing custom nodes on the instance: " + ", ".join(packages))
    specs = []
    for pkg in packages.values():
        specs.append(nodesync.git_spec(pkg) or await _upload_package(remote, pkg, manager))
    job = await _wait_job(manager, remote, await remote.agent_install_nodes(specs),
                          "installing", "Installing custom nodes on Vast.ai")

    # A clone can fail for private or local-only repositories: fall back to uploading the source.
    failed = {i["name"] for i in job.get("items", []) if i.get("state") == "error"}
    retry = [packages[s["name"]] for s in specs if s["type"] == "git" and s["name"] in failed]
    if retry:
        manager.log("Clone failed for " + ", ".join(p.name for p in retry) + "; uploading the local copy instead.", "warn")
        retry_specs = [await _upload_package(remote, pkg, manager) for pkg in retry]
        job = await _wait_job(manager, remote, await remote.agent_install_nodes(retry_specs),
                              "installing", "Installing custom nodes on Vast.ai")
        failed = (failed - {p.name for p in retry}) | {
            i["name"] for i in job.get("items", []) if i.get("state") == "error"}
    if failed:
        tail = "\n".join(job.get("log", [])[-8:])
        raise WorkerError("Installing custom nodes failed on the instance: " + ", ".join(sorted(failed))
                          + (f"\n{tail}" if tail else ""))

    await _restart_remote(manager, remote)


async def _restart_remote(manager: InstanceManager, remote: RemoteClient) -> None:
    manager.set_activity("installing", "Restarting ComfyUI on the instance…")
    try:
        before = (await remote.agent_ping()).get("boot_id")
    except RemoteError:
        before = None
    await remote.agent_restart()
    deadline = time.time() + 600
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        try:
            info = await remote.agent_ping()
        except RemoteError:
            continue
        if info.get("boot_id") != before:
            return
    raise WorkerError("ComfyUI on the instance did not come back after installing custom nodes.")


async def _upload_inputs(manager: InstanceManager, remote: RemoteClient, files: list[dict]) -> None:
    pending = []
    for item in files:
        stat = os.stat(item["local_path"])
        stamp = (stat.st_mtime, stat.st_size)
        key = f"{item['type']}:{item['subfolder']}/{item['name']}"
        if manager.uploaded.get(key) != stamp:
            pending.append((key, stamp, item))
    if not pending:
        return
    await _upload_files(manager, remote, "Uploading input files to Vast.ai", [
        {"name": item["name"], "local_path": item["local_path"], "kind": item["type"],
         "path": f"{item['subfolder']}/{item['name']}" if item["subfolder"] else item["name"]}
        for _, _, item in pending])
    for key, stamp, _ in pending:
        manager.uploaded[key] = stamp


# ----------------------------------------------------------------------
# execution and relay
# ----------------------------------------------------------------------

def _unique_name(directory: str, filename: str) -> str:
    """A file name that does not overwrite anything already in ``directory``.

    A fresh instance numbers its outputs from 1 again, so remote names
    routinely collide with files that are already here.
    """
    if not os.path.exists(os.path.join(directory, filename)):
        return filename
    stem, ext = os.path.splitext(filename)
    match = None
    for match in re.finditer(r"\d{5}", stem):
        pass
    if match is not None:
        number = int(match.group())
        for candidate in range(number + 1, number + 100000):
            name = f"{stem[:match.start()]}{candidate:05d}{stem[match.end():]}{ext}"
            if not os.path.exists(os.path.join(directory, name)):
                return name
    for index in range(1, 100000):
        name = f"{stem}_vast{index}{ext}"
        if not os.path.exists(os.path.join(directory, name)):
            return name
    return f"{stem}_{uuid.uuid4().hex[:8]}{ext}"


async def _localize_entry(remote: RemoteClient, entry: dict) -> dict:
    """Download one output file reference and point it at the local copy."""
    import folder_paths

    kind = entry.get("type", "output")
    if kind not in ("output", "temp"):
        return entry
    base = folder_paths.get_directory_by_type(kind)
    if base is None:
        return entry
    remote_name = str(entry["filename"])
    if "/" in remote_name or "\\" in remote_name or remote_name in ("", ".", ".."):
        raise WorkerError(f"The instance returned an unsafe file name: {remote_name!r}")
    subfolder = safe_relative_path(str(entry.get("subfolder") or ""))
    target_dir = os.path.join(base, subfolder) if subfolder else base
    os.makedirs(target_dir, exist_ok=True)
    local_name = _unique_name(target_dir, remote_name) if kind == "output" else remote_name
    await remote.download_view(remote_name, str(entry.get("subfolder") or ""), kind, target_dir, local_name)
    localized = dict(entry)
    localized["filename"] = local_name
    return localized


async def _localize_output(remote: RemoteClient, output: dict) -> dict:
    result = {}
    for key, value in output.items():
        if isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("filename"), str):
                    item = await _localize_entry(remote, item)
                items.append(item)
            result[key] = items
        else:
            result[key] = value
    return result


def _describe_validation_error(body) -> str:
    if not isinstance(body, dict):
        return "The instance rejected the workflow."
    lines = []
    error = body.get("error")
    if isinstance(error, dict):
        lines.append(str(error.get("message") or error.get("type") or "invalid prompt"))
    elif error:
        lines.append(str(error))
    for node_id, info in (body.get("node_errors") or {}).items():
        if not isinstance(info, dict):
            continue
        for err in info.get("errors", []):
            lines.append(f"{info.get('class_type', '?')} (#{node_id}): {err.get('message', '')} {err.get('details', '')}".strip())
    text = "\n".join(lines) or "The instance rejected the workflow."
    if "not in list" in text.lower():
        text += ("\nIf that value is a model file, add it to the remote model library "
                 "in the Vast Worker sidebar so it can be downloaded on the instance.")
    return "Remote validation failed:\n" + text


async def _cancel_remote(remote: RemoteClient, prompt_id: str) -> None:
    for call in (remote.delete_queued(prompt_id), remote.interrupt(prompt_id)):
        try:
            await call
        except Exception:
            pass


async def _execute(manager: InstanceManager, remote: RemoteClient, prompt: dict, prompt_id: str,
                   sid: str | None, extra_data: dict, partial_targets) -> dict:
    server = manager._server()
    client_id = uuid.uuid4().hex
    flags = {}
    if server is not None and sid:
        flags = dict(server.sockets_metadata.get(sid, {}).get("feature_flags", {}) or {})
    merged: dict[str, list] = {}

    try:
        ws_ctx = remote.ws_connect(client_id)
        ws = await ws_ctx.__aenter__()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RemoteUnavailable(f"could not open a connection to the instance: {exc}") from exc
    try:
        await ws.send_json({"type": "feature_flags", "data": flags})
        body = {"prompt": prompt, "client_id": client_id, "prompt_id": prompt_id, "extra_data": extra_data}
        if partial_targets:
            body["partial_execution_targets"] = partial_targets
        try:
            response = await remote.queue_prompt(body)
        except RemoteError as exc:
            if exc.status == 400:
                raise WorkerError(_describe_validation_error(exc.body)) from exc
            raise
        remote_id = response.get("prompt_id", prompt_id)

        try:
            async for msg in ws:
                manager.touch()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if server is not None and sid and len(msg.data) >= 4:
                        event = int.from_bytes(msg.data[:4], "big")
                        await server.send_bytes(event, msg.data[4:], sid)
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    message = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                kind = message.get("type")
                data = message.get("data")
                if isinstance(data, dict) and data.get("prompt_id") not in (None, remote_id):
                    continue
                if kind == "executed" and isinstance(data, dict):
                    if isinstance(data.get("output"), dict):
                        data["output"] = await _localize_output(remote, data["output"])
                        for key, value in data["output"].items():
                            if isinstance(value, list):
                                merged.setdefault(key, []).extend(value)
                elif kind == "execution_success":
                    return merged
                elif kind == "execution_error":
                    data = data if isinstance(data, dict) else {}
                    trace = data.get("traceback")
                    trace_text = "".join(trace) if isinstance(trace, list) else str(trace or "")
                    raise RemoteExecutionError(
                        f"{data.get('node_type', 'node')} (#{data.get('node_id', '?')}) failed on the instance: "
                        f"{data.get('exception_type', 'Error')}: {data.get('exception_message', '')}".strip()
                        + (f"\n\nRemote traceback:\n{trace_text}" if trace_text else ""))
                elif kind == "execution_interrupted":
                    raise RemoteInterrupted()
                elif kind == "executing" and isinstance(data, dict) and data.get("node") is None:
                    return merged  # older servers signal the end this way
                if kind in SKIP_EVENTS or server is None or not sid:
                    continue
                server.send_sync(kind, data, sid)
        except asyncio.CancelledError:
            await asyncio.shield(_cancel_remote(remote, remote_id))
            raise
        raise RemoteUnavailable("The connection to the instance was lost while the workflow was running.")
    finally:
        try:
            await ws_ctx.__aexit__(None, None, None)
        except Exception:
            pass


__all__ = ["run_remote_job", "RemoteExecutionError", "RemoteInterrupted", "Cancelled", "WorkerError"]
