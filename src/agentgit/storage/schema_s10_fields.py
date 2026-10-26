"""S10 锁前字段轨迹；历史快照和删除状态编码保持冻结。"""

from typing import Any

import sqlalchemy as sa

from agentgit.storage.schema_s10_confirmation import build_metadata as build_confirmation

TABLE_ORDER = (
    "repair_field_states",
    "repair_field_patches",
    "repair_field_trials",
    "repair_field_jobs",
    "repair_field_results",
    "repair_field_waits",
    "repair_field_summaries",
)


def build_metadata() -> sa.MetaData:
    metadata = build_confirmation()
    metadata.tables["repair_studies"].append_column(sa.Column("field_policy", sa.String(40)))
    metadata.tables["repair_studies"].append_constraint(
        sa.CheckConstraint(
            "field_policy IS NULL OR field_policy='predefined-fields/v1'", name="field_policy"
        )
    )

    def project() -> sa.Column[Any]:
        return sa.Column("repository_id", sa.Uuid, nullable=False)

    def body(name: str, *, nullable: bool = False) -> sa.Column[Any]:
        return sa.Column(name, sa.String(64), sa.ForeignKey("objects.id"), nullable=nullable)

    def created() -> sa.Column[Any]:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    def study_fk() -> sa.ForeignKeyConstraint:
        return sa.ForeignKeyConstraint(
            ["repository_id", "study_id"], ["repair_studies.repository_id", "repair_studies.id"]
        )

    def trial_fk() -> sa.ForeignKeyConstraint:
        return sa.ForeignKeyConstraint(
            ["repository_id", "id"], ["repair_field_trials.repository_id", "repair_field_trials.id"]
        )

    sa.Table(
        "repair_field_states",
        metadata,
        project(),
        sa.Column("study_id", sa.Uuid, nullable=False),
        body("state_ref"),
        body("parent_state_ref", nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("event_id", sa.Uuid, nullable=False),
        created(),
        study_fk(),
        sa.PrimaryKeyConstraint("study_id", "state_ref"),
        sa.UniqueConstraint("repository_id", "study_id", "state_ref"),
        sa.UniqueConstraint("study_id", "sequence"),
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id", "event_id"],
            [
                "repair_study_events.repository_id",
                "repair_study_events.study_id",
                "repair_study_events.id",
            ],
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 0 AND 100 AND ((sequence=0 AND parent_state_ref IS NULL) OR "
            "(sequence>0 AND parent_state_ref IS NOT NULL))",
            name="field_state_sequence",
        ),
    )
    control = metadata.tables["repair_study_jobs"]
    control.append_column(sa.Column("field_state_ref", sa.String(64)))
    control.append_constraint(
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id", "field_state_ref"],
            [
                "repair_field_states.repository_id",
                "repair_field_states.study_id",
                "repair_field_states.state_ref",
            ],
            name="fk_study_current_field_state",
        )
    )
    sa.Table(
        "repair_field_patches",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("study_id", sa.Uuid, nullable=False),
        sa.Column("effect_ref", sa.String(64), nullable=False),
        body("variant_ref"),
        sa.Column("root_patch_ref", sa.String(64), nullable=False),
        sa.Column("parent_patch_ref", sa.String(64), nullable=False),
        body("source_manifest_ref"),
        sa.Column("intervention_id", sa.Uuid, sa.ForeignKey("interventions.id"), nullable=False),
        created(),
        study_fk(),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("study_id", "effect_ref"),
        sa.CheckConstraint(
            "length(effect_ref)=64 AND length(root_patch_ref)=64 AND length(parent_patch_ref)=64",
            name="field_patch_refs",
        ),
    )
    sa.Table(
        "repair_field_trials",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("study_id", sa.Uuid, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("protocol_ref", sa.String(64), nullable=False),
        body("candidate_ref"),
        sa.Column("variant_id", sa.Uuid, sa.ForeignKey("repair_field_patches.id")),
        body("intent_ref"),
        sa.Column("intent_key", sa.String(64), nullable=False),
        sa.Column("branch_name", sa.String(100), nullable=False),
        created(),
        study_fk(),
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id", "protocol_ref"],
            [
                "repair_field_states.repository_id",
                "repair_field_states.study_id",
                "repair_field_states.state_ref",
            ],
        ),
        sa.UniqueConstraint("repository_id", "id"),
        sa.UniqueConstraint("study_id", "ordinal"),
        sa.UniqueConstraint("study_id", "protocol_ref"),
        sa.UniqueConstraint("study_id", "intent_key"),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 99 AND phase='reduce' AND "
            "((kind='field' AND variant_id IS NOT NULL) OR "
            "(kind='atomic_recheck' AND variant_id IS NULL))",
            name="field_trial_scope",
        ),
    )
    sa.Table(
        "repair_field_jobs",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("job_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("initial_run_id", sa.Uuid, nullable=False, unique=True),
        created(),
        trial_fk(),
        sa.ForeignKeyConstraint(["repository_id", "job_id"], ["jobs.repository_id", "jobs.id"]),
        sa.ForeignKeyConstraint(
            ["repository_id", "initial_run_id"], ["runs.repository_id", "runs.id"]
        ),
    )
    sa.Table(
        "repair_field_results",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        body("result_ref"),
        body("evidence_ref", nullable=True),
        body("provenance_ref", nullable=True),
        sa.Column("not_started", sa.Boolean, nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        *(
            sa.Column(name, sa.BigInteger, nullable=False)
            for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
        ),
        created(),
        trial_fk(),
        sa.CheckConstraint("verdict IN ('passed','failed','incomplete')", name="field_verdict"),
        sa.CheckConstraint(
            "model_calls>=0 AND tool_calls>=0 AND tokens>=0 AND cost_micro_usd>=0",
            name="field_usage",
        ),
        sa.CheckConstraint(
            "(not_started AND evidence_ref IS NULL AND provenance_ref IS NULL "
            "AND verdict='incomplete' "
            "AND model_calls=0 AND tool_calls=0 AND tokens=0 AND cost_micro_usd=0) OR "
            "(NOT not_started AND evidence_ref IS NOT NULL AND provenance_ref IS NOT NULL)",
            name="field_result_evidence",
        ),
        sa.CheckConstraint("verdict='incomplete' OR model_calls>0", name="field_new_models"),
    )
    sa.Table(
        "repair_field_waits",
        metadata,
        sa.Column("id", sa.Uuid, primary_key=True),
        project(),
        sa.Column("reason", sa.String(200), nullable=False),
        body("payload_ref"),
        created(),
        trial_fk(),
    )
    sa.Table(
        "repair_field_summaries",
        metadata,
        sa.Column("study_id", sa.Uuid, primary_key=True),
        project(),
        body("summary_ref"),
        sa.Column("state_ref", sa.String(64), nullable=False),
        sa.Column("event_id", sa.Uuid, nullable=False),
        created(),
        study_fk(),
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id", "state_ref"],
            [
                "repair_field_states.repository_id",
                "repair_field_states.study_id",
                "repair_field_states.state_ref",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "study_id", "event_id"],
            [
                "repair_study_events.repository_id",
                "repair_study_events.study_id",
                "repair_study_events.id",
            ],
        ),
    )
    return metadata
