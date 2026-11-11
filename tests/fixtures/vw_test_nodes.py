"""Test-only nodes used by the end-to-end suite.

Installed as a single-file custom node on the *local* side only, so running a
workflow that uses them exercises custom node sync to the instance.
"""

import os
import time

import comfy.model_management as mm
import comfy.utils
import folder_paths


class VWTestModelInfo:
    """Reports the size of a file in the loras folder."""

    CATEGORY = "vw_test"
    FUNCTION = "run"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"lora_name": (folder_paths.get_filename_list("loras"),)}}

    def run(self, lora_name):
        path = folder_paths.get_full_path_or_raise("loras", lora_name)
        return {"ui": {"text": [f"{lora_name}:{os.path.getsize(path)}"]}}


class VWTestSleep:
    """Passes an image through after sleeping, reporting progress."""

    CATEGORY = "vw_test"
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "seconds": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 600.0})}}

    def run(self, image, seconds):
        steps = max(1, int(seconds / 0.2))
        bar = comfy.utils.ProgressBar(steps)
        for _ in range(steps):
            mm.throw_exception_if_processing_interrupted()
            time.sleep(seconds / steps)
            bar.update(1)
        return (image,)


class VWTestFail:
    CATEGORY = "vw_test"
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",)}}

    def run(self, image):
        raise RuntimeError("boom from the remote side")


NODE_CLASS_MAPPINGS = {
    "VWTestModelInfo": VWTestModelInfo,
    "VWTestSleep": VWTestSleep,
    "VWTestFail": VWTestFail,
}
