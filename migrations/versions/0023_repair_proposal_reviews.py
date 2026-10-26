"""精确修复提案与版本绑定审阅；批准不创建执行任务。"""

from alembic import context, op

from agentgit.storage.schema_s10_reviews import TABLE_ORDER, build_metadata

revision = "0023_repair_proposal_reviews"
down_revision = "0022_repair_field_reduction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in TABLE_ORDER:
        build_metadata().tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_adoption_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repair_studies s
                JOIN repair_study_jobs c ON c.study_id=s.id
                JOIN repair_study_locks l ON l.id=s.id
                JOIN repair_validation_summaries v ON v.id=s.id
                JOIN agent_configs a ON a.id=NEW.source_config_id
                WHERE s.id=NEW.study_id AND s.repository_id=NEW.repository_id
                    AND a.repository_id=NEW.repository_id AND a.version=NEW.source_config_version
                    AND l.lock_ref=NEW.lock_ref AND v.summary_ref=NEW.verification_ref
                    AND c.status IN
                        ('verified_under_fixture','empirically_confirmed','rejected','needs_review')
                    AND NOT c.cancel_requested) THEN
                RAISE EXCEPTION 'adoption requires exact sealed evidence and source config';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_adoption_source BEFORE INSERT ON repair_adoption_proposals
            FOR EACH ROW EXECUTE FUNCTION guard_adoption_source();
        CREATE FUNCTION guard_proposal_review() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ticket repair_review_sessions%ROWTYPE;
        DECLARE proposal repair_adoption_proposals%ROWTYPE;
        DECLARE version integer;
        BEGIN
            SELECT * INTO ticket FROM repair_review_sessions WHERE id=NEW.review_id;
            SELECT * INTO proposal FROM repair_adoption_proposals WHERE id=NEW.proposal_id;
            SELECT max(a.version) INTO version FROM agent_configs a
                WHERE a.repository_id=NEW.repository_id;
            IF ticket.proposal_id<>NEW.proposal_id OR ticket.actor<>NEW.actor THEN
                RAISE EXCEPTION 'review decision must bind own exact ticket';
            END IF;
            IF NEW.decision='approve' THEN
                IF proposal.has_observation OR NOT proposal.production_eligible
                    OR version<>ticket.expected_config_version+1 OR NOT EXISTS
                    (SELECT 1 FROM agent_configs a WHERE a.id=NEW.new_config_id
                        AND a.repository_id=NEW.repository_id
                        AND a.version=ticket.expected_config_version+1
                        AND a.config_ref=proposal.after_config_ref) THEN
                    RAISE EXCEPTION 'config adoption requires exact eligible content and version';
                END IF;
            ELSIF version<>ticket.expected_config_version THEN
                RAISE EXCEPTION 'review snapshot is stale';
            END IF;
            IF NEW.decision='save_experiment' AND NOT EXISTS
                (SELECT 1 FROM repair_saved_experiments e WHERE e.id=NEW.saved_experiment_id
                    AND e.proposal_id=NEW.proposal_id AND e.actor=NEW.actor) THEN
                RAISE EXCEPTION 'saved experiment must belong to reviewed proposal';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER check_proposal_review BEFORE INSERT ON proposal_reviews
            FOR EACH ROW EXECUTE FUNCTION guard_proposal_review();
        CREATE FUNCTION guard_review_owned() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ref text := to_jsonb(NEW)->>TG_ARGV[0];
        BEGIN
            IF ref IS NOT NULL AND NOT EXISTS (SELECT 1 FROM object_refs r
                JOIN objects o ON o.id=r.object_id
                WHERE r.repository_id=NEW.repository_id AND r.owner_type=TG_ARGV[1]
                    AND r.owner_id=NEW.id::text AND r.role=TG_ARGV[2]
                    AND r.object_id=ref AND o.object_type='blob') THEN
                RAISE EXCEPTION 'review row requires owned immutable body';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for name, field, owner, role in (
        ("repair_adoption_proposals", "manifest_ref", "repair_adoption_proposal", "manifest"),
        (
            "repair_adoption_proposals",
            "after_config_ref",
            "repair_adoption_proposal",
            "after_config",
        ),
        ("repair_review_sessions", "ticket_ref", "repair_review", "ticket"),
        ("repair_saved_experiments", "definition_ref", "repair_saved_experiment", "definition"),
        ("proposal_reviews", "decision_ref", "repair_review_decision", "decision"),
    ):
        op.execute(
            f"CREATE TRIGGER owned_{name}_{field} BEFORE INSERT ON {name} FOR EACH ROW "
            f"EXECUTE FUNCTION guard_review_owned('{field}','{owner}','{role}')"
        )
    for name in TABLE_ORDER:
        op.execute(
            f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    for name in reversed(TABLE_ORDER):
        build_metadata().tables[name].drop(op.get_bind(), checkfirst=False)
    for name in ("guard_review_owned", "guard_proposal_review", "guard_adoption_source"):
        op.execute(f"DROP FUNCTION {name}()")
