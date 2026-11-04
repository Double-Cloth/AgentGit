"""S11 合并来源分支及独立新任务输入。"""

from alembic import context, op

from agentgit.storage.schema_s11_runs import TABLE_ORDER, build_metadata

revision = "0026_merge_runs"
down_revision = "0025_merge_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = build_metadata()
    for name in TABLE_ORDER:
        metadata.tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_merge_origin() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM merge_approvals a
                WHERE a.repository_id=NEW.repository_id AND a.id=NEW.approval_id
                    AND a.commit_id=NEW.commit_id) THEN
                RAISE EXCEPTION 'merged input requires exact approved commit';
            END IF;
            IF TG_TABLE_NAME='merge_branch_origins' THEN
                IF NOT EXISTS (SELECT 1 FROM branches b
                    WHERE b.repository_id=NEW.repository_id AND b.name=NEW.branch_name
                        AND b.head_commit_id=NEW.commit_id AND b.version=0
                        AND b.active_writer_run_id IS NULL) THEN
                    RAISE EXCEPTION 'merge fork must start at exact approved commit';
                END IF;
            ELSE
                IF NOT EXISTS (SELECT 1 FROM merge_branch_origins f
                    JOIN jobs j ON j.repository_id=f.repository_id AND j.id=NEW.job_id
                    JOIN runs r ON r.repository_id=j.repository_id AND r.id=j.run_id
                    JOIN agent_configs c ON c.repository_id=r.repository_id
                        AND c.id=r.agent_config_id
                    WHERE f.repository_id=NEW.repository_id AND f.branch_name=NEW.branch_name
                        AND f.approval_id=NEW.approval_id AND f.commit_id=NEW.commit_id
                        AND r.branch_name=NEW.branch_name AND r.base_commit_id=NEW.commit_id
                        AND r.origin_run_id IS NULL AND r.mode='live'
                        AND c.id=NEW.config_id AND c.version=NEW.config_version)
                    OR NOT EXISTS (SELECT 1 FROM object_refs x
                        WHERE x.repository_id=NEW.repository_id AND x.owner_type='merge_run_input'
                            AND x.owner_id=NEW.id::text AND x.role='state'
                            AND x.object_id=NEW.initial_state_ref) THEN
                    RAISE EXCEPTION 'merged run must own exact independent task input';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    for name, owner in (
        ("merge_branch_origins", "merge_branch"),
        ("merge_run_requests", "merge_run_request"),
    ):
        op.execute(
            f"CREATE TRIGGER origin_{name} BEFORE INSERT ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION guard_merge_origin()"
        )
        op.execute(
            f"CREATE TRIGGER owned_{name} BEFORE INSERT ON {name} FOR EACH ROW "
            f"EXECUTE FUNCTION guard_review_owned('request_ref','{owner}','request')"
        )
        op.execute(
            f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION guard_merge_origin()")
