"""S1 只提供存活和数据库就绪探测，业务路由在 S4 引入。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentgit import __version__
from agentgit.config import Settings
from agentgit.core.errors import AgentGitError
from agentgit.storage.database import probe_database


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(title="AgentGit", version=__version__)

    @app.exception_handler(AgentGitError)
    async def domain_error(request: Request, exc: AgentGitError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": exc.as_dict()})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        probe_database(config)
        return {"status": "ready"}

    return app
