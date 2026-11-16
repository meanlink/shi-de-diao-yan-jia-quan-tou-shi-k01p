from typing import Any

from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class LedgerState(Base):
    """Single-row monotonic ledger clock. Every mutation bumps it atomically."""
    __tablename__ = 'ledger_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Observation(Base):
    """Append-only per-point observation. Identity is the stable token, never the name."""
    __tablename__ = 'ledger_observations'
    __table_args__ = (UniqueConstraint('token', 'day', 'team', 'zone', 'passed',
                                       name='uq_observation_point_day_team_conclusion'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, index=True, default=0)
    token: Mapped[str] = mapped_column(String(220), index=True)
    point_key: Mapped[str] = mapped_column(String(120), default='')
    point_name: Mapped[str] = mapped_column(String(160), default='')
    day: Mapped[str] = mapped_column(String(20), index=True)
    team: Mapped[str] = mapped_column(String(40))
    zone: Mapped[str] = mapped_column(String(80))
    passed: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str] = mapped_column(String(500), default='')
    source: Mapped[str] = mapped_column(String(200), default='')
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)


class PointMerge(Base):
    """Append-only merge/alias event. Revocation is a marker, later merges still replay."""
    __tablename__ = 'ledger_merges'
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, index=True, default=0)
    source_token: Mapped[str] = mapped_column(String(220))
    target_token: Mapped[str] = mapped_column(String(220))
    reason: Mapped[str] = mapped_column(String(500), default='')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    revoked_by: Mapped[str] = mapped_column(String(150), default='')
    revoked_reason: Mapped[str] = mapped_column(String(500), default='')
    revoked_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    revoked_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Adjudication(Base):
    """A conflict that must be ruled on before a daily-table batch may publish."""
    __tablename__ = 'ledger_adjudications'
    __table_args__ = (UniqueConstraint('signature', name='uq_adjudication_signature'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # conclusion | zone
    day: Mapped[str] = mapped_column(String(20))
    team: Mapped[str] = mapped_column(String(40))
    entry_ids: Mapped[list] = mapped_column(JSON, default=list)
    options: Mapped[list] = mapped_column(JSON, default=list)
    cluster: Mapped[str] = mapped_column(String(220), default='')
    status: Mapped[str] = mapped_column(String(20), default='pending')  # pending | resolved | superseded
    decision: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    decided_entry_ids: Mapped[list] = mapped_column(JSON, default=list)
    resolution_note: Mapped[str] = mapped_column(String(500), default='')
    decided_by: Mapped[str] = mapped_column(String(150), default='')
    decided_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    decided_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reopened: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Double)


class LedgerBatch(Base):
    """Pinned daily-table snapshot. Version fixes observations and merges for replay."""
    __tablename__ = 'ledger_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='pending_review')  # pending_review | published
    frames: Mapped[dict] = mapped_column(JSON, default=dict)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    identity_map: Mapped[dict] = mapped_column(JSON, default=dict)
    merges: Mapped[list] = mapped_column(JSON, default=list)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    adjudication_ids: Mapped[list] = mapped_column(JSON, default=list)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    result_stamp: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    computed_by: Mapped[str] = mapped_column(String(150), default='')
    checksum: Mapped[str] = mapped_column(String(64), default='')
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    correction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_corrected_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    published_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    published_dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'), nullable=True)


class IdentityCorrection(Base):
    """Append-only identity fix. Replayed by version so old snapshots keep their meaning."""
    __tablename__ = 'ledger_identity_corrections'
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey('ledger_observations.id'))
    old_token: Mapped[str] = mapped_column(String(220))
    new_token: Mapped[str] = mapped_column(String(220))
    old_name: Mapped[str] = mapped_column(String(160), default='')
    new_name: Mapped[str] = mapped_column(String(160), default='')
    reason: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)


class LedgerFrame(Base):
    __tablename__ = 'ledger_frames'
    zone: Mapped[str] = mapped_column(String(80), primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=0)


class LedgerFrameEvent(Base):
    """Append-only frame-size history so a pinned version replays its own weights."""
    __tablename__ = 'ledger_frame_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    zone: Mapped[str] = mapped_column(String(80), index=True)
    size: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)


class PublishAttempt(Base):
    """Idempotent publish attempt. Retrying resumes this row, never a second batch."""
    __tablename__ = 'ledger_publish_attempts'
    __table_args__ = (UniqueConstraint('idempotency_key', name='uq_publish_idempotency'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('ledger_batches.id'))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default='started')  # started | completed | failed
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'), nullable=True)
    error: Mapped[str] = mapped_column(String(1000), default='')
    started_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class LedgerDatasetFlag(Base):
    """Publication validity of a dataset produced by a ledger batch."""
    __tablename__ = 'ledger_dataset_flags'
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('ledger_batches.id'))
    version: Mapped[int] = mapped_column(Integer, default=0)
    invalidated: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(500), default='')
    invalidated_at: Mapped[float | None] = mapped_column(Double, nullable=True)
