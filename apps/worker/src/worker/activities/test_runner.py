"""Quality gate bloquante : la suite de tests, et la notification des gates humaines.

run_tests est la gate exécutée après CHAQUE boucle dev et après CHAQUE fusion.
open_approval notifie l'humain (webhook Slack/Linear...) qu'une décision l'attend ;
l'attente elle-même se fait CÔTÉ WORKFLOW via workflow.wait_condition sur les
signaux `approve` / `reject` (voir workflows/spec_to_tests.py) — le workflow dort
sans aucun coût compute pendant que l'humain relit.
https://docs.mistral.ai/studio/workflows/building-workflows/waiting_for_conditions
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request

import mistralai.workflows as workflows

TEST_CMD = ["pytest", "-x", "-q"]       # fallback si la stack n'est pas détectée
TEST_TIMEOUT_SECONDS = 1800             # la suite Jest d'un gros front peut être longue
DEP_TIMEOUT_SECONDS = 1800              # npm ci / uv sync sur un clone frais
REVIEW_TIMEOUT_SECONDS = 24 * 3600     # la gate humaine attend jusqu'à 24h


def _env(*names: str) -> str | None:
    """Première variable d'env non vide parmi `names` (convention minuscule,
    fallback sur l'ancien nom en majuscules)."""
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    return None


def _jest_max_workers() -> str:
    return _env("pipeline_jest_max_workers", "PIPELINE_JEST_MAX_WORKERS") or "2"


def _node_max_old_space() -> str:
    """Tas V8 max (Mo) pour Node/Jest — env `pipeline_node_max_old_space` (défaut 2048)."""
    return _env("pipeline_node_max_old_space", "PIPELINE_NODE_MAX_OLD_SPACE") or "2048"


def _node_env(heap_mb: str) -> dict:
    return {"NODE_OPTIONS": f"--max-old-space-size={heap_mb}"}


# La spec de déploiement n'expose AUCUN réglage mémoire/CPU (vérifié dans le
# SDK : backend mistral_cloud/kubernetes sans champ resources) : la RAM du
# worker est fixée par la plateforme. Stratégie côté tests : Jest borné
# (--maxWorkers + tas V8), et si le log montre un OOM -> un unique retry
# --runInBand avec un tas réduit.
_OOM_MARKERS = ("out of memory", "enomem", "cannot allocate memory", "oomkill", "killed")


def _looks_like_oom(log: str) -> bool:
    low = (log or "").lower()
    return any(marker in low for marker in _OOM_MARKERS)


def test_cmd_for(worktree: str) -> list[str]:
    """Commande de test auto-détectée selon la stack du repo cible :

    - repo Python (pyproject.toml à la racine)   -> `uv run pytest -x -q`
      (ex. C:\\projets\\toollive-c8y-microservice)
    - repo Angular/Node (package.json à la racine) -> `npm run test-unit -- --ci`
      (ex. C:\\projets\\toollive-c8y, Jest via jest-preset-angular ; --maxWorkers borné
      (2 par défaut), retry --runInBand si le log montre un OOM)
    - override explicite : variable d'env `pipeline_test_cmd`
    """
    override = (os.environ.get("pipeline_test_cmd")
                or os.environ.get("PIPELINE_TEST_CMD"))
    if override:
        return shlex.split(override)
    if os.path.isfile(os.path.join(worktree, "pyproject.toml")):
        return ["uv", "run", "pytest", "-x", "-q"]
    if os.path.isfile(os.path.join(worktree, "package.json")):
        return ["npm", "run", "test-unit", "--", "--ci", "--silent",
                f"--maxWorkers={_jest_max_workers()}"]
    return TEST_CMD


