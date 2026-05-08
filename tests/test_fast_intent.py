from __future__ import annotations

from skylakegrep.src.fast_intent import (
    classify_fast_intent,
    filename_candidates,
    is_pathlike_candidate,
)


def test_fast_intent_accepts_obvious_filename_without_language_wrappers():
    d = classify_fast_intent("我的 CASE42 文件在哪")
    assert d is not None
    assert d.intent == "filename"
    assert d.primary_token == "CASE42"


def test_fast_intent_accepts_obvious_semantic_without_llm():
    d = classify_fast_intent("how does cascade decide")
    assert d is not None
    assert d.intent == "semantic"


def test_fast_intent_defers_ambiguous_short_code_query():
    assert classify_fast_intent("auth login") is None


def test_filename_candidates_use_script_ngrams_not_cjk_wrapper_stripping():
    candidates = filename_candidates("我的合同文件在哪")
    assert "合同" in candidates
    assert "文件" in candidates


def test_pathlike_candidate_is_content_shape_not_language_phrase():
    assert is_pathlike_candidate("package.json")
    assert is_pathlike_candidate("CASE42")
    assert not is_pathlike_candidate("report")
