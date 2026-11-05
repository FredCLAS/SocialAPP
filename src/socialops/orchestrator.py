"""Runs agents in a fixed order. Stops at the first agent that is held waiting for a human."""

from __future__ import annotations

from .agents import Agent, AgentResult, Context, run_logged


def run_agents(agents: list[Agent], ctx: Context) -> list[tuple[str, AgentResult]]:
    results: list[tuple[str, AgentResult]] = []
    for agent in agents:
        result = run_logged(agent, ctx)
        results.append((agent.name, result))
        if result.status == "held":
            break
    return results
