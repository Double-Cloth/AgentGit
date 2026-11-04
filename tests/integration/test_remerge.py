"""真实重复合并、双父交叉历史和显式共同祖先/执行来源选择。"""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from merge_proposal_cases import archive, history_fingerprint
from merge_proposal_cases import merge_case as merge_case
from test_merge_runs import approved

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import ApprovedMergeOrigin, MergeOrigin
from agentgit.core.merge_proposals import (
    ApproveMergeProposal,
    MergeBranchPin,
    NewMergeProposal,
    PrepareMergeReview,
    ValidateMergeRevision,
)
from agentgit.core.merge_runs import ForkMergedBranch, NewMergedRun
from agentgit.core.research import evolve
from agentgit.core.versions import LiveReplay
from agentgit.services.history import TABLES
from agentgit.services.merge import MergePreviewService
from agentgit.services.merge_inputs import load_state_input
from agentgit.services.merge_validation import preview_research_merge
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def pin(history, repo, name, run):
    branch = history.branch(repo, name)
    return MergeBranchPin(
        name=name, source_run_id=str(run), head=branch["head_commit_id"], version=branch["version"]
    )


def spawn_runs(case, approval, prefix, sides=("ours", "theirs")):
    repo, research, manager, source, session, _ = case
    pins = []
    for side in sides:
        name = prefix + "-" + side
        config = research.create_config(
            repo, evolve(session.config, prompt=session.config.prompt + "\nremerge " + name)
        )
        with research.history.engine.connect() as connection:
            version = connection.execute(
                sa.select(TABLES["agent_configs"].c.version).where(
                    TABLES["agent_configs"].c.id == config
                )
            ).scalar_one()
        manager.fork_merged_branch(
            repo,
            ForkMergedBranch(
                approval_id=approval["decision_id"], commit_id=approval["commit_id"], name=name
            ),
            "fork-" + name,
        )
        submitted = manager.start_merged_run(
            repo,
            NewMergedRun(
                branch_name=name,
                expected_head=approval["commit_id"],
                expected_branch_version=0,
                config_id=str(config),
                config_version=version,
                validator_ref=source.validator_ref,
                budget=session.config.limits,
            ),
            "start-" + name,
        )
        assert Worker(manager.jobs).run_once()["status"] == "completed"
        pins.append(pin(research.history, repo, name, submitted["run_id"]))
    return pins


def spawn_pair(case, approval, prefix):
    pins = spawn_runs(case, approval, prefix)
    return NewMergeProposal(ours=pins[0], theirs=pins[1], validator_ref=case[3].validator_ref)


def merge(manager, repo, payload):
    created = manager.create_merge_proposal(repo, payload, "create-" + uuid4().hex)
    identity = UUID(created["proposal_id"])
    expected = dict(
        expected_revision=created["revision"], expected_revision_ref=created["revision_ref"]
    )
    validation = manager.validate_merge_revision(
        repo, identity, ValidateMergeRevision(**expected), "validate"
    )
    reviewed = manager.begin_merge_review(
        repo,
        identity,
        PrepareMergeReview(**expected, validation_id=validation["validation_id"]),
        "review",
    )
    request = ApproveMergeProposal(
        **expected,
        review_id=reviewed["review_id"],
        ticket_ref=reviewed["ticket_ref"],
        candidate_digest=created["candidate_digest"],
    )
    result = manager.approve_merge_proposal(repo, identity, request, "approve-" + str(identity))
    return result, dict(
        proposal_id=str(identity), created=created, validation=validation, reviewed=reviewed
    )


