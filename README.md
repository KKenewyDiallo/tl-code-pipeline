# code-pipeline — Pipeline multi-agents Mistral Workflows (spec → dev → tests)

Pipeline autonome de développement : un agent de spec interviewe le
développeur (questions/réponses) pour valider le changement et produit une
spec `SPEC.md`, un agent planner décompose en tâches, des agents dev implémentent en
parallèle dans des git worktrees isolés, un agent tester fait tourner la boucle
dev→tests, et chaque étape sensible passe par une gate humaine (signal durable).

## Architecture

```
Humain ─► agent Spec (interview + SPEC.md) ─► GATE: approbation de la spec
              │
              ▼
        agent Planner (parse SPEC.md)
              │
              ▼ (parallèle, 1 worktree par tâche)
        agents Dev ──► activity run_tests ──┐
              ▲                             │ échec (borné à N tours)
              └─────────────────────────────┘
              │
              ▼
        fusions séquentielles + re-run complet
              │
              ▼
        GATE: review du diff ──► MR ouverte (merge manuel)
```

## Démarrage

```bash
# 1. Prérequis
#    - clé API exportée: export mistral_api_key=...   (fallback: MISTRAL_API_KEY)
#    - Mistral CLI: https://docs.mistral.ai/studio/mistral-cli/quickstart
#    - uv, git

# 2. Installer les dépendances du worker
uv add 'mistralai-workflows[mistralai]'

# 3. Lancer le worker
mistral dev

# 4. Lancer une exécution
python scripts/run.py "Ajouter ..." --repo c8y     # front Angular toollive-c8y
python scripts/run.py "Ajouter ..." --repo micro   # microservice Python
# mode cloud (repos cibles sur GitLab) :
#   export gitlab_c8y_repo=https://gitlab.com/<org>/toollive-c8y.git
#   python scripts/run.py "Ajouter ..." --repo c8y
# traiter un ticket JIRA directement (connecteur MCP `atlassian`) :
#   python scripts/jira.py 'project = TOOLLIVE AND statusCategory = "To Do" ORDER BY created DESC'
#   python scripts/run.py "implemente le ticket" --repo c8y --jira TOOLLIVE-3600

# 5. Interagir avec le workflow en cours (notification webhook avec l'execution_id) :
python scripts/status.py <execution_id>                       # état + transcript de l'agent
python scripts/talk.py <execution_id> "oui, mais sans migration"  # répondre à l'agent spec
python scripts/approve.py <execution_id> approve               # valider une gate (ou reject)
```

En mode cloud, le pipeline clone le repo cible GitLab, ouvre une Merge Request
`release/<change_id>` (review finale dans GitLab) — MR laissée ouverte, merge manuel après `approve`.
Déploiement du worker : managé (miroir GitHub, `deploy/docker/Dockerfile.worker`)
ou self-hosted (CI GitLab + Helm chart) — voir `docs/architecture.md`.

## Repos cibles

Deux repos sont configurés (détail dans `docs/architecture.md`) :

- **Front Angular** `C:\projets\toollive-c8y` (alias `c8y`) — Angular 18 +
  Cumulocity, tests Jest auto-détectés
  (`npm run test-unit -- --ci`), lint `npm run lint`, branche de base `develop`.
- **Microservice Python** `C:\projets\toollive-c8y-microservice` (alias `micro`) —
  Python 3.14 + Flask + uv, tests `uv run pytest -x -q`,
  lint `uv run ruff check`, branche de base `develop`.

La commande de test est auto-détectée selon le repo (override :
`pipeline_test_cmd`), la branche de base aussi (override :
`pipeline_base_branch`). Toutes les variables d'env du pipeline sont en
minuscules.

## JIRA — traiter un ticket directement

Le workflow est branché sur le connecteur MCP **`atlassian`** (Studio >
Connectors) : aucun credential dans le code, la plateforme résout les
credentials au runtime selon l'identité du run.

```bash
python scripts/jira.py 'project = TOOLLIVE AND statusCategory = "To Do" ORDER BY created DESC'  # chercher un ticket
python scripts/run.py "implemente le ticket" --repo c8y --jira TOOLLIVE-3600                    # lancer le pipeline dessus
```

Avec `--jira <CLE>` (input `jira_issue` du workflow) :

- le ticket est lu via `getJiraIssue` (description + commentaires) et alimente
  la demande de l'agent spec ;
- transition de démarrage (`jira_transition_start`, défaut « In Progress »)
  + commentaire avec l'`execution_id` et les commandes (`status.py`,
  `approve.py`, `talk.py`) ;
- chaque gate humaine est aussi commentée sur le ticket ; à l'ouverture de la
  MR : transition `jira_transition_review` (défaut « code-review ») + commentaire
  avec le lien de la MR ; en fin de run : commentaire final (+ transition
  `jira_transition_done` si définie, vide par défaut — le merge reste manuel).

Variables d'env (minuscules) : `jira_base_url` (défaut
`https://toollive.atlassian.net/jira`), `jira_cloud_id` (UUID ou host du site),
`jira_transition_start|review|done`, `jira_credentials_name` (credential
épinglé par le slot connecteur, défaut `atlassian`). Détail : `docs/architecture.md`.

**Déploiement managé** : les runs non attended résolvent le connecteur côté
service account du worker — il faut un credential `atlassian` au scope
**workspace** (Studio > Connectors > atlassian > Credentials). En local
(`mistral dev`), ton credential utilisateur suffit. Le slot est déclaré
`run_as="deployment", auto_auth=False` avec `credentials_name` épinglé (env
`jira_credentials_name`, défaut `atlassian`) : le preflight `auto_auth` liste
les credentials au scope user et 404 pour le service account d'un déploiement
managé (exécution `0138f8a4...860559` / run `01a1eef1-1481`), ce qui tuait
l'exécution avant la première activité ; sans preflight, les appels d'outils
passent par la gateway du connecteur qui résout le credential workspace épinglé
côté serveur.

## Points clés

- **Durabilité** : chaque activité est enregistrée ; un worker qui crashe reprend
  au dernier pas complété (Temporal sous le capot).
- **Gates humaines** : `wait_for_approval` met le workflow en sommeil sur un
  signal — aucune ressource consommée pendant l'attente.
- **Boucle bornée** : `run_dev_agent` ⇄ `run_tests`, max `MAX_DEV_ITERATIONS`
  tours, puis escalade humaine.
- **Isolation** : un git worktree par tâche ; fusions séquentielles, suite de
  tests complète re-jouée après chaque fusion.

## Fichiers

```
apps/worker/src/worker/
├── entrypoints/worker.py        # discovery des workflows
├── workflows/spec_to_tests.py   # orchestrateur durable
├── activities/
│   ├── git_tools.py             # worktree, commit, merge
│   ├── jira_tools.py            # connecteur MCP atlassian (slot + activités JIRA)
│   ├── test_runner.py           # run_tests + gates humaines (approval)
│   └── agent_runner.py         # boucle agent (LLM + tools)
└── agents/
    ├── definitions.py           # spec/planner/dev/tester (Agents SDK)
    └── run_agent.py             # boucle Runner + tools

docs/architecture.md
scripts/run.py                 # lance une exécution (--repo ..., --jira CLE)
scripts/jira.py                # recherche/lecture de tickets JIRA (connecteur atlassian)
tests/test_jira_tools.py       # tests de l'intégration JIRA
```

## Références

- Mistral Workflows : https://docs.mistral.ai/studio/workflows/overview
- Agents SDK : https://docs.mistral.ai/studio/agents/overview
