"""A2A 只扩展既有 Job 类型，不增加业务表。"""

import sqlalchemy as sa

from agentgit.storage.schema_collaboration_recorded_steps import build_metadata as build_previous


def build_metadata() -> sa.MetaData:
    metadata = build_previous()
    jobs = metadata.tables["jobs"]
    jobs.constraints.remove(next(c for c in jobs.constraints if c.name == "ck_jobs_kind"))
    jobs.append_constraint(
        sa.CheckConstraint(
            "kind IN ('research_run','mcp_run','langgraph_run','collaboration_role','a2a_run')",
            name="kind",
        )
    )
    return metadata
