"""LaTeX document structure parsing utilities."""

from __future__ import annotations

import re
from typing import Any

# Ordered hierarchy from broadest to narrowest
SECTION_LEVELS = [
    "part",
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "subparagraph",
]

SECTION_PATTERN = re.compile(
    r"\\(" + "|".join(SECTION_LEVELS) + r")\*?\{([^}]+)\}",
    re.MULTILINE,
)


def parse_sections(content: str) -> list[dict[str, Any]]:
    """Parse LaTeX content and extract all sectioning commands.

    Returns a list of dicts with keys:
      type, title, preview, start_pos, end_pos, level
    """
    matches = list(SECTION_PATTERN.finditer(content))
    sections: list[dict[str, Any]] = []

    for i, m in enumerate(matches):
        sec_type = m.group(1)
        title = m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        preview = body[:200] + "…" if len(body) > 200 else body

        sections.append(
            {
                "type": sec_type,
                "title": title,
                "preview": preview,
                "start_pos": m.start(),
                "end_pos": end,
                "level": SECTION_LEVELS.index(sec_type),
            }
        )

    return sections


def get_section_content(content: str, title: str) -> str | None:
    """Return the full text (including header) of the section with *title*."""
    for sec in parse_sections(content):
        if sec["title"].lower() == title.lower():
            return content[sec["start_pos"]:sec["end_pos"]]
    return None


def update_section(
    content: str,
    title: str,
    new_body: str,
) -> str | None:
    """Replace the body of the named section, preserving the header.

    Returns the full updated file content, or ``None`` if the section
    was not found.
    """
    sections = parse_sections(content)
    for sec in sections:
        if sec["title"].lower() != title.lower():
            continue

        # Find where the header ends
        header_re = re.compile(
            rf"\\{sec['type']}\*?\{{{re.escape(sec['title'])}\}}"
        )
        hm = header_re.search(content)
        if not hm:
            return None

        header_end = hm.end()
        return (
            content[:header_end]
            + "\n"
            + new_body.strip()
            + "\n"
            + content[sec["end_pos"]:]
        )

    return None
