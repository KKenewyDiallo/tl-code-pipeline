# Architecture — code-pipeline

Pipeline multi-agents autonome de développement, exécuté sur **Mistral
Workflows** (orchestration durable, Temporal sous le capot) :
**spec (interview développeur + SPEC.md) → gate humaine → plan → dev parallèle (git worktrees) →
boucle tests bornée → fusions séquentielles → gate humaine finale → MR ouverte (merge manuel)**.

## Vue d'ensemble

```
Humain ─► agent Spec (SPEC.md) ─► GATE 1 : approbation de SPEC.md (signal)
              │
              ▼
        agent Planner (parse SPEC.md → JSON de tâches + dépendances)
              │
              ▼ (parallèle, 1 worktree par tâche indépendante)
        agents Dev ⇄ agent Tester ──► activity run_tests   (boucle bornée)
              │
              ▼
        fusions séquentielles dans la branche de base + re-run complet des tests
              │
              ▼
        GATE 2 : review du diff cumulé (signal) ──► MR ouverte (merge manuel)
```

## Composants

```
apps/worker/src/worker/
├── entrypoints/worker.py        # discovery des workflows + run_worker
├── workflows/spec_to_tests.py   # orchestrateur durable "spec-to-tests" (6 étapes)
├── activities/
│   ├── git_tools.py             # worktree add/remove, commit, merge séquentiel, diff
│   ├── test_runner.py           # run_tests (quality gate) + gates humaines (approval)
└── agents/
    ├── definitions.py           # spec / planner / dev / tester / reviewer + bornes
    ├── run_agent.py             # boucle modèle → tools, bornée à MAX_AGENT_TURNS
    └── tools.py                 # shell/fichiers confinés au worktree (safe_path)

scripts/run.py                   # déclenche une exécution (--repo c8y|micro|<chemin>)
docs/architecture.md             # ce document
```

### Agents et modèles

| Agent    | Rôle                                             | Modèle                  |
|----------|--------------------------------------------------|-------------------------|
| spec     | interviewe le développeur, produit SPEC.md (jamais de code) | `mistral-large-latest`  |
| planner  | SPEC.md → tâches JSON avec dépendances         | `mistral-large-latest`  |
| dev      | implémente UNE tâche dans SON worktree           | `mistral-medium-latest` |
| tester   | écrit/met à jour les tests des scénarios WHEN/THEN| `mistral-medium-latest` |
| reviewer | verdict JSON sur le diff cumulé (gate finale)    | `mistral-large-latest`  |

Un objet `Agent` ne traverse pas Temporal : seuls nom/paramètres passent ;
l'agent est reconstruit dans l'activité via `run_agent` (Agents SDK, boucle
bornée à `MAX_AGENT_TURNS = 20` tours d'outils).

## Déroulement d'une exécution

`run(repo, request)` — `repo` = chemin du repo cible, `request` = demande en
langage naturel.

1. **Spec** — l'agent spec interviewe le développeur (ligne `QUESTION:`, signal
   `reply`) pour valider le changement, puis écrit `SPEC.md` à la racine du
   repo cible. Chaque critère d'acceptation a un scénario WHEN/THEN testable.
