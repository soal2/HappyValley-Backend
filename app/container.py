"""Dependency injection container – creates runtime instances based on AGENT_BACKEND."""

import structlog

from app.agents.base import AgentRuntime, ImageGenerator
from app.agents.fake_image import FakeImageGenerator
from app.agents.fake_runtime import FakeAgentRuntime
from app.agents.http_image import HttpImageGenerator
from app.agents.http_runtime import HttpAgentRuntime
from app.config import get_settings
from app.media.local_storage import LocalMediaStorage
from app.media.storage import MediaStorage

logger = structlog.get_logger()


class Container:
    """Singleton container that holds runtime instances."""

    _instance: "Container | None" = None

    def __init__(self) -> None:
        settings = get_settings()
        backend = settings.AGENT_BACKEND

        if backend == "http":
            logger.info("container.init", backend="http", mode=settings.MODE)
            self.agent_runtime: AgentRuntime = HttpAgentRuntime()
            self.image_generator: ImageGenerator = HttpImageGenerator()
        elif backend == "fake":
            logger.info("container.init", backend="fake", mode=settings.MODE)
            self.agent_runtime = FakeAgentRuntime()
            self.image_generator = FakeImageGenerator()
        else:
            raise RuntimeError("AGENT_BACKEND 只允许 http 或 fake")

        self.media_storage: MediaStorage = LocalMediaStorage()

    @classmethod
    def get(cls) -> "Container":
        """Get or create the singleton container instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset container (useful for testing)."""
        cls._instance = None
