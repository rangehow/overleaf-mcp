# 🍃 Overleaf MCP Server

The most comprehensive [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for [Overleaf](https://www.overleaf.com).

**18 tools** covering full CRUD, LaTeX structure analysis, git history & diff, PDF compilation, and download — all from your AI assistant.

**Simple setup** — set two environment variables (`OVERLEAF_SESSION`, optional `OVERLEAF_GIT_TOKEN`) when registering the MCP server, and you're done.

```
You: "List my projects"
AI:  [list_projects] You have 5 projects. Which one?

You: "Read main.tex from my thesis"
AI:  [read_file] Here's the content: …

You: "Rewrite the abstract to be more concise"
AI:  [edit_file] ✓ Edited and pushed

You: "Compile and download the PDF"
AI:  [compile_project + download_pdf] PDF saved to ~/Desktop/thesis.pdf ✓
```

> Note: earlier versions shipped an `overleaf_setup` / `overleaf_save_credentials` wizard. These tools have been removed — credentials are now read straight from environment variables.

---

## 🆚 Comparison with Other Overleaf MCP Servers

| Capability | **This Project** | tamirsida | GhoshSrinjoy | anu2711 | Junfei-Z | mjyoo2 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Language** | Python | Python | JS | Python | JS | JS |
| **Auth** | Git + Cookie | Git | Git | Cookie | Git | Git |
| list_projects | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| list_files | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| read_file | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| get_sections | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| get_section_content | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| **create_file** | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ |
| **create_project** | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **edit_file** (surgical) | ✅ | ✅ | ✅* | ✅ | ✅* | ❌ |
| **rewrite_file** | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| **update_section** | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **delete_file** | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **list_history** | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| **get_diff** | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| **compile_project** | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ |
| **download_pdf** | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ |
| **download_log** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **page/layout (SyncTeX)** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| sync_project | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| status_summary | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ |
| Multi-project | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Thread-safe locking | ✅ | ❌ | ✅ (Redis) | ❌ | ❌ | ❌ |
| Docker support | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| Free Overleaf tier | ✅† | ❌ | ❌ | ✅ | ❌ | ❌ |
| **Total tools** | **21** | 12 | 9 | 6 | 8 | 6 |

\* Full file rewrite only, not surgical old→new replacement.
† Compile/download tools work on the free tier via session cookie; Git-based tools require Git integration.

---

## ✨ Key Advantages

- **21 tools** — the most of any Overleaf MCP server
- **Zero config** — just two env vars, no config files, no per-project setup
- **Dual auth** — Git tokens for read/write + session cookies for compile/download
- **Surgical edits** — `edit_file` does exact search-and-replace (like `sed`), not full rewrites
- **LaTeX-aware** — section hierarchy parsing with indented previews
- **Git history & diff** — review changes, compare versions, filter by file/date
- **Compilation** — trigger builds and download PDFs without leaving your AI chat
- **Compilation logs** — `download_log` for debugging LaTeX errors
- **Page-layout perception** — `get_page_count` / `locate_in_pdf` / `section_page_map` expose *where* content lands in the rendered PDF (via SyncTeX), so the AI can answer "which page is section X on?" and drive a fill-exactly-N-pages loop
- **Thread-safe** — per-project locks prevent concurrent git corruption
- **Clean Python** — modular architecture, type hints, async throughout

---

## 📦 Installation

### Option 1: pip (recommended)

The package is published on PyPI as **`overleaf-mcp-plus`** (the plain
`overleaf-mcp` name was taken by an unrelated project). The CLI entry
point is still `overleaf-mcp`.

```bash
# Core (git-based tools only)
pip install overleaf-mcp-plus

# With compile/download support (recommended)
pip install "overleaf-mcp-plus[compile]"
```

Or run directly without installing, via `uvx`:

```bash
uvx --from "overleaf-mcp-plus[compile]" overleaf-mcp
```

### Option 2: From source

```bash
git clone https://github.com/rangehow/overleaf-mcp.git
cd overleaf-mcp
pip install -e .

# Or with uv
uv venv && uv pip install -e .
```

### Option 3: Docker

```bash
git clone https://github.com/rangehow/overleaf-mcp.git
cd overleaf-mcp
docker compose build
```

---

## 🔧 Setup

Configure credentials via environment variables. There are two credentials
for two different sets of features — you can supply either or both.

### Step 1 — Get the session cookie (required)

The `overleaf_session2` cookie is **HttpOnly**, so it can only be read
from the browser's DevTools panel (not from JavaScript / the console).

1. Log into <https://www.overleaf.com>.
2. Press <kbd>F12</kbd> → **Application** tab (Firefox: *Storage*; Safari:
   *Storage*).
3. Expand **Cookies → https://www.overleaf.com**.
4. Copy the **Value** of the row named `overleaf_session2`. It starts
   with `s%3A…`.

### Step 2 — Get the git token (optional, for edit tools)

1. Open <https://www.overleaf.com/user/settings>.
2. Scroll to **Git Integration → Create Token**.
3. Copy the token (it starts with `olp_`; shown only once).

### Step 3 — Pass them to the server

Point your MCP client at `overleaf-mcp` and provide the env vars. In
Claude Desktop / Claude Code (`claude_desktop_config.json` or `.mcp.json`):

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "overleaf-mcp",
      "env": {
        "OVERLEAF_SESSION": "s%3A...",
        "OVERLEAF_GIT_TOKEN": "olp_..."
      }
    }
  }
}
```

In chatui's MCP catalog the **Overleaf** card prompts for these two fields
at install time — no shell setup needed.

### Two credentials, different purposes

| Credential | What you can do | When you need it |
|---|---|---|
| **Session cookie** (`overleaf_session2`) | `list_projects`, compile, download PDF, download log, create project, **plus the full `web_*` tool family below** (list/read/rewrite/edit/upload/delete files, section parsing, status summary, citation verification) | Works on the free tier. Expires ~30 days - refresh when tools start failing with auth errors. |
| **Git token** (`olp_...`) | Read/write/edit files via git, commit history, diffs (the non-`web_`-prefixed tools) | Optional. Only needed for git-based history/diff, or if you prefer git semantics over the `web_*` tools. Requires a paid plan (Git Integration). |

You can start with just the cookie and add the git token later. **Free-tier accounts** (no paid plan, `OVERLEAF_GIT_TOKEN` unavailable) get full CRUD via the `web_*` tools — see [Free-Tier Web Tools](#-free-tier-web-tools-session-cookie-only) below.

### All Environment Variables

| Variable | Description |
|---|---|
| `OVERLEAF_SESSION` | Session cookie. Required for list_projects / compile / pdf / create_project. |
| `OVERLEAF_GIT_TOKEN` | Git token. Required for read / edit / history / diff tools. |
| `OVERLEAF_BASE_URL` | Overleaf URL (default: `https://www.overleaf.com`) |
| `OVERLEAF_GIT_HOST` | Git host (default: `git.overleaf.com`) |
| `OVERLEAF_TEMP_DIR` | Local git cache (default: `./overleaf_cache`) |
| `OVERLEAF_GIT_AUTHOR_NAME` | Git author name (default: `Overleaf MCP`) |
| `OVERLEAF_GIT_AUTHOR_EMAIL` | Git author email (default: `mcp@overleaf.local`) |
| `HISTORY_LIMIT_DEFAULT` | Default commit limit (default: `20`) |
| `HISTORY_LIMIT_MAX` | Max commit limit (default: `200`) |
| `DIFF_CONTEXT_LINES` | Diff context lines (default: `3`) |
| `DIFF_MAX_OUTPUT_CHARS` | Max diff size (default: `120000`) |

