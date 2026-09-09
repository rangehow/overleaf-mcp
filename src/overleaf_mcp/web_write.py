"""Free-tier (session-cookie-only) file writes via Overleaf's REST upload
endpoint — an alternative to the git-based edit_file/rewrite_file/create_file
tools for accounts without OVERLEAF_GIT_TOKEN (Git Integration requires a
paid Overleaf plan; this module needs only OVERLEAF_SESSION).

Reads go through ``compile.read_file_web`` (zip download, already
cookie-only). Writes go through ``POST /project/:id/upload`` — the same
endpoint the web UI's drag-and-drop file uploader calls. If a doc/file
already exists at the target path, Overleaf replaces its content in place
(same entity_id is returned); otherwise a new doc is created.

Discovering the Mongo ObjectIds this endpoint requires (``folder_id``) is
done via a single short-lived realtime round trip (see ``realtime.py``) —
no live editing/OT is performed.

Known limitation: this does not create missing intermediate folders. The
parent folder of ``file_path`` must already exist.
"""

from __future__ import annotations

import base64
import binascii

import httpx

from . import realtime
from .compile import _csrf_token, _headers, read_file_web
from .config import OVERLEAF_BASE_URL

# Overleaf's own upload size limit is much higher, but MCP transports and
# LLM context windows make very large base64 payloads impractical. This is
# a client-side sanity cap, not an Overleaf-imposed limit.
_MAX_BASE64_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB decoded


def _parent_and_name(file_path: str) -> tuple[str, str]:
    file_path = file_path.strip("/")
    if "/" in file_path:
        parent, name = file_path.rsplit("/", 1)
    else:
        parent, name = "", file_path
    return parent, name


def upload_file_content(project_id: str, file_path: str, content: str | bytes) -> str:
    """Create or overwrite ``file_path`` with ``content`` via the web upload
    endpoint. Returns a human-readable summary string.
    """
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content

    entity_map = realtime.get_entity_map(project_id)
    parent, name = _parent_and_name(file_path)
    parent_entry = entity_map.get(parent)
    if parent_entry is None or parent_entry.get("type") != "folder":
        known = ", ".join(sorted(k for k in entity_map if k)[:20]) or "(none)"
        raise RuntimeError(
            f"Parent folder '{parent or '/'}' not found in project "
            f"(this function does not create intermediate folders). "
            f"Known entries: {known}"
        )
    folder_id = parent_entry["id"]
    existed = file_path.strip("/") in entity_map

    csrf = _csrf_token(project_id)
    files = {"qqfile": ("upload", content_bytes, "application/octet-stream")}
    data = {"name": name}
    r = httpx.post(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/upload",
        params={"folder_id": folder_id},
        data=data,
        files=files,
        headers={**_headers(), "X-Csrf-Token": csrf},
        timeout=60,
    )
    r.raise_for_status()
    result = r.json()
    if not result.get("success"):
        raise RuntimeError(f"Upload failed: {result}")

    verb = "Replaced" if existed else "Created"
    return (
        f"{verb} '{file_path}' "
        f"(entity_id={result.get('entity_id')}, type={result.get('entity_type')}, "
        f"{len(content_bytes)} bytes)"
    )


def edit_file_content(
    project_id: str, file_path: str, old_string: str, new_string: str
) -> str:
    """Surgical search-and-replace — free-tier equivalent of git-based
    edit_file. ``old_string`` must match exactly once in the current file
    content. Downloads the file (session-cookie only), replaces locally,
    then re-uploads the full content.
    """
    current = read_file_web(project_id, file_path)
    count = current.count(old_string)
    if count == 0:
        raise RuntimeError(f"old_string not found in {file_path!r}")
    if count > 1:
        raise RuntimeError(
            f"old_string matches {count} times in {file_path!r}; it must "
            "match exactly once. Provide more surrounding context to "
            "disambiguate."
        )
    new_content = current.replace(old_string, new_string, 1)
    return upload_file_content(project_id, file_path, new_content)


def upload_file_base64(project_id: str, file_path: str, content_base64: str) -> str:
    """Create or overwrite a (possibly binary) file from base64-encoded
    content — for images, PDFs, and other non-text assets that must not be
    UTF-8 decoded. Free-tier equivalent of the git-based upload_file tool
    (which reads from a local filesystem path instead).
    """
    # Tolerate data: URLs (e.g. "data:image/png;base64,AAAA...") and plain
    # base64 strings alike.
    if "," in content_base64 and content_base64.strip().lower().startswith("data:"):
        content_base64 = content_base64.split(",", 1)[1]

    try:
        content_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f"Invalid base64 content: {e}") from e

    if len(content_bytes) > _MAX_BASE64_UPLOAD_BYTES:
        raise RuntimeError(
            f"Decoded content is {len(content_bytes)} bytes, exceeding the "
            f"{_MAX_BASE64_UPLOAD_BYTES} byte client-side limit for base64 "
            "uploads. Use a smaller file."
        )

    return upload_file_content(project_id, file_path, content_bytes)


def delete_file_web(project_id: str, file_path: str) -> str:
    """Delete a doc/file from the project WITHOUT requiring
    OVERLEAF_GIT_TOKEN. Uses ``DELETE /project/:id/doc/:id`` or
    ``/project/:id/file/:id`` (the same endpoints the web UI's file-tree
    context menu calls), resolving the id via the realtime project tree.
    """
    entity_map = realtime.get_entity_map(project_id)
    entry = entity_map.get(file_path.strip("/"))
    if entry is None:
        known = ", ".join(sorted(k for k in entity_map if k)[:20]) or "(none)"
        raise RuntimeError(f"'{file_path}' not found in project. Known entries: {known}")
    if entry["type"] == "folder":
        raise RuntimeError(
            f"'{file_path}' is a folder, not a file/doc. This tool only "
            "deletes individual files/docs."
        )

    kind = "file" if entry["type"] == "file" else "doc"
    csrf = _csrf_token(project_id)
    r = httpx.delete(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/{kind}/{entry['id']}",
        headers={**_headers(), "X-Csrf-Token": csrf},
        timeout=30,
    )
    r.raise_for_status()
    return f"Deleted '{file_path}' (entity_id={entry['id']}, type={entry['type']})"


def list_files_web(project_id: str) -> list[str]:
    """List doc/file paths (not folders) via the realtime project tree."""
    entity_map = realtime.get_entity_map(project_id)
    return sorted(
        path for path, entry in entity_map.items()
        if path and entry.get("type") in ("doc", "file")
    )
