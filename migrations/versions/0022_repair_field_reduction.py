"""锁前字段状态、派生补丁、实际 Job 和结算，旧删除协议保持冻结。"""

import sqlalchemy as sa
from alembic import context, op

from agentgit.storage.schema_s10_fields import TABLE_ORDER, build_metadata

revision = "0022_repair_field_reduction"
down_revision = "0021_repair_confirmation_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repair_studies", sa.Column("field_policy", sa.String(40)))
    op.create_check_constraint(
        op.f("ck_repair_studies_field_policy"),
        "repair_studies",
        "field_policy IS NULL OR field_policy='predefined-fields/v1'",
    )
    for name in TABLE_ORDER:
        build_metadata().tables[name].create(op.get_bind(), checkfirst=False)
    op.add_column("repair_study_jobs", sa.Column("field_state_ref", sa.String(64)))
    op.create_foreign_key(
        op.f("fk_study_current_field_state"),
        "repair_study_jobs",
        "repair_field_states",
        ["repository_id", "study_id", "field_state_ref"],
        ["repository_id", "study_id", "state_ref"],
    )
    op.execute("""
        CREATE FUNCTION guard_field_owned() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ref text := to_jsonb(NEW)->>TG_ARGV[0];
        BEGIN
            IF ref IS NULL THEN RETURN NEW; END IF;
            IF NOT EXISTS (SELECT 1 FROM object_refs r JOIN objects o ON o.id=r.object_id
                WHERE r.repository_id=NEW.repository_id AND r.owner_type=TG_ARGV[1]
                AND r.owner_id=to_jsonb(NEW)->>TG_ARGV[3] AND r.role=TG_ARGV[2]
                AND r.object_id=ref AND o.object_type='blob') THEN
                RAISE EXCEPTION 'field row must own its immutable body';
            END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION guard_field_state() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE task repair_study_jobs%ROWTYPE;
        BEGIN
            SELECT j.* INTO task FROM repair_study_jobs j JOIN repair_studies s
                ON s.id=j.study_id AND s.repository_id=j.repository_id
                WHERE j.study_id=NEW.study_id AND j.repository_id=NEW.repository_id
                AND s.field_policy='predefined-fields/v1' FOR UPDATE OF j;
            IF NOT FOUND OR task.lease_token IS NULL
                OR task.lease_until<=clock_timestamp()
                OR task.status NOT IN ('reducing','cancelling','waiting_reconciliation')
                OR EXISTS (SELECT 1 FROM repair_study_locks WHERE id=NEW.study_id)
                OR NEW.sequence<>(SELECT count(*) FROM repair_field_states
                    WHERE study_id=NEW.study_id)
                OR NOT EXISTS (SELECT 1 FROM repair_study_events e
                    WHERE e.id=NEW.event_id AND e.study_id=NEW.study_id
                    AND e.repository_id=NEW.repository_id AND e.revision=task.revision+1
                    AND ((NEW.sequence=0 AND e.action='field_started')
                        OR (NEW.sequence>0 AND e.action='field_trial_settled')))
                OR EXISTS (SELECT 1 FROM repair_reduction_trials t
                    WHERE t.study_id=NEW.study_id AND NOT EXISTS
                    (SELECT 1 FROM repair_reduction_results r WHERE r.id=t.id)) THEN
                RAISE EXCEPTION 'field state requires current lease and settled prior phase';
            END IF;
            IF NEW.sequence=0 THEN
                IF task.field_state_ref IS NOT NULL OR task.cancel_requested
                    OR task.status<>'reducing' THEN
                    RAISE EXCEPTION 'field initialization cannot repeat or bypass control';
                END IF;
            ELSIF task.field_state_ref IS DISTINCT FROM NEW.parent_state_ref
                OR NOT EXISTS (SELECT 1 FROM repair_field_trials t
                    JOIN repair_field_results r ON r.id=t.id
                    WHERE t.study_id=NEW.study_id AND t.protocol_ref=NEW.parent_state_ref
                    AND NOT r.not_started AND r.evidence_ref IS NOT NULL) THEN
                RAISE EXCEPTION 'field successor requires actual settled predecessor';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_state BEFORE INSERT ON repair_field_states
            FOR EACH ROW EXECUTE FUNCTION guard_field_state();
        CREATE FUNCTION guard_field_patch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_studies s
                JOIN repair_study_jobs c ON c.study_id=s.id
                JOIN repair_searches origin ON origin.id=s.search_id
                JOIN interventions i ON i.id=NEW.intervention_id
                WHERE s.id=NEW.study_id AND s.repository_id=NEW.repository_id
                    AND s.field_policy='predefined-fields/v1'
                    AND origin.manifest_ref=NEW.source_manifest_ref
                    AND c.field_state_ref IS NOT NULL AND c.status='reducing'
                    AND NOT c.cancel_requested AND c.lease_token IS NOT NULL
                    AND c.lease_until>clock_timestamp()
                    AND i.repository_id=NEW.repository_id
                    AND i.source_run_id=origin.failed_run_id
                    AND i.base_commit_id=origin.base_commit_id
                    AND i.operator IN ('PROMPT_PATCH','PLAN_PATCH'))
                OR EXISTS (SELECT 1 FROM repair_study_locks WHERE id=NEW.study_id) THEN
                RAISE EXCEPTION 'derived field patch requires owned active source and policy';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_patch BEFORE INSERT ON repair_field_patches
            FOR EACH ROW EXECUTE FUNCTION guard_field_patch();
        CREATE FUNCTION guard_field_trial() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE task repair_study_jobs%ROWTYPE;
        BEGIN
            SELECT * INTO task FROM repair_study_jobs WHERE study_id=NEW.study_id
                AND repository_id=NEW.repository_id FOR UPDATE;
            IF NOT FOUND OR task.status<>'reducing' OR task.cancel_requested
                OR task.lease_token IS NULL OR task.lease_until<=clock_timestamp()
                OR task.field_state_ref IS DISTINCT FROM NEW.protocol_ref
                OR NEW.ordinal<>(SELECT count(*) FROM repair_reduction_trials
                    WHERE study_id=NEW.study_id)+(SELECT count(*) FROM repair_field_trials
                    WHERE study_id=NEW.study_id)
                OR NEW.branch_name<>'field-' || replace(NEW.study_id::text,'-','') || '-'
                    || NEW.ordinal::text
                OR (NEW.variant_id IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM repair_field_patches p WHERE p.id=NEW.variant_id
                        AND p.study_id=NEW.study_id AND p.repository_id=NEW.repository_id))
                OR EXISTS (SELECT 1 FROM repair_field_trials t WHERE t.study_id=NEW.study_id
                    AND NOT EXISTS (SELECT 1 FROM repair_field_results r WHERE r.id=t.id))
                OR EXISTS (SELECT 1 FROM repair_study_locks WHERE id=NEW.study_id) THEN
                RAISE EXCEPTION 'field intent requires current serialized state and lease';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_trial BEFORE INSERT ON repair_field_trials
            FOR EACH ROW EXECUTE FUNCTION guard_field_trial();
        CREATE FUNCTION guard_field_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_field_trials t
                JOIN repair_studies s ON s.id=t.study_id
                JOIN repair_searches origin ON origin.id=s.search_id
                JOIN repair_study_jobs c ON c.study_id=s.id
                JOIN jobs j ON j.id=NEW.job_id AND j.repository_id=NEW.repository_id
                JOIN runs r ON r.id=NEW.initial_run_id AND r.repository_id=NEW.repository_id
                JOIN replay_requests p ON p.job_id=j.id AND p.repository_id=NEW.repository_id
                WHERE t.id=NEW.id AND t.repository_id=NEW.repository_id
                    AND c.status='reducing' AND NOT c.cancel_requested
                    AND c.lease_token IS NOT NULL AND c.lease_until>clock_timestamp()
                    AND c.field_state_ref=t.protocol_ref
                    AND j.kind='research_run' AND j.status='queued' AND j.attempt=0
                    AND j.origin_run_id IS NULL AND j.run_id=r.id AND r.status='queued'
                    AND r.branch_name=t.branch_name AND r.origin_run_id=origin.failed_run_id
                    AND r.base_commit_id=origin.base_commit_id
                    AND p.source_run_id=origin.failed_run_id
                    AND p.from_commit_id=origin.base_commit_id AND p.mode=origin.screen_mode)
                OR EXISTS (SELECT 1 FROM repair_trial_jobs WHERE job_id=NEW.job_id
                    OR initial_run_id=NEW.initial_run_id)
                OR EXISTS (SELECT 1 FROM repair_reduction_jobs WHERE job_id=NEW.job_id
                    OR initial_run_id=NEW.initial_run_id)
                OR EXISTS (SELECT 1 FROM repair_validation_jobs WHERE job_id=NEW.job_id
                    OR initial_run_id=NEW.initial_run_id) THEN
                RAISE EXCEPTION 'field dispatch requires new independent replay';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_job BEFORE INSERT ON repair_field_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_field_job();
        CREATE FUNCTION guard_field_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.not_started THEN
                IF EXISTS (SELECT 1 FROM repair_field_jobs WHERE id=NEW.id)
                    OR NOT EXISTS (SELECT 1 FROM repair_field_trials t
                        JOIN repair_study_jobs c ON c.study_id=t.study_id
                        WHERE t.id=NEW.id AND c.cancel_requested)
                    OR EXISTS (SELECT 1 FROM repair_field_trials t JOIN runs r
                        ON r.repository_id=t.repository_id AND r.branch_name=t.branch_name
                        JOIN jobs j ON j.run_id=r.id AND j.repository_id=r.repository_id
                        WHERE t.id=NEW.id) THEN
                    RAISE EXCEPTION 'empty field cancellation requires proven no dispatch';
                END IF;
            ELSIF NOT EXISTS (SELECT 1 FROM repair_field_jobs d JOIN jobs j ON j.id=d.job_id
                JOIN runs r ON r.id=j.run_id WHERE d.id=NEW.id
                    AND d.repository_id=NEW.repository_id
                    AND j.status IN ('completed','failed','cancelled','budget_exhausted')
                    AND (NEW.verdict<>'passed' OR (j.status='completed' AND r.status='completed')))
                THEN
                RAISE EXCEPTION 'field receipt requires terminal actual job';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_result BEFORE INSERT ON repair_field_results
            FOR EACH ROW EXECUTE FUNCTION guard_field_result();
        CREATE FUNCTION guard_field_summary() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_study_jobs c JOIN repair_study_events e
                ON e.study_id=c.study_id WHERE c.study_id=NEW.study_id
                    AND c.repository_id=NEW.repository_id AND c.status='reducing'
                    AND NOT c.cancel_requested AND c.field_state_ref=NEW.state_ref
                    AND c.lease_token IS NOT NULL AND c.lease_until>clock_timestamp()
                    AND e.id=NEW.event_id AND e.action='final_candidate_locked'
                    AND e.revision=c.revision+1)
                OR EXISTS (SELECT 1 FROM repair_field_trials t WHERE t.study_id=NEW.study_id
                    AND NOT EXISTS (SELECT 1 FROM repair_field_results r WHERE r.id=t.id)) THEN
                RAISE EXCEPTION 'field summary requires settled current state and lock event';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_summary BEFORE INSERT ON repair_field_summaries
            FOR EACH ROW EXECUTE FUNCTION guard_field_summary();
        CREATE FUNCTION guard_field_control() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='INSERT' AND NEW.field_state_ref IS NOT NULL THEN
                RAISE EXCEPTION 'study cannot start in a manufactured field state';
            END IF;
            IF TG_OP='UPDATE' AND NEW.field_state_ref IS DISTINCT FROM OLD.field_state_ref THEN
                IF EXISTS (SELECT 1 FROM repair_study_locks WHERE id=NEW.study_id)
                    OR NEW.field_state_ref IS NULL OR NOT EXISTS
                    (SELECT 1 FROM repair_field_states f JOIN repair_study_events e
                        ON e.id=f.event_id WHERE f.study_id=NEW.study_id
                        AND f.state_ref=NEW.field_state_ref
                        AND f.parent_state_ref IS NOT DISTINCT FROM OLD.field_state_ref
                        AND e.revision=NEW.revision) THEN
                    RAISE EXCEPTION 'field pointer cannot roll back or change after lock';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_control BEFORE INSERT OR UPDATE ON repair_study_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_field_control();
        CREATE FUNCTION guard_field_lock() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM repair_studies WHERE id=NEW.id
                AND field_policy='predefined-fields/v1') AND NOT EXISTS
                (SELECT 1 FROM repair_field_summaries f JOIN repair_study_jobs c
                    ON c.study_id=f.study_id WHERE f.study_id=NEW.id
                    AND f.repository_id=NEW.repository_id AND f.state_ref=c.field_state_ref
                    AND f.event_id=NEW.selection_event_id) THEN
                RAISE EXCEPTION 'field-enabled lock requires immutable field summary';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_field_lock BEFORE INSERT ON repair_study_locks
            FOR EACH ROW EXECUTE FUNCTION guard_field_lock();
        CREATE FUNCTION guard_atom_after_fields() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM repair_study_jobs WHERE study_id=NEW.study_id
                AND field_state_ref IS NOT NULL) THEN
                RAISE EXCEPTION 'original atom phase is frozen after field initialization';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER freeze_atoms_after_fields BEFORE INSERT ON repair_reduction_trials
            FOR EACH ROW EXECUTE FUNCTION guard_atom_after_fields();
    """)
    for table, field, owner, role, identity in (
        ("repair_field_states", "state_ref", "repair_study", "field_state", "study_id"),
        ("repair_study_jobs", "field_state_ref", "repair_study", "field_state", "study_id"),
        ("repair_field_patches", "variant_ref", "repair_field_patch", "variant", "id"),
        ("repair_field_trials", "intent_ref", "repair_field_trial", "intent", "id"),
        ("repair_field_trials", "candidate_ref", "repair_field_trial", "candidate", "id"),
        ("repair_field_results", "result_ref", "repair_field_trial", "result", "id"),
        ("repair_field_results", "evidence_ref", "repair_field_trial", "evidence", "id"),
        ("repair_field_results", "provenance_ref", "repair_field_trial", "provenance", "id"),
        ("repair_field_waits", "payload_ref", "repair_field_trial", "waiting", "id"),
        ("repair_field_summaries", "summary_ref", "repair_study", "field_summary", "study_id"),
    ):
        operations = "INSERT OR UPDATE" if table == "repair_study_jobs" else "INSERT"
        op.execute(
            f"CREATE TRIGGER owned_{table}_{field} BEFORE {operations} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION guard_field_owned('{field}','{owner}',"
            f"'{role}','{identity}')"
        )
    for name in TABLE_ORDER:
        op.execute(
            f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    for table, trigger in (
        ("repair_reduction_trials", "freeze_atoms_after_fields"),
        ("repair_study_locks", "check_field_lock"),
        ("repair_study_jobs", "check_field_control"),
        ("repair_study_jobs", "owned_repair_study_jobs_field_state_ref"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    op.drop_constraint(
        op.f("fk_study_current_field_state"), "repair_study_jobs", type_="foreignkey"
    )
    op.drop_column("repair_study_jobs", "field_state_ref")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    for name in (
        "guard_atom_after_fields",
        "guard_field_lock",
        "guard_field_control",
        "guard_field_summary",
        "guard_field_result",
        "guard_field_job",
        "guard_field_trial",
        "guard_field_patch",
        "guard_field_state",
        "guard_field_owned",
    ):
        op.execute(f"DROP FUNCTION {name}()")
    op.drop_constraint(op.f("ck_repair_studies_field_policy"), "repair_studies", type_="check")
    op.drop_column("repair_studies", "field_policy")