---

## 🔌 Client Setup

### Docker

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-T", "mcp"],
      "cwd": "/path/to/overleaf-mcp",
      "env": {
        "OVERLEAF_SESSION": "s%3A...",
        "OVERLEAF_GIT_TOKEN": "olp_..."
      }
    }
  }
}
```

(For Claude Desktop / Claude Code see the setup section above.)

---

## 🛠️ Tools Reference

### Create

| Tool | Description |
|---|---|
| `create_file` | Create a new file (auto-creates folders), commit & push |
| `create_project` | Create a new blank Overleaf project (requires session cookie) |

### Read

| Tool | Description |
|---|---|
| `list_projects` | List all projects in your Overleaf account |
| `list_files` | List files, optionally filtered by extension |
| `read_file` | Read file contents |
| `get_sections` | Parse LaTeX section hierarchy with previews |
| `get_section_content` | Get full content of a section by title |
| `verify_citations` | Detect likely-hallucinated references: verify each `.bib` entry's DOI/arXiv id against CrossRef & arXiv (zero LLM). Reports verified / suspicious / unverifiable buckets |
| `list_history` | Git commit history (with date/file/limit filters) |
| `get_diff` | Git diff between refs or working tree |
| `status_summary` | Project overview: file count, structure, status |

### Update

| Tool | Description |
|---|---|
| `edit_file` | Surgical search-and-replace (must match exactly once) |
| `rewrite_file` | Replace entire file contents |
| `update_section` | Update a LaTeX section body by title |
| `sync_project` | Pull latest changes from Overleaf |

### Delete

| Tool | Description |
|---|---|
| `delete_file` | Delete a file, commit & push |

### Compile & PDF

| Tool | Description |
|---|---|
| `compile_project` | Trigger PDF compilation on Overleaf |
| `download_pdf` | Download compiled PDF to local path |
| `download_log` | Download compilation log for debugging |

### Layout & page positions (SyncTeX)

| Tool | Description |
|---|---|
| `get_page_count` | Compile and report the total number of PDF pages (from the LaTeX log, PDF-parse fallback) |
| `locate_in_pdf` | Find which page (and bounding box) a given source line lands on, via SyncTeX |
| `section_page_map` | Map every section/subsection heading (and `\end{document}`) to its PDF page, plus **two fullness figures for the last page**: against the physical page (real height from the PDF MediaBox — correct for A4/Letter/custom `geometry`) *and* against the printable text body (bottom margin excluded, derived from the log's `geometry` dump) with a rough "N more lines fit" estimate. The text-area figure is the actionable one for a fill-exactly-N-pages workflow — the "where is each element on the page" overview. Built by parsing `output.synctex.gz` **offline** (one artifact download + local decode, ~ms for the whole map) rather than one HTTP call per section, so it's O(1) requests and stays fast + reliable on long papers; falls back to the live `sync/code` endpoint if the artifact is missing |

---

## 🆓 Free-Tier Web Tools (session-cookie only)

Git Integration (`OVERLEAF_GIT_TOKEN`) requires a **paid** Overleaf plan. The
`web_*` tools below give **free-tier accounts** the same CRUD/structure
capabilities using only `OVERLEAF_SESSION` — no git token, no paid plan.

Overleaf's project file tree (folder/doc IDs) is only exposed over its
internal real-time channel, not any REST endpoint. `web_*` tools that need
those IDs do a single short-lived, read-only round trip over that channel
(connect → `joinProject` → disconnect — no live editing/OT) to discover them,
then perform the actual read/write via plain REST calls (the same endpoints
the web UI's file tree and drag-and-drop uploader use). See
`src/overleaf_mcp/realtime.py` for the implementation and rationale.

| Tool | Free-tier equivalent of | Description |
|---|---|---|
| `web_list_files` | `list_files` | List file paths in a project |
| `web_read_file` | `read_file` | Read a file's full content |
| `web_get_sections` | `get_sections` | Parse LaTeX section hierarchy with previews |
| `web_get_section_content` | `get_section_content` | Get full content of a section by title |
| `web_status_summary` | `status_summary` | Project overview: file count, structure |
| `web_verify_citations` | `verify_citations` | Same hallucinated-reference detection as `verify_citations`, reading via the web path |
| `web_rewrite_file` | `rewrite_file` / `create_file` | Create or fully overwrite a file's content |
| `web_edit_file` | `edit_file` | Surgical search-and-replace (must match exactly once) |
| `web_update_section` | `update_section` | Update a LaTeX section body by title |
| `web_upload_file` | `upload_file` | Upload a (possibly binary) file from **base64-encoded content** — no local filesystem access needed on the MCP host, useful for chat clients attaching images directly |
| `web_delete_file` | `delete_file` | Delete a file/doc |

**Known limitations:**
- These tools do not create missing intermediate folders — the parent
  folder of `file_path` must already exist.
- There is no free-tier equivalent of `list_history` / `get_diff` (real git
  log/diff) or `sync_project` (a git-pull concept that doesn't apply since
  `web_*` tools always read live from the server).
- Overwriting an existing **binary** file (e.g. via `web_upload_file`)
  creates a new `entity_id` each time (Overleaf's own versioning behavior
  for binary files); overwriting a **text** doc keeps the same `entity_id`.

---

## 💡 Usage Examples

```
"List my projects"
"List all files in project 64a1b2c3d4e5f6a7b8c9d0e1"
"Read main.tex"
"Show me the sections in chapter1.tex"
"Get the content of the Introduction section"

