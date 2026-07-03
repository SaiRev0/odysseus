import pytest
import asyncio
from routes.email_pollers import _regex_match_rule, _llm_match_rule

def test_regex_match_hit():
    rule = {"keywords": ["XYZ", "ABC"]}
    assert _regex_match_rule(rule, "Approval for XYZ", "body text", "sender@example.com") is True

def test_regex_match_miss():
    rule = {"keywords": ["XYZ", "ABC"]}
    assert _regex_match_rule(rule, "Approval for DEF", "body text", "sender@example.com") is False

def test_regex_match_subject_only():
    rule = {"keywords": ["XYZ"], "match_subject_only": True}
    assert _regex_match_rule(rule, "Subject without keyword", "body contains XYZ here", "sender@example.com") is False
    assert _regex_match_rule(rule, "Subject with XYZ", "body text", "sender@example.com") is True

def test_sender_filter_blocks():
    rule = {"keywords": ["XYZ"], "only_from": ["boss@company.com"]}
    assert _regex_match_rule(rule, "Subject XYZ", "body", "other@company.com") is False

def test_sender_filter_allows():
    rule = {"keywords": ["XYZ"], "only_from": ["boss@company.com"]}
    assert _regex_match_rule(rule, "Subject XYZ", "body", "Boss@company.com") is True

@pytest.mark.asyncio
async def test_llm_match_yes_no_parsing(monkeypatch):
    import routes.email_pollers
    
    async def mock_llm_call(*args, **kwargs):
        # We simulate the LLM responding "YES"
        return "YES."
        
    monkeypatch.setattr(routes.email_pollers, "task_llm_call_async", mock_llm_call)
    
    rule = {"match_prompt": "Is this a test?"}
    matched = await _llm_match_rule(rule, "Subject", "Body", "sender@example.com", "url", "model", {})
    assert matched is True
