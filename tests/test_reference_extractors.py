# SPDX-License-Identifier: Apache-2.0
"""Tests for the content-agnostic reference-extractor registry.

These cover the new architecture in ``skylakegrep.src.reference_graph`` and
its first non-code plugin, ``skylakegrep.src.extractors.markdown``. The
legacy code-graph behaviour is exercised separately in
``tests/test_code_graph.py``.

The registry shape is asserted directly so a future drop-in plugin (configs,
RDF, …) only needs ``REFERENCE_EXTRACTORS["new_type"] = my_fn`` plus a
matching ``CONTENT_TYPE_EXTENSIONS`` entry.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skylakegrep.src import reference_graph
from skylakegrep.src.extractors import markdown as md_extractor


class RegistryShapeTests(unittest.TestCase):
    """Smoke-tests for the plugin contract itself."""

    def test_registry_keys_match_extension_map(self):
        self.assertEqual(
            set(reference_graph.REFERENCE_EXTRACTORS.keys()),
            set(reference_graph.CONTENT_TYPE_EXTENSIONS.keys()),
        )

    def test_code_and_markdown_are_default_plugins(self):
        self.assertIn("code", reference_graph.REFERENCE_EXTRACTORS)
        self.assertIn("markdown", reference_graph.REFERENCE_EXTRACTORS)

    def test_register_extractor_adds_new_type(self):
        original_extractors = dict(reference_graph.REFERENCE_EXTRACTORS)
        original_exts = {k: set(v) for k, v in reference_graph.CONTENT_TYPE_EXTENSIONS.items()}
        try:
            calls: list[tuple[int, Path]] = []

            def fake_extractor(files, root):
                calls.append((len(files), root))
                return []

            reference_graph.register_extractor(
                "config_demo", {".yamlx"}, fake_extractor
            )
            self.assertIs(
                reference_graph.REFERENCE_EXTRACTORS["config_demo"],
                fake_extractor,
            )
            self.assertEqual(
                reference_graph.CONTENT_TYPE_EXTENSIONS["config_demo"],
                {".yamlx"},
            )

            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "demo.yamlx").write_text("a: 1\n")
                graph = reference_graph.build_export_graph(root)
            # The fake extractor returned no edges, but the file was walked
            # and the registry dispatched to it (calls has one entry).
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 1)
            # Node still appears in the output graph (with degree zero).
            self.assertEqual(len(graph), 1)
        finally:
            reference_graph.REFERENCE_EXTRACTORS.clear()
            reference_graph.REFERENCE_EXTRACTORS.update(original_extractors)
            reference_graph.CONTENT_TYPE_EXTENSIONS.clear()
            reference_graph.CONTENT_TYPE_EXTENSIONS.update(original_exts)


def _make_markdown_fixture(root: Path) -> dict[str, Path]:
    """Four markdown files cross-linking each other.

    Layout:
      - ``index.md``    — links to all of overview, faq, missing.md, an
                          external URL, an in-document anchor and a wiki
                          link to ``overview``.
      - ``overview.md`` — links to ``faq.md#section-2`` and back to
                          ``./index.md``.
      - ``faq.md``      — links nowhere.
      - ``orphan.md``   — referenced by nobody.
      - ``docs/deep.md``— links up to ``../overview.md``.
    """

    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    index = root / "index.md"
    overview = root / "overview.md"
    faq = root / "faq.md"
    orphan = root / "orphan.md"
    deep = docs / "deep.md"

    index.write_text(
        "# Index\n\n"
        "See the [overview](./overview.md) and the [FAQ](faq.md).\n"
        "Broken: [missing](./missing.md).\n"
        "External: [anthropic](https://www.anthropic.com).\n"
        "Anchor: [top](#top).\n"
        "Wiki: [[overview]].\n"
        "Image: ![logo](./assets/logo.png)\n"
    )
    overview.write_text(
        "# Overview\n\n"
        "Read the [FAQ](./faq.md#section-2) or go [home](./index.md).\n"
    )
    faq.write_text("# FAQ\n\nNo links here.\n")
    orphan.write_text("# Orphan\n\nReferenced by nobody.\n")
    deep.write_text("# Deep\n\n[Up](../overview.md)\n")

    return {
        "index": index.resolve(),
        "overview": overview.resolve(),
        "faq": faq.resolve(),
        "orphan": orphan.resolve(),
        "deep": deep.resolve(),
    }


class MarkdownExtractorTests(unittest.TestCase):
    def test_resolves_relative_links_and_drops_anchors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _make_markdown_fixture(root)
            files = list(paths.values())
            edges = md_extractor.extract_edges(files, root)

            # Relative ``./overview.md`` and bare ``faq.md`` resolve.
            self.assertIn((str(paths["index"]), str(paths["overview"])), edges)
            self.assertIn((str(paths["index"]), str(paths["faq"])), edges)
            # Wiki ``[[overview]]`` resolves with .md suffix appended.
            self.assertEqual(
                sum(1 for e in edges if e[0] == str(paths["index"]) and e[1] == str(paths["overview"])),
                2,  # one standard link + one wiki link
            )
            # ``./faq.md#section-2`` strips the anchor and still resolves.
            self.assertIn(
                (str(paths["overview"]), str(paths["faq"])), edges
            )
            # ``../overview.md`` from docs/deep.md.
            self.assertIn(
                (str(paths["deep"]), str(paths["overview"])), edges
            )

    def test_drops_external_and_pure_anchors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _make_markdown_fixture(root)
            files = list(paths.values())
            edges = md_extractor.extract_edges(files, root)

            # No edge contains an external URL or pure anchor as a target.
            for _src, dst in edges:
                self.assertFalse(dst.startswith("http"))
                self.assertNotIn("#", Path(dst).name)
            # The broken link ``./missing.md`` does not produce an edge —
            # the resolver drops targets that don't exist on disk.
            self.assertNotIn(
                (str(paths["index"]), str(root.resolve() / "missing.md")),
                edges,
            )

    def test_image_and_non_markdown_targets_skip_when_missing(self):
        """``[logo](./assets/logo.png)`` shouldn't blow up when the asset
        is absent — the resolver returns ``None`` and the extractor skips
        it. We don't index image files in this iteration.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "x.md").write_text("![asset](./assets/foo.png)\n")
            edges = md_extractor.extract_edges([root / "x.md"], root)
            self.assertEqual(edges, [])


