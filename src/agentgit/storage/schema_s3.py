"""S3 冻结的任务与配置 Schema 快照。"""

import sqlalchemy as sa

from agentgit.storage.schema_s2 import build_metadata as build_s2


def build_metadata() -> sa.MetaData:
    metadata = build_s2()
    runs = metadata.tables["runs"]
    runs.append_column(sa.Column("task_id", sa.Uuid))
    runs.append_column(sa.Column("agent_config_id", sa.Uuid))
    runs.append_constraint(
        sa.UniqueConstraint("repository_id", "task_id", "id", name="uq_runs_task")
    )
    sa.Table(
        "agent_configs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("config_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("repository_id", "version"),
        sa.CheckConstraint("version > 0", name="version"),
        sa.Index("ix_agent_configs_config_ref", "config_ref"),
    )
    sa.Table(
        "tasks",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("repository_id", sa.Uuid, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("agent_config_id", sa.Uuid, nullable=False),
        sa.Column("goal_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("budget_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("policy_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("active_run_id", sa.Uuid),
        sa.UniqueConstraint("repository_id", "id"),
        sa.ForeignKeyConstraint(
            ["repository_id", "agent_config_id"],
            ["agent_configs.repository_id", "agent_configs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "id", "active_run_id"],
            ["runs.repository_id", "runs.task_id", "runs.id"],
        ),
        sa.Index("ix_tasks_config", "repository_id", "agent_config_id"),
        sa.Index("ix_tasks_active_run", "repository_id", "id", "active_run_id"),
        sa.Index("ix_tasks_goal_ref", "goal_ref"),
        sa.Index("ix_tasks_budget_ref", "budget_ref"),
        sa.Index("ix_tasks_policy_ref", "policy_ref"),
    )
    sa.Table(
        "task_nodes",
        metadata,
        sa.Column("repository_id", sa.Uuid, nullable=False),
        sa.Column("task_id", sa.Uuid, primary_key=True),
        sa.Column("plan_version", sa.Integer, primary_key=True),
        sa.Column("node_key", sa.String(100), primary_key=True),
        sa.Column("plan_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("deps_ref", sa.String(64), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("result_ref", sa.String(64), sa.ForeignKey("objects.id")),
        sa.ForeignKeyConstraint(["repository_id", "task_id"], ["tasks.repository_id", "tasks.id"]),
        sa.CheckConstraint("plan_version > 0", name="plan_version"),
        sa.CheckConstraint("status IN ('pending','completed')", name="status"),
        sa.Index("ix_task_nodes_task", "repository_id", "task_id"),
        sa.Index("ix_task_nodes_plan_ref", "plan_ref"),
        sa.Index("ix_task_nodes_deps_ref", "deps_ref"),
        sa.Index("ix_task_nodes_result_ref", "result_ref"),
    )
    return metadata
