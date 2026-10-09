"""Agent 基类定义"""
import logging
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Agent 执行结果"""
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_time_ms: int = 0


class BaseAgent(ABC):
    """Agent 基类"""
    
    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"lvco.agent.{name}")
    
    @abstractmethod
    async def execute(self, **kwargs) -> AgentResult:
        """执行 Agent 任务"""
        pass
    
    @abstractmethod
    async def stream_execute(self, **kwargs) -> AsyncIterator[dict]:
        """流式执行 Agent 任务"""
        pass
    
    def log_info(self, msg: str, **kwargs):
        """记录 info 日志"""
        if kwargs:
            self.logger.info(f"[{self.name}] {msg} | {kwargs}")
        else:
            self.logger.info(f"[{self.name}] {msg}")
    
    def log_error(self, msg: str, **kwargs):
        """记录 error 日志"""
        if kwargs:
            self.logger.error(f"[{self.name}] {msg} | {kwargs}")
        else:
            self.logger.error(f"[{self.name}] {msg}")
    
    def log_debug(self, msg: str, **kwargs):
        """记录 debug 日志"""
        if kwargs:
            self.logger.debug(f"[{self.name}] {msg} | {kwargs}")
        else:
            self.logger.debug(f"[{self.name}] {msg}")
    
    def log_warning(self, msg: str, **kwargs):
        """记录 warning 日志"""
        if kwargs:
            self.logger.warning(f"[{self.name}] {msg} | {kwargs}")
        else:
            self.logger.warning(f"[{self.name}] {msg}")
    
    def log_exception(self, msg: str, **kwargs):
        """记录异常日志（包含堆栈信息）"""
        if kwargs:
            self.logger.exception(f"[{self.name}] {msg} | {kwargs}")
        else:
            self.logger.exception(f"[{self.name}] {msg}")
    
    @contextmanager
    def track_execution(self, operation: str):
        """跟踪执行时间的上下文管理器"""
        start_time = time.time()
        self.log_info(f"{operation}_started")
        try:
            yield
            elapsed_ms = int((time.time() - start_time) * 1000)
            self.log_info(f"{operation}_completed", execution_time_ms=elapsed_ms)
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            self.log_exception(f"{operation}_failed", execution_time_ms=elapsed_ms, error=str(e))
            raise
