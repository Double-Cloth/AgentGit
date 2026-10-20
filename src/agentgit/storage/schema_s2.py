"""S2 冻结的 Schema 构造器；后续阶段通过新迁移扩展，不修改本快照。"""

import sqlalchemy as sa


def build_metadata() -> sa.MetaData:
    metadata = sa.MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    sa.Table(
        "repositories",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    sa.Table(
        "objects",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("codec", sa.String(30), nullable=False),
        sa.Column("object_type", sa.String(30), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("byte_length", sa.BigInteger, nullable=False),
        sa.Column("storage_key", sa.String(65), nullable=False),
        sa.CheckConstraint("id ~ '^[0-9a-f]{64}$'", name="hash"),
        sa.CheckConstraint("byte_length >= 0", name="length"),
        sa.CheckConstraint("codec = 'AGJSON-v1'", name="codec"),
        sa.CheckConstraint(
            "storage_key = substring(id, 1, 2) || '/' || substring(id, 3)", name="key"
        ),
    )
    sa.Table(
        "object_refs",
        metadata,
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), primary_key=True),
        sa.Column("object_id", sa.String(64), sa.ForeignKey("objects.id"), primary_key=True),
        sa.Column("owner_type", sa.String(30), primary_key=True),
        sa.Column("owner_id", sa.String(100), primary_key=True),
        sa.Column("role", sa.String(100), primary_key=True),
        sa.Index("ix_object_refs_object_id", "object_id"),
    )
    sa.Table(
        "commits",
        metadata,
        sa.Column("id", sa.String(64), sa.ForeignKey("objects.id"), primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("state_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("environment_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("event_manifest_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("repository_id", "id", "state_ref"),
        sa.Index("ix_commits_state_ref", "state_ref"),
        sa.Index("ix_commits_environment_ref", "environment_ref"),
        sa.Index("ix_commits_event_manifest_ref", "event_manifest_ref"),
    )
    sa.Table(
        "commit_parents",
        metadata,
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("commit_id", sa.String(64), primary_key=True),
        sa.Column("parent_order", sa.Integer, primary_key=True),
        sa.Column("parent_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_commit_parents_child",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "parent_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_commit_parents_parent",
        ),
        sa.CheckConstraint("parent_order = 0", name="single_parent"),
        sa.CheckConstraint("commit_id <> parent_id", name="no_self_parent"),
        sa.Index("ix_commit_parents_parent", "repository_id", "parent_id"),
        sa.Index("ix_commit_parents_child", "repository_id", "commit_id"),
    )
    sa.Table(
        "branches",
        metadata,
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), primary_key=True),
        sa.Column("name", sa.String(200), primary_key=True),
        sa.Column("head_commit_id", sa.String(64)),
        sa.Column("version", sa.BigInteger, nullable=False, server_default="0"),
        sa.CheckConstraint("version >= 0", name="version"),
        sa.ForeignKeyConstraint(
            ["repository_id", "head_commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.Index("ix_branches_head", "repository_id", "head_commit_id"),
    )
    sa.Table(
        "runs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("branch_name", sa.String(200), nullable=False),
        sa.Column("base_commit_id", sa.String(64)),
        sa.Column("checkpoint_commit_id", sa.String(64)),
        sa.Column("origin_run_id", sa.Uuid),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("last_event_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("checkpoint_event_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "branch_name"], ["branches.repository_id", "branches.name"]
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "base_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_runs_base",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "checkpoint_commit_id"],
            ["commits.repository_id", "commits.id"],
            name="fk_runs_checkpoint",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "origin_run_id"], ["runs.repository_id", "runs.id"]
        ),
        sa.CheckConstraint("last_event_seq >= 0", name="event_seq"),
        sa.CheckConstraint(
            "checkpoint_event_seq >= 0 AND checkpoint_event_seq <= last_event_seq",
            name="checkpoint_seq",
        ),
        sa.CheckConstraint("mode IN ('live', 'recorded', 'hybrid')", name="mode"),
        sa.CheckConstraint(
            "status IN ('queued','planning','running','waiting_approval','paused','interrupted',"
            "'outcome_unknown','completed','failed','cancelled','budget_exhausted')",
            name="status",
        ),
        sa.Index("ix_runs_repository_time", "repository_id", "created_at", "id"),
        sa.Index("ix_runs_branch", "repository_id", "branch_name"),
        sa.Index("ix_runs_checkpoint", "repository_id", "checkpoint_commit_id"),
        sa.Index("ix_runs_base", "repository_id", "base_commit_id"),
        sa.Index("ix_runs_origin", "repository_id", "origin_run_id"),
    )
    sa.Table(
        "events",
        metadata,
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("logical_step_id", sa.String(200), nullable=False),
        sa.Column("payload_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("caused_by_run_id", sa.Uuid),
        sa.Column("caused_by_seq", sa.BigInteger),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "seq"),
        sa.UniqueConstraint("repository_id", "run_id", "seq"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "caused_by_run_id", "caused_by_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
        ),
        sa.CheckConstraint("seq > 0", name="positive_seq"),
        sa.CheckConstraint(
            "(caused_by_run_id IS NULL) = (caused_by_seq IS NULL)", name="cause_pair"
        ),
        sa.CheckConstraint(
            "caused_by_run_id IS NULL OR caused_by_run_id <> run_id OR caused_by_seq < seq",
            name="cause_past",
        ),
        sa.Index("ix_events_payload_ref", "payload_ref"),
        sa.Index("ix_events_cause", "repository_id", "caused_by_run_id", "caused_by_seq"),
    )
    sa.Table(
        "run_commits",
        metadata,
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, primary_key=True),
        sa.Column("run_order", sa.BigInteger, primary_key=True),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("run_id", "commit_id"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.CheckConstraint("run_order > 0", name="positive_order"),
        sa.Index("ix_run_commits_commit", "repository_id", "commit_id"),
        sa.Index("ix_run_commits_run", "repository_id", "run_id"),
    )
    sa.Table(
        "snapshots",
        metadata,
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, primary_key=True),
        sa.Column("commit_id", sa.String(64), primary_key=True),
        sa.Column("event_seq", sa.BigInteger, nullable=False),
        sa.Column("state_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id", "state_ref"],
            ["commits.repository_id", "commits.id", "commits.state_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "run_id", "event_seq"],
            ["events.repository_id", "events.run_id", "events.seq"],
        ),
        sa.CheckConstraint("schema_version = 1", name="schema"),
        sa.Index("ix_snapshots_commit", "repository_id", "commit_id", "state_ref"),
        sa.Index("ix_snapshots_run_event", "repository_id", "run_id", "event_seq"),
    )
    sa.Table(
        "external_calls",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("logical_step_id", sa.String(200), nullable=False),
        sa.Column("call_index", sa.BigInteger, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(200), nullable=False),
        sa.Column("signature_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("effect", sa.String(30), nullable=False),
        sa.Column("intent_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("result_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.Column("origin_call_id", sa.Uuid),
        sa.Column("status", sa.String(30), nullable=False),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("run_id", "logical_step_id", "call_index", "attempt"),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "origin_call_id"],
            ["external_calls.repository_id", "external_calls.id"],
        ),
        sa.CheckConstraint("attempt > 0 AND call_index >= 0", name="occurrence"),
        sa.CheckConstraint(
            "status IN ('proposed','started','succeeded','failed','outcome_unknown')", name="status"
        ),
        sa.CheckConstraint(
            "effect IN ('READ_ONLY','IDEMPOTENT_WRITE','NON_IDEMPOTENT_WRITE','IRREVERSIBLE')",
            name="effect",
        ),
        sa.Index("ix_external_calls_intent_ref", "intent_ref"),
        sa.Index("ix_external_calls_result_ref", "result_ref"),
        sa.Index("ix_external_calls_origin", "repository_id", "origin_call_id"),
        sa.Index("ix_external_calls_run", "repository_id", "run_id"),
    )
    sa.Table(
        "artifacts",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.Column("relative_path", sa.String(240), nullable=False),
        sa.Column("object_id", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("provenance_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.UniqueConstraint("commit_id", "relative_path"),
        sa.ForeignKeyConstraint(
            ["repository_id", "commit_id"], ["commits.repository_id", "commits.id"]
        ),
        sa.Index("ix_artifacts_commit", "repository_id", "commit_id"),
        sa.Index("ix_artifacts_object_id", "object_id"),
        sa.Index("ix_artifacts_provenance_ref", "provenance_ref"),
    )
    return metadata
