"""MCP 只扩展现有 jobs 的类型集合，不另建队列或业务表。"""

import sqlalchemy as sa

from agentgit.storage.schema_s11_runs import build_metadata as build_previous


def build_metadata() -> sa.MetaData:
    metadata = build_previous()
    jobs = metadata.tables["jobs"]
    previous = next(c for c in jobs.constraints if c.name == "ck_jobs_kind")
    jobs.constraints.remove(previous)
    jobs.append_constraint(sa.CheckConstraint("kind IN ('research_run','mcp_run')", name="kind"))
    return metadata
