"""The policy gate.

An action does not issue because a model is confident. It issues because a
clause in the municipal disaster management plan or the NDMA guidelines
delegates it, and because the officer holding that delegation is the one
acting. This module is where that is decided.

Retrieval is exact-match on `action_key` today, with the pgvector column and a
similarity path already in place for when the corpus grows past a few dozen
clauses. Exact match is the right default for a delegation matrix: you do not
want fuzzy semantic similarity deciding whether an evacuation was authorised.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.db import session as db
from app.schemas.domain import Authority, DecisionStatus

log = get_logger(__name__)

#: Below this the action goes to a human regardless of what the clause allows.
CONFIDENCE_FLOOR = 0.70


@dataclass(frozen=True, slots=True)
class Clause:
    id: str
    clause: str
    source: str
    delegated_to: str
    body: str
    max_severity_without_escalation: int


_UNMATCHED = Authority(
    clause="No matching clause",
    source="—",
    delegated_to="Municipal Commissioner",
    within_delegation=False,
)


async def find_clause(action_key: str) -> Clause | None:
    row = await db.fetchrow(
        """
        select id, clause, source, delegated_to, body,
               max_severity_without_escalation
        from policy_clauses
        where $1 = any(authorises)
        order by max_severity_without_escalation desc
        limit 1
        """,
        action_key,
    )
    if row is None:
        return None
    return Clause(
        id=row["id"],
        clause=row["clause"],
        source=row["source"],
        delegated_to=row["delegated_to"],
        body=row["body"],
        max_severity_without_escalation=row["max_severity_without_escalation"],
    )


async def authority_for(action_key: str, severity: int) -> Authority:
    """Resolve the clause that governs an action at a given severity."""
    clause = await find_clause(action_key)
    if clause is None:
        log.warning("policy_clause_missing", action_key=action_key)
        return _UNMATCHED
    return Authority(
        clause=clause.clause,
        source=clause.source,
        delegated_to=clause.delegated_to,
        within_delegation=severity <= clause.max_severity_without_escalation,
    )


def gate(authority: Authority, confidence: float) -> DecisionStatus:
    """Two conditions, both required, to issue without a human.

    Authority is the hard one: no amount of model confidence promotes an action
    the delegation matrix reserves to the Commissioner.
    """
    from app.ops import autonomy

    # Emergency stop: nothing issues on its own, whatever the delegation.
    if autonomy.paused():
        return DecisionStatus.AWAITING_APPROVAL
    if authority.within_delegation and confidence >= CONFIDENCE_FLOOR:
        return DecisionStatus.AUTO_ISSUED
    return DecisionStatus.AWAITING_APPROVAL


def gate_reason(authority: Authority, confidence: float) -> str:
    """Plain-language explanation, shown next to the decision in the console."""
    from app.ops import autonomy

    if autonomy.paused():
        return (f"Automation is paused ({autonomy.state.reason or 'emergency stop'}, by "
                f"{autonomy.state.by or 'an officer'}), so every decision waits for a person.")
    if not authority.within_delegation:
        return (
            f"{authority.clause} reserves this to the {authority.delegated_to}, "
            "so it cannot issue automatically."
        )
    if confidence < CONFIDENCE_FLOOR:
        return (
            f"Within {authority.delegated_to} delegation under {authority.clause}, "
            f"but model confidence is {confidence:.0%}, below the {CONFIDENCE_FLOOR:.0%} "
            "floor for automatic issue."
        )
    return f"Auto-issued under {authority.clause} ({authority.delegated_to} delegation)."
