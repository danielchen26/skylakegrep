from skylakegrep.src.document_policy import (
    is_living_authority_document,
    is_unnamed_version_snapshot,
    prefer_living_authority_results,
)


def test_generic_policy_query_prefers_living_authority_over_snapshots():
    results = [
        {"path": "docs/RELEASING.md", "score": 1.5},
        {"path": "docs/tool-1.2.3.md", "score": 1.2},
        {"path": "docs/architecture.md", "score": 1.0},
    ]

    filtered = prefer_living_authority_results(
        "what checks are required before publishing?",
        results,
    )

    assert [result["path"] for result in filtered] == [
        "docs/RELEASING.md",
    ]


def test_explicit_version_query_retains_named_snapshot():
    results = [
        {"path": "docs/RELEASING.md", "score": 1.5},
        {"path": "docs/tool-1.2.3.md", "score": 1.2},
    ]

    filtered = prefer_living_authority_results(
        "what changed in tool 1.2.3?",
        results,
    )

    assert filtered == results
    assert not is_unnamed_version_snapshot(
        "what changed in tool 1.2.3?",
        "docs/tool-1.2.3.md",
    )


def test_explicit_version_query_excludes_other_versions_of_same_document():
    results = [
        {"path": "docs/RELEASING.md", "score": 1.5},
        {"path": "docs/tool-1.2.3.md", "score": 1.2},
        {"path": "docs/tool-1.1.0.md", "score": 1.1},
    ]

    filtered = prefer_living_authority_results(
        "what changed in tool 1.2.3?",
        results,
    )

    assert [result["path"] for result in filtered] == [
        "docs/RELEASING.md",
        "docs/tool-1.2.3.md",
    ]


def test_historical_task_without_leading_authority_is_unchanged():
    results = [
        {"path": "docs/changelog.md", "score": 1.5},
        {"path": "docs/tool-1.2.3.md", "score": 1.2},
    ]

    assert not is_living_authority_document("docs/changelog.md")
    assert prefer_living_authority_results("summarize release history", results) == results


def test_explicitly_named_supporting_document_is_retained():
    results = [
        {"path": "README.md", "score": 1.5},
        {"path": "docs/architecture.md", "score": 1.2},
        {"path": "docs/draft-notes.md", "score": 1.0},
    ]

    filtered = prefer_living_authority_results(
        "compare the README with the architecture document",
        results,
    )

    assert [result["path"] for result in filtered] == [
        "README.md",
        "docs/architecture.md",
    ]
