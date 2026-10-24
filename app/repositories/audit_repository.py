"""Repositories for append-only AuditEvents and Historical ImportBatches/Issues."""

from __future__ import annotations

import json
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.domain.enums import AuditEventType
from app.models import AuditEvent, ImportBatch, ImportIssue
from app.repositories.base import Page, paginate_statement


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        event_type: AuditEventType | str,
        entity_type: str,
        entity_id: str | int,
        summary: str,
        actor_user_id: int | None = None,
        branch_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=str(event_type.value if isinstance(event_type, AuditEventType) else event_type),
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor_user_id=actor_user_id,
            branch_id=branch_id,
            summary=summary[:300],
            details_json=json.dumps(details, default=str, sort_keys=True) if details else None,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def search(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        branch_id: int | None = None,
        page: int = 1,
        per_page: int = 30,
    ) -> Page[AuditEvent]:
        stmt = (
            select(AuditEvent)
            .options(
                joinedload(AuditEvent.actor_user),
                joinedload(AuditEvent.branch),
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        )
        if event_type and event_type.strip():
            stmt = stmt.where(AuditEvent.event_type == event_type.strip().upper())
        if entity_type and entity_type.strip():
            stmt = stmt.where(AuditEvent.entity_type == entity_type.strip().upper())
        if branch_id is not None:
            stmt = stmt.where(AuditEvent.branch_id == branch_id)
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)


class ImportBatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, batch_id: int) -> ImportBatch | None:
        stmt = (
            select(ImportBatch)
            .options(joinedload(ImportBatch.initiated_by_user))
            .where(ImportBatch.id == batch_id)
        )
        return self.session.scalar(stmt)

    def get_latest(self) -> ImportBatch | None:
        stmt = (
            select(ImportBatch)
            .options(joinedload(ImportBatch.initiated_by_user))
            .order_by(ImportBatch.id.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def list_batches(self, *, limit: int = 20) -> Sequence[ImportBatch]:
        stmt = (
            select(ImportBatch)
            .options(joinedload(ImportBatch.initiated_by_user))
            .order_by(ImportBatch.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt).all())

    def search_issues(
        self,
        *,
        batch_id: int | None = None,
        disposition: str | None = None,
        record_type: str | None = None,
        issue_code: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[ImportIssue]:
        stmt = (
            select(ImportIssue)
            .options(joinedload(ImportIssue.batch))
            .order_by(ImportIssue.id.asc())
        )
        if batch_id is not None:
            stmt = stmt.where(ImportIssue.batch_id == batch_id)
        if disposition and disposition.strip():
            stmt = stmt.where(ImportIssue.disposition == disposition.strip().upper())
        if record_type and record_type.strip():
            stmt = stmt.where(ImportIssue.record_type == record_type.strip().upper())
        if issue_code and issue_code.strip():
            stmt = stmt.where(ImportIssue.issue_code == issue_code.strip().upper())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def add_batch(self, batch: ImportBatch) -> ImportBatch:
        self.session.add(batch)
        self.session.flush()
        return batch

    def add_issue(self, issue: ImportIssue) -> ImportIssue:
        self.session.add(issue)
        self.session.flush()
        return issue
