# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
fetch_arxiv.py - Download, extract, and summarize an arXiv paper TeX source.

Usage:
    python fetch_arxiv.py <arxiv_id>

Example:
    python fetch_arxiv.py 2507.14393

Steps:
    1. Download https://arxiv.org/src/<arxiv_id> as a .tar.gz file (skipped if cached)
    2. Extract the tarball
    3. Locate the main .tex entrypoint
    4. Run `summarize <entrypoint>` to produce a summary
    5. Print the summary to stdout (to be used directly by the language model)
"""

import sys
import os
import re
import tarfile
import urllib.request
import ssl
import subprocess

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def strip_latex(tex: str) -> str:
    """Strip LaTeX commands and return readable plain text."""
    # Only process content inside \begin{document}...\end{document}
    doc_match = re.search(r'\\begin\{document\}(.*?)\\end\{document\}', tex, re.DOTALL)
    if doc_match:
        tex = doc_match.group(1)

    # Remove comments
    tex = re.sub(r'%.*', '', tex)
    # Remove non-content environments (including tcolorbox variants)
    for env in ('figure', 'table', 'lstlisting', 'verbatim', 'tikzpicture',
                'equation', 'align', 'array', 'tabular', 'longtable',
                'tcolorbox', 'userbox', 'mathematicianbox', 'reviewerbox', 'supervisorbox'):
        tex = re.sub(rf'\\begin\{{{env}\*?\}}.*?\\end\{{{env}\*?\}}', ' ', tex, flags=re.DOTALL)
    # Remove \newcommand, \renewcommand, \newtcolorbox, \definecolor, \lstset, \lstdefinelanguage blocks
    tex = re.sub(r'\\(newcommand|renewcommand|newtcolorbox|definecolor|colorlet|lstset|lstdefinelanguage|hypersetup|graphicspath|setlength|setcounter|pagestyle|thispagestyle)\b.*?(?=\n\n|\Z)', ' ', tex, flags=re.DOTALL)
    # Remove any remaining lines that look like LaTeX option key=value pairs (preamble leftovers)
    tex = re.sub(r'^[ \t]*[\w!@#$%^&*]+=[^,\n]+,?\s*$', '', tex, flags=re.MULTILINE)
    tex = re.sub(r'^[ \t]*\d+pt\d+pt.*$', '', tex, flags=re.MULTILINE)
    # Skip any leading noise before the first real paragraph (sentence starting with a capital letter, 40+ chars)
    first_para = re.search(r'(?m)^[A-Z][^\n]{40,}', tex)
    if first_para:
        tex = tex[first_para.start():]
    # Promote section headings
    tex = re.sub(r'\\(section|subsection|subsubsection|paragraph)\*?\{([^}]*)\}', r'\n\n## \2\n', tex)
    # Keep meaningful command arguments
    tex = re.sub(r'\\(textbf|textit|emph|text|mbox|hbox|vbox|footnote|cite|ref|label|url|href)\{([^}]*)\}', r'\2', tex)
    # Remove all other commands with arguments
    tex = re.sub(r'\\[a-zA-Z]+\*?\{[^}]*\}', ' ', tex)
    # Remove remaining commands
    tex = re.sub(r'\\[a-zA-Z]+\*?', ' ', tex)
    # Remove leftover braces/brackets/square brackets
    tex = re.sub(r'[{}]', '', tex)
    tex = re.sub(r'\[.*?\]', '', tex)
    # Remove lines that are mostly noise (short lines with only symbols/numbers)
    lines = tex.split('\n')
    lines = [l for l in lines if len(l.strip()) > 3 or l.strip() == '']
    tex = '\n'.join(lines)
    # Collapse whitespace
    tex = re.sub(r'\n{3,}', '\n\n', tex)
    tex = re.sub(r'[ \t]+', ' ', tex)
    return tex.strip()


def find_entrypoint(extract_dir: str) -> str | None:
    """Find the main .tex file (main.tex preferred, else first with \\documentclass)."""
    candidates = [f for f in os.listdir(extract_dir) if f.endswith(".tex")]
    # Prefer main.tex
    if "main.tex" in candidates:
        return os.path.join(extract_dir, "main.tex")
    # Fall back to first file containing \documentclass
    for fname in candidates:
        fpath = os.path.join(extract_dir, fname)
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                if "\\documentclass" in f.read(4000):
                    return fpath
        except OSError:
            continue
    # Last resort: any .tex file
    if candidates:
        return os.path.join(extract_dir, candidates[0])
    return None


def fetch_and_summarize(arxiv_id: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)

    tar_path = os.path.join(CACHE_DIR, f"{arxiv_id}.tar.gz")
    extract_dir = os.path.join(CACHE_DIR, arxiv_id)

    # Step 1: Download
    if not os.path.exists(tar_path):
        url = f"https://arxiv.org/src/{arxiv_id}"
        print(f"[fetch_arxiv] Downloading {url} ...", flush=True)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=ssl_ctx) as response, open(tar_path, "wb") as out_file:
            out_file.write(response.read())
    else:
        print(f"[fetch_arxiv] Using cached: {tar_path}", flush=True)

    # Step 2: Extract
    if not os.path.exists(extract_dir):
        os.makedirs(extract_dir, exist_ok=True)
        print(f"[fetch_arxiv] Extracting to {extract_dir} ...", flush=True)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(extract_dir)
    else:
        print(f"[fetch_arxiv] Already extracted: {extract_dir}", flush=True)

    # Step 3: Find entrypoint
    entrypoint = find_entrypoint(extract_dir)
    if not entrypoint:
        print(f"[fetch_arxiv] ERROR: No .tex file found in {extract_dir}", flush=True)
        sys.exit(1)
    print(f"[fetch_arxiv] Entrypoint: {entrypoint}", flush=True)

    # Step 4: Strip LaTeX to plain text and output for the language model to summarize
    # We do NOT call an LLM here; the language model receiving this output will summarize it.
    with open(entrypoint, encoding="utf-8", errors="ignore") as f:
        tex_content = f.read()
    plain_text = strip_latex(tex_content)
    print(f"[fetch_arxiv] Extracted {len(plain_text)} chars of plain text.\n", flush=True)

    # Hard cap at 4800 chars (~3K tokens) so the output fits in the model's context window
    MAX_CHARS = 4800
    if len(plain_text) > MAX_CHARS:
        # Keep the beginning (abstract/intro) and end (conclusion) for best coverage
        half = MAX_CHARS // 2
        plain_text = plain_text[:half] + "\n\n...[middle truncated]...\n\n" + plain_text[-half:]

    print(plain_text, flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fetch_arxiv.py <arxiv_id>")
        sys.exit(1)
    fetch_and_summarize(sys.argv[1])
