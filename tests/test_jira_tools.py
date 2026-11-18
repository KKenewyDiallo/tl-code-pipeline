"""Tests ciblés de l'intégration JIRA (pas de Temporal, pas de réseau) :
- helpers purs de worker.activities.jira_tools (cloud_id, ADF, parsing, appels
  tool avec un ToolCallClient stub) ;
- le workflow spec-to-tests déclare bien le slot connecteur `atlassian`
  (@uses_connectors) et son entrypoint accepte `jira_issue`.

Run : uv run --with pytest python -m pytest tests/ -q
"""

import asyncio
import inspect
import json

import pytest

import worker.activities.jira_tools as jt


class StubClient:
    """ToolCallClient stub : réponses prédéfinies par tool, appels enregistrés."""

    def __init__(self, responses, fail_on=()):
        self.responses = responses
        self.fail_on = set(fail_on)
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name, arguments=None):
        self.calls.append((tool_name, arguments or {}))
        if tool_name in self.fail_on:
            raise RuntimeError(f"boom {tool_name}")
        payload = self.responses[tool_name]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return {"content": [{"type": "text", "text": text}]}


ISSUE = {
    "id": "62978",
    "key": "TOOLLIVE-3600",
    "fields": {
        "summary": "Récupérer les infos de la CRTL",
        "description": "Lire les colonnes R, S, T du fichier CRTL.\n",
        "status": {"name": "new"},
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "labels": ["c8y"],
        "assignee": None,
        "reporter": {"displayName": "pef"},
        "comment": {"comments": [
            {"author": {"displayName": "pef"},
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "Hello "},
                                                   {"type": "text", "text": "world"}]}]}},
        ]},
    },
}

TRANSITIONS = {"transitions": [
    {"id": "21", "name": "In Progress", "to": {"name": "devs-en-cours"}, "isAvailable": True},
    {"id": "61", "name": "code-review", "to": {"name": "code-review"}, "isAvailable": True},
]}


def test_cloud_id_default(monkeypatch):
    monkeypatch.delenv("jira_cloud_id", raising=False)
    monkeypatch.delenv("JIRA_CLOUD_ID", raising=False)
    monkeypatch.delenv("jira_base_url", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    assert jt.cloud_id() == "toollive.atlassian.net"


def test_cloud_id_from_env(monkeypatch):
    monkeypatch.setenv("jira_cloud_id", "9aa8afda-b2d8-45cc-bd9c-ea65a7606cb8")
    assert jt.cloud_id() == "9aa8afda-b2d8-45cc-bd9c-ea65a7606cb8"
    monkeypatch.delenv("jira_cloud_id")
    monkeypatch.setenv("jira_base_url", "https://toollive.atlassian.net/jira")
    assert jt.cloud_id() == "toollive.atlassian.net"


def test_issue_url(monkeypatch):
    monkeypatch.delenv("jira_base_url", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    assert jt.issue_url("TOOLLIVE-3600") == "https://toollive.atlassian.net/jira/browse/TOOLLIVE-3600"


def test_adf_to_text():
    adf = {"type": "doc", "content": [
        {"type": "heading", "content": [{"type": "text", "text": "Titre"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "ligne"}]},
    ]}
    assert jt._adf_to_text(adf) == "Titre\nligne"
    assert jt._adf_to_text(None) == ""
    assert jt._adf_to_text("plain") == "plain"


def test_parse_tool_result():
    dumped = {"content": [{"type": "text", "text": json.dumps({"a": 1})}]}
    assert jt._parse_tool_result(dumped) == {"a": 1}
    dumped = {"content": [{"type": "text", "text": "pas du json"}]}
    assert jt._parse_tool_result(dumped) == "pas du json"


def test_fetch_issue_normalizes():
    stub = StubClient({"getJiraIssue": ISSUE})
    out = asyncio.run(jt._fetch_issue(stub, "TOOLLIVE-3600"))
    tool, args = stub.calls[0]
    assert tool == "getJiraIssue"
    assert args["issueIdOrKey"] == "TOOLLIVE-3600"
    assert args["cloudId"] == "toollive.atlassian.net"
    assert "comment" in args["fields"]
    assert out["key"] == "TOOLLIVE-3600"
    assert out["summary"] == "Récupérer les infos de la CRTL"
    assert out["status"] == "new"
    assert out["labels"] == ["c8y"]
    assert out["url"].endswith("/browse/TOOLLIVE-3600")
    assert out["comments"][0]["body"] == "Hello world"


def test_fetch_issue_not_found_raises():
    stub = StubClient({"getJiraIssue": {"errorMessages": ["Issue does not exist"]}})
    with pytest.raises(RuntimeError, match="introuvable"):
        asyncio.run(jt._fetch_issue(stub, "NOPE-1"))


def test_comment_ok_and_non_blocking_error():
    stub = StubClient({"addCommentToJiraIssue": {"id": "1"}})
    out = asyncio.run(jt._comment(stub, "TOOLLIVE-3600", "hello"))
    assert out == {"ok": True}
    _, args = stub.calls[0]
    assert args["commentBody"] == "hello" and args["contentFormat"] == "markdown"
    failing = StubClient({}, fail_on=("addCommentToJiraIssue",))
    out = asyncio.run(jt._comment(failing, "TOOLLIVE-3600", "hello"))
    assert out["ok"] is False and "boom" in out["error"]


def test_transition_by_name():
    stub = StubClient({"getTransitionsForJiraIssue": TRANSITIONS, "transitionJiraIssue": {}})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "code-review"))
    assert out["ok"] is True and out["to"] == "code-review"
    tool, args = stub.calls[1]
    assert tool == "transitionJiraIssue" and args["transition"] == {"id": "61"}


def test_transition_unavailable_is_soft():
    stub = StubClient({"getTransitionsForJiraIssue": TRANSITIONS})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "Done"))
    assert out["ok"] is False and "indisponible" in out["reason"]
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", ""))
    assert out["ok"] is False


def test_search_briefs():
    stub = StubClient({"searchJiraIssuesUsingJql": {"issues": [ISSUE]}})
    out = asyncio.run(jt._search(stub, "key = TOOLLIVE-3600", 5))
    _, args = stub.calls[0]
    assert args["jql"] == "key = TOOLLIVE-3600" and args["maxResults"] == 5
    assert out[0]["key"] == "TOOLLIVE-3600" and out[0]["status"] == "new"


def test_workflow_declares_atlassian_slot():
    import worker.workflows.spec_to_tests as m
    meta = getattr(m.SpecToTests, "__plugin_metadata__", {})
    assert "atlassian" in json.dumps(meta)


def test_entrypoint_accepts_jira_issue():
    import worker.workflows.spec_to_tests as m
    fn = m.SpecToTests.run
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    params = inspect.signature(fn).parameters
    assert "jira_issue" in params
    assert params["jira_issue"].default == ""
