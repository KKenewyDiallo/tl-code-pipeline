"""Activities JIRA via le connecteur MCP `atlassian` (Studio > Connectors).

Aucun credential dans le code : le slot `atlassian_connector` est déclaré ici et
attaché au workflow par `@uses_connectors` (voir workflows/spec_to_tests.py) ; la
plateforme résout les credentials au runtime selon l'identité du run (utilisateur
déclencheur en OBO, worker sinon). En local (`mistral dev`) le worker tourne sous
l'identité de l'utilisateur : le credential `atlassian` connecté dans Studio suffit.
En déploiement managé (run non attended), il faut un credential `atlassian` au
scope workspace/organisation pour que le service account du worker le résolve.

Le slot est déclaré `run_as="deployment"` (identité service du worker — aucun flux
OAuth interactif possible en non attended) avec `auto_auth=False` : le preflight
auto_auth du SDK liste les credentials au scope USER
(`GET /v1/connectors/<id>/user/credentials`), qui répond 404 « No consumer found
when listing credentials for type user » quand il est appelé par le service account
d'un déploiement managé, et cette erreur d'activité fait échouer l'exécution entière
avant même la première activité (exécution `0138f8a4...860559`, run
`01a1eef1-1481-701a-bf52-fead246a72ec`, 2026-11-18T13:55:39Z). Sans preflight, les
appels d'outils passent par la gateway du connecteur
(`POST /v1/connectors/<id>/tools/<tool>/call`), qui résout côté serveur le credential
épinglé par `credentials_name` (env `jira_credentials_name`, défaut « atlassian »)
au scope workspace — requis pour un déploiement managé.

Outils MCP utilisés (connecteur Atlassian) :
- lecture  : getJiraIssue, searchJiraIssuesUsingJql, getTransitionsForJiraIssue
- écriture : addCommentToJiraIssue, transitionJiraIssue

`cloudId` : env `jira_cloud_id` (UUID ou host du site, ex. `toollive.atlassian.net`),
sinon le host de `jira_base_url` (défaut https://toollive.atlassian.net/jira).

Convention : toutes les variables d'env du pipeline sont en minuscules, avec
fallback sur l'ancien nom en majuscules.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import ConnectorSlot, ToolCallClient, connector

DEFAULT_JIRA_BASE_URL = "https://toollive.atlassian.net/jira"
DEFAULT_JIRA_CREDENTIALS_NAME = "atlassian"

ISSUE_FIELDS = ["summary", "description", "status", "issuetype", "priority",
                "labels", "assignee", "reporter", "comment"]
SEARCH_FIELDS = ["summary", "status", "issuetype", "priority", "labels", "assignee"]


def _env(*names: str) -> str | None:
    """Première variable d'env non vide parmi `names` (convention minuscule,
    fallback sur l'ancien nom en majuscules)."""
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


def _build_atlassian_connector() -> ConnectorSlot:
    """Slot `atlassian` pour runs managés non attended : identité service du worker
    (`run_as="deployment"`), preflight auto_auth désactivé (son listing des credentials
    au scope user 404 pour le service account et tue l'exécution), et credential
    workspace épinglé par nom (`jira_credentials_name`, défaut « atlassian »)."""
    return connector(
        "atlassian",
        run_as="deployment",
        auto_auth=False,
        credentials_name=_env("jira_credentials_name", "JIRA_CREDENTIALS_NAME")
        or DEFAULT_JIRA_CREDENTIALS_NAME,
    )


# Slot déclaré au niveau module ; attaché au workflow via @uses_connectors(...).
atlassian_connector = _build_atlassian_connector()


def cloud_id() -> str:
    """cloudId Atlassian (UUID ou host du site) : env `jira_cloud_id`, sinon le
    host de `jira_base_url` (défaut https://toollive.atlassian.net/jira)."""
    cid = _env("jira_cloud_id", "JIRA_CLOUD_ID")
    if cid:
        return cid
    base = _env("jira_base_url", "JIRA_BASE_URL") or DEFAULT_JIRA_BASE_URL
    return urllib.parse.urlparse(base).hostname or base


