"""视觉审核客户端：mock 实现（人脸比对、工牌 OCR、工服检测）。

P6 使用 mock：face_score ≥ threshold + badge_ok + uniform_ok → auto_pass；
任一不通过 → manual_review。真实视觉服务在 P9 集成替换。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class VisionResult:
    """视觉审核结果。"""

    face_score: Decimal  # 0–1, 1=完全匹配
    badge_ok: bool
    uniform_ok: bool
    raw: dict[str, Any]


DEFAULT_FACE_THRESHOLD = Decimal("0.8000")
DEFAULT_BADGE_CHECK = True
DEFAULT_UNIFORM_CHECK = True


class VisionClient:
    """视觉审核 mock：配置阈值与故障注入。"""

    def __init__(
        self,
        *,
        face_threshold: Decimal = DEFAULT_FACE_THRESHOLD,
        check_badge: bool = DEFAULT_BADGE_CHECK,
        check_uniform: bool = DEFAULT_UNIFORM_CHECK,
    ) -> None:
        self._face_threshold = face_threshold
        self._check_badge = check_badge
        self._check_uniform = check_uniform

    @classmethod
    def from_settings(cls) -> VisionClient:
        return cls()

    async def aclose(self) -> None:
        pass

    async def check_face(self, photo_path: str, reference_path: str | None = None) -> VisionResult:
        """人脸 1:1 比对：mock 返回最高分 + 通过。"""
        return self._ok(face_score=Decimal("0.9500"))

    async def check_badge(self, photo_path: str) -> VisionResult:
        """工牌 OCR 校验：mock 返回通过。"""
        return self._ok()

    async def check_uniform(self, photo_path: str) -> VisionResult:
        """工服检测：mock 返回通过。"""
        return self._ok()

    async def full_check(
        self,
        photo_path: str,
        *,
        face_reference: str | None = None,
    ) -> VisionResult:
        """全量视觉审核（人脸 + 工牌 + 工服）。"""
        face = await self.check_face(photo_path, reference_path=face_reference)
        badge = await self.check_badge(photo_path) if self._check_badge else self._ok()
        uniform = await self.check_uniform(photo_path) if self._check_uniform else self._ok()
        return VisionResult(
            face_score=face.face_score,
            badge_ok=badge.badge_ok,
            uniform_ok=uniform.uniform_ok,
            raw={
                "face_score": float(face.face_score),
                "badge_ok": badge.badge_ok,
                "uniform_ok": uniform.uniform_ok,
            },
        )

    # ---- config helpers ----

    @property
    def face_threshold(self) -> Decimal:
        return self._face_threshold

    @face_threshold.setter
    def face_threshold(self, value: Decimal) -> None:
        self._face_threshold = value

    def auto_pass(self, result: VisionResult) -> bool:
        """根据阈值判断是否自动通过。"""
        if result.face_score < self._face_threshold:
            return False
        if self._check_badge and not result.badge_ok:
            return False
        if self._check_uniform and not result.uniform_ok:
            return False
        return True

    @staticmethod
    def _ok(face_score: Decimal = Decimal("0.9500")) -> VisionResult:
        return VisionResult(
            face_score=face_score,
            badge_ok=True,
            uniform_ok=True,
            raw={},
        )
