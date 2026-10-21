FROM python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a

# 固定 Debian 快照及直接依赖；只在受信任构建阶段使用网络。
RUN printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260918T000000Z bookworm main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260918T000000Z bookworm-security main\n' > /etc/apt/sources.list \
    && rm /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 ripgrep=13.0.0-4+b2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace /opt/agentgit \
    && dpkg-query -W -f='${Package}=${Version}\n' > /opt/agentgit/packages.lock \
    && python --version > /opt/agentgit/python.lock

COPY src/agentgit/core/canonical.py src/agentgit/core/errors.py src/agentgit/core/paths.py /usr/local/lib/python3.12/site-packages/agentgit/core/
COPY src/agentgit/adapters/execution/protocol.py src/agentgit/adapters/execution/supervisor.py /usr/local/lib/python3.12/site-packages/agentgit/adapters/execution/

ENV PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp LANG=C.UTF-8
USER 10001:10003
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python", "-I", "-m", "agentgit.adapters.execution.supervisor"]
