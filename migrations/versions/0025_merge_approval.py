"""S11 精确批准、独立合并 Commit 和有界双父约束。"""

import sqlalchemy as sa
from alembic import context, op

from agentgit.storage.schema_s11_approval import build_metadata

revision = "0025_merge_approval"
down_revision = "0024_merge_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().tables["merge_approvals"].create(op.get_bind(), checkfirst=False)
    op.drop_constraint("ck_commit_parents_single_parent", "commit_parents", type_="check")
    op.create_check_constraint(
        "ck_commit_parents_two_parents", "commit_parents", "parent_order BETWEEN 0 AND 1"
    )
    op.create_unique_constraint("uq_distinct_parents", "commit_parents", ["commit_id", "parent_id"])
    op.execute("""
        CREATE FUNCTION guard_merge_closed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM id FROM merge_proposals WHERE repository_id=NEW.repository_id
                AND id=NEW.proposal_id FOR UPDATE;
            IF NOT FOUND OR EXISTS (SELECT 1 FROM merge_approvals WHERE proposal_id=NEW.proposal_id)
                OR EXISTS (SELECT 1 FROM merge_rejections WHERE proposal_id=NEW.proposal_id) THEN
                RAISE EXCEPTION 'merge proposal is missing or closed';
            END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION guard_published_merge_parent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM merge_approvals WHERE commit_id=NEW.commit_id) THEN
                RAISE EXCEPTION 'merge parents already published';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER published_merge_parent BEFORE INSERT ON commit_parents
            FOR EACH ROW EXECUTE FUNCTION guard_published_merge_parent();
        CREATE FUNCTION check_approved_merge() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE a merge_approvals%ROWTYPE;
                p merge_proposals%ROWTYPE;
                r merge_revisions%ROWTYPE;
                t merge_review_sessions%ROWTYPE;
                count_parents integer;
        BEGIN
            SELECT count(*) INTO count_parents FROM commit_parents WHERE commit_id=NEW.commit_id;
            IF count_parents>0 AND NOT EXISTS (SELECT 1 FROM commit_parents
                WHERE commit_id=NEW.commit_id AND parent_order=0) THEN
                RAISE EXCEPTION 'parent order must start at zero';
            END IF;
            SELECT * INTO a FROM merge_approvals WHERE commit_id=NEW.commit_id;
            IF NOT FOUND THEN
                IF count_parents=2 THEN
                    RAISE EXCEPTION 'dual parents require exact approval';
                END IF;
                RETURN NULL;
            END IF;
            SELECT * INTO p FROM merge_proposals WHERE id=a.proposal_id;
            SELECT * INTO r FROM merge_revisions WHERE proposal_id=p.id
                ORDER BY number DESC LIMIT 1;
            SELECT * INTO t FROM merge_review_sessions WHERE id=a.review_id;
            IF count_parents<>2 OR r.id<>a.revision_id OR t.proposal_id<>a.proposal_id
                OR t.revision_id<>a.revision_id OR t.actor<>a.actor
                OR EXISTS (SELECT 1 FROM merge_rejections WHERE proposal_id=a.proposal_id)
                OR NOT EXISTS (SELECT 1 FROM merge_validations v WHERE v.id=t.validation_id
                    AND v.revision_id=a.revision_id AND v.proposal_id=a.proposal_id AND v.passed)
                OR NOT EXISTS (SELECT 1 FROM commit_parents WHERE commit_id=a.commit_id
                    AND parent_order=0 AND parent_id=p.ours_head)
                OR NOT EXISTS (SELECT 1 FROM commit_parents WHERE commit_id=a.commit_id
                    AND parent_order=1 AND parent_id=p.theirs_head)
                OR NOT EXISTS (SELECT 1 FROM branches WHERE repository_id=a.repository_id
                    AND name=p.ours_branch AND head_commit_id=a.commit_id
                    AND version=p.ours_version+1 AND active_writer_run_id IS NULL)
                OR NOT EXISTS (SELECT 1 FROM branches WHERE repository_id=a.repository_id
                    AND name=p.theirs_branch AND head_commit_id=p.theirs_head
                    AND version=p.theirs_version AND active_writer_run_id IS NULL)
                OR EXISTS (SELECT 1 FROM run_commits WHERE commit_id=a.commit_id) THEN
                RAISE EXCEPTION 'approval, parents or target CAS mismatch';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER exact_dual_parents AFTER INSERT ON commit_parents
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_approved_merge();
        CREATE CONSTRAINT TRIGGER exact_merge_approval AFTER INSERT ON merge_approvals
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_approved_merge();
        CREATE TRIGGER owned_merge_approvals BEFORE INSERT ON merge_approvals
            FOR EACH ROW EXECUTE FUNCTION
                guard_review_owned('decision_ref','merge_approval','decision');
        CREATE TRIGGER immutable_merge_approvals BEFORE UPDATE OR DELETE ON merge_approvals
            FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
    """)
    for name in (
        "merge_revisions",
        "merge_validations",
        "merge_review_sessions",
        "merge_rejections",
        "merge_approvals",
    ):
        op.execute(
            f"CREATE TRIGGER closed_{name} BEFORE INSERT ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION guard_merge_closed()"
        )


def downgrade() -> None:
    if not context.config.attributes.get("testing", False):
        raise RuntimeError("正式迁移只前进，只有独立测试库允许回退")
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM merge_approvals)")).scalar_one():
        raise RuntimeError("测试回退不能删除已批准合并历史")
    op.execute("DROP TRIGGER exact_dual_parents ON commit_parents")
    op.execute("DROP TRIGGER published_merge_parent ON commit_parents")
    for name in (
        "merge_revisions",
        "merge_validations",
        "merge_review_sessions",
        "merge_rejections",
    ):
        op.execute(f"DROP TRIGGER closed_{name} ON {name}")
    build_metadata().tables["merge_approvals"].drop(op.get_bind(), checkfirst=False)
    op.execute("DROP FUNCTION check_approved_merge()")
    op.execute("DROP FUNCTION guard_published_merge_parent()")
    op.execute("DROP FUNCTION guard_merge_closed()")
    op.drop_constraint("uq_distinct_parents", "commit_parents", type_="unique")
    op.drop_constraint("ck_commit_parents_two_parents", "commit_parents", type_="check")
    op.create_check_constraint(
        "ck_commit_parents_single_parent", "commit_parents", "parent_order = 0"
    )
