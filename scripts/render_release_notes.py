"""Render docs/skylakegrep-X.Y.Z.md → docs/skylakegrep-X.Y.Z.html.

Reuses the themed sidebar / topbar layout shared with 0.4.2.html.
Run from repo root:
    .venv/bin/python scripts/render_release_notes.py 0.5.0 0.5.1
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown  # type: ignore

DOCS = Path(__file__).resolve().parent.parent / "docs"

HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="description" content="skylakegrep v{ver} — release notes" />
<meta name="theme-color" content="#13192a" />
<title>v{ver} · skylakegrep</title>
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml" />
<link rel="preconnect" href="https://rsms.me/" />
<link rel="stylesheet" href="https://rsms.me/inter/inter.css" />
<link rel="stylesheet" href="styles.css?v=4.2.0" />
<style>
   .docs-shell {{ grid-template-columns: var(--sidebar-w) minmax(0, 1fr) !important;
                  max-width: 1500px !important; }}
   .content {{ width: 100% !important; max-width: 100% !important; }}
   .md-body {{ max-width: 880px; margin: 0 auto; padding: 16px 0 64px; color: var(--text); }}
   .md-body h1 {{ font-size: clamp(28px, 4vw, 40px); margin: 0 0 16px;
                  letter-spacing: -0.02em; color: var(--ink); font-weight: 700; }}
   .md-body h2 {{ font-size: clamp(22px, 2.5vw, 28px); margin: 40px 0 12px;
                  color: var(--ink); font-weight: 600; letter-spacing: -0.015em;
                  padding-top: 8px; border-top: 1px solid var(--rule-faint); }}
   .md-body h3 {{ font-size: 17px; margin: 28px 0 10px; color: var(--ink); font-weight: 600; }}
   .md-body p, .md-body li {{ font-size: 15px; line-height: 1.7; color: var(--text); }}
   .md-body a {{ color: var(--link); text-decoration: none; border-bottom: 1px dashed rgba(165,243,252,0.32); }}
   .md-body code {{ font-family: var(--mono); font-size: 13px; background: var(--code-bg);
                    padding: 2px 6px; border-radius: 4px; color: var(--code-text);
                    border: 1px solid var(--code-border); }}
   .md-body pre {{ background: var(--code-bg-deep); padding: 16px 18px; border-radius: 10px;
                   border: 1px solid var(--code-border); overflow-x: auto; margin: 14px 0; }}
   .md-body pre code {{ background: transparent; padding: 0; border: none; font-size: 12.5px; }}
   .md-body table {{ border-collapse: collapse; margin: 18px 0; width: 100%; font-size: 13.5px; }}
   .md-body th, .md-body td {{ padding: 9px 12px; border-bottom: 1px solid var(--rule); text-align: left; }}
   .md-body th {{ color: var(--ink); font-weight: 600; background: var(--panel); }}
   .md-body blockquote {{ margin: 14px 0; padding: 12px 18px; border-left: 3px solid var(--accent);
                          background: var(--accent-soft); border-radius: 6px; color: var(--text); }}
   .md-body ul, .md-body ol {{ padding-left: 22px; }}
   .md-body li {{ margin: 4px 0; }}
   .md-body hr {{ border: none; border-top: 1px solid var(--rule-faint); margin: 28px 0; }}
</style>
</head><body>
<header class="topbar">
<a class="topbrand" href="index.html"><span class="brand-mark">∞</span><span class="brand-name">skylakegrep</span></a>
<nav class="topnav"><a href="https://pypi.org/project/skylakegrep/">PyPI</a>
<a href="https://github.com/danielchen26/skylakegrep">GitHub</a>
<a class="topnav-cta" href="index.html#install">Install</a></nav>
</header>
<div class="docs-shell">
<aside class="sidebar"><div class="sidebar-title">skylakegrep</div>
<nav>
<div class="nav-section"><p>Pages</p>
<a href="index.html">Home</a>
<a href="concepts.html">Concepts</a>
<a href="architecture.html">Architecture</a>
<a href="cli.html">CLI reference</a>
<a href="reference.html">JSON schema</a>
<a href="benchmarks.html">Benchmarks</a>
<a href="changelog.html" class="active">Release notes</a>
</div>
<div class="nav-section"><p>Project</p>
<a href="https://github.com/danielchen26/skylakegrep">GitHub</a>
<a href="https://pypi.org/project/skylakegrep/">PyPI</a>
<a href="principles.html">Principles</a>
</div></nav></aside>
<main class="content subpage-content">
<article class="md-body">
<p style="font-size:11px;letter-spacing:0.18em;color:rgba(165,243,252,0.65);font-family:var(--mono);margin-bottom:8px;text-transform:uppercase;">release notes · v{ver}</p>
"""

FOOT = """</article>
</main>
</div>
</body></html>
"""


def render(version: str) -> Path:
    md_path = DOCS / f"skylakegrep-{version}.md"
    out_path = DOCS / f"skylakegrep-{version}.html"
    body_md = md_path.read_text()
    # Strip the leading `# title` line — the topbar eyebrow + first
    # `<h1>` rendered from the next title line is enough.
    body_html = markdown.markdown(
        body_md,
        extensions=["extra", "tables", "fenced_code", "sane_lists"],
    )
    out_path.write_text(HEAD.format(ver=version) + body_html + FOOT)
    return out_path


def main() -> None:
    versions = sys.argv[1:] or ["0.5.0", "0.5.1"]
    for v in versions:
        out = render(v)
        print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
