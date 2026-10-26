"""S10 独立确认/回归的实际派发、结算和终态摘要。"""

from alembic import context, op

from agentgit.storage.schema_s10_confirmation import STUDY_STATUSES, TABLE_ORDER, build_metadata
from agentgit.storage.schema_s10_pools import POOL_STUDY_STATUSES

revision = "0021_repair_confirmation_jobs"
down_revision = "0020_repair_validation_pools"
branch_labels = None
depends_on = None


def statuses(values: tuple[str, ...]) -> None:
    name = op.f("ck_repair_study_jobs_study_job_status")
    op.drop_constraint(name, "repair_study_jobs", type_="check")
    op.create_check_constraint(
        name, "repair_study_jobs", "status IN (" + ",".join(f"'{s}'" for s in values) + ")"
    )


def upgrade() -> None:
    for name in TABLE_ORDER:
        build_metadata().tables[name].create(op.get_bind(), checkfirst=False)
    statuses(STUDY_STATUSES)
    op.execute("""
        CREATE FUNCTION guard_confirmation_trial() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE task repair_study_jobs%ROWTYPE;
        BEGIN
            SELECT * INTO task FROM repair_study_jobs WHERE study_id=NEW.study_id
                AND repository_id=NEW.repository_id FOR UPDATE;
            IF NOT FOUND OR task.cancel_requested OR task.lease_token IS NULL
                OR task.lease_until<=clock_timestamp()
                OR task.status<>(CASE WHEN NEW.phase='confirm' THEN 'confirming' ELSE 'regressing'
                END)
                OR NEW.ordinal<>(SELECT count(*) FROM repair_validation_trials WHERE
                study_id=NEW.study_id)
                OR NEW.branch_name<>'validate-' || replace(NEW.study_id::text,'-','') || '-' ||
                NEW.ordinal::text
                OR NOT EXISTS (SELECT 1 FROM repair_validation_openings o
                    JOIN repair_validation_samples s ON s.suite_id=o.suite_id
                    WHERE o.id=NEW.study_id AND o.repository_id=NEW.repository_id
                        AND s.repository_id=NEW.repository_id AND s.id=NEW.sample_id
                        AND s.ordinal=NEW.ordinal AND s.phase=NEW.phase AND
                        o.plan_ref=NEW.protocol_ref)
                OR EXISTS (SELECT 1 FROM repair_validation_trials t WHERE t.study_id=NEW.study_id
                    AND NOT EXISTS (SELECT 1 FROM repair_validation_results r WHERE r.id=t.id))
                OR EXISTS (SELECT 1 FROM repair_validation_summaries WHERE id=NEW.study_id) THEN
                RAISE EXCEPTION
                'validation intent requires opened sample, live lease and settled predecessors';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_confirmation_trial BEFORE INSERT ON repair_validation_trials
            FOR EACH ROW EXECUTE FUNCTION guard_confirmation_trial();
        CREATE FUNCTION guard_confirmation_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_validation_trials t
                JOIN repair_validation_samples s ON s.id=t.sample_id
                JOIN repair_study_jobs c ON c.study_id=t.study_id
                JOIN repair_studies st ON st.id=t.study_id
                JOIN repair_searches source ON source.id=st.search_id
                JOIN jobs j ON j.id=NEW.job_id AND j.repository_id=NEW.repository_id
                JOIN runs r ON r.id=NEW.initial_run_id AND r.repository_id=NEW.repository_id
                JOIN replay_requests p ON p.job_id=j.id AND p.repository_id=NEW.repository_id
                WHERE t.id=NEW.id AND t.repository_id=NEW.repository_id
                    AND NOT c.cancel_requested AND c.lease_token IS NOT NULL
                    AND c.lease_until>clock_timestamp()
                    AND c.status=(CASE WHEN t.phase='confirm'
                        THEN 'confirming' ELSE 'regressing' END)
                    AND j.kind='research_run' AND j.status='queued' AND j.attempt=0
                    AND j.origin_run_id IS NULL AND j.run_id=r.id
                    AND r.status='queued' AND r.branch_name=t.branch_name
                    AND r.origin_run_id=s.source_run_id AND r.base_commit_id=s.base_commit_id
                    AND p.source_run_id=s.source_run_id AND p.from_commit_id=s.base_commit_id
                    AND p.mode=(CASE WHEN t.phase='confirm'
                        THEN source.screen_mode ELSE 'live' END))
                OR EXISTS (SELECT 1 FROM repair_trial_jobs WHERE job_id=NEW.job_id OR
                initial_run_id=NEW.initial_run_id)
                OR EXISTS (SELECT 1 FROM repair_reduction_jobs WHERE job_id=NEW.job_id OR
                initial_run_id=NEW.initial_run_id) THEN
                RAISE EXCEPTION 'validation dispatch must use new independent authorized replay';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_confirmation_job BEFORE INSERT ON repair_validation_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_confirmation_job();
        CREATE FUNCTION guard_confirmation_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.not_started THEN
                IF EXISTS (SELECT 1 FROM repair_validation_jobs WHERE id=NEW.id)
                    OR NOT EXISTS (SELECT 1 FROM repair_validation_trials t JOIN repair_study_jobs j
                        ON j.study_id=t.study_id WHERE t.id=NEW.id AND j.cancel_requested)
                    OR EXISTS (SELECT 1 FROM repair_validation_trials t JOIN runs r
                        ON r.repository_id=t.repository_id AND r.branch_name=t.branch_name
                        JOIN jobs j ON j.run_id=r.id AND j.repository_id=r.repository_id WHERE
                        t.id=NEW.id) THEN
                    RAISE EXCEPTION
                    'only proven undispatched cancellation can release a validation reservation';
                END IF;
            ELSIF NOT EXISTS (SELECT 1 FROM repair_validation_jobs d JOIN jobs j ON j.id=d.job_id
                JOIN runs r ON r.id=j.run_id WHERE d.id=NEW.id AND d.repository_id=NEW.repository_id
                    AND j.status IN ('completed','failed','cancelled','budget_exhausted')
                    AND (NEW.verdict<>'passed' OR (j.status='completed' AND r.status='completed'
                        AND NEW.model_calls>0))) THEN
                RAISE EXCEPTION 'validation receipt requires terminal independent execution';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_confirmation_result BEFORE INSERT ON repair_validation_results
            FOR EACH ROW EXECUTE FUNCTION guard_confirmation_result();
        CREATE FUNCTION guard_confirmation_summary() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM 1 FROM repair_study_jobs j JOIN repair_study_events e ON e.study_id=j.study_id
                WHERE j.study_id=NEW.id AND j.repository_id=NEW.repository_id
                    AND j.status IN ('confirming','regressing','cancelling')
                    AND j.lease_token IS NOT NULL AND j.lease_until>clock_timestamp()
                    AND e.id=NEW.event_id AND e.action='validation_finished'
                    AND e.revision=j.revision+1 FOR UPDATE OF j;
            IF NOT FOUND OR EXISTS (SELECT 1 FROM repair_validation_trials t WHERE t.study_id=NEW.id
                AND NOT EXISTS (SELECT 1 FROM repair_validation_results r WHERE r.id=t.id)) THEN
                RAISE EXCEPTION 'validation summary requires settled execution under current lease';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_confirmation_summary BEFORE INSERT ON repair_validation_summaries
            FOR EACH ROW EXECUTE FUNCTION guard_confirmation_summary();
        CREATE FUNCTION guard_confirmation_control() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND OLD.status IN
            ('rejected','verified_under_fixture','empirically_confirmed') THEN
                RAISE EXCEPTION 'validation terminal state cannot be rewritten';
            END IF;
            IF NEW.status IN
            ('confirming','regressing','rejected','verified_under_fixture','empirically_confirmed')
                AND NOT EXISTS (SELECT 1 FROM repair_validation_openings WHERE id=NEW.study_id) THEN
                RAISE EXCEPTION 'validation control requires immutable pool opening';
            END IF;
            IF NEW.status IN ('rejected','verified_under_fixture','empirically_confirmed')
                AND NOT EXISTS (SELECT 1 FROM repair_validation_summaries WHERE id=NEW.study_id)
                THEN
                RAISE EXCEPTION 'validation terminal decision requires immutable summary';
            END IF;
            IF NEW.status IN ('verified_under_fixture','empirically_confirmed') AND (
                (SELECT count(*) FROM repair_validation_trials t JOIN repair_validation_results r
                ON r.id=t.id
                    WHERE t.study_id=NEW.study_id AND NOT r.not_started AND r.model_calls>0
                        AND r.verdict IN ('passed','failed'))=10
                AND (SELECT count(*) FROM repair_validation_trials t JOIN
                repair_validation_results r ON r.id=t.id
                    WHERE t.study_id=NEW.study_id AND t.phase='confirm' AND r.verdict='passed')>=5
                AND (SELECT count(*) FROM repair_validation_trials t JOIN
                repair_validation_results r ON r.id=t.id
                    WHERE t.study_id=NEW.study_id AND t.phase='regression' AND r.verdict='passed')=4
            ) IS NOT TRUE THEN
                RAISE EXCEPTION
                'verified requires six independent confirmations and four passing regressions';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_confirmation_control BEFORE INSERT OR UPDATE ON repair_study_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_confirmation_control();
    """)
    for table, field, owner, role in (
        ("repair_validation_trials", "intent_ref", "repair_validation_trial", "intent"),
        ("repair_validation_trials", "projection_ref", "repair_validation_trial", "projection"),
        ("repair_validation_results", "result_ref", "repair_validation_trial", "result"),
        ("repair_validation_results", "evidence_ref", "repair_validation_trial", "evidence"),
        ("repair_validation_waits", "payload_ref", "repair_validation_trial", "waiting"),
        ("repair_validation_summaries", "summary_ref", "repair_study", "validation_summary"),
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
    op.execute("DROP TRIGGER check_confirmation_control ON repair_study_jobs")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    for name in (
        "guard_confirmation_control",
        "guard_confirmation_summary",
        "guard_confirmation_result",
        "guard_confirmation_job",
        "guard_confirmation_trial",
    ):
        op.execute(f"DROP FUNCTION {name}()")
    statuses(POOL_STUDY_STATUSES)
