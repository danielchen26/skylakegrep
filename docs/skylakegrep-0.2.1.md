# skylakegrep 0.2.1 — release notes

A focused positioning + advertise update on top of `0.2.0`. The
project's positioning was inherited from the `0.1.0` rebrand era
("local semantic code search") and undersold what the `0.2.0`
content-agnostic substrate actually does. `0.2.1` brings the
description, README, GitHub Pages site, and metadata in line with
the architecture that already shipped: **fully-offline semantic
search over your local files — code, markdown, PDFs, Word docs,
plain text, and any content type you register**.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What changed

This is a documentation / metadata release. **No code behavior
changes.** Indexes built on `0.2.0` continue working unchanged.

### Description rewrites (positioning correction)

The package description, GitHub repo description, README hero,
GitHub Pages meta tags, og:image alt text, and Twitter card all
moved from the `0.1.0`-era "local semantic **code** search"
framing to the content-agnostic positioning that matches the
`0.2.0` architecture:

| Surface | Before | After |
| --- | --- | --- |
| `pyproject.toml` description | "Free local semantic code search using Ollama" | "Fully-offline semantic search over your local files — powered by Ollama" |
| GitHub repo description | empty | "Fully-offline semantic search over your local files — powered by Ollama" |
| README hero alt + intro | "semantic code-search CLI for ... your codebase" | "semantic search CLI for your local files — code, markdown, PDFs, Word docs, plain text, anything you index" |
| `docs/index.html` `<meta description>` | "command-line semantic code search tool" | "fully-offline semantic search over your local files — code, markdown, PDFs, Word docs, plain text" |
| `docs/index.html` og + twitter cards | code-search framing | content-agnostic + 30 / 30 framing |
| `docs/index.html` hero `<h1>` lead | "fully-offline semantic code-search CLI for engineers" | "fully-offline semantic search CLI for your local files — code, markdown, PDFs, Word docs, plain text, anything you index" |

### New advertise sections (explicit catalog of supported content)

`README.md` and `docs/index.html` both gained two new sections that
make the content-agnostic surface concrete and discoverable:

  1. **What you can index and search** — a six-row catalog covering
     code (Rust / Python / JS / TS), markdown (with link graph),
     PDF, `.docx`, plain text / TOML / YAML / CSV / JSON, and a
     **Custom** row showing how to register your own content type
     in one line. Each row names the parser, the reference graph
     handling (if any), and the version it was introduced in.
  2. **What's new in 0.2.x** — a six-card / six-row advertise of
     the substrate upgrade (`bge-m3`), the content-agnostic
     reference-graph registry, the σ-adaptive cascade gap, the
     universal aux-path filter, the 30 / 30 public-OSS bench
     headline, and the latency net (−19 % aggregate, +57 % Tokio
     as a real trade-off).

These were missing in `0.2.0` — the bench numbers and substrate
were updated in metadata text but not given dedicated advertise
real estate.

### Capability matrix updates

The "Capability matrix" `<details>` block in `README.md` gained
seven new `0.2.0` rows for the substrate, the registry, the markdown
extractor, the σ-cascade, the universal path filter, the symbol
channel, and the public-OSS bench result.

### Use-case card refresh

The four use-case cards in `docs/index.html`'s overview hero were
refreshed:

  - "ONBOARDING / New codebase walkthrough" gained the **30 / 30
    recall** anchor and the explicit Django · React · Tokio
    citation.
  - "PERSONAL FILES / Search your `~/Downloads`" was renamed to
    **DOCS · NOTES · PDFs / Search your knowledge base** and now
    explicitly mentions the markdown link graph
    (`[](link)`, `[[wiki]]`) as a `0.2.0` retrieval prior — making
    the docs/notes/PDFs use case a first-class citizen of the
    overview rather than a footnote.
  - The "3 formats / Code + PDF + docx" stat tile became
    **Any file / Code · markdown · PDFs · docx · text** with a
    pluggable-registry sub-line.

### Files changed

```
pyproject.toml          (version + description)
README.md               (hero alt, intro paragraph, nav, capability matrix,
                        new "What you can search" section,
                        new "What's new in 0.2.x" section)
docs/index.html         (meta + og + twitter + hero h1/lead + stat tiles +
                        use cases + new "What you can index and search" +
                        new "What's new in 0.2.x" sections)
docs/skylakegrep-0.2.1.md   (this file)
```

## Why now

`0.2.0` shipped a substantial substrate upgrade and a
content-agnostic registry, but the public-facing surface still
described the project as "code search". A user landing on
[pypi.org/project/skylakegrep](https://pypi.org/project/skylakegrep/),
the GitHub repo page, or the GitHub Pages site would not see that
markdown link graphs, PDFs, and arbitrary registered content
types are first-class — they would assume code-only and either
look elsewhere (false negative) or build a code-search-style
index that ignores their docs (under-utilised).

`0.2.1` is a small, focused metadata release to close that gap.

## Reproduce / verify

Same protocol as `0.2.0` — no code changes:

```bash
git clone --depth=1 https://github.com/django/django   /tmp/oss-bench/django
git clone --depth=1 https://github.com/facebook/react  /tmp/oss-bench/react
git clone --depth=1 https://github.com/tokio-rs/tokio  /tmp/oss-bench/tokio

ollama pull bge-m3
.venv/bin/python benchmarks/public_oss_bench.py
```

See `docs/parity-benchmarks.md` for the per-task table and
methodology.

## Known follow-ups (not in 0.2.1)

- Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
  to reflect the new positioning — visual assets, separate cosmetic
  pass.
- Re-run the self-test bench (`benchmarks/agent_context_benchmark.py`)
  on `bge-m3` and update `docs/token-benchmarking.md` top-k 5 row.
- Symbol-channel auto-router behind `SKYGREP_SYMBOL_CHANNEL=auto|on|off`,
  validated against an expanded symbol-heavy benchmark — slated for 0.3.0.
