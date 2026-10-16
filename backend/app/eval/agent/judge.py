"""智能体评估之答案评判（可选）：LLM-as-judge 评估回答质量。

与 kb_judge 设计一致：未配置模型密钥时不构建评判器，评估退化为工具链指标。
"""

from __future__ import annotations

from app.agents.models import build_chat_model
from app.config import Settings, get_settings


class AgentAnswerJudge:
    """答案质量评判器（占位）：可用时扩展 LLM 评分，当前返回 None。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model_name: str | None = None
        if self._settings.model_api_key and self._settings.model_name:
            self._model_name = self._settings.model_name
            build_chat_model()  # 验证连通性

    @property
    def model_name(self) -> str | None:
        return self._model_name

    @property
    def available(self) -> bool:
        return self._model_name is not None


def build_agent_judge(settings: Settings | None = None) -> AgentAnswerJudge | None:
    """有模型密钥时构建评判器；开发态无密钥返回 None。"""
    judge = AgentAnswerJudge(settings or get_settings())
    return judge if judge.available else None
