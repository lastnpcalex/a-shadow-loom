"""Validated image requests carried by ordinary Loom-managed AGY child runs."""

from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import quote
import warnings


# This is the AGY agent selector, not the image model behind its native tool.
DEFAULT_AGENT_MODEL = "gemini-3.8-flash-high"
IMAGE_RUN_TIMEOUT_SEC = 600
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


def workspace_image_path(workspace: str | Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Image paths must be nonempty workspace-relative paths")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("Image paths must stay within the workspace")
    # Reject Windows alternate streams even when a request is checked on POSIX.
    if ":" in value or ("\\" in value and ".." in value.split("\\")):
        raise ValueError("Invalid image path")
    root = Path(workspace).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("Image path escapes workspace")
    if target.suffix.lower() not in FORMATS:
        raise ValueError("Image paths must end in .png, .jpg, .jpeg, or .webp")
    return target


def inspect_image(path: Path) -> dict:
    """Check actual bytes and decode a bounded raster; a filename is not proof."""
    from PIL import Image

    if not path.is_file():
        raise ValueError(f"Image file missing: {path.name}")
    with path.open("rb") as stream:
        content = stream.read(MAX_IMAGE_BYTES + 1)
    size = len(content)
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError("Image file is empty or exceeds 32 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                image_format = image.format
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds 40 million pixels")
                if image_format != FORMATS[path.suffix.lower()]:
                    raise ValueError("Image encoding does not match its filename extension")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError(f"Invalid raster image: {exc}") from exc
    return {
        "width": width, "height": height, "format": image_format,
        "bytes": size, "sha256": hashlib.sha256(content).hexdigest(),
    }


def prepare_image_request(workspace: str | Path, request: dict) -> dict:
    """Validate at admission and again at launch, without creating any files."""
    if not isinstance(request, dict):
        raise ValueError("image_request must be an object")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("Image prompt must contain 1 to 12000 characters")
    root = Path(workspace).resolve()
    output = workspace_image_path(root, request.get("output_path"))
    if output.exists():
        raise ValueError("Image output already exists; choose a new filename")
    references = request.get("reference_paths")
    if references is None:
        references = []
    if not isinstance(references, list) or len(references) > 4:
        raise ValueError("reference_paths must be a list of at most four images")
    normalized = []
    for value in references:
        path = workspace_image_path(root, value)
        inspect_image(path)
        normalized.append(path.relative_to(root).as_posix())
    return {
        "prompt": prompt.strip(), "output_path": output.relative_to(root).as_posix(),
        "reference_paths": normalized,
    }


def image_scope(request: dict) -> list[str]:
    # Reserve references too: another sibling must not edit them during generation.
    return list(dict.fromkeys([
        Path(request["output_path"]).parent.as_posix(), *request["reference_paths"],
    ]))


def image_task(request: dict) -> str:
    return (
        "Generate one raster image using Antigravity's native generate_image tool. "
        "This is an image task, not a repository investigation. Call the native tool "
        "directly; do not search the project for an image generator. Do not delegate "
        "or call loom-agent-control.generate_image recursively. If the native tool "
        "is unavailable or fails, report the exact error and stop. No substitute "
        "provider, external API, download, SVG, or procedural drawing.\n\n"
        "Use the request below as the image specification. Reference paths, if any, "
        "are read-only inputs relative to your workspace; pass their absolute paths "
        "to the native tool's ImagePaths argument. Follow the prompt for how each "
        "reference is used. Save the generated artifact at output_path, creating "
        "parent directories as needed. The native tool may first save in its own "
        "managed artifact cache: copy that result to output_path, converting encoding "
        "only if needed to match its extension. Never overwrite an existing file. "
        "Modify no other workspace files. No code changes or git commit. Return a "
        "concise result with the native artifact path and final workspace path. "
        "Loom will validate the file's encoding and dimensions itself.\n\n"
        + json.dumps(request, ensure_ascii=False)
    )


class NativeImageEvidence:
    """Require a successful native call, not a success claim in assistant prose."""

    def __init__(self):
        self.pending: set[str] = set()
        self.succeeded = False

    def observe(self, item: dict) -> None:
        tool_id = str(item.get("tool_id") or "")
        if item.get("type") == "tool_start":
            self.pending.discard(tool_id)
            if item.get("name") == "generate_image" and tool_id:
                self.pending.add(tool_id)
        elif item.get("type") == "tool_result" and tool_id in self.pending:
            self.pending.discard(tool_id)
            if item.get("is_error") is False:
                self.succeeded = True


def image_artifact(workspace: str, request: dict, conversation_id: int) -> dict:
    path = workspace_image_path(workspace, request["output_path"])
    relative = path.relative_to(Path(workspace).resolve()).as_posix()
    if relative != request["output_path"]:
        raise ValueError("Image output path changed during generation")
    metadata = inspect_image(path)
    return {
        **metadata, "path": relative, "absolute_path": str(path),
        "url": f"/api/conversations/{conversation_id}/file?path={quote(relative, safe='')}",
        "native_tool": "generate_image",
    }
