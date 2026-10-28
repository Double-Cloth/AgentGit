"""S11 耐久合并提案及精确修订/验证；不放开双父或批准写入。"""

from alembic import context, op

from agentgit.storage.schema_s11_proposals import TABLE_ORDER, build_metadata

revision = "0024_merge_proposals"
down_revision = "0023_repair_proposal_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in TABLE_ORDER:
        build_metadata().tables[name].create(op.get_bind(), checkfirst=False)
    op.execute("""
        CREATE FUNCTION guard_merge_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE previous text;
        DECLARE prior_number integer;
        BEGIN
            PERFORM id FROM merge_proposals WHERE repository_id=NEW.repository_id
                AND id=NEW.proposal_id FOR UPDATE;
            IF NOT FOUND OR EXISTS (SELECT 1 FROM merge_rejections
                WHERE proposal_id=NEW.proposal_id) THEN
                RAISE EXCEPTION 'merge proposal is missing or closed';
            END IF;
            SELECT coalesce(max(r.number),0) INTO prior_number FROM merge_revisions r
                WHERE r.proposal_id=NEW.proposal_id;
            IF NEW.number<>prior_number+1 THEN
                RAISE EXCEPTION 'merge revisions must append in exact order';
            END IF;
            IF prior_number>0 THEN
                SELECT revision_ref INTO previous FROM merge_revisions r
                    WHERE r.proposal_id=NEW.proposal_id AND r.number=prior_number;
                IF NEW.previous_ref<>previous THEN
                    RAISE EXCEPTION 'merge revision must bind its immediate predecessor';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER exact_merge_revision BEFORE INSERT ON merge_revisions
            FOR EACH ROW EXECUTE FUNCTION guard_merge_revision();
        CREATE FUNCTION guard_merge_record() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_revision uuid;
        DECLARE current_digest text;
        BEGIN
            PERFORM id FROM merge_proposals WHERE repository_id=NEW.repository_id
                AND id=NEW.proposal_id FOR UPDATE;
            IF NOT FOUND OR EXISTS (SELECT 1 FROM merge_rejections
                WHERE proposal_id=NEW.proposal_id) THEN
                RAISE EXCEPTION 'merge proposal is missing or closed';
            END IF;
            SELECT r.id,r.candidate_digest INTO current_revision,current_digest
                FROM merge_revisions r WHERE r.proposal_id=NEW.proposal_id
                ORDER BY r.number DESC LIMIT 1;
            IF NEW.revision_id IS DISTINCT FROM current_revision THEN
                RAISE EXCEPTION 'merge record requires current exact revision';
            END IF;
            IF TG_TABLE_NAME='merge_validations' THEN
                IF NEW.candidate_digest<>current_digest THEN
                    RAISE EXCEPTION 'validation candidate mismatch';
                END IF;
            ELSIF TG_TABLE_NAME='merge_review_sessions' THEN
                IF NOT EXISTS (SELECT 1 FROM merge_validations v
                    WHERE v.id=NEW.validation_id AND v.revision_id=NEW.revision_id
                        AND v.proposal_id=NEW.proposal_id AND v.repository_id=NEW.repository_id
                        AND v.passed) THEN
                    RAISE EXCEPTION 'review requires passed exact validation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    for name in ("merge_validations", "merge_review_sessions", "merge_rejections"):
        op.execute(
            f"CREATE TRIGGER exact_{name} BEFORE INSERT ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION guard_merge_record()"
        )
    for name, field, owner, role in (
        ("merge_proposals", "manifest_ref", "merge_proposal", "manifest"),
        ("merge_revisions", "revision_ref", "merge_revision", "revision"),
        ("merge_validations", "validation_ref", "merge_validation", "validation"),
        ("merge_conflicts", "conflict_ref", "merge_conflict", "conflict"),
        ("merge_review_sessions", "ticket_ref", "merge_review", "ticket"),
        ("merge_rejections", "decision_ref", "merge_rejection", "decision"),
    ):
        op.execute(
            f"CREATE TRIGGER owned_{name} BEFORE INSERT ON {name} FOR EACH ROW "
            f"EXECUTE FUNCTION guard_review_owned('{field}','{owner}','{role}')"
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
    op.execute("DROP FUNCTION guard_merge_revision()")
    op.execute("DROP FUNCTION guard_merge_record()")