"How many pages does project 64a1b2c3d4e5f6a7b8c9d0e1 compile to?"
"Which page does each section land on?"   (→ section_page_map)
"Trim the intro so the paper fills exactly 7 pages"  (measure → edit → recompile loop)

"Fix the typo: change 'teh' to 'the' in main.tex"
"Rewrite the abstract with: [new text]"
"Update the Methods section with: [new content]"
"Create a new file references.bib with: [content]"
"Delete the old appendix.tex file"

"Show me the last 5 commits"
"What changed in main.tex since last week?"
"Show the diff between HEAD~3 and HEAD"

"Compile my thesis and tell me if it succeeded"
"Download the PDF to ~/Desktop/thesis.pdf"
"Show me the compilation log — I have an error"

"I'm on the free tier — read main.tex"              (→ web_read_file)
"Replace the intro paragraph with: [new text]"       (→ web_edit_file)
"Upload this image to figures/plot.png"              (→ web_upload_file, base64 content)
"Delete the old draft.tex"                            (→ web_delete_file)
```

---

## 🏗️ Architecture

```
┌─────────────────┐  MCP (stdio)  ┌──────────────────────────┐
│  AI Assistant    │◄─────────────►│  overleaf-mcp server     │
│  (Claude, etc.)  │               │                          │
└─────────────────┘               │  ┌────────────────────┐  │
                                   │  │ Git Client         │  │── Git HTTPS ──► git.overleaf.com
                                   │  │ (clone/pull/push)  │  │
                                   │  └────────────────────┘  │
                                   │  ┌────────────────────┐  │
                                   │  │ LaTeX Parser       │  │
                                   │  │ (sections/struct)  │  │
                                   │  └────────────────────┘  │
                                   │  ┌────────────────────┐  │
                                   │  │ Compile Module     │  │── HTTPS ──► www.overleaf.com
                                   │  │ (PDF/log download) │  │
                                   │  └────────────────────┘  │
                                   └──────────────────────────┘
