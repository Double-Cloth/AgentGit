"""直接运行有限离线研究任务；持久化 Worker/业务 CLI 在 S4 引入。"""

import argparse
import json
from pathlib import Path

from agentgit.config import Settings
from agentgit.core.errors import AgentGitError
from agentgit.core.models import RunStatus
from agentgit.core.research import AgentConfig, BudgetLimits, ResearchGoal
from agentgit.services.history import HistoryService
from agentgit.services.research import ResearchService
from agentgit.storage.database import make_engine
from agentgit.storage.objects import ObjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description="使用六份合成资料生成带证据的三框架对比报告")
    parser.add_argument("--goal", default="比较三个 fixture 框架，生成带来源的 Markdown 报告")
    parser.add_argument("--max-steps", type=int, default=32, help="模型决策步骤上限")
    args = parser.parse_args()
    if not 1 <= args.max_steps <= 1000:
        parser.error("--max-steps 必须位于 1–1000")
    settings = Settings.from_env()
    engine = make_engine(settings)
    try:
        root = Path(settings.data_root).resolve()
        history = HistoryService(
            engine, ObjectStore(root / "objects", max_bytes=settings.max_object_bytes)
        )
        service = ResearchService(history, root)
        repository = history.create_repository("离线研究演示")
        config = service.create_config(
            repository, AgentConfig(limits=BudgetLimits(max_steps=args.max_steps))
        )
        task = service.create_task(repository, config, ResearchGoal(text=args.goal))
        session = service.start(repository, task)
        state = session.execute()
        print(
            json.dumps(
                {
                    "repository_id": str(repository),
                    "task_id": str(task),
                    "run_id": str(session.run_id),
                    "status": state.status.value,
                    "checkpoint": session.head,
                    "workspace": str(session.workspace.root),
                    "error_code": state.error_code,
                    "usage": state.usage.model_dump(mode="json"),
                    "evaluation": state.evaluation.model_dump(mode="json")
                    if state.evaluation
                    else None,
                }
            )
        )
        return 0 if state.status == RunStatus.COMPLETED else 1
    except AgentGitError as exc:
        print(json.dumps({"error": exc.as_dict()}))
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
