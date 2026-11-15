"""Offline regression test for the /project/<id>/compile POST body.

Overleaf validates this body strictly (since ~2026-11) and answers 400
``Unrecognized key: "rootDocId" at "body"`` for ``"rootDocId": null`` —
which broke compile_project, download_pdf and every layout tool
(rangehow/overleaf-mcp#2). The fake below mimics that rejection.
"""

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import overleaf_mcp.compile as C  # noqa: E402
import overleaf_mcp.layout as L  # noqa: E402


def _strict_overleaf_post(sent):
    def fake_post(url, json=None, **kwargs):
        sent.append(json)
        req = httpx.Request("POST", url)
        if "rootDocId" in json:
            return httpx.Response(
                400,
                json={
                    "error": 'Validation error: Unrecognized key: "rootDocId" at "body"',
                    "statusCode": 400,
                },
                request=req,
            )
        return httpx.Response(
            200, json={"status": "success", "outputFiles": []}, request=req
        )

    return fake_post


@pytest.fixture
def sent(monkeypatch):
    sent = []
    fake = _strict_overleaf_post(sent)
    monkeypatch.setattr(C, "_csrf_token", lambda pid: "csrf")
    monkeypatch.setattr(L, "_csrf_token", lambda pid: "csrf")
    monkeypatch.setattr(C, "_headers", lambda: {})
    monkeypatch.setattr(L, "_headers", lambda: {})
    monkeypatch.setattr(C.httpx, "post", fake)
    monkeypatch.setattr(L.httpx, "post", fake)
    return sent


def test_compile_project_body_accepted_by_strict_overleaf(sent):
    result = C.compile_project("p1")
    assert result["status"] == "success"
    assert "rootDocId" not in sent[0]


def test_layout_compile_body_accepted_and_keeps_editor_id(sent):
    result = L._compile_with_editor_id("p1", "editor-123")
    assert result["status"] == "success"
    assert "rootDocId" not in sent[0]
    assert sent[0]["editorId"] == "editor-123"


def test_download_pdf_uses_per_build_url_with_clsiserverid(monkeypatch, tmp_path):
    # The legacy /project/<id>/output/output.pdf shortcut 404s on current
    # Overleaf; the PDF must come from the per-build URL in the compile result.
    build_url = "/project/p1/user/u1/build/b1/output/output.pdf"
    monkeypatch.setattr(
        C,
        "compile_project",
        lambda pid: {
            "status": "success",
            "output_files": [
                {"path": "output.log", "url": "/project/p1/user/u1/build/b1/output/output.log"},
                {"path": "output.pdf", "url": build_url},
            ],
            "clsi_server_id": "clsi-7",
        },
    )
    monkeypatch.setattr(C, "_headers", lambda: {})
    fetched = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        return httpx.Response(200, content=b"%PDF-1.5", request=httpx.Request("GET", url))

    monkeypatch.setattr(C.httpx, "get", fake_get)
    out = tmp_path / "out.pdf"
    C.download_pdf("p1", str(out))
    assert fetched == [f"{C.OVERLEAF_BASE_URL}{build_url}?clsiserverid=clsi-7"]
    assert out.read_bytes() == b"%PDF-1.5"