async def _run_logged(cmd: list[str], cwd: str, timeout: int,
                      extra_env: dict | None = None) -> dict:
    if os.name == "nt" and cmd[0] in ("npm", "npx", "ng", "uv"):
        cmd = ["cmd", "/c", *cmd]      # npm/ng/uv sont des .cmd sous Windows
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env={**os.environ, **(extra_env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return {"passed": proc.returncode == 0, "cmd": " ".join(cmd),
            "log": out.decode(errors="replace")}


async def ensure_deps(worktree: str) -> dict:
    """Installe les dépendances d'un clone frais (le worker clone à vide) :
    - package.json sans node_modules -> `npm ci` (fallback `npm install`)
    - pyproject.toml sans .venv      -> `uv sync`
    Helper interne (appelé par run_tests), pas une activité séparée.
    """
    logs = []
    if (os.path.isfile(os.path.join(worktree, "package.json"))
            and not os.path.isdir(os.path.join(worktree, "node_modules"))):
        res = await _run_logged(["npm", "ci", "--no-audit", "--no-fund"],
                                worktree, DEP_TIMEOUT_SECONDS,
                                extra_env=_node_env(_node_max_old_space()))
        if not res["passed"]:    # lockfile absent ou incompatible -> install
            res2 = await _run_logged(["npm", "install", "--no-audit", "--no-fund"],
                                     worktree, DEP_TIMEOUT_SECONDS,
                                     extra_env=_node_env(_node_max_old_space()))
            res = res2 if res2["passed"] else res
        logs.append(res["log"][-2000:])
    if (os.path.isfile(os.path.join(worktree, "pyproject.toml"))
            and not os.path.isdir(os.path.join(worktree, ".venv"))):
        res = await _run_logged(["uv", "sync"], worktree, DEP_TIMEOUT_SECONDS)
        logs.append(res["log"][-2000:])
    return {"log": "\n".join(logs)}


@workflows.activity()
async def run_tests(worktree: str) -> dict:
    heap = _node_max_old_space()
    deps = await ensure_deps(worktree)
    result = await _run_logged(test_cmd_for(worktree), worktree, TEST_TIMEOUT_SECONDS,
                               extra_env=_node_env(heap))
    result["log"] = (deps["log"] + "\n" + result["log"]) if deps["log"] else result["log"]
    if result["passed"] or not _looks_like_oom(result["log"]):
        return result
    # Stratégie mémoire (RAM worker non réglable côté déploiement) : un unique
    # retry en --runInBand (1 worker Jest) avec un tas V8 réduit (<= 1024 Mo).
    try:
        retry_heap = str(min(1024, int(heap)))
    except ValueError:
        retry_heap = "1024"
    retry_cmd = [a for a in test_cmd_for(worktree) if not a.startswith("--maxWorkers")]
    retry_cmd.append("--runInBand")
    retry = await _run_logged(retry_cmd, worktree, TEST_TIMEOUT_SECONDS,
                              extra_env=_node_env(retry_heap))
    retry["log"] = (result["log"]
                    + f"\n\n[retry --runInBand, NODE_OPTIONS heap={retry_heap}MB]\n"
                    + retry["log"])
    retry["retried_in_band"] = True
    return retry


@workflows.activity()
async def open_approval(subject: str, payload: str, execution_id: str = "") -> dict:
    """Notifie l'humain qu'une gate attend sa décision (signal approve/reject).

    Canal : webhook générique (Slack incoming webhook, Linear, ...) lu dans
    `approval_webhook_url`. Le message contient l'execution_id et la commande
    pour répondre : `python scripts/approve.py <execution_id> approve|reject`.
    """
    url = (os.environ.get("approval_webhook_url")
           or os.environ.get("APPROVAL_WEBHOOK_URL"))
    base = {"subject": subject, "execution_id": execution_id}
    if not url:
        return {**base, "notified": False,
                "reason": "approval_webhook_url non défini — notification ignorée "
                          "(pense à envoyer le signal à la main)"}
    text = (
        f":bell: *Gate d'approbation — {subject}*\n"
        f"Exécution : `{execution_id}`\n"
        f"Répondre : `python scripts/approve.py {execution_id} approve` "
        f"(ou `reject`)\n"
        f"```{payload[:2500]}```"
    )

    def _post() -> int:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status

    try:
        status = await asyncio.to_thread(_post)
        return {**base, "notified": True, "status": status}
    except Exception as exc:  # le workflow continue même si la notif échoue
        return {**base, "notified": False, "error": str(exc)}
