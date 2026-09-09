"""tests/test_tool_safety_annotations.py — every tool declares its safety hints.

WHY THIS GUARD EXISTS (measured, 2026-07-29)
--------------------------------------------
``annotations.readOnlyHint`` is a CONTROL SIGNAL, not documentation. A host
cannot know whether a tool mutates anything, so a careful one assumes the
worst: Tofu's ``lib/tasks_pkg/tool_dispatch/_flags.py`` puts every MCP tool
into the WRITE partition (serial dispatch + approval-eligible) unless the tool
says ``readOnlyHint: true``.

This package shipped 25 tools with ZERO annotations, so a purely read-only
workflow — open a project, list files, read a few, diff two revisions — ran
one-call-at-a-time with an approval prompt on every step.

WHAT THE ASSERTIONS PIN, AND WHY BY NAME
-----------------------------------------
Every tool is asserted BY NAME, not by count. A count assertion passes just as
happily when two tools swap sides, and the swap that matters is the dangerous
one: a mutating tool mislabelled read-only rejoins the parallel pool AND stops
asking for approval. ``delete_file`` is the worst case and is pinned twice.

A tool absent from both tables is a FAILURE rather than a default, because the
default (write) is silent: the author never learns they skipped the decision.

THE CLASSIFICATION IS DERIVED FROM BEHAVIOUR, NOT FROM THE NAME
----------------------------------------------------------------
The spec's test is "the tool does not modify its ENVIRONMENT" — wider than
"does not modify the Overleaf project". Seven tools that *sound* like reads
fail that test, and each is pinned here with its reason:

  * ``compile_project`` / ``download_log`` / ``get_page_count`` /
    ``locate_in_pdf`` / ``section_page_map`` run a COMPILE on Overleaf's
    servers (``layout._compile_with_editor_id`` mirrors
    ``compile.compile_project``). Returning information does not make an
    operation read-only; what it does to obtain that information is the test.
  * ``download_pdf`` / ``download_source_zip`` / ``download_source`` write to
    the user's filesystem at a caller-supplied path, and ``download_source``
    clobbers a non-empty directory when ``overwrite=true``.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'src'))

from overleaf_mcp import server as srv  # noqa: E402

#: Tools that observe without changing anything.
EXPECTED_READ_ONLY = {
    'list_projects', 'list_files', 'read_file', 'verify_citations',
    'get_sections', 'get_section_content', 'list_history', 'get_diff',
    'status_summary',
    # Free-tier (session-cookie only) equivalents — see web_write.py /
    # realtime.py. Same read-only behaviour as their non-`web_` counterparts.
    'web_list_files', 'web_read_file', 'web_get_sections',
    'web_get_section_content', 'web_status_summary', 'web_verify_citations',
}

#: name -> (destructive, idempotent) for every mutating tool.
EXPECTED_WRITE = {
    'create_file':         (False, False),
    'create_project':      (False, False),
    'upload_file':         (True,  False),
    'edit_file':           (True,  False),
    'rewrite_file':        (True,  False),
    'update_section':      (True,  False),
    'delete_file':         (True,  False),
    'sync_project':        (False, True),
    'compile_project':     (False, False),
    'download_log':        (False, False),
    'get_page_count':      (False, False),
    'locate_in_pdf':       (False, False),
    'section_page_map':    (False, False),
    'download_pdf':        (True,  True),
    'download_source_zip': (True,  True),
    'download_source':     (True,  True),
    # Free-tier (session-cookie only) write equivalents.
    'web_rewrite_file':    (True,  False),
    'web_edit_file':       (True,  False),
    'web_upload_file':     (True,  False),
    'web_delete_file':     (True,  False),
    'web_update_section':  (True,  False),
}


def _tool(name):
    return next((t for t in srv._TOOLS if t.name == name), None)


def _read_only_of(tool):
    """Read the hint the way a HOST does, tolerating either SDK spelling.

    MCP SDK v1 names the attribute ``readOnlyHint``; v2 renamed every model
    field to snake_case. A test that checked only one spelling would pass
    vacuously on the other SDK.
    """
    ann = tool.annotations
    if ann is None:
        return None
    for attr in ('readOnlyHint', 'read_only_hint'):
        v = getattr(ann, attr, None)
        if v is not None:
            return v
    return None


def _hint(tool, camel, snake):
    ann = tool.annotations
    for attr in (camel, snake):
        v = getattr(ann, attr, None)
        if v is not None:
            return v
    return None


# ── Completeness ─────────────────────────────────────────────────────

def test_every_tool_is_classified():
    """No tool may sit unclassified — the default is silent."""
    names = {t.name for t in srv._TOOLS}
    classified = EXPECTED_READ_ONLY | set(EXPECTED_WRITE)
    assert names - classified == set(), (
        f'unclassified tool(s): {sorted(names - classified)} — add them to '
        f'_READ_ONLY_TOOLS / _WRITE_TOOLS in server.py AND to this test')
    assert classified - names == set(), (
        f'test names non-existent tool(s): {sorted(classified - names)}')


def test_every_tool_carries_annotations():
    missing = [t.name for t in srv._TOOLS if t.annotations is None]
    assert not missing, f'tools with no annotations: {missing}'


def test_the_two_tables_are_disjoint():
    assert not (EXPECTED_READ_ONLY & set(EXPECTED_WRITE))


# ── Per-name verdicts ────────────────────────────────────────────────

@pytest.mark.parametrize('name', sorted(EXPECTED_READ_ONLY))
def test_read_only_tools_say_so(name):
    t = _tool(name)
    assert t is not None, f'{name} vanished from _TOOLS'
    assert _read_only_of(t) is True, (
        f'{name} must declare readOnlyHint=True — without it the host runs it '
        f'serially and asks the user for approval on every call')


@pytest.mark.parametrize('name', sorted(EXPECTED_WRITE))
def test_write_tools_declare_false_explicitly(name):
    """Writes say ``false`` rather than omitting the hint.

    Omitted and false behave identically at runtime but differ to a reviewer:
    omitted means nobody classified this tool.
    """
    t = _tool(name)
    assert t is not None, f'{name} vanished from _TOOLS'
    assert _read_only_of(t) is False, (
        f'{name} mutates state and MUST declare readOnlyHint=False explicitly')


@pytest.mark.parametrize('name,expected', sorted(EXPECTED_WRITE.items()))
def test_write_tools_declare_destructive_and_idempotent(name, expected):
    destructive, idempotent = expected
    t = _tool(name)
    assert _hint(t, 'destructiveHint', 'destructive_hint') is destructive, name
    assert _hint(t, 'idempotentHint', 'idempotent_hint') is idempotent, name


def test_destructive_tools_are_never_read_only():
    """The dangerous direction, pinned on its own.

    A destructive tool marked read-only would rejoin the parallel pool and
    skip the approval prompt.
    """
    for name, (destructive, _) in EXPECTED_WRITE.items():
        if destructive:
            assert _read_only_of(_tool(name)) is False, name


def test_delete_file_is_destructive_and_not_read_only():
    """Pinned by name: the single worst tool to misclassify."""
    t = _tool('delete_file')
    assert _read_only_of(t) is False
    assert _hint(t, 'destructiveHint', 'destructive_hint') is True


# ── End-to-end through the HOST's real extractor ─────────────────────

def _host_extractor():
    """Import Tofu's real ``_extract_read_only_hint``, or skip.

    Driving the host's own parser is the point: hints that the supplier writes
    but the consumer cannot read are worth nothing, and only the real function
    proves the round trip.
    """
    root = os.environ.get('TOFU_ROOT') or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'chatui')
    coerce_py = os.path.join(root, 'lib', 'mcp', 'client', '_coerce.py')
    if not os.path.isfile(coerce_py):
        pytest.skip('Tofu checkout not available next to this repo')
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from lib.mcp.client._coerce import _extract_read_only_hint
    except Exception as e:  # missing Tofu deps in this venv
        pytest.skip(f'Tofu _coerce not importable here: {e}')
    return _extract_read_only_hint


def test_host_parses_exactly_our_read_set():
    """The host must resolve the SAME 9 tools we declared read-only.

    This is the assertion that would have caught a supplier/consumer spelling
    mismatch: the annotations can be perfectly correct on the wire and still
    resolve to nothing on the other side.
    """
    extract = _host_extractor()
    got = {t.name for t in srv._TOOLS if extract(t)}
    assert got == EXPECTED_READ_ONLY, (
        f'host resolved {sorted(got)} but we declared '
        f'{sorted(EXPECTED_READ_ONLY)}')


def test_host_sees_every_write_tool_as_a_write():
    extract = _host_extractor()
    leaked = [n for n in EXPECTED_WRITE if extract(_tool(n))]
    assert not leaked, (
        f'write tools the host would run in parallel without approval: {leaked}')