def issue_url(issue_key: str) -> str:
    """URL web du ticket (`https://<site>/jira/browse/<KEY>`)."""
    base = (_env("jira_base_url", "JIRA_BASE_URL") or DEFAULT_JIRA_BASE_URL).rstrip("/")
    return f"{base}/browse/{issue_key}"


_INLINE_TYPES = {"text", "mention", "emoji", "date", "status", "inlineCard", "hardBreak"}


def _adf_to_text(node: Any) -> str:
    """Convertit un noeud ADF (Atlassian Document Format) en texte brut — utile si
    l'API renvoie du JSON ADF au lieu du markdown demandé. Les noeuds inline sont
    concaténés tels quels, les noeuds bloc sont séparés par un saut de ligne."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("text") is not None:
            return str(node["text"])
        out = ""
        for child in node.get("content") or []:
            ctext = _adf_to_text(child)
            if not ctext:
                continue
            ctype = child.get("type") if isinstance(child, dict) else None
            if ctype in _INLINE_TYPES:
                out += ctext
            else:
                if out and not out.endswith("\n"):
                    out += "\n"
                out += ctext
        return out
    if isinstance(node, list):
        return "\n".join(t for t in (_adf_to_text(n) for n in node) if t)
    return str(node)


def _blocks_text(dumped: Any) -> str:
    """Texte concaténé des blocs `content` d'une réponse de tool MCP."""
    content = dumped.get("content") if isinstance(dumped, dict) else getattr(dumped, "content", None)
    parts = []
    for block in content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text if isinstance(text, str) else str(text))
    return "\n".join(parts)


def _parse_tool_result(dumped: Any) -> Any:
    """Résultat d'un call_tool : JSON parsé si le texte est du JSON, sinon texte brut."""
    text = _blocks_text(dumped)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def _call(jira: ToolCallClient, tool: str, **arguments: Any) -> Any:
    """Appelle un outil du connecteur et parse sa réponse (JSON ou texte)."""
    return _parse_tool_result(await jira.call_tool(tool_name=tool, arguments=arguments))


def _brief(issue: dict) -> dict:
    """Vue synthétique d'un ticket (résultats de recherche)."""
    fields = issue.get("fields") or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "issuetype": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "labels": fields.get("labels") or [],
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "url": issue_url(issue.get("key") or ""),
    }


