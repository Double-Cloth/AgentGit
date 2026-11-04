"""只在测试 Schema 往返新迁移，旧表、约束和触发器保持兼容。"""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from test_kernel_migrations import config_for, schema_signature
from test_versions import versioned as versioned

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import CommitManifest, parse_contract
from agentgit.services.history import TABLES
from agentgit.storage.schema_s11_proposals import TABLE_ORDER

pytestmark = pytest.mark.integration


def test_merge_schema_round_trip_and_production_downgrade_guard(history):
    with history.engine.begin() as connection:
        config = config_for(connection)
        before = schema_signature(connection)
        assert set(TABLE_ORDER) <= set(inspect(connection).get_table_names())
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == ScriptDirectory.from_config(config).get_current_head()
        )
        with pytest.raises(RuntimeError, match="正式迁移"):
            command.downgrade(config_for(connection, testing=False), "0023_repair_proposal_reviews")
        command.downgrade(config, "0023_repair_proposal_reviews")
        assert not set(TABLE_ORDER) & set(inspect(connection).get_table_names())
        command.upgrade(config, "head")
        assert schema_signature(connection) == before


def test_single_parent_index_cannot_start_at_one(versioned):
    service, repository, _, commits, _ = versioned
    history = service.history
    original = parse_contract(history.objects.get(commits[-1], ObjectType.COMMIT), CommitManifest)
    manifest = parse_contract(
        encode({**original.model_dump(mode="json"), "parents": [commits[-1]]}), CommitManifest
    )
    obj = history.objects.put_json(ObjectType.COMMIT, manifest.model_dump(mode="json"))
    with (
        pytest.raises(DBAPIError, match="parent order must start at zero"),
        history.engine.begin() as connection,
    ):
        history._ref(connection, repository, obj, "commit", obj.id, "manifest")
        connection.execute(
            TABLES["commits"]
            .insert()
            .values(
                id=obj.id,
                repository_id=repository,
                state_ref=manifest.state_ref,
                environment_ref=manifest.environment_ref,
                event_manifest_ref=manifest.event_manifest_ref,
            )
        )
        connection.execute(
            TABLES["commit_parents"]
            .insert()
            .values(
                repository_id=repository,
                commit_id=obj.id,
                parent_order=1,
                parent_id=commits[-1],
            )
        )
    assert history.log(repository) == list(reversed(commits))
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["commits"])
            ).scalar_one()
            == 3
        )
