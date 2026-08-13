"""Cross-repo contract for chatui's progressive MCP tool selection.

Overleaf exposes 25 tools, above chatui's default inline limit of 16. These
tests drive chatui's real selector so Chinese user intents do not fall back to
an arbitrary alphabetical subset of the English-only catalogue.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

from overleaf_mcp import server as srv


def _host_selector():
    root = os.environ.get('TOFU_ROOT') or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'chatui')
    selector_py = os.path.join(root, 'lib', 'mcp', 'tool_search.py')
    if not os.path.isfile(selector_py):
        pytest.skip('chatui checkout not available next to this repo')
    if root not in sys.path:
        sys.path.insert(0, root)
    from lib.mcp.tool_search import select_active_mcp_tools
    return select_active_mcp_tools


def _snapshot():
    rows = []
    for tool in srv._TOOLS:
        namespaced = f'mcp__overleaf__{tool.name}'
        rows.append({
            'server_id': 'overleaf',
            'tool_name': tool.name,
            'namespaced_name': namespaced,
            'description': tool.description or '',
            'openai_def': {
                'type': 'function',
                'function': {
                    'name': namespaced,
                    'description': tool.description or '',
                    'parameters': tool.input_schema,
                },
            },
            'meta': dict(tool.meta or {}),
            'catalog_version': '0.3.1',
        })
    return rows


def _selected(query: str) -> list[str]:
    definitions = _host_selector()(
        _snapshot(), task_id=f'overleaf-contract-{uuid.uuid4()}',
        query=query, limit=8)
    prefix = 'mcp__overleaf__'
    return [row['function']['name'].removeprefix(prefix) for row in definitions]


@pytest.mark.parametrize(('query', 'expected'), [
    ('列出我的 Overleaf 项目', 'list_projects'),
    ('检查论文引用是否有幻觉', 'verify_citations'),
    ('定位 main.tex 第 80 行在 PDF 哪一页', 'locate_in_pdf'),
    ('修改摘要章节', 'update_section'),
    ('下载并解压 Overleaf 源码', 'download_source'),
    ('查看 LaTeX 编译错误', 'download_log'),
])
def test_chinese_intent_retrieves_expected_tool_first(query, expected):
    assert _selected(query)[0] == expected


def test_workflow_bundle_brings_safe_companion_tools_into_view():
    selected = _selected('修改摘要章节')
    assert selected[0] == 'update_section'
    assert {'edit_file', 'rewrite_file'} <= set(selected)
