"""The README's Tools Reference must document exactly the tools the server exposes."""

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from overleaf_mcp.server import _TOOLS  # noqa: E402


def test_readme_tools_reference_matches_server():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## 🛠️ Tools Reference", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.M))
    exposed = {t.name for t in _TOOLS}
    assert documented == exposed
    assert f"**{len(exposed)} tools**" in readme