```

**Modules:**
- `credentials.py` — Read session cookie + git token from environment variables
- `config.py` — Project config + Overleaf endpoints
- `git_client.py` — Git operations (clone, pull, push, diff, history)
- `latex.py` — LaTeX document structure parsing
- `compile.py` — PDF compilation and download (uses session cookie)
- `server.py` — MCP server with 18 tool definitions

---

## 🪪 Local-copy metadata sidecar

Whenever `download_source` extracts a project to a local directory, or
`ensure_repo` clones one into the git cache, the server drops a small
`.overleaf-project.json` file at the root of that copy:

```json
{
  "project_id": "69e46cd469c0fc49d2627320",
  "name": "[阿布] 毕业论文魔改版",
  "overleaf_url": "https://www.overleaf.com/project/69e46cd469c0fc49d2627320",
  "source": "zip",
  "downloaded_at": "2026-04-22T10:54:55+00:00"
}
```

This lets any tool (including an AI assistant) unambiguously identify
which Overleaf project a local working directory corresponds to — no
more guessing by file names. For git-backed copies the file is added to
`.git/info/exclude` so it is never pushed back to Overleaf.

## 🔒 Security

- Credentials are read only from environment variables — nothing is persisted to disk by the server
- Path traversal protection on all file operations
- Per-project thread locks prevent concurrent git corruption
- Session cookie expiry surfaces as a clear auth error, prompting the user to refresh `OVERLEAF_SESSION`

---

## 🐳 Self-Hosted Overleaf

For self-hosted Overleaf instances:

```bash
export OVERLEAF_BASE_URL="https://your-overleaf.example.com"
export OVERLEAF_GIT_HOST="your-overleaf.example.com"
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

Inspired by and building upon ideas from:
- [tamirsida/overleaf_mcp](https://github.com/tamirSida/overleaf_MCP) — Full CRUD Python server
- [GhoshSrinjoy/Overleaf-mcp](https://github.com/GhoshSrinjoy/Overleaf-mcp) — Redis queue, history/diff
- [anu2711/overleaf-mcp](https://github.com/anu2711/overleaf-mcp) — Compile/download via session cookie
- [mjyoo2/OverleafMCP](https://github.com/mjyoo2/OverleafMCP) — Original read-only MCP (103 ⭐)
- [Junfei-Z/overleaf-claude-mcp](https://github.com/Junfei-Z/overleaf-claude-mcp) — Write/push fork