def _issue_detail(issue: dict) -> dict:
    """Ticket complet normalisé (getJiraIssue) : description markdown + commentaires."""
    fields = issue.get("fields") or {}
    desc = fields.get("description")
    if isinstance(desc, dict):
        desc = _adf_to_text(desc)
    comments = (fields.get("comment") or {}).get("comments") or issue.get("comments") or []
    detail = _brief(issue)
    detail.update({
        "id": issue.get("id"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "description": desc or "",
        "comments": [
            {
                "author": (c.get("author") or {}).get("displayName"),
                "body": (_adf_to_text(c.get("body")) if isinstance(c.get("body"), dict)
                         else (c.get("body") or "")),
            }
            for c in comments
        ],
    })
    return detail


async def _fetch_issue(jira: ToolCallClient, issue_key: str) -> dict:
    result = await _call(
        jira, "getJiraIssue",
        cloudId=cloud_id(), issueIdOrKey=issue_key,
        fields=ISSUE_FIELDS, responseContentFormat="markdown",
    )
    if not isinstance(result, dict) or "key" not in result:
        raise RuntimeError(
            f"ticket JIRA {issue_key!r} introuvable ou illisible: {str(result)[:400]}")
    return _issue_detail(result)


async def _search(jira: ToolCallClient, jql: str, max_results: int) -> list:
    result = await _call(
        jira, "searchJiraIssuesUsingJql",
        cloudId=cloud_id(), jql=jql, maxResults=max_results,
        fields=SEARCH_FIELDS, responseContentFormat="markdown",
    )
    issues = result.get("issues") if isinstance(result, dict) else None
    return [_brief(i) for i in issues or []]


async def _comment(jira: ToolCallClient, issue_key: str, body: str) -> dict:
    """Poste un commentaire markdown. NON bloquant : en cas d'erreur, retourne
    {"ok": False, "error": ...} sans faire échouer le pipeline."""
    try:
        await _call(jira, "addCommentToJiraIssue",
                    cloudId=cloud_id(), issueIdOrKey=issue_key,
                    commentBody=body, contentFormat="markdown")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


async def _transition(jira: ToolCallClient, issue_key: str, transition_name: str) -> dict:
    """Transition de statut par NOM (ex. « In Progress », « code-review ») : résout
    l'id via getTransitionsForJiraIssue puis appelle transitionJiraIssue. Si la
    transition n'est pas disponible sur le ticket : {"ok": False, "reason": ...}."""
    if not transition_name:
        return {"ok": False, "reason": "nom de transition vide"}
    try:
        transitions = await _call(jira, "getTransitionsForJiraIssue",
                                  cloudId=cloud_id(), issueIdOrKey=issue_key)
        wanted = transition_name.strip().lower()
        match = None
        for t in (transitions or {}).get("transitions") or []:
            names = {str(t.get("name", "")).lower(),
                     str((t.get("to") or {}).get("name", "")).lower()}
            if wanted in names:
                match = t
                break
        if not match:
            return {"ok": False,
                    "reason": f"transition {transition_name!r} indisponible sur {issue_key}"}
        await _call(jira, "transitionJiraIssue",
                    cloudId=cloud_id(), issueIdOrKey=issue_key,
                    transition={"id": match["id"]})
        return {"ok": True, "transition": match.get("name"),
                "to": (match.get("to") or {}).get("name")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@workflows.activity()
async def fetch_jira_issue(issue_key: str,
                           jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Lit un ticket JIRA (getJiraIssue) et le normalise. Échoue fort si le ticket
    est introuvable — une mauvaise clé passée en entrée doit arrêter le run."""
    return await _fetch_issue(jira, issue_key)


@workflows.activity()
async def jira_search(jql: str, max_results: int = 10,
                      jira: ToolCallClient = Depends(atlassian_connector)) -> list:
    """Recherche JQL (searchJiraIssuesUsingJql), ex.
    `project = TOOLLIVE AND statusCategory = "To Do" ORDER BY created DESC`."""
    return await _search(jira, jql, max_results)


@workflows.activity()
async def jira_comment(issue_key: str, body: str,
                       jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Poste un commentaire markdown sur le ticket (addCommentToJiraIssue).
    Non bloquant pour le pipeline (voir _comment)."""
    return await _comment(jira, issue_key, body)


@workflows.activity()
async def jira_transition(issue_key: str, transition_name: str,
                          jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition de statut par nom explicite (voir _transition)."""
    return await _transition(jira, issue_key, transition_name)


@workflows.activity()
async def jira_transition_start(issue_key: str,
                                jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition de démarrage du run : env `jira_transition_start` (défaut
    « In Progress »)."""
    name = _env("jira_transition_start", "JIRA_TRANSITION_START") or "In Progress"
    return await _transition(jira, issue_key, name)


@workflows.activity()
async def jira_transition_review(issue_key: str,
                                 jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition à l'ouverture de la MR : env `jira_transition_review` (défaut
    « code-review »)."""
    name = _env("jira_transition_review", "JIRA_TRANSITION_REVIEW") or "code-review"
    return await _transition(jira, issue_key, name)


@workflows.activity()
async def jira_transition_done(issue_key: str,
                               jira: ToolCallClient = Depends(atlassian_connector)) -> dict:
    """Transition finale après approbation : env `jira_transition_done` (défaut
    vide = aucune transition, le merge reste manuel)."""
    name = _env("jira_transition_done", "JIRA_TRANSITION_DONE") or ""
    return await _transition(jira, issue_key, name)
