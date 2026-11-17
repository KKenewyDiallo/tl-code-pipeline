"""Boucle d'exécution d'un agent : appels modèle + tools, bornée en tours.

Squelette volontairement explicite (sans le plugin durable déprécié) :
- l'agent est reconstruit dans l'activité (rien ne traverse Temporal),
- la boucle s'arrête à `max_turns`, réponse finale sinon,
- chaque tool call est tracé (la télémétrie Mistral ajoute le tracing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from mistralai.client import Mistral

from worker.agents.definitions import (
    AgentSpec,
    MAX_AGENT_SECONDS,
    MAX_AGENT_TURNS,
    MAX_API_ATTEMPTS,
    MAX_REPEATED_TOOL_CALLS,
)

logger = logging.getLogger(__name__)


def _is_transient_api_error(exc: BaseException) -> bool:
    """Erreur API transitoire (429 / 5xx Cloudflare, timeout, connexion) :
    on retente l'appel au lieu de tuer l'activité."""
    status = getattr(getattr(exc, "raw_response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError))


async def _complete_with_retry(client: Mistral, model: str,
                               messages: list, tools: Any) -> Any:
    """Appel chat.complete avec retries bornés sur erreur transitoire."""
    delay = 5.0
    for attempt in range(MAX_API_ATTEMPTS):
        try:
            return client.chat.complete(model=model, messages=messages, tools=tools)
        except Exception as exc:
            if attempt == MAX_API_ATTEMPTS - 1 or not _is_transient_api_error(exc):
                raise
            logger.warning("appel API transitoire, retry %d/%d dans %.0fs: %r",
                           attempt + 1, MAX_API_ATTEMPTS, delay, exc)
            await asyncio.sleep(delay)
            delay *= 3
    raise AssertionError("unreachable")


def _content_text(content: Any) -> str:
    """Contenu d'un message Mistral en texte brut : `str` ou liste de blocs
    (`thinking`, `text`, ...). Les blocs sont des dicts (JSON brut) OU des
    objets pydantic du SDK (TextChunk...) : on lit type/text en attributs.
    Seuls les blocs `text` sont gardés."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype, btext = block.get("type"), block.get("text", "")
            else:
                btype = getattr(block, "type", None)
                btext = getattr(block, "text", "")
            if btype == "text" and btext:
                parts.append(btext if isinstance(btext, str) else str(btext))
        return "\n".join(parts)
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
    for turn in range(1, MAX_AGENT_TURNS + 1):
        if stop or time.monotonic() > deadline:
            break
        response = await _complete_with_retry(client, agent.model, messages, tools_schema)
        choice = response.choices[0]
        messages.append(choice.message.model_dump())
        text = _content_text(choice.message.content)
        if text:
            last_answer = text
        logger.info("agent %s tour %d: tool_calls=%d texte=%d chars",
                    agent.name, turn, len(choice.message.tool_calls or []), len(text))

        if not choice.message.tool_calls:
            if text:
                return text
            logger.warning("agent %s tour %d: message vide sans tool call -> relance texte",
                           agent.name, turn)
            messages.append({"role": "user", "content": (
                "Your last message had no text and no tool call. "
                "You MUST answer with text now (a short answer is fine).")})
            continue

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
    response = await _complete_with_retry(
        client,
        agent.model,
        messages + [{
            "role": "user",
            "content": ("You have reached the maximum number of tool calls. "
                        "Do not call any tool: answer NOW with your final "
                        "answer only, based on what you have already read."),
        }],
        None,
    )
    final = _content_text(response.choices[0].message.content)
    if final:
        logger.info("agent %s: réponse finale forcée, %d chars", agent.name, len(final))
        return final
    if last_answer:
        logger.warning("agent %s: réponse finale forcée vide, fallback dernier texte (%d chars)",
                       agent.name, len(last_answer))
        return last_answer
    logger.warning("agent %s: AUCUN texte produit pendant toute la boucle", agent.name)
    return f"[agent {agent.name}: aucun texte produit après {MAX_AGENT_TURNS} tours d'outils]"
