"""工位 1 Steer：安全边界发现待接替请求（P1 保留挂位，业务暂不启用）。

接管语义（新请求取代当前 Run 的模型调用）依赖取消/接力链路，随 T9 接入；
在此之前该工位默认关闭，配置可打开但不产生行为。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.middlewares.base import Station


@dataclass(frozen=True)
class SteerSettings:
    enabled: bool = False


class SteerStation(Station):
    key = "steer"
    label = "Steer 安全边界"

    def __init__(self, *, settings: SteerSettings | None = None) -> None:
        resolved = settings or SteerSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
