"""Boucle d'exécution d'un agent : appels modèle + tools, bornée en tours.

Squelette volontairement explicite (sans le plugin durable déprécié) :
- l'agent est reconstruit dans l'activité (rien ne traverse Temporal),
- la boucle s'arrête à `max_turns`, réponse finale sinon,
- chaque tool call est tracé (la télémétrie Mistral ajoute le tracing).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from mistralai.client import Mistral

from worker.agents.definitions import (
    AgentSpec,
    MAX_AGENT_SECONDS,
    MAX_AGENT_TURNS,
    MAX_REPEATED_TOOL_CALLS,
)


def _content_text(content: Any) -> str:
    """Contenu d'un message Mistral en texte brut : `str` ou liste de blocs
    (`thinking`, `text`, ...). Seuls les blocs `text` sont gardés."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content) if content is not None else ""


async def run_agent(agent: AgentSpec, user_message: str, tool_impls: dict[str, Callable]) -> str:
    """Exécute la boucle modèle -> tools -> modèle jusqu'à la réponse finale."""
    client = Mistral()  # lit MISTRAL_API_KEY

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent.instructions},
        {"role": "user", "content": user_message},
    ]
    tools_schema = [
        {"type": "function", "function": {"name": name, **spec}}
        for name, (spec, _) in tool_impls.items()
    ] if tool_impls else None

    last_answer = ""
    deadline = time.monotonic() + MAX_AGENT_SECONDS
    call_counts: dict[tuple[str, str], int] = {}
    stop = False
    for _ in range(MAX_AGENT_TURNS):
        if stop or time.monotonic() > deadline:
            break
        response = client.chat.complete(
            model=agent.model,
            messages=messages,
            tools=tools_schema,
        )
        choice = response.choices[0]
        messages.append(choice.message.model_dump())
        text = _content_text(choice.message.content)
        if text:
            last_answer = text

        if not choice.message.tool_calls:
            return text

        for call in choice.message.tool_calls:
            fn = call.function
            key = (fn.name, fn.arguments or "{}")
            call_counts[key] = call_counts.get(key, 0) + 1
            if call_counts[key] >= MAX_REPEATED_TOOL_CALLS:
                stop = True
                break
            entry = tool_impls.get(fn.name)
            if entry is None:
                result = {"error": f"unknown tool {fn.name!r}; available: {sorted(tool_impls)}"}
            else:
                try:
                    result = await entry[1](**json.loads(fn.arguments or "{}"))
                except Exception as exc:  # l'agent voit l'erreur et corrige
                    result = {"error": str(exc)}
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, default=str),
            })

    # MAX_AGENT_TURNS atteint : un dernier appel SANS tools force le modèle à
    # répondre en texte avec ce qu'il a déjà lu (la boucle reste bornée).
    response = client.chat.complete(
        model=agent.model,
        messages=messages + [{
            "role": "user",
            "content": ("You have reached the maximum number of tool calls. "
                        "Do not call any tool: answer NOW with your final "
                        "answer only, based on what you have already read."),
        }],
        tools=None,
    )
    return _content_text(response.choices[0].message.content) or last_answer
