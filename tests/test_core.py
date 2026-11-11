"""Unit tests for the controller modules that need no running ComfyUI.

    python -m unittest discover -s tests -t .
"""

import base64
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vast_worker import bootstrap, config, nodesync, profile, util, vast_api  # noqa: E402
from vast_worker.analysis import extract_class_types, extract_model_refs, workflow_model_hints  # noqa: E402
from vast_worker.executor import _describe_validation_error, _unique_name  # noqa: E402
from vast_worker.library import ModelLibrary, filename_from_url  # noqa: E402


class DataDirCase(unittest.TestCase):
    """Gives each test its own data directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = {k: os.environ.get(k) for k in
                     ("VAST_WORKER_DATA_DIR", "VAST_API_KEY", "HF_TOKEN", "CIVITAI_TOKEN", "VAST_WORKER_SETTINGS_JSON")}
        os.environ["VAST_WORKER_DATA_DIR"] = self.tmp.name
        for key in ("VAST_API_KEY", "HF_TOKEN", "CIVITAI_TOKEN", "VAST_WORKER_SETTINGS_JSON"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class UtilTests(unittest.TestCase):
    def test_safe_join_keeps_paths_inside_base(self):
        base = os.path.abspath("/tmp/base")
        self.assertEqual(util.safe_join(base, "a/b.png"), os.path.join(base, "a", "b.png"))
        self.assertEqual(util.safe_join(base, "", "x"), os.path.join(base, "x"))
        for bad in ("../x", "a/../../x", "/etc/passwd", "C:\\windows", "a\\..\\..\\x"):
            with self.assertRaises(ValueError, msg=bad):
                util.safe_join(base, bad)

    def test_is_model_filename(self):
        self.assertTrue(util.is_model_filename("sdxl/base.safetensors"))
        self.assertTrue(util.is_model_filename("model.GGUF"))
        self.assertFalse(util.is_model_filename("a photo of a cat"))
        self.assertFalse(util.is_model_filename("multi\nline.safetensors"))
        self.assertFalse(util.is_model_filename(42))

    def test_redact_removes_secrets(self):
        self.assertEqual(util.redact("key=abcdef123 ok", "abcdef123"), "key=*** ok")
        self.assertEqual(util.redact("short ab", "ab"), "short ab")  # too short to be a secret

    def test_deep_merge_does_not_mutate(self):
        base = {"a": {"b": 1, "c": 2}}
        merged = util.deep_merge(base, {"a": {"b": 5}})
        self.assertEqual(merged, {"a": {"b": 5, "c": 2}})
        self.assertEqual(base["a"]["b"], 1)


class ConfigTests(DataDirCase):
    def test_settings_are_sanitized(self):
        saved = config.save_settings({
            "gpu": {"min_vram_gb": "48", "max_dph": -3, "gpu_names": "RTX 4090, RTX 5090", "bogus": 1,
                    "exclude_countries": ["cn", "ru"], "min_reliability": 7},
            "lifecycle": {"idle_minutes": 0, "confirm_rental": "false", "max_rent_attempts": 99},
            "connection": {"scheme": "ftp"},
            "unknown_section": {"x": 1},
        })
        self.assertEqual(saved["gpu"]["min_vram_gb"], 48)
        self.assertEqual(saved["gpu"]["max_dph"], 0.01)
        self.assertEqual(saved["gpu"]["gpu_names"], ["RTX 4090", "RTX 5090"])
        self.assertEqual(saved["gpu"]["exclude_countries"], ["CN", "RU"])
        self.assertEqual(saved["gpu"]["min_reliability"], 1.0)
        self.assertNotIn("bogus", saved["gpu"])
        self.assertEqual(saved["lifecycle"]["idle_minutes"], 1)
        self.assertIs(saved["lifecycle"]["confirm_rental"], False)
        self.assertEqual(saved["lifecycle"]["max_rent_attempts"], 5)
        self.assertEqual(saved["connection"]["scheme"], "https")
        self.assertNotIn("unknown_section", saved)
        self.assertEqual(config.load_settings(), saved)

    def test_upload_and_country_settings(self):
        defaults = config.load_settings()
        self.assertEqual(defaults["upload"], {"local_models": "auto", "confirm_over_mb": 2000, "civitai_lookup": True})
        self.assertEqual(defaults["gpu"]["countries"], [])
        saved = config.save_settings({
            "upload": {"local_models": "sometimes", "confirm_over_mb": -5},
            "gpu": {"countries": "jp, kr ,jp", "gpu_names": [" RTX 4090 ", ""]},
        })
        self.assertEqual(saved["upload"], {"local_models": "auto", "confirm_over_mb": 0, "civitai_lookup": True})
        self.assertEqual(saved["gpu"]["countries"], ["JP", "KR"])
        self.assertEqual(saved["gpu"]["gpu_names"], ["RTX 4090"])
        self.assertEqual(config.save_settings({"upload": {"local_models": "off"}})["upload"]["local_models"], "off")

    def test_secrets_are_private_and_never_listed(self):
        self.assertEqual(config.secrets_status()["vast_api_key"], None)
        config.set_secrets({"vast_api_key": " abc123 ", "not_a_secret": "x"})
        self.assertEqual(config.get_secret("vast_api_key"), "abc123")
        self.assertEqual(config.secrets_status(), {"vast_api_key": "stored", "hf_token": None, "civitai_token": None})
        mode = stat.S_IMODE(os.stat(os.path.join(self.tmp.name, "secrets.json")).st_mode)
        self.assertEqual(mode, 0o600)
        config.set_secrets({"vast_api_key": ""})
        self.assertIsNone(config.get_secret("vast_api_key"))

    def test_environment_is_a_fallback_for_secrets(self):
        os.environ["HF_TOKEN"] = "hf_env"
        self.assertEqual(config.get_secret("hf_token"), "hf_env")
        self.assertEqual(config.secrets_status()["hf_token"], "env")
        config.set_secrets({"hf_token": "hf_stored"})
        self.assertEqual(config.get_secret("hf_token"), "hf_stored")

    def test_state_round_trip(self):
        self.assertEqual(config.load_state(), {})
        config.save_state({"instance": {"id": 7, "token": "t"}})
        self.assertEqual(config.load_state()["instance"]["id"], 7)


class VastApiHelperTests(unittest.TestCase):
    def test_offer_query(self):
        gpu = dict(config.DEFAULT_SETTINGS["gpu"], gpu_names=["RTX 4090"], max_dph=0.5, min_vram_gb=24)
        query = vast_api.build_offer_query(gpu, 80)
        self.assertEqual(query["gpu_ram"], {"gte": 24000.0})
        self.assertEqual(query["dph_total"], {"lte": 0.5})
        self.assertNotIn("gpu_name", query)  # names are matched locally, so partial names work
        self.assertGreaterEqual(query["limit"], 1000)
        self.assertEqual(query["geolocation"], {"notin": ["CN"]})
        self.assertEqual(query["verified"], {"eq": True})
        self.assertEqual(query["type"], "ondemand")
        self.assertEqual(query["allocated_storage"], 80.0)
        self.assertEqual(query["compute_cap"], {"gte": 750})  # no Pascal-era cards by default
        open_query = vast_api.build_offer_query(
            dict(gpu, gpu_names=[], exclude_countries=[], verified_only=False, min_compute_cap=0), 80)
        self.assertEqual(open_query["limit"], 20)
        for key in ("geolocation", "verified", "compute_cap"):
            self.assertNotIn(key, open_query)

    def test_gpu_filter_is_exact_for_full_names_and_partial_otherwise(self):
        offers = [{"gpu_name": n} for n in
                  ("RTX 4060", "RTX 4060 Ti", "RTX 4090", "RTX 4090D", "RTX 3060", "RTX 3060 Ti", "Tesla P40")]
        names = lambda patterns: [o["gpu_name"] for o in vast_api.filter_by_gpu(offers, patterns)]  # noqa: E731
        self.assertEqual(names(["4060"]), ["RTX 4060", "RTX 4060 Ti"])
        self.assertEqual(names(["4060ti"]), ["RTX 4060 Ti"])
        self.assertEqual(names(["rtx 4060 TI"]), ["RTX 4060 Ti"])
        self.assertEqual(names(["RTX 4090"]), ["RTX 4090"])          # a full name does not drag in the 4090D
        self.assertEqual(names(["RTX 3060"]), ["RTX 3060"])
        self.assertEqual(names(["40"]), ["RTX 4060", "RTX 4060 Ti", "RTX 4090", "RTX 4090D", "Tesla P40"])
        self.assertEqual(names(["RTX 4090", "3060"]), ["RTX 4090", "RTX 3060", "RTX 3060 Ti"])
        self.assertEqual(names(["nothing"]), [])
        self.assertEqual(len(names([])), 7)
        self.assertEqual(len(names(["  "])), 7)

    def test_sort_orders(self):
        offers = [{"id": 1, "dph_total": 0.3, "dlperf": 50, "dlperf_per_dphtotal": 166},
                  {"id": 2, "dph_total": 0.1, "dlperf": 10, "dlperf_per_dphtotal": 100},
                  {"id": 3, "dph_total": 0.2, "dlperf": 90, "dlperf_per_dphtotal": 450}]
        order = lambda key: [o["id"] for o in sorted(offers, key=vast_api.SORTS[key])]  # noqa: E731
        self.assertEqual(order("price"), [2, 3, 1])
        self.assertEqual(order("performance"), [3, 1, 2])
        self.assertEqual(order("value"), [3, 1, 2])

    def test_country_conditions(self):
        gpu = dict(config.DEFAULT_SETTINGS["gpu"])
        allow = vast_api.build_offer_query(dict(gpu, countries=["JP", "KR"], exclude_countries=[]), 80)
        self.assertEqual(allow["geolocation"], {"in": ["JP", "KR"]})
        both = vast_api.build_offer_query(dict(gpu, countries=["JP", "KR", "CN"], exclude_countries=["CN"]), 80)
        self.assertEqual(both["geolocation"], {"in": ["JP", "KR"]})  # a blocked country never gets through
        block = vast_api.build_offer_query(dict(gpu, countries=[], exclude_countries=["CN", "RU"]), 80)
        self.assertEqual(block["geolocation"], {"notin": ["CN", "RU"]})
        self.assertEqual(vast_api.country_code("Texas, US"), "US")
        self.assertEqual(vast_api.country_code("Japan, JP"), "JP")
        self.assertEqual(vast_api.country_code("somewhere"), "")
        self.assertEqual(vast_api.country_code(None), "")

    def test_market_summary(self):
        offers = [
            {"gpu_name": "RTX 3090", "gpu_ram": 24576, "dph_total": 0.30, "geolocation": "Texas, US", "compute_cap": 860},
            {"gpu_name": "RTX 3090", "gpu_ram": 24576, "dph_total": 0.19, "geolocation": "Japan, JP", "compute_cap": 860},
            {"gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.40, "geolocation": "Ohio, US", "compute_cap": 890},
            {"gpu_name": "", "dph_total": 1, "geolocation": ""},
        ]
        market = vast_api.summarize_market(offers)
        self.assertEqual([g["name"] for g in market["gpus"]], ["RTX 3090", "RTX 4090"])
        self.assertEqual(market["gpus"][0], {"name": "RTX 3090", "offers": 2, "min_dph": 0.19, "vram_gb": 24, "compute_cap": 860})
        self.assertEqual([(c["code"], c["offers"]) for c in market["countries"]], [("US", 2), ("JP", 1)])
        self.assertNotIn("gpu_ram", vast_api.build_market_query())  # broad on purpose

    def test_port_map(self):
        inst = {"ports": {"8188/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33526"}],
                          "1111/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40001"}, {"HostIp": "::", "HostPort": "40001"}],
                          "5000/udp": [{"HostPort": "1"}], "junk": [], "22/tcp": None}}
        self.assertEqual(vast_api.parse_port_map(inst), {8188: 33526, 1111: 40001})
        self.assertEqual(vast_api.parse_port_map({"ports": [8080]}), {})
        self.assertEqual(vast_api.parse_port_map({}), {})

    def test_offer_summary(self):
        summary = vast_api.summarize_offer({"id": 1, "gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.3,
                                            "reliability2": 0.99, "cpu_ram": 64000})
        self.assertEqual(summary["gpu_ram_gb"], 24.0)
        self.assertEqual(summary["reliability"], 0.99)


class ProfileTests(unittest.TestCase):
    def test_create_payload(self):
        settings = config.sanitize_settings({"instance": {"extra_env": {"FOO": "bar"}}})
        payload = profile.build_create_payload(settings, "vastai/comfy:x", "tok", "label-1")
        env = payload["env"]
        self.assertEqual(payload["image"], "vastai/comfy:x")
        self.assertEqual(payload["label"], "label-1")
        self.assertEqual(payload["runtype"], "ssh_direct")
        self.assertEqual(payload["onstart"], "entrypoint.sh")
        self.assertEqual(env["-p 8188:8188"], "1")
        self.assertEqual(env["-p 1111:1111"], "1")
        self.assertEqual(env["WEB_PASSWORD"], "tok")
        self.assertEqual(env["ENABLE_HTTPS"], "true")
        self.assertEqual(env["FOO"], "bar")
        self.assertIn("localhost:8188:18188", env["PORTAL_CONFIG"])
        # Model-hub tokens are sent per download, never baked into the instance.
        self.assertNotIn("HF_TOKEN", env)
        self.assertNotIn("CIVITAI_TOKEN", env)

    def test_http_and_args_runtype(self):
        settings = config.sanitize_settings({"connection": {"scheme": "http"}, "instance": {"runtype": "args"}})
        payload = profile.build_create_payload(settings, "img", "tok", "l")
        self.assertNotIn("ENABLE_HTTPS", payload["env"])
        self.assertNotIn("onstart", payload)

    def test_image_tag(self):
        self.assertEqual(profile.image_tag_for("0.44.0"), "vastai/comfy:v0.44.0-cuda-12.9-py312")


class BootstrapTests(unittest.TestCase):
    def test_manifest_is_valid_yaml_and_carries_the_agent(self):
        text = bootstrap.build_bootstrap_manifest()
        manifest = json.loads(text)
        try:
            import yaml

            self.assertEqual(yaml.safe_load(text), manifest)
        except ImportError:
            pass
        self.assertEqual(manifest["version"], 1)
        staged = manifest["write_files"][0]
        self.assertEqual(base64.b64decode(staged["content"]), bootstrap.agent_source())
        # The provisioner expands $VARS in every string; base64 has no "$".
        self.assertNotIn("$", staged["content"])
        self.assertIn("${WORKSPACE:-/workspace}/ComfyUI/custom_nodes/vast_worker_agent.py", manifest["post_commands"][0])
        self.assertEqual(manifest["post_commands"][-1], "supervisorctl restart comfyui")
        self.assertEqual(len(bootstrap.agent_version()), 16)


class LibraryTests(DataDirCase):
    def test_add_find_remove_and_persist(self):
        lib = ModelLibrary()
        lib.add("flux/ae.safetensors", "vae", "https://example.com/ae.safetensors")
        lib.add("x.safetensors", "unet", "https://example.com/x")
        self.assertEqual(lib.find("x.safetensors")["directory"], "diffusion_models")  # legacy name folded
        self.assertEqual(lib.names_for("unet"), ["x.safetensors"])
        self.assertEqual(lib.names_for("vae"), ["flux/ae.safetensors"])
        self.assertEqual(len(ModelLibrary().list()), 2)  # a second instance reads the same file
        self.assertTrue(lib.remove("vae", "flux/ae.safetensors"))
        self.assertFalse(lib.remove("vae", "flux/ae.safetensors"))

    def test_rejects_bad_entries(self):
        lib = ModelLibrary()
        for args in (("../evil.safetensors", "loras", "https://e.com/x"),
                     ("a.safetensors", "", "https://e.com/x"),
                     ("a.safetensors", "loras", "ftp://e.com/x"),
                     ("", "loras", "https://e.com/x")):
            with self.assertRaises(ValueError, msg=str(args)):
                lib.add(*args)

    def test_ambiguous_name_needs_a_folder(self):
        lib = ModelLibrary()
        lib.add("same.safetensors", "loras", "https://e.com/1")
        lib.add("same.safetensors", "checkpoints", "https://e.com/2")
        self.assertIsNone(lib.find("same.safetensors"))
        self.assertEqual(lib.find("same.safetensors", "loras")["url"], "https://e.com/1")

    def test_filename_from_url(self):
        self.assertEqual(filename_from_url("https://huggingface.co/a/b/resolve/main/my%20model.safetensors?download=true"),
                         "my model.safetensors")
        self.assertEqual(filename_from_url("https://civitai.com/api/download/models/12345"), "")


class AnalysisTests(DataDirCase):
    def test_workflow_hints_cover_nodes_subgraphs_and_legacy_list(self):
        workflow = {
            "models": [{"name": "top.safetensors", "url": "https://e.com/top", "directory": "checkpoints"}],
            "nodes": [{"properties": {"models": [{"name": "a.safetensors", "url": "https://e.com/a", "directory": "unet"},
                                                 {"name": "nourl.safetensors"}, "junk"]}}, "junk"],
            "definitions": {"subgraphs": [{"nodes": [{"properties": {"models": [
                {"name": "sub.safetensors", "url": "https://e.com/sub", "directory": "loras"}]}}]}]},
        }
        hints = workflow_model_hints(workflow)
        self.assertEqual(set(hints), {"top.safetensors", "a.safetensors", "sub.safetensors"})
        self.assertEqual(hints["a.safetensors"]["directory"], "diffusion_models")
        self.assertEqual(workflow_model_hints(None), {})

    def test_model_refs(self):
        lib = ModelLibrary()
        lib.add("lib.safetensors", "loras", "https://e.com/lib")
        prompt = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd.safetensors"}},
            "2": {"class_type": "LoraLoader", "inputs": {"lora_name": "lib.safetensors", "model": ["1", 0], "strength_model": 1.0}},
            "3": {"class_type": "Mystery", "inputs": {"weights": "odd.pt", "text": "a cat.png in the sun"}},
            "4": {"class_type": "LoraLoader", "inputs": {"lora_name": "lib.safetensors"}},  # duplicate
            "5": {"class_type": "Hinted", "inputs": {"thing": "hinted.gguf"}},
        }
        workflow = {"nodes": [{"properties": {"models": [
            {"name": "hinted.gguf", "url": "https://e.com/h", "directory": "text_encoders"}]}}]}
        refs = {r.name: r for r in extract_model_refs(prompt, workflow, lib, node_classes={}, model_folders=[])}
        self.assertEqual(set(refs), {"sd.safetensors", "lib.safetensors", "odd.pt", "hinted.gguf"})
        self.assertEqual((refs["sd.safetensors"].directory, refs["sd.safetensors"].url), ("checkpoints", None))
        self.assertEqual((refs["lib.safetensors"].directory, refs["lib.safetensors"].url), ("loras", "https://e.com/lib"))
        self.assertEqual((refs["odd.pt"].directory, refs["odd.pt"].url), (None, None))
        self.assertEqual((refs["hinted.gguf"].directory, refs["hinted.gguf"].url), ("text_encoders", "https://e.com/h"))

    def test_class_types(self):
        self.assertEqual(extract_class_types({"1": {"class_type": "B"}, "2": {"class_type": "A"}, "3": {"class_type": "B"}, "x": 1}),
                         ["A", "B"])


class ProbeTests(DataDirCase):
    """The folder_paths patch: dropdown merge and input-to-folder probing."""

    def setUp(self):
        super().setUp()
        from vast_worker import library

        self.library_module = library
        self.fake = types.ModuleType("folder_paths")
        self.fake.get_filename_list = lambda folder: {"loras": ["local.safetensors"]}.get(folder, [])
        self._saved = sys.modules.get("folder_paths")
        sys.modules["folder_paths"] = self.fake
        library._original_get_filename_list = None
        library._probe_cache.clear()

    def tearDown(self):
        self.library_module._original_get_filename_list = None
        self.library_module._probe_cache.clear()
        if self._saved is None:
            sys.modules.pop("folder_paths", None)
        else:
            sys.modules["folder_paths"] = self._saved
        super().tearDown()

    def test_merge_only_while_enabled_and_probe(self):
        lib = ModelLibrary()
        lib.add("remote.safetensors", "loras", "https://e.com/r")
        lib.add("local.safetensors", "loras", "https://e.com/l")
        switch = {"on": False}
        self.library_module.install_folder_paths_patch(lib, lambda: switch["on"])
        self.assertEqual(self.fake.get_filename_list("loras"), ["local.safetensors"])
        switch["on"] = True
        self.assertEqual(self.fake.get_filename_list("loras"), ["local.safetensors", "remote.safetensors"])
        self.assertEqual(self.library_module.local_filename_list("loras"), ["local.safetensors"])

        fake = self.fake

        class Loader:
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"lora_name": (fake.get_filename_list("loras"),), "strength": ("FLOAT", {})},
                        "optional": {"v3_style": ("COMBO", {"options": fake.get_filename_list("vae")})}}

        probed = self.library_module.probe_model_inputs("Loader", Loader)
        self.assertEqual(probed, {"lora_name": "loras", "v3_style": "vae"})
        # Probing must not leak into normal calls afterwards.
        self.assertEqual(self.fake.get_filename_list("loras"), ["local.safetensors", "remote.safetensors"])

        refs = extract_model_refs({"1": {"class_type": "Loader", "inputs": {"lora_name": "remote.safetensors"}}},
                                  None, lib, node_classes={"Loader": Loader}, model_folders=["loras"])
        self.assertEqual((refs[0].directory, refs[0].url), ("loras", "https://e.com/r"))


class CivitaiTests(DataDirCase):
    def test_hash_is_cached_until_the_file_changes(self):
        import hashlib

        from vast_worker import civitai

        path = os.path.join(self.tmp.name, "model.safetensors")
        with open(path, "wb") as f:
            f.write(b"first contents")
        first = civitai.file_sha256(path)
        self.assertEqual(first, hashlib.sha256(b"first contents").hexdigest())
        cache = config._read_json("hashes.json", {})
        self.assertEqual(cache[path]["sha256"], first)
        cache[path]["sha256"] = "from-cache"          # prove the second call does not re-read the file
        config._write_json("hashes.json", cache)
        self.assertEqual(civitai.file_sha256(path), "from-cache")
        with open(path, "wb") as f:
            f.write(b"different contents, different size")
        self.assertEqual(civitai.file_sha256(path), hashlib.sha256(b"different contents, different size").hexdigest())

    def test_picks_the_link_of_the_matching_file(self):
        from vast_worker import civitai

        sha = "ab" * 32
        data = {"id": 396118, "name": "anime s1", "model": {"name": "Some LoRA"},
                "downloadUrl": "https://civitai.com/api/download/models/396118",
                "files": [{"hashes": {"SHA256": "00" * 32}, "downloadUrl": "https://civitai.com/api/download/models/396118?fileId=1"},
                          {"hashes": {"SHA256": sha.upper()}, "downloadUrl": "https://civitai.com/api/download/models/396118?fileId=2"}]}
        found = civitai.pick_download(data, sha)
        self.assertEqual(found["url"], "https://civitai.com/api/download/models/396118?fileId=2")
        self.assertEqual(found["title"], "Some LoRA — anime s1")
        self.assertEqual(found["version_id"], 396118)
        # No file with our hash: fall back to the version's own link.
        self.assertEqual(civitai.pick_download(data, "cd" * 32)["url"], "https://civitai.com/api/download/models/396118")
        self.assertIsNone(civitai.pick_download({"downloadUrl": "javascript:alert(1)"}, sha))
        self.assertIsNone(civitai.pick_download({"error": "Model not found"}, sha))
        self.assertIsNone(civitai.pick_download("nonsense", sha))


class NodeSyncTests(unittest.TestCase):
    def test_public_url(self):
        self.assertEqual(nodesync._public_url("git@github.com:owner/repo.git"), "https://github.com/owner/repo.git")
        self.assertEqual(nodesync._public_url("ssh://git@github.com/owner/repo"), "https://github.com/owner/repo")
        self.assertEqual(nodesync._public_url("https://user:token@github.com/o/r.git"), "https://github.com/o/r.git")
        self.assertIsNone(nodesync._public_url("/some/local/path"))
        self.assertIsNone(nodesync._public_url("file:///tmp/x.git"))

    def test_archive_skips_vcs_caches_and_big_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = os.path.join(tmp, "pack")
            for sub in ("", ".git", "__pycache__", "sub"):
                os.makedirs(os.path.join(pkg_dir, sub), exist_ok=True)
            files = {"__init__.py": b"x = 1\n", "sub/util.py": b"y = 2\n", ".git/config": b"secret",
                     "__pycache__/a.pyc": b"\0", "small.pt": b"tiny"}
            for name, data in files.items():
                with open(os.path.join(pkg_dir, name), "wb") as f:
                    f.write(data)
            with open(os.path.join(pkg_dir, "weights.safetensors"), "wb") as f:
                f.truncate(nodesync.MAX_MODEL_FILE_BYTES + 1)
            archive = os.path.join(tmp, "pack.tar.gz")
            skipped = nodesync.build_archive(nodesync.LocalPackage("pack", pkg_dir, False), archive)
            with tarfile.open(archive) as tar:
                names = sorted(tar.getnames())
            self.assertEqual(names, ["__init__.py", "small.pt", "sub/util.py"])
            self.assertEqual(skipped, ["weights.safetensors"])

    def test_git_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@e.com")
            run = lambda *cmd: subprocess.run(["git", "-C", tmp, *cmd], env=env, check=True, capture_output=True)  # noqa: E731
            run("init", "-q")
            with open(os.path.join(tmp, "__init__.py"), "w") as f:
                f.write("")
            run("add", "-A")
            run("commit", "-q", "-m", "x")
            pkg = nodesync.LocalPackage("pack", tmp, False)
            self.assertIsNone(nodesync.git_spec(pkg))  # no remote: must be uploaded instead
            run("remote", "add", "origin", "git@github.com:owner/pack.git")
            spec = nodesync.git_spec(pkg)
            self.assertEqual(spec["url"], "https://github.com/owner/pack.git")
            self.assertEqual(len(spec["commit"]), 40)
            self.assertIsNone(nodesync.git_spec(nodesync.LocalPackage("one.py", os.path.join(tmp, "__init__.py"), True)))


class AgentTests(unittest.TestCase):
    """The remote agent, loaded with stand-ins for the ComfyUI modules it imports."""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        cls._saved = {name: sys.modules.get(name) for name in ("folder_paths", "server")}
        cls.tmp = tempfile.TemporaryDirectory()
        folder_paths = types.ModuleType("folder_paths")
        folder_paths.models_dir = cls.tmp.name
        folder_paths.folder_names_and_paths = {"loras": ([os.path.join(cls.tmp.name, "loras")], set())}
        folder_paths.get_folder_paths = lambda key: folder_paths.folder_names_and_paths[key][0]
        folder_paths.get_full_path = lambda key, name: None
        folder_paths.get_input_directory = lambda: os.path.join(cls.tmp.name, "input")
        folder_paths.get_directory_by_type = lambda kind: os.path.join(cls.tmp.name, kind)

        class Routes:
            def _passthrough(self, path):
                return lambda handler: handler
            get = post = put = _passthrough

        server = types.ModuleType("server")
        server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=Routes()))
        sys.modules.update(folder_paths=folder_paths, server=server)
        spec = importlib.util.spec_from_file_location(
            "vast_worker_agent_under_test", os.path.join(ROOT, "agent", "vast_worker_agent.py"))
        cls.agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.agent)

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        cls.tmp.cleanup()

    def test_space_check(self):
        gb = 10 ** 9
        self.assertIsNone(self.agent._space_problem(5 * gb, 20 * gb, "x"))
        self.assertIsNone(self.agent._space_problem(0, 2 * gb, "x"))
        message = self.agent._space_problem(11 * gb, 9.5 * gb, "these models")
        self.assertIn("11.0 GB needed, 9.5 GB free", message)
        self.assertIn("Machine setup", message)
        # A file that only just fits is refused: ComfyUI needs room for its own output.
        self.assertIsNotNone(self.agent._space_problem(10 * gb, 10 * gb + 1000, "x"))

    def test_tokens_only_go_to_their_own_host_over_https(self):
        tokens = {"hf_token": "hf_x", "civitai_token": "cv_x"}
        header = self.agent._auth_header
        self.assertEqual(header("https://huggingface.co/a/b/resolve/main/f", tokens), {"Authorization": "Bearer hf_x"})
        self.assertEqual(header("https://civitai.com/api/download/models/1", tokens), {"Authorization": "Bearer cv_x"})
        self.assertEqual(header("https://cdn-lfs.example.com/huggingface.co/f", tokens), {})
        self.assertEqual(header("https://evilhuggingface.co/f", tokens), {})
        self.assertEqual(header("http://huggingface.co/f", tokens), {})
        self.assertEqual(header("https://huggingface.co/f", {}), {})

    def test_paths_stay_inside_their_folder(self):
        base = os.path.join(self.tmp.name, "loras")
        self.assertEqual(self.agent._model_path("loras", "sub/a.safetensors"), os.path.join(base, "sub", "a.safetensors"))
        self.assertEqual(self.agent._model_path("ultralytics/bbox", "f.pt"),
                         os.path.join(self.tmp.name, "ultralytics", "bbox", "f.pt"))
        for directory, name in (("loras", "../../etc/passwd"), ("../outside", "x"), ("/abs", "x"), ("loras", "/abs.pt")):
            with self.assertRaises(ValueError, msg=f"{directory} {name}"):
                self.agent._model_path(directory, name)
        with self.assertRaises(ValueError):
            self.agent._upload_target({"kind": "input", "path": "../x.png"})
        with self.assertRaises(ValueError):
            self.agent._upload_target({"kind": "elsewhere", "path": "x.png"})


class ExecutorHelperTests(unittest.TestCase):
    def test_unique_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_unique_name(tmp, "ComfyUI_00001_.png"), "ComfyUI_00001_.png")
            for name in ("ComfyUI_00001_.png", "ComfyUI_00002_.png", "clip.mp4", "clip_vast1.mp4"):
                open(os.path.join(tmp, name), "w").close()
            self.assertEqual(_unique_name(tmp, "ComfyUI_00001_.png"), "ComfyUI_00003_.png")
            self.assertEqual(_unique_name(tmp, "clip.mp4"), "clip_vast2.mp4")

    def test_validation_error_text(self):
        body = {"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
                "node_errors": {"4": {"class_type": "CheckpointLoaderSimple", "errors": [
                    {"message": "Value not in list", "details": "ckpt_name: 'x.safetensors' not in []"}]}}}
        text = _describe_validation_error(body)
        self.assertIn("CheckpointLoaderSimple (#4)", text)
        self.assertIn("remote model library", text)
        self.assertIn("rejected", _describe_validation_error("nonsense"))


if __name__ == "__main__":
    unittest.main()
