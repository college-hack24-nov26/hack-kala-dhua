"""The administration console: incidents, resources, allocation, decisions,
alerts, field tasks and the agent trace. Staff only, enforced here and again
by row level security in the database."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query

from app.core.errors import Conflict, NotFound, OutsideDelegation
from app.core.security import CurrentPrincipal, StaffPrincipal
from app.db import session as db
from app.db.repositories import queries as q
from app.schemas.domain import (
    Alert,
    AllocationPlan,
    Decision,
    DecisionAction,
    DecisionStatus,
    FieldTask,
    FieldTaskUpdate,
    Incident,
    Resource,
)

router = APIRouter(tags=["operations"])


@router.get("/incidents", response_model=list[Incident])
async def incidents(_: CurrentPrincipal) -> list[Incident]:
    return await q.list_incidents()


@router.get("/resources", response_model=list[Resource])
async def resources(_: StaffPrincipal) -> list[Resource]:
    return [Resource(**r) for r in await q.list_resources()]


@router.get("/allocation/latest", response_model=AllocationPlan | None)
async def allocation(_: StaffPrincipal) -> AllocationPlan | None:
    return await q.latest_plan()


@router.get("/decisions", response_model=list[Decision])
async def decisions(_: StaffPrincipal) -> list[Decision]:
    return await q.list_decisions()


@router.post("/decisions/{decision_id}", response_model=Decision)
async def act_on_decision(
    decision_id: str,
    body: DecisionAction,
    principal: StaffPrincipal,
) -> Decision:
    """Approve, reject or override a pending decision.

    The delegation check is not a formality. A ward officer may act on what
    their clause delegates; anything beyond it needs a commissioner, and the
    error names the clause that blocked them.
    """
    row = await db.fetchrow(
        "select status, authority, ward_id, action from decisions where id = $1",
        decision_id,
    )
    if row is None:
        raise NotFound("No such decision.")
    if row["status"] not in (
        DecisionStatus.AWAITING_APPROVAL.value,
        DecisionStatus.AUTO_ISSUED.value,
    ):
        raise Conflict("That decision has already been acted on.")

    authority = row["authority"] or {}
    within = bool(authority.get("within_delegation"))

    if not within and not principal.can_escalate:
        raise OutsideDelegation(
            f"{authority.get('clause', 'The governing clause')} reserves this to the "
            f"{authority.get('delegated_to', 'Municipal Commissioner')}.",
            clause=authority.get("clause"),
            delegated_to=authority.get("delegated_to"),
        )
    if within and row["ward_id"] and principal.ward_id and row["ward_id"] != principal.ward_id:
        if not principal.can_escalate:
            raise OutsideDelegation(
                "Your delegation covers a different ward.",
                clause=authority.get("clause"),
            )

    status = {
        "approve": DecisionStatus.APPROVED,
        "reject": DecisionStatus.REJECTED,
        "override": DecisionStatus.OVERRIDDEN,
    }[body.action]

    await db.execute(
        """
        update decisions
           set status = $2, decided_by = $3, decided_at = $4, override_note = $5
         where id = $1
        """,
        decision_id,
        status.value,
        principal.full_name or principal.user_id or "Unknown officer",
        datetime.now(UTC),
        body.note,
    )

    for decision in await q.list_decisions():
        if decision.id == decision_id:
            return decision
    raise NotFound("Decision disappeared while being updated.")


@router.get("/alerts", response_model=list[Alert])
async def alerts(ward_id: str | None = Query(default=None)) -> list[Alert]:
    return await q.list_alerts(ward_id)


@router.get("/tasks", response_model=list[FieldTask])
async def tasks(
    principal: CurrentPrincipal,
    operator: str | None = Query(default=None),
) -> list[FieldTask]:
    # A field operator only ever sees their own agency's work.
    scope = operator if principal.is_staff else principal.operator
    return await q.list_field_tasks(scope)


@router.patch("/tasks/{task_id}", response_model=FieldTask)
async def update_task(
    task_id: str,
    body: FieldTaskUpdate,
    principal: CurrentPrincipal,
) -> FieldTask:
    row = await db.fetchrow("select operator, status from field_tasks where id = $1", task_id)
    if row is None:
        raise NotFound("No such task.")
    if not principal.is_staff and row["operator"] != principal.operator:
        raise OutsideDelegation("That task belongs to another operator.")
    if body.status == "complete" and not (body.proof_note or "").strip():
        raise Conflict("A task is closed with proof of what was done, not just a status.")

    now = datetime.now(UTC)
    await db.execute(
        """
        update field_tasks
           set status = $2,
               proof_note = coalesce($3, proof_note),
               accepted_at = case when accepted_at is null and $2 <> 'queued'
                                  then $4 else accepted_at end,
               completed_at = case when $2 = 'complete' then $4 else completed_at end
         where id = $1
        """,
        task_id,
        body.status.value,
        body.proof_note,
        now,
    )
    for task in await q.list_field_tasks():
        if task.id == task_id:
            return task
    raise NotFound("Task disappeared while being updated.")
