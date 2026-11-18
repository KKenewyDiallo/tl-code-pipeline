"""Recherche / lecture de tickets JIRA via le connecteur MCP `atlassian` (lecture seule).

Usage :
    python scripts/jira.py 'project = TOOLLIVE AND statusCategory = "To Do" ORDER BY created DESC'
    python scripts/jira.py --issue TOOLLIVE-3600          # détail d'un ticket
    python scripts/jira.py --max 50 'created >= -7d ORDER BY created DESC'

Le script tourne sous TON identité avec TON credential du connecteur
`atlassian` (Studio > Connectors — l'authentification se fait dans le navigateur
au premier usage). cloudId : env `jira_cloud_id`, sinon le host de
`jira_base_url` (défaut https://toollive.atlassian.net/jira).

Note : JIRA refuse les JQL « unbounded » — la requête doit contenir une
restriction (project, created >= -Nd, assignee, ...).
"""

import argparse
import json
import os
import urllib.parse

from mistralai.client import Mistral

DEFAULT_JIRA_BASE_URL = "https://toollive.atlassian.net/jira"


def _cloud_id() -> str:
    cid = os.environ.get("jira_cloud_id") or os.environ.get("JIRA_CLOUD_ID")
    if cid:
        return cid
    base = (os.environ.get("jira_base_url") or os.environ.get("JIRA_BASE_URL")
            or DEFAULT_JIRA_BASE_URL)
    return urllib.parse.urlparse(base).hostname or base


def _tool_text(res) -> str:
    parts = []
    for block in res.content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text if isinstance(text, str) else str(text))
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jql", nargs="?",
                        help="requête JQL (doit contenir une restriction, ex. "
                             "'project = TOOLLIVE AND created >= -14d ORDER BY created DESC')")
    parser.add_argument("--issue", help="clé d'un ticket à afficher en détail")
    parser.add_argument("--max", type=int, default=20, help="nb max de résultats (défaut 20)")
    args = parser.parse_args()

    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))

    if args.issue:
        res = client.beta.connectors.call_tool(
            connector_id_or_name="atlassian",
            tool_name="getJiraIssue",
            arguments={"cloudId": _cloud_id(), "issueIdOrKey": args.issue,
                       "fields": ["summary", "description", "status", "issuetype",
                                  "priority", "labels", "assignee", "reporter", "comment"],
                       "responseContentFormat": "markdown"},
        )
        print(json.dumps(json.loads(_tool_text(res)), indent=2, ensure_ascii=False)[:8000])
        return

    jql = args.jql or "created >= -14d ORDER BY created DESC"
    res = client.beta.connectors.call_tool(
        connector_id_or_name="atlassian",
        tool_name="searchJiraIssuesUsingJql",
        arguments={"cloudId": _cloud_id(), "jql": jql, "maxResults": args.max,
                   "fields": ["summary", "status", "issuetype", "priority", "labels", "assignee"],
                   "responseContentFormat": "markdown"},
    )
    data = json.loads(_tool_text(res))
    issues = data.get("issues") or []
    base = (os.environ.get("jira_base_url") or os.environ.get("JIRA_BASE_URL")
            or DEFAULT_JIRA_BASE_URL).rstrip("/")
    for i in issues:
        f = i.get("fields") or {}
        status = (f.get("status") or {}).get("name", "")
        itype = (f.get("issuetype") or {}).get("name", "")
        print(f"{i.get('key', ''):<14} {status:<26} {itype:<12} {(f.get('summary') or '')[:90]}")
    print(f"\n{len(issues)} ticket(s) — détail : python scripts/jira.py --issue <CLE>"
          f" — lancer le pipeline : python scripts/run.py \"...\" --jira <CLE>")


if __name__ == "__main__":
    main()
