"""PDF layout perception for an Overleaf project.

Answers questions the source-level parser (``latex.py``) fundamentally
cannot — because they depend on how TeX laid the document out:

  - How many pages does the compiled PDF have?
  - Which page (and where on it) does a given source line land on?
  - Which page does each ``\\section`` start on?

The mechanism is SyncTeX, exposed by Overleaf's web tier as the
``GET /project/<id>/sync/code`` endpoint (the same one that powers the
editor's click-to-jump). It maps a source ``(file, line, column)`` to one
or more PDF rectangles ``{page, h, v, width, height}`` in PostScript points.

Critical protocol details (all discovered by probing the live API and
reading Overleaf's ``CompileController.proxySyncCode`` / ``_syncTeX``):

  1. The sync endpoint REQUIRES both ``editorId`` and ``buildId`` query
     params — ``_syncTeX`` throws (→ HTTP 500) if either is missing. The
     ``buildId`` comes from the compile response's output files; the
     ``editorId`` is a client-minted UUID that MUST be sent in the compile
     POST body and then reused on the sync call (the server ties the
     synctex artifact to the editorId that requested the build).
  2. The ``file`` param is the label SyncTeX recorded, NOT necessarily the
     project-relative path. TeX writes ``dir/./file.tex`` for a root doc in
     a subfolder, and Overleaf's controller explicitly tolerates the
     ``/./`` form. We derive the exact labels from the ``Input:`` lines of
     ``output.synctex.gz`` and pick the compile ROOT automatically, so the
     caller does not have to guess which of several ``.tex`` files is root.
  3. Page count is read from ``output.log`` (pdfTeX's
     ``Output written on ... (N pages`` line), with a PDF-parse fallback.

This module needs only the session cookie (like the rest of ``compile.py``)
— no git token.
"""

from __future__ import annotations

import bisect
import gzip
import logging
import re
import uuid

import httpx

from .compile import _build_output_url, _csrf_token, _headers
from .config import OVERLEAF_BASE_URL

logger = logging.getLogger("overleaf-mcp")

# ``Output written on output.pdf (8 pages, 123456 bytes).`` — pdfTeX/LuaTeX.
# The filename between ``on`` and ``(`` varies (output.pdf, output.dvi→pdf),
# so we match loosely up to the page count.
_PAGES_RE = re.compile(r"Output written on\s+\S+\s+\((\d+)\s+pages?", re.IGNORECASE)

# ``Input:<tag>:<absolute-path>`` records inside a decompressed .synctex.gz.
_SYNCTEX_INPUT_RE = re.compile(r"Input:\d+:([^\n]+)")

# TeX Live tree / system files we never want to treat as a candidate root.
_SYSTEM_PATH_HINT = "/texmf"

# The container prefix Overleaf compiles under; stripped to get the label
# the web ``sync/code`` endpoint expects.
_COMPILE_PREFIX = "/compile/"

# SyncTeX stores coordinates in "scaled points" (sp): 1 TeX pt = 65536 sp, and
# 1 TeX pt = 1/72.27 in while PDF/PostScript "big points" (bp, what sync/code
# returns) are 1/72 in. So sp → bp is ``× (72/72.27) / 65536``. Verified: this
# reproduces the live endpoint's h/v/width/height to < 0.001 bp.
_SP_TO_BP = (72.0 / 72.27) / 65536.0

# A SyncTeX box/hbox/vbox record:
#   TYPE TAG,LINE:H,V:WIDTH,HEIGHT,DEPTH
# TYPE ∈ ``( [ h v`` (open-box / hbox / vbox). ``h``/``height`` are ignored for
# page mapping; we need PAGE, LINE, and the vertical baseline ``V+DEPTH`` (the
# endpoint returns the box BOTTOM, i.e. V + Depth — matching to 3 decimals).
_SYNCTEX_BOX_RE = re.compile(
    r"^[\(\[hv](\d+),(\d+):(-?\d+),(-?\d+):(-?\d+),(-?\d+),(-?\d+)"
)

# Slack (bp) when deciding a box is still "within the text body" vs below it
# (the footer). A body box can round a hair past the geometry text bottom;
# 3 bp keeps the last body line while excluding the footer (which sits a full
# footskip — tens of bp — lower).
_BODY_BOTTOM_EPS_BP = 3.0