2. **GATE 1** — `open_approval` notifie l'humain, `wait_for_approval` dort sur
   le signal `approve`/`reject` (durable, aucun worker actif pendant l'attente).
3. **Planner** — décompose en tâches `{id, title, capability, files_hint,
   depends_on, acceptance}` ; les interfaces partagées sont dans la première tâche.
4. **Dev parallèle** — les tâches sans dépendances tournent en parallèle
   (`workflows.workflow.parallel`), chacune dans son worktree. Boucle bornée :
   dev → commit → tester → `run_tests` ; `MAX_DEV_ITERATIONS = 3` tours max,
   sinon escalade humaine (jamais de boucle infinie).
5. **Fusions séquentielles** — une branche `agent/<task_id>` à la fois, et la
   suite de tests complète est re-jouée après CHAQUE fusion. Conflit → abort +
   escalade. Si la suite ne peut pas tourner dans le worker (OOM killer même en
   `--runInBand`, ou outil de test introuvable genre `jest: not found` après un
   `npm ci` interrompu — RAM fixée par la plateforme), la gate est non bloquante
   (`unrunnable`, avec une réinstallation forcée des dépendances tentée une fois) :
   on pousse quand même la branche `release/*` + MR, la CI valide les tests.
6. **GATE 2** — review du diff cumulé, signal `approve`/`reject`, puis MR laissée ouverte (merge manuel sur GitLab ; l'auto-merge échouait en HTTP 405).

## Garanties

- **Durabilité** : chaque activité est enregistrée ; un worker qui crashe
  reprend au dernier pas complété.
- **Isolation** : 1 tâche = 1 worktree = 1 branche ; aucun agent ne commit sur
  la branche de base. Les tools shell/fichiers des agents dev sont confinés au
  worktree (`safe_path` bloque tout path traversal, `sudo`, `rm -rf /`,
  `git push`).
- **Bornes** : `MAX_DEV_ITERATIONS = 3`, `MAX_AGENT_TURNS = 20`.
- **Pas de hors-scope** : un agent dev n'implémente que sa tâche ; le reviewer
  rejette tout hunk hors `files_hint`.

## Repos cibles configurés

Le pipeline détecte automatiquement la stack du repo passé en entrée :

| Cible | Repo | Stack | Tests (auto-détectés) | Lint | Branche de base |
|-------|------|-------|-----------------------|------|-----------------|
| `c8y` | `C:\projets\toollive-c8y` | Angular 18 + Cumulocity (`@c8y/*` 1021.x) | `npm run test-unit -- --ci --silent` (Jest, jest-preset-angular) | `npm run lint` (eslint) | `develop` |
| `micro` | `C:\projets\toollive-c8y-microservice` | Python 3.14 + Flask + uv | `uv run pytest -x -q` | `uv run ruff check` | `develop` |

Détection dans `activities/test_runner.py::test_cmd_for` :
`pyproject.toml` à la racine → pytest/uv ; `package.json` → Jest/npm ;
sinon fallback `TEST_CMD`. Override : variable d'env `pipeline_test_cmd`.

Les conventions de chaque repo cible sont dans son propre `AGENTS.md` (stack,
contraintes que l'agent ne peut pas deviner depuis le code) — l'agent spec le
lit en premier :

- `toollive-c8y/AGENTS.md` — Angular/Cumulocity, modules métier, conventions
  (TOOLLIVE-C8Y.md, docs/COMMENT_DOCUMENTER.md), E2E codeceptjs hors scope.
- `toollive-c8y-microservice/AGENTS.md` — Flask/uv, secrets via `./secrets`
  (jamais en dur), pas de modif CI.

**Worktrees** : `../worktrees/<nom-du-repo>/<task_id>` à côté du repo cible
(chemin absolu retourné par `create_worktree`), pour éviter les collisions entre
les deux repos.

**Branche de base** : `git_tools.base_branch` — env `pipeline_base_branch`, sinon
la première de `develop`, `main`, `master` qui existe (convention toollive :
`develop`, même si `main`/`master` existent aussi). Le diff de review est calculé
contre cette branche.

Lancement :

```bash
python scripts/run.py "<demande>" --repo c8y      # front Angular
python scripts/run.py "<demande>" --repo micro    # microservice Python
python scripts/run.py "<demande>" --repo C:\chemin\vers\autre\repo   # mode local
python scripts/run.py "<demande>" --repo https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git  # mode cloud
```

En mode cloud, les alias `c8y` / `micro` sont résolus via les variables
d'env `gitlab_c8y_repo` / `gitlab_micro_repo` (URL GitLab des repos cibles).
Le worker clone alors le repo cible dans `pipeline_workspace` avec
`gitlab_token` (voir `git_tools.ensure_workspace`), pousse une branche
`release/<change_id>`, ouvre une **Merge Request GitLab** (la gate de review
finale = la review de la MR), la MR reste ouverte, merge manuel après le signal `approve`.

**Auth forge — 100% PAT, pas de clé SSH** : pour les opérations git
(clone/push), les URL SSH (`git@host:...`, `ssh://...`) sont converties en
`https://host/...` (`git_tools.normalize_repo_url`) et le PAT (`gitlab_token`)
est envoyé en Basic auth via `http.extraHeader` (doc GitLab : tout username non
vide + PAT en mot de passe). L'API REST (création/merge de la MR) prend le même
PAT en `Authorization: Bearer` (`_parse_repo_url` accepte https/ssh/scp-like).
Aucun credential dans une URL loggée ; `GIT_TERMINAL_PROMPT=0` dans le
conteneur. Scopes du PAT : `read_repository`, `write_repository`, `api`.

## Interaction humaine avec le workflow (signaux + query)

Le workflow `SpecToTests` expose **3 signaux** et **1 query** ; il dort sur
`workflow.wait_condition` (aucun coût compute pendant l'attente) :

| Canal | Côté workflow | Côté humain | Usage |
|---|---|---|---|
| signal `approve` / `reject` | gate de validation (proposal, review finale) | `python scripts/approve.py <execution_id> approve\|reject` | décision binaire sur une gate |
| signal `reply` (payload `{"text": ...}`) | interview de l'agent spec : le workflow transmet le texte à l'agent et relance | `python scripts/talk.py <execution_id> "ta réponse"` | **discuter avec l'agent** (phase `spec`) |
| query `state` | lecture seule : `phase`, `pending_question`, `verdict`, `conversation` (transcript) | `python scripts/status.py <execution_id>` | voir où en est l'exécution et lire le transcript |

```python
@workflows.workflow.signal(name="reply")
async def on_reply(self, text: str) -> None:
    self.conversation.append({"role": "human", "text": text})
    self.replies += 1

@workflows.workflow.query(name="state")
def get_state(self) -> dict:
    return {"phase": self.phase, "pending_question": self.pending_question,
            "verdict": self.verdict, "conversation": self.conversation}
# attente : await wf.wait_condition(lambda: self.replies > consumed, timeout=...)
```

Pendant la phase `spec`, l'agent peut terminer sa réponse par une ligne
`QUESTION: ...` : le pipeline notifie le webhook, dort jusqu'au signal `reply`
(ou `approve` pour forcer la finalisation, `reject` pour avorter), puis relance
l'agent avec le transcript complet. Pas de limite de tours (attente 24 h par question ; escalade après `MAX_EMPTY_SPEC_ROUNDS = 3` tours sans progression).
Quand `SPEC.md` est prêt, l'agent termine par `CHANGE_ID: <id>`.

- Notification : `test_runner.open_approval` poste sur le webhook
  `approval_webhook_url` (Slack...) avec l'`execution_id` et la commande à taper.
- Signaux : `client.workflows.executions.signal_workflow_execution(execution_id, name, input)`
  (REST : `POST /v1/workflows/executions/{id}/signals`).
- Query : `client.workflows.executions.query_workflow_execution(execution_id, name="state")`
  (REST : `POST /v1/workflows/executions/{id}/queries`).

Phases exposées par la query `state` : `init → spec → gate_spec → plan → dev
→ merge → gate_review → done`.

## Déploiement du worker (cloud)

Deux modes (doc : *Managed deployments* / *Self-hosted*) :

| | Managé | Self-hosted |
|---|---|---|
| Source du build | **miroir GitHub public** du repo `code-pipeline` (GitLab non supporté pour l'instant par les managed deployments) | repo **GitLab** privé, image buildée par la CI GitLab |
| Infra | aucune — Mistral Cloud build et run (`create_deployment(name=..., spec={"github_url": ..., "backend_spec": {"type": "mistral_cloud", "dockerfile_path": "deploy/docker/Dockerfile.worker"}})`) | votre cluster K8s via le Helm chart Mistral |
| Auth worker | service account injecté (pas de clé dans le repo) | `mistral_api_key` + `DEPLOYMENT_NAME` (env) |

Dans les deux modes : `deploy/docker/Dockerfile.worker` build l'image du
worker (`python -m worker.entrypoints.worker`). L'image embarque tout
l'outillage des activités : **git**, certificats CA, **Node.js 20 + npm**
(suite Jest du front) et **uv** (microservice Python). `run_tests` appelle d'abord
`ensure_deps` : `npm ci` (fallback `npm install`) si `node_modules` manque,
`uv sync` si `.venv` manque — un clone frais est directement testable.
**Timeout d'exécution et mémoire du worker** : le timeout d'exécution se déclare
sur le workflow (`@workflow.define(execution_timeout=...)` — 1h par défaut dans le
SDK, ce qui produisait des `TIMED_OUT` à 3600004 ms ; le pipeline le monte à 6h via
`WORKFLOW_EXECUTION_TIMEOUT`). La **mémoire du worker n'est PAS réglable** côté
déploiement (les specs backend `mistral_cloud`/`kubernetes` n'ont aucun champ
resources) : la stratégie est côté tests — Jest lancé avec `--maxWorkers=2` et
`NODE_OPTIONS=--max-old-space-size=2048` (env `pipeline_jest_max_workers` /
`pipeline_node_max_old_space`), plus un unique retry `--runInBand` (tas ≤ 1024 Mo)
si le log montre un OOM. L'identité git des commits est posée dans l'image
(Dockerfile) et en local dans chaque clone/worktree (`_ensure_identity`, surchargeable
via `pipeline_git_user_name` / `pipeline_git_user_email`) — sans elle, `git commit`
échoue (« Committer identity unknown »).

`DEPLOYMENT_NAME` sert au
routage des exécutions, `MISTRAL_API_KEY` (majuscules) est un nom réservé
injecté automatiquement par le runtime managé — le code lit `mistral_api_key`
en priorité avec fallback sur ce nom — et les autres secrets (`gitlab_token`,
`approval_webhook_url`, URLs des repos) sont bindés via le Secrets Manager
(managé) ou les secrets du cluster (self-hosted).

### Procédure managée (repo GitHub public `pef-tl/tl-code-pipeline`)

1. Créer les secrets dans **Studio > Secrets Manager** (workspace) :
   `gitlab_token` (PAT, scopes `read_repository` `write_repository` `api`),
   `approval_webhook_url`,
   `gitlab_c8y_repo` (= `git@gitlab.toollive.bearstech.com:toollive-team/toollive-business/toollive-c8y.git`),
   `gitlab_micro_repo` (= `.../toollive-c8y-microservice.git`),
   `pipeline_workspace` (= `/tmp/pipeline-workspaces`).
   **Ne pas binder `MISTRAL_API_KEY` (majuscules) dans la liste `secrets`** :
   c'est un nom réservé du runtime managé, injecté automatiquement dans le
   worker (l'API refuse de le binder explicitement). Le code lit
   `mistral_api_key` en priorité, avec fallback sur `MISTRAL_API_KEY`.
2. `export mistral_api_key=...` puis `python scripts/deploy.py` →
   `create_deployment(name="code-pipeline", spec={"github_url": "https://github.com/pef-tl/tl-code-pipeline", "revision": "main", "backend_spec": {"type": "mistral_cloud", "dockerfile_path": "deploy/docker/Dockerfile.worker", "secrets": [...]}})`.
   Mistral Cloud clone, build et run le worker ; chaque push sur `main` redéploie.
   Attendre le statut **Active** (Studio > Deployments).
3. Lancer une exécution : `python scripts/run.py "<demande>" --repo c8y`
   (l'alias lit `gitlab_c8y_repo` côté client).
4. Gate humaine : notification webhook avec l'`execution_id`, puis
   `python scripts/approve.py <execution_id> approve`.
5. Observer : Studio > Executions (history, traces), MR GitLab ouverte par le
   pipeline en fin de run.

**Les repos cibles (toollive-c8y, toollive-c8y-microservice) restent sur
GitLab** : le worker les clone via leur URL + `gitlab_token`, pousse les
branches `agent/*` et `release/<change_id>`, ouvre la MR via l'API (merge manuel)
GitLab. Aucun changement côté repos cibles.

## Variables d'environnement

Convention : **toutes les variables du pipeline sont en minuscules**
(`gitlab_token`, `pipeline_workspace`, ...). Le code tolère aussi l'ancien
nom en majuscules en fallback (`GITLAB_TOKEN`, ...) pour les déploiements
créés avant la convention. Seule exception stricte : `MISTRAL_API_KEY`
en majuscules, nom réservé injecté par le runtime managé.

| Variable | Rôle |
|----------|------|
| `mistral_api_key` (fallback `MISTRAL_API_KEY`, nom réservé injecté par le runtime managé) | clé API (scripts client + appels LLM des agents dans le worker) |
| `pipeline_repo` | repo cible par défaut de `scripts/run.py` (défaut: `c8y`) |
| `gitlab_c8y_repo` / `gitlab_micro_repo` | URL GitLab résolues par les alias `c8y` / `micro` en mode cloud |
| `pipeline_test_cmd` | override de la commande de test |
| `pipeline_base_branch` | override de la branche de base (défaut: auto-détection `develop`/`main`/`master`) |
| `gitlab_token` (ou `pipeline_git_token`) | token d'accès GitLab des repos cibles (clone/push/MR) — scopes `read_repository`, `write_repository`, `api` |
| `approval_webhook_url` | webhook de notification des gates (Slack incoming webhook…) |
| `pipeline_workspace` | dossier de clone des repos cibles en mode URL (défaut `/tmp/pipeline-workspaces`) |
| `pipeline_git_user_name` / `pipeline_git_user_email` | identité des commits git (défaut : config de l'image / `code-pipeline <code-pipeline@localhost>`) |
| `pipeline_jest_max_workers` | workers Jest max (défaut `2`) — borne mémoire, la RAM du worker n'étant pas réglable |
| `pipeline_node_max_old_space` | tas V8 Node max en Mo (défaut `2048`) |
| `DEPLOYMENT_NAME` | nom du déploiement worker (requis au boot ; routage des exécutions) |

## Points à brancher (TODO restants)

- Choisir le canal de notification concret (Slack incoming webhook recommandé)
  et binder `approval_webhook_url` comme secret du déploiement.
- Déployer le worker : miroir GitHub public + `create_deployment` (managé),
  ou CI GitLab + Helm chart (self-hosted). Voir *Déploiement du worker*.

## Démarrage

```bash
export mistral_api_key=...
uv sync                 # installe le worker
mistral dev             # terminal 1 : le worker attend
python scripts/run.py "Ajouter ..." --repo c8y   # terminal 2 : lance une exécution
```

Références : Mistral Workflows
(https://docs.mistral.ai/studio/workflows/overview), Agents SDK
(https://docs.mistral.ai/studio/agents/overview).
