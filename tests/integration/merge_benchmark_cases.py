"""S11 三十例真实输入构造；仅显式 Live 变更，不插入伪造历史或调用。"""

import json
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge_proposals import MergeBranchPin, NewMergeProposal
from agentgit.core.models import EffectClass, parse_contract
from agentgit.core.research import AgentConfig, AgentState, Plan, PlanNode, ResearchGoal, evolve
from agentgit.core.versions import ArtifactPatch, LiveOverrides, LiveReplay
from agentgit.runtime.validation import ResearchReport, render_report
from agentgit.services.approvals import ApprovalService
from agentgit.services.calls import RecordedCallGateway
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker

ASSETS = Path(__file__).resolve().parents[1] / "fixtures/merge/v1/cases.json"


def specs():
    return json.loads(ASSETS.read_text("utf-8"))["cases"]


def append_plan(plan, prefix, slots=(), *, search=False, save=True):
    extra = []
    for index, slot in enumerate(slots):
        extra.append(
            PlanNode(
                key=f"{prefix}-read-{index}",
                kind="read",
                source_slot=slot,
                dependencies=("write-sources",),
            )
        )
    if search:
        extra.append(
            PlanNode(key=prefix + "-search", kind="search", dependencies=("write-sources",))
        )
    if save:
        extra.append(
            PlanNode(
                key=prefix + "-save",
                kind="write_sources",
                dependencies=tuple(node.key for node in extra) or ("write-sources",),
            )
        )
    return Plan(nodes=(*plan.nodes, *extra))


def branch_overrides(number, side, config, state, report):
    """完整变更定义，场景身份在正式执行前冻结。"""
    cfg = evolve(config, prompt=config.prompt + f"\nM{number:02d} {side}")
    plan = None
    patches = ()
    if number <= 10:
        slots = {
            1: ((0,), (1,)),
            2: ((2,), (3,)),
            3: ((4,), (5,)),
            4: ((0,), (0,)),
            5: ((0,), ()),
            6: ((), (5,)),
            7: ((), (2,)),
            8: ((0, 1), (2, 3)),
        }.get(number, ((), ()))[side == "theirs"]
        prefix = "shared" if number == 4 else side
        if slots:
            plan = append_plan(state.plan, prefix, slots)
        elif number == 7 and side == "ours":
            plan = append_plan(state.plan, prefix, search=True, save=False)
        elif number == 9 and side == "ours":
            cfg = evolve(cfg, limits=evolve(cfg.limits, max_tool_calls=12))
        elif number == 10:
            plan = append_plan(state.plan, prefix)
    elif number <= 20:
        changed = report.model_dump(mode="json")
        if number == 11:
            changed["claims"][0]["value"] = "200_m" if side == "ours" else "0.2_km"
        elif number == 12:
            changed["claims"][1]["value"] = "variant-a" if side == "ours" else "variant-b"
        elif side == "ours":
            if number == 13:
                changed["evaluation_time"] = "2026-10-19T00:00:00Z"
            elif number == 14:
                changed["sources"][0]["source"]["valid_until"] = "2020-01-01T00:00:00Z"
            elif number == 15:
                changed["sources"][0]["source"]["collected_at"] = "2030-01-01T00:00:00Z"
            elif number == 16:
                changed["claims"][0]["quote"] = "原文不包含的引文"
            elif number == 17:
                changed["sources"] = changed["sources"][1:]
            elif number == 18:
                changed["claims"][0]["source_id"] = next(
                    item["source"]["id"]
                    for item in changed["sources"]
                    if item["source"]["framework"] != changed["claims"][0]["framework"]
                )
            elif number == 19:
                changed["sources"][0]["source"]["body_sha256"] = "0" * 64
            elif number == 20:
                changed["claims"][0]["unit"] = "m"
        if changed != report.model_dump(mode="json"):
            markdown = (
                render_report(report)
                if number == 20
                else render_report(ResearchReport.model_validate_json(encode(changed)))
            )
            patches = (
                ArtifactPatch(
                    path="sources.json",
                    text=encode(changed).decode(),
                    media_type="application/json",
                ),
                ArtifactPatch(path="report.md", text=markdown, media_type="text/markdown"),
            )
    elif number in (21, 22, 29):
        nodes = list(state.plan.nodes)
        if number == 22 and side == "ours":
            nodes = [
                evolve(
                    node, dependencies=tuple(key for key in node.dependencies if key != "read-0")
                )
                for node in nodes
                if node.key != "read-0"
            ]
        else:
            key = "read-0" if side == "ours" else "read-1"
            parent = "read-1" if side == "ours" else "read-0"
            if number != 29 or side == "ours":
                nodes = [
                    evolve(node, dependencies=(*node.dependencies, parent))
                    if node.key == key
                    else node
                    for node in nodes
                ]
        plan = Plan(nodes=tuple(nodes))
    elif number in (23, 24, 25) and side == "ours":
        cap = {23: "max_tool_calls", 24: "max_model_calls", 25: "max_steps"}[number]
        cfg = evolve(cfg, limits=evolve(cfg.limits, **{cap: 1}))
    elif number == 26 and side == "ours":
        cfg = evolve(cfg, tools=("artifact_write",))
    elif number == 27:
        plan = append_plan(state.plan, "shared", (0 if side == "ours" else 1,))
    elif number == 28 and side == "ours":
        cfg = evolve(cfg, tools=("corpus_search", "source_read"))
    return LiveOverrides(config=cfg, plan=plan, artifacts=patches)


