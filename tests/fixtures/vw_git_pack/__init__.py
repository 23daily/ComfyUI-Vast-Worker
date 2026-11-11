"""Test-only node pack that the end-to-end suite installs from a git remote."""

from .helper import shout


class VWGitEcho:
    CATEGORY = "vw_test"
    FUNCTION = "run"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"default": "hello"})}}

    def run(self, text):
        return {"ui": {"text": [shout(text)]}}


NODE_CLASS_MAPPINGS = {"VWGitEcho": VWGitEcho}
