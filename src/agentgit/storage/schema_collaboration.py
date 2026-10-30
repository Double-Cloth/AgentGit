"""协作组和四角色任务索引；执行仍复用原 Run/Job。"""

import sqlalchemy as sa

from agentgit.storage.schema_langgraph_runs import build_metadata as build_previous

TABLE_ORDER = ("collaborations", "collaboration_tasks")


def build_metadata() -> sa.MetaData:
    metadata = build_previous()
    jobs = metadata.tables["jobs"]
    jobs.constraints.remove(next(c for c in jobs.constraints if c.name == "ck_jobs_kind"))
    jobs.append_constraint(
        sa.CheckConstraint(
            "kind IN ('research_run','mcp_run','langgraph_run','collaboration_role')", name="kind"
        )
    )
    sa.Table(
        "collaborations",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("program_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("bindings_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("environment_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="prepared"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.CheckConstraint(
            "status IN ('prepared','running','paused','completed','failed','cancelled',"
            "'budget_exhausted','outcome_unknown')",
            name="status",
        ),
        sa.Index("ix_collaborations_program", "program_ref"),
        sa.Index("ix_collaborations_bindings", "bindings_ref"),
        sa.Index("ix_collaborations_environment", "environment_ref"),
    )
    sa.Table(
        "collaboration_tasks",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("collaboration_id", sa.Uuid, nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("policy_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("budget_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("private_input_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("repository_id", "collaboration_id", "role"),
        sa.ForeignKeyConstraint(
            ["repository_id", "collaboration_id"],
            ["collaborations.repository_id", "collaborations.id"],
        ),
        sa.ForeignKeyConstraint(["repository_id", "run_id"], ["runs.repository_id", "runs.id"]),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.CheckConstraint("role IN ('supervisor','researcher','verifier','writer')", name="role"),
        sa.Index("ix_collaboration_tasks_policy", "policy_ref"),
        sa.Index("ix_collaboration_tasks_budget", "budget_ref"),
        sa.Index("ix_collaboration_tasks_private", "private_input_ref"),
    )
    return metadata
