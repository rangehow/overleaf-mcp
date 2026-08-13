"""tests/test_v2_handler_registration.py — the v2 handler wiring stays wired.

WHY THIS GUARD EXISTS (measured, 2026-07-29)
--------------------------------------------
Version 0.2.1 of this package was BROKEN ON INSTALL for anyone who got the new
SDK. It registered handlers with the v1 decorator API::

    @server.list_tools()
    @server.call_tool()

MCP Python SDK 2.0.0 (shipped 2026-07-28 alongside protocol revision
2026-07-28) REMOVED that API from the low-level ``Server``. There is no
``__getattr__`` fallback, and the decorators run at MODULE SCOPE, so the
failure was an import-time crash reproduced against a real mcp==2.0.0 venv::

    File "src/overleaf_mcp/server.py", line 590, in <module>
        @server.list_tools()
    AttributeError: 'Server' object has no attribute 'list_tools'

Because the dependency was declared as an unbounded ``mcp>=1.0.0``, every
`uvx overleaf-mcp` / `pip install overleaf-mcp-plus` after that date resolved
to 2.x and produced a server that died before answering a single request.

WHAT THIS GUARD PINS, AND WHY EACH PART
----------------------------------------
Importing the module is NOT sufficient evidence that the port worked: a
half-finished migration (handlers defined but never passed to the constructor)
imports perfectly and then serves nothing. So the assertions below go through
``server.get_request_handler(...)`` and DRIVE the handlers.

A note on that lookup, because getting it wrong produces a convincing false
alarm: ``get_request_handler`` is keyed on the METHOD STRING (``"tools/list"``),
not on the request CLASS. Passing ``ListToolsRequest`` returns ``None`` and
reads exactly like "the handlers were never registered".
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'src'))

import overleaf_mcp  # noqa: E402
from overleaf_mcp import server as srv  # noqa: E402

_V2 = hasattr(srv.Server, '__init__') and 'on_list_tools' in __import__(
    'inspect').signature(srv.Server.__init__).parameters

pytestmark = pytest.mark.skipif(
    not _V2, reason='installed MCP SDK predates the v2 on_* constructor API')


def _call(method, *args):
    entry = srv.server.get_request_handler(method)
    assert entry is not None, (
        f'no handler registered for {method!r}. The v2 SDK takes handlers as '
        f'CONSTRUCTOR parameters (on_list_tools= / on_call_tool=) — a handler '
        f'merely defined at module level is never reachable.'
    )
    return asyncio.run(entry.handler(*args))


def test_both_handlers_are_registered():
    """tools/list and tools/call must both resolve to a live handler."""
    for method in ('tools/list', 'tools/call'):
        assert srv.server.get_request_handler(method) is not None, (
            f'{method} unregistered — the server would import cleanly and then '
            f'answer nothing, which is worse than failing loudly.')


def test_decorator_api_is_not_used():
    """The removed v1 decorator API must not reappear.

    Pins the actual regression: re-introducing ``@server.list_tools()`` is an
    import-time AttributeError on any v2 SDK.
    """
    assert not hasattr(srv.server, 'list_tools')
    assert not hasattr(srv.server, 'call_tool')


def test_list_tools_returns_a_full_result_object():
    """v2 removed the automatic wrapping of a bare ``list[Tool]``.

    Returning a list instead of ``ListToolsResult`` is a mistake that only
    surfaces at request time, so it is pinned here.
    """
    res = _call('tools/list', None, None)
    assert type(res).__name__ == 'ListToolsResult'
    assert res.tools, 'tool catalogue came back empty'
    assert len(res.tools) >= 20, f'only {len(res.tools)} tools exposed'


def test_tool_order_is_deterministic():
    """Two calls must yield the same order.

    Protocol revision 2026-07-28 asks servers for a deterministic ``tools/list``
    order precisely because a shifting catalogue invalidates the client's
    prompt cache on every reconnect.
    """
    first = [t.name for t in _call('tools/list', None, None).tools]
    second = [t.name for t in _call('tools/list', None, None).tools]
    assert first == second


def test_static_tool_catalog_advertises_public_v2_cache_hint():
    """The immutable 25-tool list should not be transferred every request."""
    hint = srv.server.cache_hints['tools/list']
    assert hint.ttl_ms == 86_400_000
    assert hint.scope == 'public'


def test_server_identity_uses_v2_display_metadata():
    assert srv.server.title == 'Overleaf MCP'
    assert 'compile' in srv.server.description
    assert srv.server.website_url == 'https://github.com/rangehow/overleaf-mcp'


def test_tools_still_serialize_camelcase_on_the_wire():
    """v2 moved model FIELDS to snake_case but kept camelCase wire aliases.

    Clients (including Tofu's bridge, which reads ``inputSchema``) parse the
    wire form, so this must not drift to ``input_schema``.
    """
    tool = _call('tools/list', None, None).tools[0]
    wire = tool.model_dump(by_alias=True, exclude_none=True)
    assert 'inputSchema' in wire
    assert 'input_schema' not in wire


def test_call_tool_returns_result_object_not_content_list():
    """v1 returned ``list[TextContent]``; v2 requires a ``CallToolResult``."""
    from mcp.types import CallToolRequestParams
    res = _call('tools/call', None,
                CallToolRequestParams(name='nonexistent_tool', arguments={}))
    assert type(res).__name__ == 'CallToolResult'
    assert res.content and res.content[0].text


def test_failures_come_back_as_normal_results_not_exceptions():
    """A failing tool must surface as ordinary result TEXT, not an exception.

    Two independent reasons, both load-bearing:

    1. v2 no longer converts an escaping exception into
       ``CallToolResult(is_error=True)`` — it becomes a top-level JSON-RPC
       error the model never sees as tool output, so it cannot react to it.
    2. Tofu's MCP credential health-probe classifies this server by matching
       phrases in the RESULT TEXT of a SUCCESSFUL call (the ``health_probe``
       entry for 'overleaf' in Tofu's lib/mcp/registry.py pins
       'error fetching projects' / 'overleaf_session'). Raising — or flipping
       ``is_error`` to True — would silently break session-expiry detection.
    """
    from mcp.types import CallToolRequestParams
    for var in ('OVERLEAF_SESSION', 'OVERLEAF_GIT_TOKEN'):
        os.environ.pop(var, None)
    res = _call('tools/call', None,
                CallToolRequestParams(name='list_projects', arguments={}))
    assert type(res).__name__ == 'CallToolResult'
    assert res.is_error is False, (
        'is_error flipped to True — Tofu classifies Overleaf auth failures by '
        'matching text in a SUCCESSFUL result; this breaks expiry detection')
    assert 'OVERLEAF_SESSION' in res.content[0].text


def test_version_matches_the_v2_release_line():
    """0.3.0+ is the v2-only line; 0.2.x is the v1 stopgap."""
    major, minor, *_ = overleaf_mcp.__version__.split('.')
    assert (int(major), int(minor)) >= (0, 3), (
        f'version {overleaf_mcp.__version__} speaks the v2 API but is numbered '
        f'in the 0.2.x line, which pins mcp<2')


def test_git_and_cookie_credentials_are_independent(monkeypatch):
    """Git tools must not demand the unrelated web-dashboard session."""
    for var in ('OVERLEAF_SESSION', 'OVERLEAF_GIT_TOKEN'):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv('OVERLEAF_GIT_TOKEN', 'olp_test')
    assert srv._auto_setup_guard('read_file') is None
    assert srv._auto_setup_guard('edit_file') is None
    cookie_error = srv._auto_setup_guard('list_projects')
    assert 'OVERLEAF_SESSION' in cookie_error

    monkeypatch.delenv('OVERLEAF_GIT_TOKEN')
    monkeypatch.setenv('OVERLEAF_SESSION', 's%3Atest')
    assert srv._auto_setup_guard('list_projects') is None
    git_error = srv._auto_setup_guard('read_file')
    assert 'OVERLEAF_GIT_TOKEN' in git_error
    assert 'requires the OVERLEAF_SESSION' not in git_error

    monkeypatch.delenv('OVERLEAF_SESSION')
    assert srv._auto_setup_guard('nonexistent_tool') is None
