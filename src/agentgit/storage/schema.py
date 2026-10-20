"""当前服务使用的元数据；历史迁移继续引用各自冻结的快照。"""

import sqlalchemy as sa

from agentgit.storage.schema_s4_jobs import build_metadata as build_jobs


def build_metadata() -> sa.MetaData:
    metadata = build_jobs()
    jobs = metadata.tables["jobs"]
    previous = next(c for c in jobs.constraints if c.name == "ck_jobs_status")
    jobs.constraints.remove(previous)
    jobs.append_constraint(
        sa.CheckConstraint(
            "status IN ('queued','running','paused','waiting_approval','completed',"
            "'failed','cancelled','outcome_unknown','budget_exhausted')",
            name="status",
        )
    )
    return metadata