def _compile_with_editor_id(project_id: str, editor_id: str) -> dict:
    """Trigger a compile that tags its synctex artifact with ``editor_id``.

    Mirrors :func:`compile.compile_project` but threads the client-minted
    ``editorId`` into the POST body — mandatory for a subsequent
    ``sync/code`` call to succeed (see module docstring, point 1).

    Returns the raw compile JSON (status, outputFiles, clsiServerId, …).
    """
    csrf = _csrf_token(project_id)
    r = httpx.post(
        f"{OVERLEAF_BASE_URL}/project/{project_id}/compile",
        json={
            "check": "silent",
            "draft": False,
            "incrementalCompilesEnabled": True,
            "stopOnFirstError": False,
            "editorId": editor_id,
        },
        headers={
            **_headers(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-csrf-token": csrf,
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def _build_id(compile_info: dict) -> str | None:
    """Extract the build id shared by every output file of a compile."""
    for f in compile_info.get("outputFiles", []):
        if f.get("build"):
            return f["build"]
    return None


def _output_file(compile_info: dict, *, path=None, suffix=None) -> dict | None:
    """Find an output file by exact ``path`` or by filename ``suffix``."""
    for f in compile_info.get("outputFiles", []):
        p = f.get("path", "")
        if path is not None and p == path:
            return f
        if suffix is not None and p.endswith(suffix):
            return f
    return None


def _fetch_output_text(compile_info: dict, out_file: dict) -> str:
    """Download a per-build output file as text (adds the mandatory clsi id)."""
    url = out_file.get("url") or ""
    full = _build_output_url(url, compile_info.get("clsi_server_id") or compile_info.get("clsiServerId"))
    r = httpx.get(full, headers=_headers(), follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.text


def _fetch_output_bytes(compile_info: dict, out_file: dict) -> bytes:
    """Download a per-build output file as bytes (adds the mandatory clsi id)."""
    url = out_file.get("url") or ""
    full = _build_output_url(url, compile_info.get("clsi_server_id") or compile_info.get("clsiServerId"))
    r = httpx.get(full, headers=_headers(), follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.content


def _fetch_log_text(compile_info: dict) -> str | None:
    """Download ``output.log`` once as text (None if absent/unfetchable)."""
    log = _output_file(compile_info, suffix=".log")
    if not log:
        return None
    try:
        return _fetch_output_text(compile_info, log)
    except Exception as e:
        logger.warning("[layout] could not fetch log: %s", e)
        return None


def _pages_from_log_text(log_text: str | None) -> int | None:
    """Parse the pdfTeX page count from log text. None if not found."""
    if not log_text:
        return None
    m = _PAGES_RE.search(log_text)
    return int(m.group(1)) if m else None


def _pages_from_log(compile_info: dict) -> int | None:
    """Parse the pdfTeX page count from ``output.log``. None if not found."""
    return _pages_from_log_text(_fetch_log_text(compile_info))


# TeX point (1/72.27 in) → PDF/PostScript "big point" (1/72 in). Coordinates
# from SyncTeX / the MediaBox are in bp; \dimen values in the log are in pt.
_TEX_PT_TO_BP = 72.0 / 72.27
# One inch in TeX points — LaTeX's fixed top reference offset before \voffset.
_ONE_INCH_PT = 72.27

# ``* \field=NNNpt`` lines the geometry package writes to the log.
_GEOMETRY_FIELD_RE = re.compile(r"^\*\s*\\([A-Za-z]+)\s*=\s*(-?[\d.]+)pt", re.MULTILINE)


def _parse_geometry_fields(log_text: str | None) -> dict[str, float]:
    """Extract the ``* \\field=NNNpt`` vertical-layout block from the log.

    The geometry package (and the LaTeX kernel's ``\\showthe``-style dump)
    prints the resolved page dimensions here. Returns a name→pt mapping
    (e.g. ``{'textheight': 704.6, 'topmargin': -38.1, ...}``); empty when the
    document uses no geometry/layout dump.
    """
    if not log_text:
        return {}
    out: dict[str, float] = {}
    for name, val in _GEOMETRY_FIELD_RE.findall(log_text):
        try:
            out[name] = float(val)
        except (ValueError, TypeError) as e:
            logger.debug("[layout] geometry field %s parse failed: %s", name, e)
    return out


def _text_block_bottom_bp(fields: dict[str, float]) -> float | None:
    """Compute the text-body bottom edge (pt from page top, in bp) from geometry.

    LaTeX places the top of the header ``1in + \\voffset + \\topmargin`` below
    the page top; the text body then starts a further ``\\headheight +
    \\headsep`` down and spans ``\\textheight``. So::

        text_top    = 1in + voffset + topmargin + headheight + headsep   (pt)
        text_bottom = text_top + textheight                              (pt)

    Converted pt→bp to match SyncTeX/MediaBox coordinates. This is the
    boundary of the printable body — content whose baseline sits at
    ``text_bottom`` fills the page's text area exactly (verified: on the ACL
    paper this equals the observed last-line ``v`` to 3 decimals). Returns
    None if the required fields are absent.
    """
    required = ("textheight", "topmargin", "headheight", "headsep")
    if not all(k in fields for k in required):
        return None
    top_pt = (
        _ONE_INCH_PT
        + fields.get("voffset", 0.0)
        + fields["topmargin"]
        + fields["headheight"]
        + fields["headsep"]
    )
    bottom_pt = top_pt + fields["textheight"]
    return bottom_pt * _TEX_PT_TO_BP


def _pages_from_pdf(compile_info: dict) -> int | None:
    """Fallback: count PDF pages by scanning the PDF bytes for /Type /Page.

    Deliberately dependency-free (no pypdf) — a regex over the raw PDF is
    good enough for a page COUNT and keeps the compile-extra footprint
    small. Counts ``/Type /Page`` while excluding ``/Pages`` nodes.
    """
    pdf = _output_file(compile_info, path="output.pdf")
    if not pdf:
        return None
    try:
        data = _fetch_output_bytes(compile_info, pdf)
    except Exception as e:
        logger.warning("[layout] could not fetch PDF for page count: %s", e)
        return None
    # /Type /Page (not /Pages). Allow optional whitespace, forbid a trailing 's'.
    count = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    return count or None


def _parse_mediabox_height(pdf_bytes: bytes) -> float | None:
    """Return the page height in PostScript points from the PDF MediaBox.

    Pure/offline: regex the FIRST ``/MediaBox [x0 y0 x1 y1]`` in the raw PDF
    (this is the page/root box; individual pages inherit it unless overridden,
    and the overwhelmingly common case is a uniform page size). Height is
    ``y1 - y0``. Returns None when no MediaBox is present (e.g. a
    linearised/object-stream PDF whose box is inside a compressed stream) so
    the caller can fall back explicitly rather than silently assume a size.

    This is what makes "how full is the last page" correct for A4
    (≈841.9 pt), Letter (792 pt), and custom ``geometry`` heights alike —
    replacing the previously hard-coded 792.
    """
    m = re.search(
        rb"/MediaBox\s*\[\s*([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s*\]",
        pdf_bytes,
    )
    if not m:
        return None
    try:
        y0 = float(m.group(2))
        y1 = float(m.group(4))
    except (ValueError, TypeError) as e:
        logger.debug("[layout] MediaBox parse failed: %s", e)
        return None
    height = y1 - y0
    return height if height > 0 else None


# Documented fallback ONLY when the PDF carries no readable MediaBox. Reported
# via page_height_source='assumed_letter' so it is never mistaken for a
# measured value — the hard-coded constant can no longer masquerade as truth.
_ASSUMED_LETTER_HEIGHT_PT = 792.0


def _page_height(compile_info: dict) -> tuple[float | None, str]:
    """Resolve the true page height (pt) + its source label.

    Order: PDF MediaBox (measured) → assumed Letter (explicit fallback).
    Returns ``(height, source)`` with source in
    ``{'mediabox', 'assumed_letter', 'unknown'}``.
    """
    pdf = _output_file(compile_info, path="output.pdf")
    if pdf:
        try:
            data = _fetch_output_bytes(compile_info, pdf)
            h = _parse_mediabox_height(data)
            if h is not None:
                return h, "mediabox"
        except Exception as e:
            logger.warning("[layout] could not read PDF MediaBox: %s", e)
    return _ASSUMED_LETTER_HEIGHT_PT, "assumed_letter"


def get_page_count(project_id: str) -> dict:
    """Compile the project and return its total page count.

    Returns ``{status, pages, source}`` where ``source`` is ``'log'`` or
    ``'pdf'`` (which artifact the count came from), or ``pages=None`` if
    neither could be determined.
    """
    editor_id = str(uuid.uuid4())
    info = _compile_with_editor_id(project_id, editor_id)
    status = info.get("status")
    pages = _pages_from_log(info)
    source = "log"
    if pages is None:
        pages = _pages_from_pdf(info)
        source = "pdf"
    return {"status": status, "pages": pages, "source": source if pages is not None else None}


def _synctex_input_labels(compile_info: dict) -> list[str]:
    """Return the ordered, de-duplicated project source labels SyncTeX saw.

    Reads ``output.synctex.gz``, extracts ``Input:`` paths, strips the
    ``/compile/`` container prefix, and drops TeX Live system files — what
    remains are the labels the ``sync/code`` ``file`` param accepts. The
    first entry is the compile ROOT.
    """
    syn = _output_file(compile_info, path="output.synctex.gz")
    if not syn:
        return []
    try:
        raw = gzip.decompress(_fetch_output_bytes(compile_info, syn)).decode("latin-1")
    except Exception as e:
        logger.warning("[layout] could not read synctex.gz: %s", e)
        return []
    labels: list[str] = []
    for p in _SYNCTEX_INPUT_RE.findall(raw):
        p = p.strip()
        if _SYSTEM_PATH_HINT in p:
            continue
        if p.startswith(_COMPILE_PREFIX):
            p = p[len(_COMPILE_PREFIX):]
        if p and p not in labels:
            labels.append(p)
    return labels


def _fetch_and_parse_synctex(compile_info: dict) -> dict | None:
    """Download ``output.synctex.gz`` once and decode it. None if unavailable.

    None means the artifact is missing/corrupt (e.g. a cached incremental
    compile that didn't emit synctex) — the caller then falls back to the
    live ``sync/code`` endpoint.
    """
    syn = _output_file(compile_info, path="output.synctex.gz")
    if not syn:
        logger.warning("[layout] no output.synctex.gz in compile output")
        return None
    try:
        raw = gzip.decompress(_fetch_output_bytes(compile_info, syn)).decode("latin-1")
    except Exception as e:
        logger.warning("[layout] could not fetch/decompress synctex.gz: %s", e)
        return None
    parsed = parse_synctex(raw)
    if not parsed["by_line"]:
        logger.warning("[layout] synctex.gz parsed but held no box records")
        return None
    return parsed


def parse_synctex(raw: str) -> dict:
    """Decode a raw (decompressed) ``.synctex.gz`` body into a queryable map.

    This is the OFFLINE engine that lets ``section_page_map`` resolve every
    section→page from ONE artifact download instead of one ``sync/code`` HTTP
    call per section (which, on a long paper, is both ~1s/section slow AND
    races CLSI build eviction). The decoder is calibrated against the live
    endpoint: for a source ``(tag,line)`` the endpoint returns the FIRST
    box record's ``page`` and ``v = (V+Depth)·k`` — reproduced here to
    < 0.001 bp.

    Returns::

        {
          "inputs":   {tag:int -> label:str},   # e.g. 1 -> 'latex/./acl_latex.tex'
          "by_line":  {(tag,line) -> {"page","v","h","width","height"}},  # first box
          "lines_by_tag": {tag:int -> sorted [line:int]},  # for proximity search
          "page_bottoms": {page:int -> max_v_bp:float},  # deepest content on page
          "max_page": int,
        }

    ``by_line`` holds the FIRST box for each source line (what forward-sync
    returns). Not every source line emits a box (a ``\\subsection`` /
    ``\\paragraph`` heading often doesn't), so ``lines_by_tag`` lets a lookup
    fall back to the NEAREST boxed line — which is exactly what SyncTeX's own
    forward-search does. ``page_bottoms`` holds the DEEPEST box baseline seen
    on each page — used (capped at the text-body bottom) to measure last-page
    fullness without a live call.
    """
    inputs: dict[int, str] = {}
    by_line: dict[tuple[int, int], dict] = {}
    lines_by_tag: dict[int, list[int]] = {}
    page_bottoms: dict[int, float] = {}
    page_box_vs: dict[int, list[float]] = {}
    page = None
    max_page = 0

    for line in raw.split("\n"):
        if not line:
            continue
        c = line[0]
        if c == "I":
            m = _SYNCTEX_INPUT_RE.match(line)
            if m:
                # Re-parse to recover the tag number too.
                mm = re.match(r"Input:(\d+):(.+)$", line)
                if mm:
                    p = mm.group(2).strip()
                    if p.startswith(_COMPILE_PREFIX):
                        p = p[len(_COMPILE_PREFIX):]
                    inputs[int(mm.group(1))] = p
            continue
        if c == "{":
            num = line[1:].strip()
            if num.isdigit():
                page = int(num)
                max_page = max(max_page, page)
            continue
        if c == "}":
            page = None
            continue
        if page is None:
            continue
        m = _SYNCTEX_BOX_RE.match(line)
        if not m:
            continue
        tag, ln, h_sp, v_sp, w_sp, ht_sp, d_sp = (int(x) for x in m.groups())
        v_bp = (v_sp + d_sp) * _SP_TO_BP
        key = (tag, ln)
        if key not in by_line:
            by_line[key] = {
                "page": page,
                "v": v_bp,
                "h": h_sp * _SP_TO_BP,
                "width": w_sp * _SP_TO_BP,
                "height": (ht_sp + d_sp) * _SP_TO_BP,
            }
            lines_by_tag.setdefault(tag, []).append(ln)
        if v_bp > page_bottoms.get(page, float("-inf")):
            page_bottoms[page] = v_bp
        page_box_vs.setdefault(page, []).append(v_bp)

    for tag in lines_by_tag:
        lines_by_tag[tag].sort()

    return {
        "inputs": inputs,
        "by_line": by_line,
        "lines_by_tag": lines_by_tag,
        "page_bottoms": page_bottoms,
        "page_box_vs": page_box_vs,
        "max_page": max_page,
    }


def _body_bottom_on_page(parsed: dict, page: int, text_bottom: float | None) -> float | None:
    """Deepest content baseline on ``page`` that lies WITHIN the text body.

    The absolute ``page_bottoms[page]`` is polluted by the page-number FOOTER,
    which sits BELOW the text body (its ``v`` exceeds the geometry text bottom).
    Using it makes a full page read as >100%% full. When we know the text-body
    bottom, cap candidates at ``text_bottom + epsilon`` and take the deepest of
    those; this yields the true last line of body content. Without geometry we
    fall back to the absolute deepest (best available).
    """
    vs = parsed.get("page_box_vs", {}).get(page)
    if not vs:
        return parsed.get("page_bottoms", {}).get(page)
    if text_bottom is None:
        return max(vs)
    body = [v for v in vs if v <= text_bottom + _BODY_BOTTOM_EPS_BP]
    return max(body) if body else None


def _synctex_tags_for_label(parsed: dict, label: str) -> list[int]:
    """Return the SyncTeX input tag(s) whose recorded path matches ``label``.

    Matches on the ``/./``-normalised path so a caller's label
    (``latex/./acl_latex.tex`` or ``./main.tex``) hits the recorded form.
    """
    def norm(s: str) -> str:
        return s.replace("/./", "/").lstrip("./")

    target = norm(label)
    tags = [t for t, p in parsed["inputs"].items() if norm(p) == target]
    if not tags:  # basename fallback
        base = target.rsplit("/", 1)[-1]
        tags = [t for t, p in parsed["inputs"].items() if norm(p).rsplit("/", 1)[-1] == base]
    return tags


def _page_for_line(parsed: dict, tags: list[int], line: int) -> int | None:
    """Page for a source line via the offline map, with PROXIMITY fallback.

    An exact ``(tag,line)`` box wins. When the line emitted no box (common for
    ``\\subsection`` / ``\\paragraph`` headings), fall back to the NEAREST
    boxed line under the same tag — mirroring SyncTeX's own forward-search, so
    the offline result matches what the live endpoint returns for that line.
    Ties (equidistant boxes above and below) prefer the LATER line, i.e. the
    content that follows the heading. None only if the tag has no boxes at all.
    """
    for t in tags:
        rec = parsed["by_line"].get((t, line))
        if rec:
            return rec["page"]
    # proximity fallback
    for t in tags:
        boxed = parsed.get("lines_by_tag", {}).get(t)
        if not boxed:
            continue
        i = bisect.bisect_left(boxed, line)
        cands = []
        if i < len(boxed):
            cands.append(boxed[i])
        if i > 0:
            cands.append(boxed[i - 1])
        if cands:
            best = min(cands, key=lambda ln_: (abs(ln_ - line), -ln_))
            return parsed["by_line"][(t, best)]["page"]
    return None


def _resolve_label(labels: list[str], file: str | None) -> str | None:
    """Pick the SyncTeX label matching ``file`` (or the root if file is None).

    Matches on the ``/./``-normalised basename+path so a caller can pass the
    plain project-relative path (``latex/acl_latex.tex``) and still hit the
    ``latex/./acl_latex.tex`` label SyncTeX recorded.
    """
    if not labels:
        return None
    if file is None:
        return labels[0]  # compile root

    def norm(s: str) -> str:
        return s.replace("/./", "/").lstrip("./")

    target = norm(file)
    for lab in labels:
        if norm(lab) == target:
            return lab
    # Fall back to basename match (handles a bare filename argument).
    base = target.rsplit("/", 1)[-1]
    for lab in labels:
        if norm(lab).rsplit("/", 1)[-1] == base:
            return lab
    return None


def _sync_code(
    project_id: str,
    compile_info: dict,
    editor_id: str,
    label: str,
    line: int,
    column: int = 0,
) -> tuple[list[dict], bool]:
    """Call ``GET /sync/code`` for one source line.

    Returns ``(rects, failed)``:
      - ``rects`` — the PDF rectangles ``{page,h,v,width,height}`` (bp, ``v``
        from page top). Empty ``[]`` on both a legitimate no-mapping (a blank
        line / comment → HTTP 200 with ``pdf:[]``) AND on a failed call.
      - ``failed`` — True when the HTTP call did NOT succeed (non-200 or an
        exception). This lets the caller tell a genuinely-empty line
        (``[], False``) apart from a build that vanished mid-run
        (``[], True``) — a single failure must NOT abort the whole map, but a
        run where EVERY call fails means the build was evicted and the layout
        is unavailable, which we surface clearly instead of as an all-None map.

    A common cause of ``failed`` on a long document is CLSI build eviction:
    the compiled build (and its synctex artifact) can expire on the server
    partway through a long sequence of sync calls, after which every
    subsequent request 404s. We degrade gracefully rather than crash.
    """
    build_id = _build_id(compile_info)
    clsi = compile_info.get("clsi_server_id") or compile_info.get("clsiServerId")
    # Overleaf's proxySyncCode tolerates an INTERNAL 'dir/./file' label but
    # rejects a LEADING './' (its Path.resolve check throws → HTTP 500). A
    # top-level root is recorded by TeX as './main.tex'; send it as 'main.tex'.
    query_file = label[2:] if label.startswith("./") else label
    params = {
        "file": query_file,
        "line": str(line),
        "column": str(column),
        "editorId": editor_id,
        "buildId": build_id,
    }
    if clsi:
        params["clsiserverid"] = clsi
    try:
        r = httpx.get(
            f"{OVERLEAF_BASE_URL}/project/{project_id}/sync/code",
            params=params,
            headers=_headers(),
            follow_redirects=True,
            timeout=60,
        )
    except Exception as e:
        logger.warning("[layout] sync/code request failed for line %s: %s", line, e)
        return [], True
    if r.status_code != 200:
        # 404 = build/synctex gone (eviction) or bad label; 500 = server-side
        # synctex error. Log and degrade — this line just gets page=None.
        logger.warning(
            "[layout] sync/code line %s → HTTP %s (%.120s)",
            line, r.status_code, r.text.replace("\n", " "),
        )
        return [], True
    try:
        return r.json().get("pdf", []), False
    except Exception as e:
        logger.warning("[layout] sync/code line %s: bad JSON: %s", line, e)
        return [], True


def locate_in_pdf(project_id: str, line: int, file: str | None = None, column: int = 0) -> dict:
    """Locate a source ``(file, line)`` in the compiled PDF.

    Compiles the project (tagging the build so SyncTeX is queryable), then
    forward-syncs the requested line. If ``file`` is omitted the compile
    ROOT is used. Returns::

        {
          "status": "success",
          "file": "<resolved synctex label>",
          "line": <int>,
          "page": <int|None>,        # first rect's page
          "rects": [ {page,h,v,width,height}, ... ],
        }

    ``page`` is None (and ``rects`` empty) when SyncTeX has no mapping for
    the line — try a nearby non-blank line.
    """
    editor_id = str(uuid.uuid4())
    info = _compile_with_editor_id(project_id, editor_id)
    labels = _synctex_input_labels(info)
    label = _resolve_label(labels, file)
    if label is None:
        return {
            "status": info.get("status"),
            "error": (
                f"Could not resolve a SyncTeX label for file={file!r}. "
                f"Known source labels: {labels or '(none — compile may have failed)'}"
            ),
            "line": line,
            "page": None,
            "rects": [],
        }
    rects, failed = _sync_code(project_id, info, editor_id, label, line, column)
    out = {
        "status": info.get("status"),
        "file": label,
        "line": line,
        "page": rects[0]["page"] if rects else None,
        "rects": rects,
    }
    if failed:
        out["error"] = (
            "SyncTeX lookup failed for this line (the compiled build may have "
            "been evicted on the server — try recompiling, then retry)."
        )
    return out


def _iter_section_lines(text: str):
    """Yield ``(line_no, type, title)`` for each sectioning command in text.

    Reuses ``latex.SECTION_PATTERN`` so the recognised commands stay in sync
    with the rest of the package. Line numbers are 1-based.
    """
    from .latex import SECTION_PATTERN, _extract_braced

    for m in SECTION_PATTERN.finditer(text):
        open_idx = m.end() - 1
        extracted = _extract_braced(text, open_idx)
        title = extracted[0] if extracted else ""
        line_no = text.count("\n", 0, m.start()) + 1
        yield line_no, m.group(1), title


def section_page_map(project_id: str, file: str | None = None) -> dict:
    """Compile once, then map every ``\\section`` (and kin) to its PDF page.

    Reads the source of the compile ROOT (or ``file`` if given) via the
    project ZIP, finds each sectioning command's line, and forward-syncs it
    in ONE reused build (a single compile, N cheap sync calls). Also reports
    the total page count and where ``\\end{document}`` lands, so a caller can
    judge how full the last page is.

    Returns::

        {
          "status": ...,
          "pages": <int|None>,
          "page_height_pt": <float|None>,      # true height (MediaBox)
          "page_height_source": "mediabox" | "assumed_letter" | "unknown",
          "text_area_bottom_pt": <float|None>, # text-body bottom (geometry)
          "root": "<synctex label used>",
          "sections": [ {type, title, line, page}, ... ],
          "end": {"line", "page", "v",
                  "fill_pct", "remaining_pt",              # vs physical page
                  "text_area_fill_pct", "text_area_remaining_pt"} | None,
        }

    Two fullness figures for the LAST page, both quantitative so a
    fill-exactly-N-pages loop can reason numerically:

      - ``fill_pct`` / ``remaining_pt`` — against the real PHYSICAL page
        height (MediaBox); includes the bottom margin. Always present when
        the page height is known.
      - ``text_area_fill_pct`` / ``text_area_remaining_pt`` — against the
        printable TEXT-BODY bottom (from the geometry dump in the log),
        margin EXCLUDED. This is the actionable "how many more lines fit?"
        figure. Present only when the document reports geometry.
    """
    from .compile import read_file_web

    editor_id = str(uuid.uuid4())
    info = _compile_with_editor_id(project_id, editor_id)
    labels = _synctex_input_labels(info)
    label = _resolve_label(labels, file)
    if label is None:
        return {
            "status": info.get("status"),
            "error": (
                "Could not determine the compile root from SyncTeX. "
                f"Known labels: {labels or '(none — compile may have failed)'}"
            ),
            "pages": _pages_from_log(info),
            "sections": [],
            "end": None,
        }

    # The synctex label → project-relative path for reading the source out of
    # the ZIP. TeX writes 'dir/./file.tex' for a root in a subfolder and
    # './file.tex' for a top-level root; the ZIP entries have neither the
    # '/./ ' nor the leading './'.
    rel_path = label.replace("/./", "/")
    if rel_path.startswith("./"):
        rel_path = rel_path[2:]
    try:
        text = read_file_web(project_id, rel_path)
    except Exception as e:
        logger.warning("[layout] could not read %s from project zip: %s", rel_path, e)
        return {
            "status": info.get("status"),
            "error": f"Could not read source file {rel_path!r}: {e}",
            "pages": _pages_from_log(info),
            "sections": [],
            "end": None,
        }

    log_text = _fetch_log_text(info)
    pages = _pages_from_log_text(log_text)
    page_height, height_source = _page_height(info)
    # Text-body bottom edge from the geometry dump (robust; the raw SyncTeX
    # per-page extents are polluted by footers / lineno margin numbers, so we
    # do NOT use them). None when the document prints no geometry block.
    text_bottom = _text_block_bottom_bp(_parse_geometry_fields(log_text))

    # PRIMARY ENGINE: one offline parse of output.synctex.gz resolves ALL
    # section→page mappings with ZERO per-line HTTP calls (page numbers are
    # exact vs the live endpoint; verified). This is what turns a ~60s /
    # O(sections) run — which also raced CLSI build eviction — into ~1s and a
    # small CONSTANT number of requests (compile + a few artifact downloads).
    parsed = _fetch_and_parse_synctex(info)
    engine = "synctex-offline"
    tags = _synctex_tags_for_label(parsed, label) if parsed else []

    sections: list[dict] = []
    n_synced = 0        # per-line lookups that resolved to a page
    n_failed = 0        # per-line lookups that failed to resolve
    fallback_live = parsed is None or not tags

    for line_no, sec_type, title in _iter_section_lines(text):
        if fallback_live:
            # Offline artifact unavailable → degrade to the live endpoint
            # (still graceful: _sync_code never raises).
            rects, failed = _sync_code(project_id, info, editor_id, label, line_no)
            page = rects[0]["page"] if rects else None
            n_failed += failed
            n_synced += not failed
            engine = "sync-code-live"
        else:
            page = _page_for_line(parsed, tags, line_no)
            n_synced += page is not None
            n_failed += page is None
        sections.append({
            "type": sec_type,
            "title": title,
            "line": line_no,
            "page": page,
        })

    end = None
    m = re.search(r"\\end\{document\}", text)
    if m:
        end_line = text.count("\n", 0, m.start()) + 1
        # For the last-page fullness we need the deepest content baseline. The
        # \end{document} line itself has no box, so the offline map uses the
        # DEEPEST box on the last page (page_bottoms) — verified equal to the
        # live \end{document} v to 3 decimals. Live fallback if offline is out.
        end_page = None
        v = None
        if not fallback_live and parsed:
            end_page = parsed["max_page"] or pages
            # Deepest BODY content on the last page (footer excluded via the
            # text-body cap) — this is the actionable last-line baseline.
            v = _body_bottom_on_page(parsed, end_page, text_bottom)
        if v is None:
            rects, failed = _sync_code(project_id, info, editor_id, label, end_line)
            if rects:
                v = rects[0].get("v")
                end_page = rects[0]["page"]
        if v is not None:
            end = {"line": end_line, "page": end_page, "v": round(v, 3)}
            # (1) Physical fullness: ``v`` is the last content's distance from
            # the page TOP, so space to the physical page bottom is
            # ``page_height - v`` (INCLUDES the bottom margin).
            if page_height:
                end["fill_pct"] = round(100.0 * v / page_height, 1)
                end["remaining_pt"] = round(page_height - v, 1)
            # (2) Text-area fullness: space to the TEXT-BODY bottom, excluding
            # the bottom margin — the actionable "can I add more lines?"
            # figure for a fill-to-N-pages loop. Only when geometry is known.
            if text_bottom:
                end["text_area_remaining_pt"] = round(text_bottom - v, 1)
                end["text_area_fill_pct"] = round(100.0 * v / text_bottom, 1)

    result = {
        "status": info.get("status"),
        "pages": pages,
        "page_height_pt": round(page_height, 2) if page_height else None,
        "page_height_source": height_source,
        "text_area_bottom_pt": round(text_bottom, 2) if text_bottom else None,
        "root": label,
        "engine": engine,
        "sections": sections,
        "end": end,
        "sync_failures": n_failed,
    }
    # If EVERY lookup failed on the LIVE fallback path, the build was almost
    # certainly evicted before we could query it (a partial map with all
    # page=None is misleading). Turn that into a clear, actionable error.
    if fallback_live and n_failed and n_synced == 0:
        result["error"] = (
            "Layout unavailable — every SyncTeX lookup failed (the compiled "
            "build likely expired on the server before it could be queried). "
            "Recompile the project and retry; for a very long document the "
            "map may need to run promptly after a fresh compile."
        )
        return result
    return result


def format_section_page_map(result: dict) -> str:
    """Render a :func:`section_page_map` result as human-readable text."""
    if result.get("error"):
        return f"Error: {result['error']}"
    lines = [
        f"Layout map — root {result.get('root')!r}, "
        f"total {result.get('pages', '?')} page(s), status={result.get('status')}",
        "",
    ]
    if not result["sections"]:
        lines.append("(no sectioning commands found in the root file)")
    for s in result["sections"]:
        pg = s["page"] if s["page"] is not None else "?"
        indent = "  " * _SECTION_DEPTH.get(s["type"], 0)
        lines.append(f"  p.{pg:<3} {indent}\\{s['type']}{{{s['title']}}}  (line {s['line']})")
    if result.get("sync_failures"):
        lines.append("")
        lines.append(
            f"  Note: {result['sync_failures']} SyncTeX lookup(s) failed "
            "(shown as p.?) — likely transient build eviction; recompile and "
            "retry if you need those pages."
        )
    if result.get("engine") == "sync-code-live":
        lines.append("")
        lines.append(
            "  (mapped via the live sync/code endpoint — the offline "
            "output.synctex.gz was unavailable this run)"
        )
    ph = result.get("page_height_pt")
    hs = result.get("page_height_source")
    if ph:
        measured = "measured from PDF MediaBox" if hs == "mediabox" else f"ASSUMED ({hs})"
        lines.append("")
        lines.append(f"  Page height: {ph} pt ({measured})")
    end = result.get("end")
    if end:
        fill = end.get("fill_pct")
        rem = end.get("remaining_pt")
        ta_rem = end.get("text_area_remaining_pt")
        ta_fill = end.get("text_area_fill_pct")
        if ta_rem is not None:
            # The actionable figure: fullness of the printable text body,
            # bottom margin excluded (geometry-derived).
            lines.append(
                f"  Last page (p.{end['page']}) text area is ~{ta_fill}% full — "
                f"≈{ta_rem} pt of body space remain (bottom margin excluded; "
                "≈11-12 pt per line at 11pt, so that's roughly "
                f"{max(0, int(ta_rem // 11))} more line(s))"
            )
            if fill is not None:
                lines.append(
                    f"    (to the physical page bottom incl. margin: ~{fill}% full, ≈{rem} pt; "
                    f"last content baseline at v={end.get('v')} pt from top)"
                )
        elif fill is not None:
            lines.append(
                f"  Last page (p.{end['page']}) is ~{fill}% full — "
                f"≈{rem} pt of vertical space remain below the last content "
                f"(includes the bottom margin; geometry not reported, so text-area "
                f"figure unavailable; last content baseline at v={end.get('v')} pt from top)"
            )
        else:
            lines.append(
                f"  \\end{{document}} → page {end['page']} (v={end.get('v')} pt from top)"
            )
    return "\n".join(lines)


# Indent depth for the pretty-printer (broader = shallower indent).
_SECTION_DEPTH = {
    "part": 0,
    "chapter": 0,
    "section": 0,
    "subsection": 1,
    "subsubsection": 2,
    "paragraph": 3,
    "subparagraph": 4,
}
