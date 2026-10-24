"""S10 独立缩减编排、共享预算意图与最终候选锁。"""

from alembic import context, op

from agentgit.storage.schema_s10_reduction import TABLE_ORDER, build_metadata

revision = "0019_repair_reduction_studies"
down_revision = "0018_repair_dispatch_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in TABLE_ORDER:
        build_metadata().tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_study_owned() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ref text := to_jsonb(NEW)->>TG_ARGV[0];
        BEGIN
            IF ref IS NULL AND TG_ARGV[0]='evidence_ref' THEN RETURN NEW; END IF;
            IF NOT EXISTS (SELECT 1 FROM object_refs r JOIN objects o ON o.id=r.object_id
                WHERE r.repository_id=NEW.repository_id AND r.owner_type=TG_ARGV[1]
                    AND r.owner_id=to_jsonb(NEW)->>TG_ARGV[3] AND r.role=TG_ARGV[2]
                    AND r.object_id=ref AND o.object_type='blob') THEN
                RAISE EXCEPTION 'study row must own its frozen body';
            END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION guard_study_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_jobs j JOIN repair_proposals p ON
                p.search_id=j.search_id
                JOIN repair_trials t ON t.proposal_id=p.id AND t.search_id=j.search_id
                JOIN repair_trial_results r ON r.id=t.id JOIN repair_events e ON
                e.search_id=j.search_id
                WHERE j.search_id=NEW.search_id AND j.repository_id=NEW.repository_id
                    AND j.status='needs_confirmation' AND NOT j.cancel_requested
                    AND p.id=NEW.selected_proposal_id AND t.id=NEW.selected_trial_id
                    AND r.verdict='passed' AND e.id=NEW.source_event_id AND
                e.action='search_stopped')
                OR EXISTS (SELECT 1 FROM repair_trials t WHERE t.search_id=NEW.search_id AND NOT
                EXISTS
                    (SELECT 1 FROM repair_trial_results r WHERE r.id=t.id)) THEN
                RAISE EXCEPTION 'study requires sealed screening and independent passing result';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_study_source BEFORE INSERT ON repair_studies FOR EACH ROW EXECUTE
                FUNCTION guard_study_source();
        CREATE FUNCTION guard_study_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'study jobs cannot be deleted'; END IF;
            IF TG_OP='INSERT' THEN
                IF NEW.status<>'queued' OR NEW.revision<>0 OR NEW.cancel_requested OR
                NEW.lease_token IS NOT NULL
                    OR NEW.stop_reason IS NOT NULL OR NEW.state_ref<>(SELECT initial_state_ref FROM
                repair_studies WHERE id=NEW.study_id) THEN
                    RAISE EXCEPTION 'study job must start with frozen initial state';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.study_id<>OLD.study_id OR NEW.repository_id<>OLD.repository_id OR
                NEW.created_at<>OLD.created_at
                OR NEW.revision<>OLD.revision+1 OR (OLD.cancel_requested AND NOT
                NEW.cancel_requested)
                OR OLD.status IN ('cancelled','budget_exhausted','needs_review')
                OR (OLD.status='needs_attention' AND (NOT NEW.cancel_requested OR NEW.status NOT IN
                ('cancelling','cancelled')))
                OR (EXISTS (SELECT 1 FROM repair_study_locks l WHERE l.id=NEW.study_id) AND
                NEW.state_ref<>OLD.state_ref) THEN
                RAISE EXCEPTION 'study update violates fencing, terminal state or candidate lock';
            END IF;
            IF NEW.status='awaiting_samples' AND NOT EXISTS (SELECT 1 FROM repair_study_locks WHERE
                id=NEW.study_id) THEN
                RAISE EXCEPTION 'waiting for samples requires immutable candidate lock';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_study_job BEFORE INSERT OR UPDATE OR DELETE ON repair_study_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_study_job();
        CREATE FUNCTION guard_reduction_trial() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE task repair_study_jobs%ROWTYPE;
        BEGIN
            SELECT * INTO task FROM repair_study_jobs WHERE study_id=NEW.study_id AND
                repository_id=NEW.repository_id FOR UPDATE;
            IF NOT FOUND OR task.status<>'reducing' OR task.cancel_requested OR task.lease_token IS
                NULL OR task.lease_until<=clock_timestamp()
                OR NEW.protocol_ref<>task.state_ref OR NEW.ordinal<>(SELECT count(*) FROM
                repair_reduction_trials WHERE study_id=NEW.study_id)
                OR NEW.branch_name<>'reduce-' || replace(NEW.study_id::text,'-','') || '-' ||
                NEW.ordinal::text
                OR EXISTS (SELECT 1 FROM repair_study_locks WHERE id=NEW.study_id)
                OR EXISTS (SELECT 1 FROM repair_reduction_trials t WHERE t.study_id=NEW.study_id
                AND NOT EXISTS
                    (SELECT 1 FROM repair_reduction_results r WHERE r.id=t.id)) THEN
                RAISE EXCEPTION
                'reduction intent requires current serialized state and no unresolved predecessor';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_reduction_trial BEFORE INSERT ON repair_reduction_trials FOR EACH ROW
                EXECUTE FUNCTION guard_reduction_trial();
        CREATE FUNCTION guard_reduction_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_reduction_trials t JOIN repair_studies s ON
                s.id=t.study_id
                JOIN repair_searches source ON source.id=s.search_id JOIN repair_study_jobs c ON
                c.study_id=s.id
                JOIN jobs j ON j.repository_id=t.repository_id AND j.id=NEW.job_id JOIN runs r ON
                r.id=NEW.initial_run_id
                WHERE t.id=NEW.id AND t.repository_id=NEW.repository_id AND c.status='reducing' AND
                NOT c.cancel_requested
                    AND c.lease_token IS NOT NULL AND c.lease_until>clock_timestamp() AND
                c.state_ref=t.protocol_ref
                    AND j.kind='research_run' AND j.status='queued' AND j.attempt=0
                    AND j.run_id=r.id AND
                j.origin_run_id IS NULL
                    AND r.status='queued' AND r.branch_name=t.branch_name AND
                r.origin_run_id=source.failed_run_id
                    AND r.base_commit_id=source.base_commit_id
                    AND EXISTS (SELECT 1 FROM replay_requests p
                        WHERE p.job_id=j.id AND p.repository_id=t.repository_id
                            AND p.source_run_id=source.failed_run_id
                            AND p.from_commit_id=source.base_commit_id
                            AND p.mode=source.screen_mode))
                OR EXISTS (SELECT 1 FROM repair_trial_jobs WHERE job_id=NEW.job_id OR
                initial_run_id=NEW.initial_run_id) THEN
                RAISE EXCEPTION 'reduction dispatch requires new independent authorized replay job';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_reduction_job BEFORE INSERT ON repair_reduction_jobs FOR EACH ROW
                EXECUTE FUNCTION guard_reduction_job();
        CREATE FUNCTION guard_reduction_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.not_started THEN
                IF EXISTS (SELECT 1 FROM repair_reduction_jobs WHERE id=NEW.id) OR NOT EXISTS
                    (SELECT 1 FROM repair_reduction_trials t JOIN repair_study_jobs j ON
                j.study_id=t.study_id
                     WHERE t.id=NEW.id AND j.cancel_requested) OR EXISTS
                    (SELECT 1 FROM repair_reduction_trials t JOIN runs r ON
                r.repository_id=t.repository_id AND r.branch_name=t.branch_name
                    JOIN jobs j ON j.repository_id=r.repository_id AND j.run_id=r.id
                     WHERE t.id=NEW.id) THEN
                    RAISE EXCEPTION
                'only proven undispatched cancelled intent can be settled without execution';
                END IF;
            ELSIF NOT EXISTS (SELECT 1 FROM repair_reduction_jobs d JOIN jobs j ON j.id=d.job_id
                JOIN runs r ON r.id=j.run_id WHERE d.id=NEW.id AND d.repository_id=NEW.repository_id
                    AND j.status IN ('completed','failed','cancelled','budget_exhausted')
                    AND (NEW.verdict<>'passed' OR (j.status='completed' AND r.status='completed')))
                THEN
                RAISE EXCEPTION 'reduction result needs terminal independent job';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_reduction_result BEFORE INSERT ON repair_reduction_results FOR EACH
                ROW EXECUTE FUNCTION guard_reduction_result();
        CREATE FUNCTION guard_study_lock() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_study_jobs j JOIN repair_study_events e ON
                e.study_id=j.study_id
                WHERE j.study_id=NEW.id AND j.repository_id=NEW.repository_id AND
                e.id=NEW.selection_event_id
                    AND e.action='final_candidate_locked' AND NOT j.cancel_requested AND
                j.status='reducing'
                    AND j.lease_token IS NOT NULL AND j.lease_until>clock_timestamp())
                OR EXISTS (SELECT 1 FROM repair_reduction_trials t WHERE t.study_id=NEW.id AND NOT
                EXISTS
                    (SELECT 1 FROM repair_reduction_results r WHERE r.id=t.id)) THEN
                RAISE EXCEPTION 'candidate lock requires completed owned reduction state';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_study_lock BEFORE INSERT ON repair_study_locks FOR EACH ROW EXECUTE
                FUNCTION guard_study_lock();
    """)
    for table, field, owner, role, identity in (
        ("repair_studies", "manifest_ref", "repair_study", "manifest", "id"),
        ("repair_studies", "initial_state_ref", "repair_study", "reduction_state", "id"),
        ("repair_study_jobs", "state_ref", "repair_study", "reduction_state", "study_id"),
        ("repair_reduction_trials", "intent_ref", "repair_reduction_trial", "intent", "id"),
        ("repair_reduction_trials", "candidate_ref", "repair_reduction_trial", "candidate", "id"),
        ("repair_reduction_results", "result_ref", "repair_reduction_trial", "result", "id"),
        ("repair_reduction_results", "evidence_ref", "repair_reduction_trial", "evidence", "id"),
        ("repair_study_events", "payload_ref", "repair_study_event", "payload", "id"),
        ("repair_study_locks", "lock_ref", "repair_study", "final_lock", "id"),
        ("repair_study_locks", "summary_ref", "repair_study", "reduction_summary", "id"),
        ("repair_reduction_waits", "payload_ref", "repair_reduction_trial", "waiting", "id"),
    ):
        operations = "INSERT OR UPDATE" if table == "repair_study_jobs" else "INSERT"
        op.execute(
            f"CREATE TRIGGER owned_{table}_{field} BEFORE {operations} ON {table} FOR EACH ROW "
            f"EXECUTE FUNCTION guard_study_owned('{field}','{owner}','{role}','{identity}')"
        )
    for table in TABLE_ORDER:
        if table != "repair_study_jobs":
            op.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
            )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    tables = build_metadata().tables
    for name in reversed(TABLE_ORDER):
        tables[name].drop(op.get_bind(), checkfirst=False)
    for name in (
        "guard_study_lock",
        "guard_reduction_result",
        "guard_reduction_job",
        "guard_reduction_trial",
        "guard_study_job",
        "guard_study_source",
        "guard_study_owned",
    ):
        op.execute(f"DROP FUNCTION {name}()")
