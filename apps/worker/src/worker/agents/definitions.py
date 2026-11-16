"""Définitions des agents du pipeline ( Agents SDK — exécutés dans des activités ).

Chaque agent est décrit ici (modèle, instructions, tools) puis reconstruit
dans l'activité qui l'exécute : un objet Agent ne traverse pas Temporal,
seuls son nom et ses paramètres passent. Voir :
https://docs.mistral.ai/studio/agents/overview
"""

from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------- modèles ---
# Routing : gros modèle pour le raisonnement (spec/planner/review),
# modèle plus rapide/économique pour les boucles dev/tests.
SPEC_MODEL = "mistral-large-latest"
DEV_MODEL = "mistral-medium-latest"

MAX_DEV_ITERATIONS = 5          # borne de la boucle dev -> tests
MAX_AGENT_TURNS = 20            # borne de la boucle d'outils d'un agent
MAX_AGENT_SECONDS = 15 * 60     # borne wall-clock d'une boucle d'agent (tools lents)
MAX_REPEATED_TOOL_CALLS = 3     # même appel tool répété -> réponse finale forcée
MAX_SPEC_ROUNDS = 6             # borne de l'interview interactif avec l'agent spec
MAX_PLANNER_ATTEMPTS = 3        # tentatives du planner (prompt strict si réponse vide)
MAX_API_ATTEMPTS = 3            # retries d'un appel API sur erreur transitoire (429/5xx)


@dataclass
class AgentSpec:
    name: str
    model: str
    instructions: str
    tools: list[Callable[..., Any]] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)  # noms d'autres agents


# ------------------------------------------------------------------ SPEC ----
# L'agent Spec travaille dans le repo cible (tools confinés à sa racine).
# Son livrable est un simple fichier SPEC.md, validé par le développeur.
SPEC_INSTRUCTIONS = """\
You are the specification agent of an autonomous coding pipeline.
You work inside the target repository with file/shell tools confined to it.
You never write implementation code.

WORKFLOW:
1. Read the repo's AGENTS.md / README and the relevant source files to ground
   the request in the actual code.
2. INTERVIEW THE DEVELOPER to validate the change: whenever you need a
   decision or a clarification, end your reply with exactly one line:
       QUESTION: <your question>
   The pipeline pauses, the developer answers, and calls you again with the
   full conversation appended. Ask a few targeted questions (what matters,
   what is out of scope, expected behavior), then proceed with the best
   reasonable assumption. The developer validates the proposed changes with
   you here, during this interview.
3. Once the change is clear and validated, write the spec to `SPEC.md` at the
   repository root (overwrite it) — one markdown file with sections:
   Goal / Context / Proposed changes (with file paths) / Acceptance criteria
   (one testable WHEN/THEN scenario per requirement — no scenario, no
   requirement) / Risks / Out of scope.
4. End your final reply with exactly one line:
       CHANGE_ID: <kebab-case-slug>
   The developer then reviews and approves SPEC.md at the approval gate before
   planning starts.

Budget your tool calls: the agent loop is hard-bounded (20 turns, ~15 min).
After about 10 turns you MUST stop exploring, write SPEC.md, and end with
CHANGE_ID: <slug> even if imperfect — the approval gate reviews it.
"""

# --------------------------------------------------------------- PLANNER ----
PLANNER_INSTRUCTIONS = """\
You are the planning agent. You receive the validated spec: `SPEC.md` at the
repository root (written by the spec agent and approved by the developer).
Decompose it into a list of unit tasks for developer agents.

For each task emit JSON:
  { "id": "...", "title": "...",
    "capability": "...",
    "files_hint": ["..."],
    "depends_on": ["task-id", ...],
    "acceptance": "criterion verifiable by the test suite" }

Rules:
- Tasks must be independently implementable in isolated git worktrees.
- Define shared interfaces (types, API signatures) in the FIRST task so
  parallel tasks code against a stable contract.
- Tasks with no interdependency run in parallel; the rest, sequentially.
- Work efficiently: read each file at most once and emit the JSON as soon as
  you have enough context — the agent loop is bounded (20 turns).
"""

# -------------------------------------------------------------------- DEV ---
DEV_INSTRUCTIONS = """\
You are a developer agent implementing EXACTLY ONE bounded task inside an
isolated git worktree. You have shell and file tools.

Rules:
- Implement only your task. Never refactor unrelated code.
- Follow the repo's AGENTS.md conventions and the validated `SPEC.md`.
- Run the linter/formatter on every file you touch.
- Commit with a message referencing the task id: "task <id>: <title>".
- Do NOT merge your branch. Do NOT modify CI configuration.
- When your acceptance criterion is met, return a summary of the diff.
"""

# ----------------------------------------------------------------- TESTER ---
TESTER_INSTRUCTIONS = """\
You are the test agent. Given `SPEC.md` (validated spec) and a branch diff,
write or update the test suite that verifies its acceptance criteria.

Rules:
- One test per WHEN/THEN acceptance criterion in SPEC.md. Name tests after
  the criterion so a human can trace spec -> test.
- Tests must fail without the implementation and pass with it.
- Run the full suite before returning; report failures with the exact log.
"""

# ---------------------------------------------------------------- REVIEWER --
REVIEWER_INSTRUCTIONS = """\
You are the code review agent. You receive the aggregate diff and the
validated proposal. Verify:
1. Every tasks.md checkbox is implemented.
2. Every spec scenario has a passing test.
3. No out-of-scope change (diff hunks outside the task's files_hint are a
   finding). 4. No secret, no debug code, no weakened test.
Return a JSON verdict: { "approve": bool, "findings": ["..."] }.
"""


def spec_agent(tools: list) -> AgentSpec:
    return AgentSpec("spec-agent", SPEC_MODEL, SPEC_INSTRUCTIONS, tools=tools)


def planner_agent(tools: list) -> AgentSpec:
    return AgentSpec("planner-agent", SPEC_MODEL, PLANNER_INSTRUCTIONS, tools=tools)


def dev_agent(tools: list) -> AgentSpec:
    return AgentSpec("dev-agent", DEV_MODEL, DEV_INSTRUCTIONS, tools=tools)


def tester_agent(tools: list) -> AgentSpec:
    return AgentSpec("tester-agent", DEV_MODEL, TESTER_INSTRUCTIONS, tools=tools)


def reviewer_agent(tools: list) -> AgentSpec:
    return AgentSpec("reviewer-agent", SPEC_MODEL, REVIEWER_INSTRUCTIONS, tools=tools)


# Registre nom -> factory : le workflow ne passe que le NOM de l'agent à
# l'activity (un objet Agent ne traverse pas Temporal), qui le reconstruit ici.
AGENT_FACTORIES = {
    "spec": spec_agent,
    "planner": planner_agent,
    "dev": dev_agent,
    "tester": tester_agent,
    "reviewer": reviewer_agent,
}
