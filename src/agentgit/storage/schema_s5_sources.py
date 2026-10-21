"""S5 恢复来源查找索引；先前迁移继续使用冻结的元数据。"""

import sqlalchemy as sa

from agentgit.storage.schema_s5_replays import build_metadata as build_replays


def build_metadata() -> sa.MetaData:
    metadata = build_replays()
    jobs = metadata.tables["jobs"]
    sa.Index("ix_jobs_repository_origin", jobs.c.repository_id, jobs.c.origin_run_id)
    return metadata