def test_two_generations_keep_approved_base_audit_without_fabricated_usage(
    history, merge_case, tmp_path, monkeypatch, request
):
    case = approved(merge_case)
    repo, research, manager, _, _, first = case
    generations = []
    current = first
    for number in (2, 3):
        payload = spawn_pair(case, current, f"generation-{number}")
        before = history_fingerprint(history, repo)

        def readonly(*args, **kwargs):
            pytest.fail("三方重审不能发布 CAS 或执行调用")

        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", readonly)
            preview = preview_research_merge(
                history,
                repo,
                MergeOrigin(run_id=payload.ours.source_run_id, commit_id=payload.ours.head),
                MergeOrigin(run_id=payload.theirs.source_run_id, commit_id=payload.theirs.head),
                payload.validator_ref,
            )
        assert history_fingerprint(history, repo) == before
        origin = preview.validation.origins[0]
        assert (
            isinstance(origin, ApprovedMergeOrigin) and origin.approval_id == current["decision_id"]
        )
        state = preview.state.proposed_state
        assert state is not None and preview.validation.validated
        assert (
            state.source_usage[0].usage is None
            and state.source_usage[0].execution == "not_executed"
        )
        assert all(
            item.usage is not None and item.execution is None for item in state.source_usage[1:]
        )
        assert all(isinstance(proof.origin, MergeOrigin) for proof in state.read_proofs)
        with pytest.raises(AgentGitError) as error:
            load_state_input(
                history,
                repo,
                ApprovedMergeOrigin(approval_id=str(uuid4()), commit_id=current["commit_id"]),
                research.corpus,
            )
        assert error.value.code == ErrorCode.NOT_FOUND
        if number == 3:
            with monkeypatch.context() as bounded:
                bounded.setattr("agentgit.services.merge_inputs.MAX_MERGE_DEPTH", 0)
                with pytest.raises(AgentGitError) as limit:
                    load_state_input(history, repo, origin, research.corpus)
                assert limit.value.code == ErrorCode.OUTPUT_LIMIT
            with monkeypatch.context() as bounded:
                bounded.setattr("agentgit.services.merge_inputs.MAX_INPUTS", 1)
                with pytest.raises(AgentGitError) as limit:
                    load_state_input(history, repo, origin, research.corpus)
                assert limit.value.code == ErrorCode.OUTPUT_LIMIT
        current, evidence = merge(manager, repo, payload)
        evidence.update(result=current, base=origin.model_dump(mode="json"))
        generations.append(evidence)
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(first=first, generations=generations),
    )


def test_criss_cross_best_bases_and_shared_commit_require_explicit_run(
    history, merge_case, tmp_path, request
):
    repo, research, manager, source, session = merge_case
    replicas = {}
    for side, original in (("x", source.ours), ("y", source.theirs)):
        replicas[side] = []
        for number in (1, 2):
            name = f"replica-{side}-{number}"
            created = manager.replay(
                repo,
                LiveReplay(
                    source_run_id=original.source_run_id,
                    from_commit=original.head,
                    branch_name=name,
                ),
                name,
            )
            assert Worker(manager.jobs).run_once()["status"] == "completed"
            replicas[side].append(pin(history, repo, name, created["run_id"]))
        assert replicas[side][0].head == replicas[side][1].head
    left, _ = merge(
        manager,
        repo,
        NewMergeProposal(
            ours=replicas["x"][0], theirs=replicas["y"][0], validator_ref=source.validator_ref
        ),
    )
    right, _ = merge(
        manager,
        repo,
        NewMergeProposal(
            ours=replicas["y"][1], theirs=replicas["x"][1], validator_ref=source.validator_ref
        ),
    )
    case = (repo, research, manager, source, session, left)
    # 只取每个根的一侧新任务，保留两条互不隶属的合并历史。
    left_run = spawn_runs(case, left, "cross-left", sides=("ours",))[0]
    right_run = spawn_runs(case, right, "cross-right", sides=("ours",))[0]
    payload = NewMergeProposal(ours=left_run, theirs=right_run, validator_ref=source.validator_ref)
    selection = MergePreviewService(history).bases(repo, payload.ours.head, payload.theirs.head)
    assert selection.status == "ambiguous_merge_base"
    assert set(selection.bases) == {replicas["x"][0].head, replicas["y"][0].head}
    before = history_fingerprint(history, repo)
    with pytest.raises(AgentGitError, match="ambiguous_merge_base"):
        manager.create_merge_proposal(repo, payload, "ambiguous")
    selected = evolve(payload, selected_base=replicas["x"][0].head)
    with pytest.raises(AgentGitError, match="唯一明确"):
        manager.create_merge_proposal(repo, selected, "run-ambiguous")
    assert history_fingerprint(history, repo) == before
    final = evolve(selected, selected_base_run_id=replicas["x"][0].source_run_id)
    result, evidence = merge(manager, repo, final)
    assert (
        evidence["validation"]["record"]["result"]["origins"][0]["run_id"]
        == final.selected_base_run_id
    )
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(
            left=left,
            right=right,
            selection=selection.model_dump(mode="json"),
            result=result,
            replicas={
                key: [item.model_dump(mode="json") for item in value]
                for key, value in replicas.items()
            },
        ),
    )
