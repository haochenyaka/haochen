"""客人房间访问用例：门锁凭证仅对「本人未终态订单所属房间」可见，且只回元数据（A6 + A2）。

不变量：
- 归属由订单（guest_id + room_id）判定；他人房间、无订单房间一律 403，不区分二者；
- 响应只含生效窗口与版本等元数据，不含 code_hash、设备凭证号等可取用材料（A2 明文零落库）；
- 越权读取不产生任何写副作用。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import NotFoundError, PermissionDeniedError
from app.models.domain import LockCredential
from app.repositories.guest_repository import GuestRepository
from app.repositories.lock_credential_repository import LockCredentialRepository
from app.repositories.order_repository import OrderRepository


class RoomAccessService:
    def __init__(self, session: AsyncSession) -> None:
        self._guests = GuestRepository(session)
        self._orders = OrderRepository(session)
        self._credentials = LockCredentialRepository(session)

    async def credential_metadata(self, user_id: UUID, room_no: str) -> LockCredential:
        guest = await self._guests.get_by_user_id(user_id)
        if guest is None:
            raise PermissionDeniedError("当前账号未绑定客人档案")
        order = await self._orders.get_active_for_guest_in_room(guest.id, room_no)
        if order is None:
            raise PermissionDeniedError("该房间不在当前账号的有效订单内")
        credential = await self._credentials.get_active_for_room(room_no)
        if credential is None:
            raise NotFoundError("lock_credential", room_no)
        return credential
