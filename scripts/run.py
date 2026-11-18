"""Déclenche une exécution du pipeline spec-to-tests.

Usage :
    python scripts/run.py "<demande produit>" [--repo c8y|micro|<URL ou chemin>]

Exemples :
    python scripts/run.py "Ajouter un icone de recherche dans le tl-input" --repo c8y
    # Reprendre/compléter une MR en cours (travaille sur sa branche source, la met à jour) :
    python scripts/run.py "repars de la MR 1585 : applique les retours de code review" --repo c8y
    python scripts/run.py "Exposer le statut OEM dans le microservice" --repo micro
    python scripts/run.py "..." --repo https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git
    # Si le secret gitlab_token n'est pas bindé au déploiement worker, embarquer le PAT dans l'URL :
    python scripts/run.py "..." --repo "https://oauth2:<PAT>@gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git"
    # Traiter un ticket JIRA directement (lu via le connecteur MCP `atlassian`,
    # le pipeline commente le ticket et y applique les transitions de statut) :
    python scripts/run.py "implémente le ticket" --repo c8y --jira TOOLLIVE-3600
    # Lister/rechercher des tickets côté client : python scripts/jira.py 'project = TOOLLIVE AND statusCategory = "To Do"'

En mode cloud, les alias c8y/micro sont résolus via `gitlab_c8y_repo` /
`gitlab_micro_repo` (URL GitLab, ex. https://gitlab.toollive.bearstech.com/
toollive-team/toollive-business/toollive-c8y.git). Répondre aux gates avec :
    python scripts/approve.py <execution_id> approve|reject
"""

import argparse
import json
import os
import urllib.parse

from mistralai.client import Mistral

DEFAULT_JIRA_BASE_URL = "https://toollive.atlassian.net/jira"


def _cloud_id() -> str:
    """cloudId Atlassian (UUID ou host du site) : env `jira_cloud_id`, sinon le
    host de `jira_base_url` (défaut https://toollive.atlassian.net/jira)."""
    cid = os.environ.get("jira_cloud_id") or os.environ.get("JIRA_CLOUD_ID")
    if cid:
        return cid
    base = (os.environ.get("jira_base_url") or os.environ.get("JIRA_BASE_URL")
            or DEFAULT_JIRA_BASE_URL)
    return urllib.parse.urlparse(base).hostname or base


def _tool_text(res) -> str:
    """Texte concaténé des blocs `content` d'une réponse de tool connecteur."""
    parts = []
    for block in res.content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text if isinstance(text, str) else str(text))
    return "\n".join(parts)


def _fetch_issue_brief(client: Mistral, issue_key: str) -> dict | None:
    """Lit le ticket côté client via le connecteur `atlassian` (credential de
    l'utilisateur connecté) pour l'afficher au lancement — best-effort : en cas
    d'échec, seul le worker le lira (il a son propre accès via le slot)."""
    try:
        res = client.beta.connectors.call_tool(
            connector_id_or_name="atlassian",
            tool_name="getJiraIssue",
            arguments={"cloudId": _cloud_id(), "issueIdOrKey": issue_key,
                       "fields": ["summary", "status", "issuetype", "priority", "labels"],
                       "responseContentFormat": "markdown"},
        )
        data = json.loads(_tool_text(res))
        fields = data.get("fields") or {}
        base = (os.environ.get("jira_base_url") or os.environ.get("JIRA_BASE_URL")
                or DEFAULT_JIRA_BASE_URL).rstrip("/")
        return {"key": data.get("key"), "summary": fields.get("summary"),
                "status": (fields.get("status") or {}).get("name"),
                "url": f"{base}/browse/{data.get('key')}"}
    except Exception:
        return None

# Alias des repos cibles. En mode cloud, pointer vers les URL GitLab via les
# variables d'env `gitlab_c8y_repo` / `gitlab_micro_repo` (le worker clone alors le
# repo avec `gitlab_token`). En local, les chemins par défaut fonctionnent tels quels.
REPOS = {
    "c8y": (os.environ.get("gitlab_c8y_repo") or os.environ.get("GITLAB_C8Y_REPO")
            or r"C:\projets\toollive-c8y"),
    "micro": (os.environ.get("gitlab_micro_repo") or os.environ.get("GITLAB_MICRO_REPO")
              or r"C:\projets\toollive-c8y-microservice"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="demande produit, en langage naturel")
    parser.add_argument(
        "--repo",
        default=(os.environ.get("pipeline_repo") or os.environ.get("PIPELINE_REPO")
                 or "c8y"),
        help="alias 'c8y' / 'micro', ou chemin absolu du repo cible (défaut: c8y)",
    )
    parser.add_argument(
        "--jira",
        default=(os.environ.get("jira_issue") or os.environ.get("JIRA_ISSUE") or ""),
        help="clé du ticket JIRA à traiter (ex. TOOLLIVE-3600) : lu via le connecteur "
             "MCP `atlassian`, le pipeline commente le ticket et y applique les "
             "transitions de statut",
    )
    args = parser.parse_args()
    repo = REPOS.get(args.repo, args.repo)
    # Ne jamais afficher un PAT embarqué dans l'URL (https://oauth2:<PAT>@host/...)
    import re
    repo_display = re.sub(r"(https?://)[^@/\s]+:[^@/\s]+@", r"\1***@", repo)

    # `mistral_api_key` en priorité ; fallback MISTRAL_API_KEY (nom réservé,
    # injecté en majuscules par le runtime managé).
    client = Mistral(api_key=os.environ.get("mistral_api_key")
                     or os.environ.get("MISTRAL_API_KEY"))
    jira_info = _fetch_issue_brief(client, args.jira) if args.jira else None
    execution = client.workflows.execute_workflow(
        workflow_identifier="spec-to-tests",
        input={"repo": repo, "request": args.request, "jira_issue": args.jira},
        wait_for_result=False,
    )
    print(json.dumps({"execution_id": execution.execution_id, "repo": repo_display,
                      "jira": jira_info or (args.jira or None)},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
