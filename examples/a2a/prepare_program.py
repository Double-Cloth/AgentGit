"""把明确发现的固定卡片和公开文本转换为有界程序；不联网或读取凭据。"""

import argparse
import sys
from pathlib import Path
from uuid import uuid4

from agentgit.core.a2a import A2ABinding
from agentgit.core.a2a_runs import A2AProgram
from agentgit.core.canonical import encode
from agentgit.core.models import parse_contract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binding", type=Path)
    parser.add_argument("--text", default="整理公开资料：Aster、Birch、Cedar。")
    arguments = parser.parse_args()
    with arguments.binding.open("rb") as stream:
        raw = stream.read(2_097_153)
    if len(raw) > 2_097_152:
        parser.error("绑定文件超过2MiB")
    binding = parse_contract(raw, A2ABinding)
    body = dict(
        configuration=dict(
            acceptedOutputModes=["text/plain", "application/json"], returnImmediately=True
        ),
        message=dict(
            messageId=str(uuid4()),
            role="ROLE_USER",
            parts=[dict(mediaType="text/plain", text=arguments.text)],
        ),
    )
    program = A2AProgram(binding=binding, messages=(encode(body).decode(),))
    sys.stdout.buffer.write(encode(program.model_dump(mode="json")) + b"\n")


if __name__ == "__main__":
    main()