class BuildExportGraphTests(unittest.TestCase):
    """End-to-end registry walk over a markdown-only repo."""

    def test_in_degrees_match_link_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _make_markdown_fixture(root)
            graph = reference_graph.build_export_graph(root)

            # All five files appear as nodes.
            for key in ("index", "overview", "faq", "orphan", "deep"):
                self.assertIn(str(paths[key]), graph, key)

            in_overview = graph[str(paths["overview"])]["in_degree"]
            in_faq = graph[str(paths["faq"])]["in_degree"]
            in_index = graph[str(paths["index"])]["in_degree"]
            in_orphan = graph[str(paths["orphan"])]["in_degree"]
            in_deep = graph[str(paths["deep"])]["in_degree"]

            # ``overview`` is referenced by index (standard + wiki = 2) and
            # by docs/deep.md → in_degree == 3.
            self.assertEqual(in_overview, 3)
            # ``faq`` is referenced by index + overview → in_degree == 2.
            self.assertEqual(in_faq, 2)
            # ``index`` is referenced once by overview.
            self.assertEqual(in_index, 1)
            # ``orphan`` and ``deep`` are not referenced.
            self.assertEqual(in_orphan, 0)
            self.assertEqual(in_deep, 0)

    def test_pagerank_monotonic_with_in_degree_for_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _make_markdown_fixture(root)
            graph = reference_graph.build_export_graph(root)

            pr_overview = graph[str(paths["overview"])]["pagerank"]
            pr_faq = graph[str(paths["faq"])]["pagerank"]
            pr_orphan = graph[str(paths["orphan"])]["pagerank"]
            self.assertGreater(pr_overview, pr_faq)
            self.assertGreater(pr_faq, pr_orphan)

    def test_mixed_corpus_routes_per_content_type(self):
        """A repo with both ``.py`` and ``.md`` files gets each dispatched
        to the correct plugin. The combined graph contains both content
        types as nodes; markdown links don't contribute code edges and vice
        versa.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Markdown: a.md links to b.md.
            (root / "a.md").write_text("[b](./b.md)\n")
            (root / "b.md").write_text("# B\n")
            # Python: foo.py imports bar.
            (root / "foo.py").write_text("import bar\n")
            (root / "bar.py").write_text("X = 1\n")

            graph = reference_graph.build_export_graph(root)
            md_b = str((root / "b.md").resolve())
            py_bar = str((root / "bar.py").resolve())
            self.assertEqual(graph[md_b]["in_degree"], 1)
            self.assertEqual(graph[py_bar]["in_degree"], 1)


if __name__ == "__main__":
    unittest.main()
