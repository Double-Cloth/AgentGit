"""冻结搜索及试验意图，保留预算、租约与不可变结算审计。"""

from alembic import context, op

from agentgit.storage.schema_s9 import TABLE_ORDER, build_metadata

revision = "0017_repair_search_ledger"
down_revision = "0016_static_debug_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = build_metadata().tables
    for name in TABLE_ORDER:
        tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_repair_owned_body() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ref text := to_jsonb(NEW)->>TG_ARGV[0];
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM object_refs r JOIN objects o ON o.id=r.object_id
                WHERE r.repository_id=NEW.repository_id AND r.owner_type=TG_ARGV[1]
                    AND r.owner_id=NEW.id::text AND r.role=TG_ARGV[2]
                    AND r.object_id=ref AND o.object_type='blob') THEN
                RAISE EXCEPTION 'repair row must own its immutable payload';
            END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION guard_repair_job_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                RAISE EXCEPTION 'repair jobs cannot be deleted';
            END IF;
            IF TG_OP='INSERT' THEN
                IF NEW.revision<>0 OR NEW.status<>'queued' OR NEW.cancel_requested
                    OR NEW.lease_token IS NOT NULL OR NEW.stop_reason IS NOT NULL THEN
                    RAISE EXCEPTION 'repair jobs must start queued without a lease';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.search_id<>OLD.search_id OR NEW.repository_id<>OLD.repository_id
                OR NEW.created_at<>OLD.created_at OR NEW.revision<>OLD.revision+1
                OR (OLD.cancel_requested AND NOT NEW.cancel_requested)
                OR OLD.status IN ('needs_confirmation','budget_exhausted','no_solution_in_space',
                                   'not_found','cancelled')
                OR (OLD.status='needs_attention' AND (NOT NEW.cancel_requested
                    OR NEW.status NOT IN ('cancelling','cancelled'))) THEN
                RAISE EXCEPTION 'repair job update violates fencing or terminal history';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_repair_job_update BEFORE INSERT OR UPDATE OR DELETE ON repair_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_repair_job_update();
        CREATE FUNCTION guard_repair_trial() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE task repair_jobs%ROWTYPE;
        BEGIN
            SELECT * INTO task FROM repair_jobs WHERE repository_id=NEW.repository_id
                AND search_id=NEW.search_id FOR UPDATE;
            IF NOT FOUND OR task.status<>'running' OR task.cancel_requested
                OR task.lease_token IS NULL OR task.lease_until<=clock_timestamp()
                OR NEW.ordinal<>(SELECT count(*) FROM repair_trials WHERE search_id=NEW.search_id)
                OR NEW.branch_name<>'repair-' || replace(NEW.search_id::text,'-','')
                    || '-' || NEW.ordinal::text THEN
                RAISE EXCEPTION 'repair intent must reserve under a current serialized lease';
            END IF;
            IF NEW.operation='recorded_preflight' THEN
                IF NEW.ordinal<>0 THEN
                    RAISE EXCEPTION 'recorded preflight must be the first and only preflight';
                END IF;
            ELSIF NOT EXISTS (SELECT 1 FROM repair_trials t JOIN repair_trial_results r
                ON r.repository_id=t.repository_id AND r.id=t.id
                WHERE t.search_id=NEW.search_id AND t.operation='recorded_preflight'
                    AND r.verdict='chain_valid') THEN
                RAISE EXCEPTION 'screening requires a verified recorded recovery chain';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_repair_trial BEFORE INSERT ON repair_trials
            FOR EACH ROW EXECUTE FUNCTION guard_repair_trial();
        CREATE FUNCTION guard_repair_trial_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_trials t
                JOIN repair_searches s ON s.repository_id=t.repository_id AND s.id=t.search_id
                JOIN jobs j ON j.repository_id=t.repository_id AND j.id=NEW.job_id
                JOIN runs r ON r.repository_id=j.repository_id AND r.id=j.run_id
                JOIN replay_requests p ON p.repository_id=j.repository_id AND p.job_id=j.id
                WHERE t.repository_id=NEW.repository_id AND t.id=NEW.id
                    AND j.run_id=NEW.initial_run_id AND j.origin_run_id IS NULL
                    AND j.status='queued' AND r.branch_name=t.branch_name
                    AND r.origin_run_id=s.failed_run_id AND r.base_commit_id=s.base_commit_id
                    AND p.source_run_id=s.failed_run_id AND p.from_commit_id=s.base_commit_id
                    AND ((t.operation='recorded_preflight' AND p.mode='recorded'
                          AND p.until_commit_id=s.source_head_commit_id)
                         OR (t.operation='screen' AND p.mode=s.screen_mode))) THEN
                RAISE EXCEPTION 'repair dispatch must bind a new job at the frozen source/base';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_repair_trial_job BEFORE INSERT ON repair_trial_jobs
            FOR EACH ROW EXECUTE FUNCTION guard_repair_trial_job();
        CREATE FUNCTION guard_repair_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.verdict='not_started' THEN
                IF EXISTS (SELECT 1 FROM repair_trial_jobs WHERE id=NEW.id)
                    OR EXISTS (SELECT 1 FROM repair_trials t JOIN runs r
                        ON r.repository_id=t.repository_id AND r.branch_name=t.branch_name
                        JOIN jobs j ON j.repository_id=r.repository_id AND j.run_id=r.id
                        WHERE t.id=NEW.id)
                    OR NEW.model_calls<>0 OR NEW.tool_calls<>0 OR NEW.tokens<>0
                    OR NEW.cost_micro_usd<>0
                    OR NOT EXISTS (SELECT 1 FROM repair_trials t JOIN repair_jobs j
                        ON j.search_id=t.search_id WHERE t.id=NEW.id
                        AND j.cancel_requested) THEN
                    RAISE EXCEPTION 'only undispatched cancelled intents can release zero usage';
                END IF;
            ELSIF NOT EXISTS (SELECT 1 FROM repair_trial_jobs d JOIN jobs j
                ON j.repository_id=d.repository_id AND j.id=d.job_id
                JOIN runs r ON r.repository_id=j.repository_id AND r.id=j.run_id
                JOIN repair_trials t ON t.repository_id=d.repository_id AND t.id=d.id
                WHERE d.repository_id=NEW.repository_id AND d.id=NEW.id
                    AND j.status IN ('completed','failed','cancelled','budget_exhausted')
                    AND ((NEW.verdict='chain_valid' AND t.operation='recorded_preflight'
                          AND j.status='completed')
                         OR (NEW.verdict='passed' AND t.operation='screen'
                             AND j.status='completed' AND r.status='completed')
                         OR NEW.verdict='failed')) THEN
                RAISE EXCEPTION 'repair settlement requires a known terminal independent job';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_repair_result BEFORE INSERT ON repair_trial_results
            FOR EACH ROW EXECUTE FUNCTION guard_repair_result();
    """)
    for table, field, owner, role in (
        ("repair_searches", "manifest_ref", "repair_search", "manifest"),
        ("repair_proposals", "definition_ref", "repair_proposal", "definition"),
        ("repair_trials", "intent_ref", "repair_trial", "intent"),
        ("repair_trial_results", "result_ref", "repair_trial", "result"),
        ("repair_events", "payload_ref", "repair_event", "payload"),
    ):
        op.execute(f"""
            CREATE TRIGGER owned_{table} BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION guard_repair_owned_body('{field}','{owner}','{role}')
        """)
    for table in TABLE_ORDER:
        if table != "repair_jobs":
            op.execute(f"""
                CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()
            """)


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    tables = build_metadata().tables
    for name in reversed(TABLE_ORDER):
        tables[name].drop(op.get_bind(), checkfirst=False)
    for name in (
        "guard_repair_result",
        "guard_repair_trial_job",
        "guard_repair_trial",
        "guard_repair_job_update",
        "guard_repair_owned_body",
    ):
        op.execute(f"DROP FUNCTION {name}()")
