"""机器人渠道：配送下单与状态查询客户端。"""

from app.robot.client import (
    RobotClient,
    RobotDelivery,
    RobotError,
    RobotUnavailableError,
)

__all__ = ["RobotClient", "RobotDelivery", "RobotError", "RobotUnavailableError"]