def payment_authorization(history):
    rule = ToolRule(
        provider="fixture:payment/v1",
        signature_digest="a" * 64,
        effect=EffectClass.IRREVERSIBLE,
        scope="external",
    )
    return rule, ApprovalService(history, ToolPolicy(rules=(rule,)), actor="merge-fixture-operator")


class PaymentIntentWorker(Worker):
    """通过精确审批与本地故障模拟器保存未知结果，没有外部付款客户端。"""

    def _attach(self, lease):
        session, origins, order = super()._attach(lease)

        def pending(_state):
            rule, authorization = payment_authorization(session.history)
            gateway = RecordedCallGateway(
                session.history,
                session.repository_id,
                session.run_id,
                lambda: (session.head, session.version),
                authorization=authorization,
            )

            def never():
                raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "本地故障模拟：没有外部客户端")

            outcome = gateway.call(
                step="fixture-payment",
                attempt=1,
                provider=rule.provider,
                signature=rule.signature_digest,
                effect=rule.effect,
                request=encode(dict(schema_version=1, purpose="merge-safety-fixture")),
                invoke=never,
                max_bytes=4096,
            )
            if outcome.error is not None:
                raise outcome.error

        session.before_evaluate = pending
        return session, origins, order


def build_case(history, tmp_path, spec):
    number = int(spec["id"][1:])
    repo = history.create_repository("S11 " + spec["id"])
    research = ResearchService(history, tmp_path / "runs")
    cfg = evolve(
        AgentConfig(),
        prompt=AgentConfig().prompt + "\n" + spec["id"],
        limits=evolve(AgentConfig().limits, max_replans=0),
    )
    config = research.create_config(repo, cfg)
    goal = ResearchGoal(text="三十组合并样例 " + spec["id"])
    validator = ValidatorService(history).freeze(repo, goal, research.corpus)
    task = research.create_task(repo, config, goal)
    session = research.start(repo, task)
    assert session.execute().evaluation.passed
    report_entry = next(
        item
        for item in history.restore(repo, session.head).artifacts
        if item.path == "sources.json"
    )
    report = parse_contract(
        history.objects.get(report_entry.object_ref, ObjectType.BLOB), ResearchReport
    )
    base = session.head
    if number in (21, 22, 29):
        with history.engine.connect() as connection:
            links = TABLES["run_commits"]
            path = (
                connection.execute(
                    sa.select(links.c.commit_id)
                    .where(links.c.repository_id == repo, links.c.run_id == session.run_id)
                    .order_by(links.c.run_order)
                )
                .scalars()
                .all()
            )
        base = next(
            commit
            for commit in path
            if (
                state := parse_contract(encode(history.restore(repo, commit).data), AgentState)
            ).plan
            is not None
            and not state.completed
            and state.pending_tool is None
        )
    state = parse_contract(encode(history.restore(repo, base).data), AgentState)
    manager = ManagementService(research)
    pins = []
    branches = []
    for side in ("ours", "theirs"):
        overrides = branch_overrides(number, side, cfg, state, report)
        created = manager.replay(
            repo,
            LiveReplay(
                source_run_id=str(session.run_id),
                from_commit=base,
                branch_name=side,
                overrides=overrides,
            ),
            side,
        )
        worker = (
            PaymentIntentWorker(manager.jobs)
            if number == 30 and side == "ours"
            else Worker(manager.jobs)
        )
        finished = worker.run_once()
        if number == 30 and side == "ours":
            assert finished["status"] == "waiting_approval"
            job = manager.jobs.get(repo, UUID(created["job_id"]))
            run = manager.jobs.run(repo, job["run_id"])
            _, authorization = payment_authorization(history)
            approval = authorization.list_for_run(repo, job["run_id"])[-1]
            authorization.decide(
                repo,
                approval["id"],
                actor="merge-fixture-operator",
                decision="approve",
                expected_version=run["state_version"],
            )
            assert PaymentIntentWorker(manager.jobs).run_once()["status"] == "outcome_unknown"
        job = manager.jobs.get(repo, UUID(created["job_id"]))
        branch = history.branch(repo, side)
        if number != 30 or side != "ours":
            assert branch["active_writer_run_id"] is None
        pins.append(
            MergeBranchPin(
                name=side,
                source_run_id=str(job["run_id"]),
                head=branch["head_commit_id"],
                version=branch["version"],
            )
        )
        branches.append(
            dict(side=side, submitted=created, job=job, overrides=overrides.model_dump(mode="json"))
        )
    return (
        repo,
        research,
        manager,
        NewMergeProposal(ours=pins[0], theirs=pins[1], validator_ref=validator),
        dict(
            case=spec,
            root_run_id=str(session.run_id),
            base=base,
            branches=branches,
        ),
    )
