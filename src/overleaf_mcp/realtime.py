"""Minimal realtime (legacy Socket.IO 0.9) client — read-only ID discovery.

Overleaf's project tree (folders/docs/fileRefs, each with a Mongo ObjectId)
is NOT exposed by any REST endpoint. The only place it appears is on the
internal real-time channel the web editor uses (`joinProject`). The web
upload endpoint (see web_write.py) needs a folder's ObjectId, so we need a
way to discover it without an OVERLEAF_GIT_TOKEN.

This module implements the SMALLEST possible use of that channel:
    1. HTTP GET /socket.io/1/ -> session id + load-balancer affinity cookie
    2. Open one WebSocket, emit `joinProject`, read the response
    3. Disconnect immediately

It deliberately does NOT implement ShareJS/OT text operations, cursor
tracking, or any sustained connection — just a single short-lived,
read-only round trip to fetch the file/folder id map. All actual content
writes go through the plain REST upload endpoint.

Overleaf's `/socket.io/socket.io.js` identifies as a custom fork of the
legacy Socket.IO 0.9 protocol (`0.9.17-overleaf-6`), not modern
Engine.IO/Socket.IO v2+, which is why this is hand-rolled rather than using
the `python-socketio` client (which only supports the newer protocol).
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
import websockets

from .config import OVERLEAF_BASE_URL
from .credentials import get_session

_JOIN_TIMEOUT_DEFAULT = 20.0


def _host_and_schemes() -> tuple[str, str, str]:
    parsed = urlparse(OVERLEAF_BASE_URL)
    host = parsed.netloc
    http_scheme = parsed.scheme or "https"
    ws_scheme = "wss" if http_scheme == "https" else "ws"
    return host, http_scheme, ws_scheme


async def _join_project_async(project_id: str, timeout: float) -> dict:
    session_cookie = get_session()
    if not session_cookie:
        raise RuntimeError(
            "OVERLEAF_SESSION environment variable is not set; required for "
            "realtime project-tree discovery."
        )
    host, http_scheme, ws_scheme = _host_and_schemes()

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(
            f"{http_scheme}://{host}/socket.io/1/",
            params={"projectId": project_id},
            headers={"Cookie": f"overleaf_session2={session_cookie}"},
        )
        r.raise_for_status()
        # Response body: "<sessionid>:<heartbeatTimeout>:<closeTimeout>:<transports>"
        sessionid = r.text.split(":")[0]
        if not sessionid:
            raise RuntimeError(f"Unexpected socket.io handshake response: {r.text!r}")

        # Replay the session cookie plus any cookie the handshake set (e.g. a
        # load-balancer affinity cookie) so the WebSocket lands on the same
        # backend node that created this session id.
        cookie_jar = {"overleaf_session2": session_cookie}
        for c in client.cookies.jar:
            cookie_jar[c.name] = c.value
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_jar.items())

    ws_url = f"{ws_scheme}://{host}/socket.io/1/websocket/{sessionid}"
    deadline = time.monotonic() + timeout
    async with websockets.connect(
        ws_url,
        additional_headers={"Cookie": cookie_header},
        open_timeout=timeout,
    ) as ws:
        # Server sends "1::" to confirm the default-namespace connection.
        await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))

        payload = json.dumps({"name": "joinProject", "args": [{"project_id": project_id}]})
        await ws.send(f"5:1+::{payload}")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Timed out waiting for joinProjectResponse over the "
                    "realtime channel."
                )
            frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if not isinstance(frame, str):
                continue
            if '"name":"joinProjectResponse"' in frame:
                # Frame shape: "5:::{"name":"joinProjectResponse","args":[{...}]}"
                parts = frame.split(":", 3)
                if len(parts) < 4:
                    raise RuntimeError(f"Malformed joinProjectResponse frame: {frame[:300]}")
                obj = json.loads(parts[3])
                args = obj.get("args") or []
                if not args:
                    raise RuntimeError(f"Empty joinProjectResponse args: {frame[:300]}")
                return args[0]
            if frame.startswith("7:"):
                raise RuntimeError(f"Socket.IO error frame: {frame[:300]}")
            # Ignore heartbeats / unrelated events and keep waiting.


def join_project(project_id: str, timeout: float = _JOIN_TIMEOUT_DEFAULT) -> dict:
    """Synchronous one-shot `joinProject`. Returns the raw response payload,
    typically ``{"publicId": ..., "project": {...}, "permissionsLevel": ...}``.
    """
    return asyncio.run(_join_project_async(project_id, timeout))


def _walk_tree(folder: dict, prefix: str, out: dict[str, dict]) -> None:
    for d in folder.get("docs") or []:
        out[f"{prefix}{d['name']}"] = {"id": d["_id"], "type": "doc"}
    for f in folder.get("fileRefs") or []:
        out[f"{prefix}{f['name']}"] = {"id": f["_id"], "type": "file"}
    for sub in folder.get("folders") or []:
        sub_path = f"{prefix}{sub['name']}"
        out[sub_path] = {"id": sub["_id"], "type": "folder"}
        _walk_tree(sub, f"{sub_path}/", out)


def get_entity_map(project_id: str, timeout: float = _JOIN_TIMEOUT_DEFAULT) -> dict[str, dict]:
    """Return ``{relative_path: {"id": ..., "type": "doc"|"file"|"folder"}}``
    for every entity in the project, plus ``""`` mapped to the root folder.
    """
    resp = join_project(project_id, timeout=timeout)
    project = resp.get("project") or {}
    root_folders = project.get("rootFolder") or []
    if not root_folders:
        raise RuntimeError("joinProjectResponse is missing rootFolder")
    root = root_folders[0]
    out: dict[str, dict] = {"": {"id": root["_id"], "type": "folder"}}
    _walk_tree(root, "", out)
    return out
