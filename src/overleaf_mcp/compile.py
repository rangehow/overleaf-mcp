"""Compile and PDF download via Overleaf web API (session-cookie auth).

These tools require the ``compile`` optional dependency group::

    pip install overleaf-mcp[compile]

Environment variable ``OVERLEAF_SESSION`` must contain the value of
the ``overleaf_session2`` cookie from your browser.
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile

import httpx

from .config import OVERLEAF_BASE_URL
from .credentials import get_session

logger = logging.getLogger("overleaf-mcp")

_HAS_BS4 = False
try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    _HAS_BS4 = True
except ImportError:
    pass


def _session_cookie() -> str:
    cookie = get_session()
    if not cookie:
        raise RuntimeError(
            "Overleaf session cookie not configured. Set the OVERLEAF_SESSION "
            "environment variable with the value of your `overleaf_session2` "
            "browser cookie (DevTools → Application → Cookies → overleaf.com)."
        )
    return cookie


def _headers() -> dict[str, str]:
    return {"Cookie": f"overleaf_session2={_session_cookie()}"}


def _csrf_token(project_id: str) -> str:
    """Fetch the CSRF token for a project page."""
    if not _HAS_BS4:
        raise ImportError("beautifulsoup4 required: pip install overleaf-mcp[compile]")
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/project/{project_id}",
        headers=_headers(),
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    meta = soup.find("meta", {"name": "ol-csrfToken"})
    if not meta:
        raise RuntimeError("Cannot find CSRF token — session cookie may have expired.")
    return meta.get("content", "")


def list_projects_web() -> list[dict[str, str]]:
    """List all projects via the Overleaf web dashboard (session cookie auth)."""
    if not _HAS_BS4:
        raise ImportError("beautifulsoup4 required: pip install overleaf-mcp[compile]")
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/",
        headers=_headers(),
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    meta = soup.find("meta", {"name": "ol-prefetchedProjectsBlob"})
    if not meta:
        raise RuntimeError("Not authenticated — check OVERLEAF_SESSION cookie.")
    data = json.loads(meta.get("content", "{}"))
    return [
        {"id": p["id"], "name": p["name"]}
        for p in data.get("projects", [])
        if not p.get("trashed") and not p.get("archived")
    ]


def compile_project(project_id: str) -> dict:
    """Trigger PDF compilation and return status + output file list."""
    csrf = _csrf_token(project_id)
    r = httpx.post(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/compile",
        json={
            "check": "silent",
            "draft": False,
            "incrementalCompilesEnabled": True,
            "rootDocId": None,
            "stopOnFirstError": False,
        },
        headers={
            **_headers(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-csrf-token": csrf,
        },
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return {
        "status": data.get("status"),
        "output_files": data.get("outputFiles", []),
    }


def download_pdf(project_id: str, output_path: str) -> str:
    """Download the compiled PDF to a local file path.

    Uses the per-build output URL returned by compile_project, because the
    shortcut /project/<id>/output/output.pdf returns 404 on current Overleaf.
    """
    compile_info = compile_project(project_id)
    pdf_url = None
    for f in compile_info.get("output_files", []):
        if f.get("path") == "output.pdf" and f.get("url"):
            pdf_url = f["url"]
            break
    if not pdf_url:
        raise RuntimeError(
            f"No output.pdf in compile result (status={compile_info.get('status')}). "
            "Check compile logs with download_log."
        )
    full_url = pdf_url if pdf_url.startswith("http") else f"{OVERLEAF_BASE_URL}{pdf_url}"
    r = httpx.get(
        full_url,
        headers=_headers(),
        follow_redirects=True,
        timeout=120,
    )
    r.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(r.content)
    return f"PDF saved to {output_path} ({len(r.content)} bytes)"


def download_project_zip(project_id: str) -> list[str]:
    """Download the project as a ZIP and return the file list."""
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/download/zip",
        headers=_headers(),
        follow_redirects=True,
        timeout=60,
    )
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return zf.namelist()


def download_source_zip(project_id: str, output_path: str) -> str:
    """Download the full project source as a ZIP file to ``output_path``.

    Uses the web endpoint ``/project/<id>/download/zip`` with the session
    cookie. Returns a human-readable summary string.
    """
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/download/zip",
        headers=_headers(),
        follow_redirects=True,
        timeout=120,
    )
    r.raise_for_status()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(r.content)
    # Count entries for a nicer summary
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            n_entries = len(zf.namelist())
    except Exception:
        n_entries = -1
    size_kb = len(r.content) / 1024
    return (
        f"Source ZIP saved to {output_path} "
        f"({size_kb:.1f} KB, {n_entries} entries)"
    )


def download_source(project_id: str, output_dir: str, overwrite: bool = False) -> str:
    """Download the project source and extract it into ``output_dir``.

    Creates ``output_dir`` if missing. If it already contains files and
    ``overwrite`` is False, raises. Returns a summary with file count.
    """
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/download/zip",
        headers=_headers(),
        follow_redirects=True,
        timeout=120,
    )
    r.raise_for_status()
    abs_dir = os.path.abspath(output_dir)
    if os.path.isdir(abs_dir) and os.listdir(abs_dir) and not overwrite:
        raise RuntimeError(
            f"Output directory {abs_dir!r} is not empty. "
            "Pass overwrite=true to extract anyway."
        )
    os.makedirs(abs_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        # Guard against zip-slip — reject entries that escape abs_dir
        safe_members = []
        for m in zf.namelist():
            dest = os.path.normpath(os.path.join(abs_dir, m))
            if not dest.startswith(abs_dir + os.sep) and dest != abs_dir:
                raise RuntimeError(f"Unsafe zip entry (zip-slip): {m!r}")
            safe_members.append(m)
        zf.extractall(abs_dir, members=safe_members)
        n_entries = len(safe_members)
    size_kb = len(r.content) / 1024
    return (
        f"Source extracted to {abs_dir} "
        f"({n_entries} entries, {size_kb:.1f} KB zip)"
    )


def read_file_web(project_id: str, file_path: str) -> str:
    """Read a single file from the project ZIP (session-cookie fallback)."""
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/download/zip",
        headers=_headers(),
        follow_redirects=True,
        timeout=60,
    )
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        with zf.open(file_path) as fh:
            return fh.read().decode("utf-8")


def create_project_web(name: str, template: str | None = None) -> dict[str, str]:
    """Create a new blank Overleaf project via the web API.

    Posts to ``/project/new`` with ``application/x-www-form-urlencoded`` body
    (``projectName=<name>``), matching what the Overleaf dashboard's
    ``ProjectController.newProject`` handler expects. See the upstream
    acceptance test ``services/web/test/acceptance/src/
    ProjectDuplicateNameTests.mjs`` which uses exactly this shape:

        fetch('/project/new', {
            method: 'POST',
            body: new URLSearchParams([['projectName', projectName]]),
        })

    The earlier ``/project/new/blank`` JSON endpoint returns 404 on the
    current production site. ``template`` is optional (e.g. ``'example'``
    for the Overleaf example project); omit for a blank project.
    """
    if not _HAS_BS4:
        raise ImportError("beautifulsoup4 required: pip install overleaf-mcp[compile]")
    # Get CSRF from dashboard
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}/",
        headers=_headers(),
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    meta = soup.find("meta", {"name": "ol-csrfToken"})
    if not meta:
        raise RuntimeError("Cannot find CSRF token — session may have expired.")
    csrf = meta.get("content", "")

    form_data: dict[str, str] = {"projectName": name}
    if template:
        form_data["template"] = template

    r = httpx.post(
        f"{OVERLEAF_BASE_URL}/project/new",
        data=form_data,  # httpx encodes as application/x-www-form-urlencoded
        headers={
            **_headers(),
            "Accept": "application/json",
            "x-csrf-token": csrf,
        },
        timeout=30,
    )
    r.raise_for_status()
    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError(
            f"create_project: unexpected response (HTTP {r.status_code}): "
            f"{r.text[:500]}"
        ) from e
    pid = data.get("project_id")
    if not pid:
        raise RuntimeError(f"create_project: no project_id in response: {data!r}")
    return {"id": pid, "name": name}
