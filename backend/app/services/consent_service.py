"""PIPL 告知同意：用途清单、告知内容与同意记录服务。

边界：
- 同意记录只追加；当前状态取同一用途最新事件，撤回也是一条新事件。
- 告知版本与同意版本必须一致，避免客户端展示旧版政策后把同意记到新版本上。
- 敏感个人信息（人脸、证件）必须单独告知、单独同意；默认不采集。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.privacy import ConsentRecord
from app.repositories.consent_repository import ConsentRepository


@dataclass(frozen=True)
class ConsentPurpose:
    purpose: str
    title: str
    required: bool
    description: str


CONSENT_PURPOSES: tuple[ConsentPurpose, ...] = (
    ConsentPurpose(
        purpose="privacy_policy",
        title="隐私政策与个人信息处理规则",
        required=True,
        description="首次使用前展示；同意后才能创建账号、下单与发起对话。",
    ),
    ConsentPurpose(
        purpose="identity_verification",
        title="入住身份核验",
        required=False,
        description="仅在开通身份核验的门店单独征得；拒绝不影响基础咨询与预订。",
    ),
    ConsentPurpose(
        purpose="face_recognition",
        title="人脸识别",
        required=False,
        description="敏感个人信息，必须单独同意；只保存核验结果，不保存人脸原图。",
    ),
    ConsentPurpose(
        purpose="marketing",
        title="营销信息推送",
        required=False,
        description="可选；撤回后停止推送，不影响服务使用。",
    ),
)

_PURPOSES_BY_CODE = {item.purpose: item for item in CONSENT_PURPOSES}
CONSENT_PURPOSE_CODES = frozenset(_PURPOSES_BY_CODE)

DATA_CATEGORIES: tuple[str, ...] = (
    "账号与身份标识：微信 openid、用户 ID",
    "联系信息：手机号（仅在业务确有必要时采集）",
    "订单与入住：房型、入住/退房日期、金额、房间号",
    "对话内容：文本消息与工具调用审计（进入模型前按手机号/证件号/密码脱敏）",
    "长期记忆：经抽取的偏好（明确排除联系方式与证件号码）",
    "门锁凭证：仅 code_hash 与生效窗口，不保存明文密码",
)

RETENTION_POLICY: tuple[str, ...] = (
    "对话与运行审计：默认保留 180 天（以门店配置为准）",
    "同意与撤回记录：服务关系结束后保留 3 年用于举证",
    "数据库备份：本地与异地各保留 30 天",
    "门锁凭证：退房即吊销；哈希按审计要求保留，不保存明文",
)

CROSS_BORDER_POLICY = (
    "默认不向境外提供个人信息。Langfuse 默认按自建处理，外发第三方必须显式授权且强制脱敏；"
    "模型服务应选择境内节点，或完成安全评估/标准合同等出境合规手续后再启用。"
)


@dataclass(frozen=True)
class PrivacyNotice:
    policy_version: str
    purposes: tuple[ConsentPurpose, ...]
    data_categories: tuple[str, ...]
    retention: tuple[str, ...]
    cross_border: str


class UnknownConsentPurpose(ValueError):
    """用途不在告知清单内。"""


class StalePolicyVersion(ValueError):
    """客户端展示的政策版本与当前版本不一致。"""


class ConsentService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._repository = ConsentRepository(session)

    @property
    def policy_version(self) -> str:
        return self._settings.privacy_policy_version

    async def record(
        self,
        *,
        user_id: UUID,
        purpose: str,
        granted: bool,
        policy_version: str | None = None,
        channel: str = "api",
    ) -> ConsentRecord:
        if purpose not in _PURPOSES_BY_CODE:
            raise UnknownConsentPurpose(f"未知的同意用途: {purpose}")
        version = policy_version or self.policy_version
        if version != self.policy_version:
            raise StalePolicyVersion(f"隐私告知已更新，请重新展示 {self.policy_version} 版后再提交")

        latest = await self._repository.latest_for(user_id, purpose)
        if latest is not None and latest.granted == granted and latest.policy_version == version:
            return latest

        return await self._repository.append(
            user_id=user_id,
            purpose=purpose,
            policy_version=version,
            granted=granted,
            channel=channel,
        )

    async def current(self, user_id: UUID) -> dict[str, ConsentRecord]:
        return await self._repository.latest_for_user(user_id)


def build_notice(settings: Settings | None = None) -> PrivacyNotice:
    resolved = settings or get_settings()
    return PrivacyNotice(
        policy_version=resolved.privacy_policy_version,
        purposes=CONSENT_PURPOSES,
        data_categories=DATA_CATEGORIES,
        retention=RETENTION_POLICY,
        cross_border=CROSS_BORDER_POLICY,
    )
