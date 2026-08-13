"""Network orchestration boundaries that pure helper tests cannot cover."""

from __future__ import annotations

import asyncio

import httpx

from overleaf_mcp import server as srv


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


def _run_download_log(monkeypatch, text: str) -> str:
    monkeypatch.setenv('OVERLEAF_SESSION', 's%3Atest')
    monkeypatch.setattr(
        srv._compile_mod,
        'compile_project',
        lambda project_id: {
            'status': 'success',
            'clsi_server_id': 'clsi-1',
            'output_files': [{'path': 'output.log', 'url': '/build/output.log'}],
        },
    )
    monkeypatch.setattr(httpx, 'get', lambda *args, **kwargs: _FakeResponse(text))
    return asyncio.run(srv._dispatch(
        'download_log', {'project_id': 'a' * 24}))


def test_download_log_returns_fetched_text(monkeypatch):
    assert _run_download_log(monkeypatch, 'LaTeX compile log') == 'LaTeX compile log'


def test_download_log_keeps_tail_when_large(monkeypatch):
    result = _run_download_log(monkeypatch, 'prefix-' + 'x' * 50_001)
    marker = '[… truncated to last 50000 chars]\n\n'
    assert result.startswith(marker)
    assert result.removeprefix(marker) == 'x' * 50_000
