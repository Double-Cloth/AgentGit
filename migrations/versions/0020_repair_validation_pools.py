"""S10 筛选前预注册与最终候选后的一次性样本池开启。"""

from alembic import context, op

from agentgit.storage.schema_s10_pools import POOL_STUDY_STATUSES, TABLE_ORDER, build_metadata
from agentgit.storage.schema_s10_reduction import STUDY_STATUSES

revision = "0020_repair_validation_pools"
down_revision = "0019_repair_reduction_studies"
branch_labels = None
depends_on = None


def status_constraint(statuses: tuple[str, ...]) -> None:
    name = op.f("ck_repair_study_jobs_study_job_status")
    op.drop_constraint(name, "repair_study_jobs", type_="check")
    op.create_check_constraint(
        name, "repair_study_jobs", "status IN (" + ",".join(f"'{s}'" for s in statuses) + ")"
    )


def upgrade() -> None:
    for name in TABLE_ORDER:
        build_metadata().tables[name].create(op.get_bind(), checkfirst=False)
    status_constraint(POOL_STUDY_STATUSES)
    op.execute("""
        CREATE FUNCTION guard_validation_registration() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_id uuid;
        BEGIN
            IF TG_TABLE_NAME='repair_validation_suites' THEN
                source_id := NEW.search_id;
            ELSE
                SELECT search_id INTO source_id FROM repair_validation_suites
                    WHERE id=NEW.suite_id AND repository_id=NEW.repository_id;
            END IF;
            PERFORM 1 FROM repair_jobs j WHERE j.search_id=source_id
                AND j.repository_id=NEW.repository_id AND j.status='queued'
                AND j.revision=0 AND NOT j.cancel_requested FOR UPDATE;
            IF NOT FOUND OR EXISTS (SELECT 1 FROM repair_trials WHERE search_id=source_id)
                OR EXISTS (SELECT 1 FROM repair_studies WHERE search_id=source_id) THEN
                RAISE EXCEPTION 'validation conditions must be registered before screening starts';
            END IF;
            IF TG_TABLE_NAME='repair_validation_samples' THEN
                IF NEW.phase='confirm' AND NOT EXISTS (
                    SELECT 1 FROM repair_searches s WHERE s.id=source_id
                        AND s.failed_run_id=NEW.source_run_id
                        AND s.base_commit_id=NEW.base_commit_id) THEN
                    RAISE EXCEPTION 'confirmation must preserve the screened task checkpoint';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM run_commits c
                    WHERE c.repository_id=NEW.repository_id AND c.run_id=NEW.source_run_id
                        AND c.commit_id=NEW.base_commit_id) THEN
                    RAISE EXCEPTION 'validation origin must own its checkpoint';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_validation_suite BEFORE INSERT ON repair_validation_suites
            FOR EACH ROW EXECUTE FUNCTION guard_validation_registration();
        CREATE TRIGGER check_validation_sample BEFORE INSERT ON repair_validation_samples
            FOR EACH ROW EXECUTE FUNCTION guard_validation_registration();
        CREATE FUNCTION guard_validation_opening() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM 1 FROM repair_study_jobs j JOIN repair_studies s ON s.id=j.study_id
                JOIN repair_validation_suites v ON v.search_id=s.search_id
                    AND v.repository_id=s.repository_id
                JOIN repair_study_locks l ON l.id=j.study_id
                JOIN repair_study_events e ON e.study_id=j.study_id
                WHERE j.study_id=NEW.id AND j.repository_id=NEW.repository_id
                    AND v.id=NEW.suite_id AND j.status='awaiting_samples'
                    AND NOT j.cancel_requested AND j.lease_token IS NULL
                    AND e.id=NEW.event_id AND e.revision=j.revision+1
                    AND e.action='sample_pool_opened' FOR UPDATE OF j;
            IF NOT FOUND OR (SELECT count(*) FROM repair_validation_samples
                WHERE suite_id=NEW.suite_id)<>10 THEN
                RAISE EXCEPTION 'opening requires a sealed candidate and ten preregistered samples';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_validation_opening BEFORE INSERT ON repair_validation_openings
            FOR EACH ROW EXECUTE FUNCTION guard_validation_opening();
        CREATE FUNCTION guard_validation_ready() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status='validation_ready' AND NOT EXISTS (
                SELECT 1 FROM repair_validation_openings WHERE id=NEW.study_id) THEN
                RAISE EXCEPTION 'validation_ready requires immutable opening';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_validation_ready BEFORE INSERT OR UPDATE ON repair_study_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_validation_ready();
    """)
    for table, field, owner, role in (
        ("repair_validation_suites", "manifest_ref", "repair_validation_suite", "manifest"),
        ("repair_validation_samples", "generation_ref", "repair_validation_sample", "ticket"),
        ("repair_validation_samples", "sample_ref", "repair_validation_sample", "sample"),
        ("repair_validation_openings", "opening_ref", "repair_study", "validation_opening"),
        ("repair_validation_openings", "plan_ref", "repair_study", "validation_plan"),
    ):
        op.execute(
            f"CREATE TRIGGER owned_{table}_{field} BEFORE INSERT ON {table} FOR EACH ROW "
            f"EXECUTE FUNCTION guard_study_owned('{field}','{owner}','{role}','id')"
        )
    for name in TABLE_ORDER:
        op.execute(
            f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    op.execute("DROP TRIGGER check_validation_ready ON repair_study_jobs")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    for name in (
        "guard_validation_ready",
        "guard_validation_opening",
        "guard_validation_registration",
    ):
        op.execute(f"DROP FUNCTION {name}()")
    status_constraint(STUDY_STATUSES)
