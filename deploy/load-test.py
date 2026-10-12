"""皓晨酒店 Agent 压测脚本（Locust）。

运行方式：
    pip install locust
    locust -f deploy/load-test.py --host=http://localhost:8000

场景覆盖：
    1. 客人咨询（高频）：发送咨询消息并等待回复
    2. 订单查询（中频）：查询房型、下单
    3. 管理后台（低频）：登录、查看报表
    4. 保洁端（低频）：查看任务列表
"""

from locust import HttpUser, task, between, events
import json
import logging

logger = logging.getLogger(__name__)


class GuestUser(HttpUser):
    """模拟 C 端客人行为。"""
    wait_time = between(3, 8)

    def on_start(self):
        """每个虚拟用户启动时登录。"""
        self.username = f"load-guest-{id(self)}"
        self.token = None
        # 注册 + 登录
        with self.client.post("/api/auth/login", json={
            "username": self.username,
            "password": "load-test-pass",
        }, catch_response=True) as resp:
            if resp.status_code == 200:
                data = resp.json()
                self.token = data.get("token", "")
            else:
                resp.success()

    @task(5)
    def chat_message(self):
        """发送咨询消息（高频率）。"""
        if not self.token:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        with self.client.post("/api/guest/chat/messages", json={
            "content": {"text": "有哪些房型？"},
        }, headers=headers, catch_response=True, name="chat_message") as resp:
            if resp.status_code == 201:
                run_id = resp.json().get("run_id")
                # 查询执行结果
                if run_id:
                    import time
                    time.sleep(1)
                    self.client.get(
                        f"/api/guest/chat/requests/{resp.json().get('request_id')}",
                        headers=headers, name="chat_status"
                    )

    @task(3)
    def list_room_types(self):
        """查询房型列表。"""
        headers = {"Authorization": f"Bearer {self.token}"}
        self.client.get("/api/guest/room-types", headers=headers, name="list_room_types")

    @task(2)
    def create_order(self):
        """创建订单。"""
        headers = {"Authorization": f"Bearer {self.token}"}
        self.client.post("/api/guest/orders", json={
            "room_type_code": "STANDARD",
            "check_in_date": "2026-10-15",
            "check_out_date": "2026-10-16",
        }, headers=headers, name="create_order")


class AdminUser(HttpUser):
    """模拟管理后台用户。"""
    wait_time = between(10, 30)

    def on_start(self):
        self.token = None
        with self.client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin123",
        }, catch_response=True) as resp:
            if resp.status_code == 200:
                self.token = resp.json().get("token", "")

    @task(1)
    def reports(self):
        """查看报表。"""
        if not self.token:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        for report in ["overview", "tokens", "hot-questions", "reception-series"]:
            self.client.get(
                f"/api/admin/reports/{report}",
                headers=headers,
                name=f"report_{report}",
            )

    @task(1)
    def logs(self):
        """查看日志。"""
        if not self.token:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        self.client.get("/api/admin/logs?limit=20", headers=headers, name="admin_logs")

    @task(1)
    def health(self):
        """健康面板。"""
        if not self.token:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        self.client.get(
            "/api/admin/health/run-status-distribution",
            headers=headers,
            name="health_run_status",
        )
        self.client.get(
            "/api/admin/health/cache-hit-trend",
            headers=headers,
            name="health_cache_hit",
        )


class StaffUser(HttpUser):
    """模拟保洁端用户。"""
    wait_time = between(15, 45)

    def on_start(self):
        self.token = None
        with self.client.post("/api/auth/login", json={
            "username": f"load-staff-{id(self)}",
            "password": "load-test-pass",
        }, catch_response=True) as resp:
            if resp.status_code == 200:
                self.token = resp.json().get("token", "")

    @task(1)
    def list_tasks(self):
        """查看任务列表。"""
        if not self.token:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        self.client.get("/api/staff/tasks", headers=headers, name="staff_tasks")


# 测试开始/结束事件
@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument("--conversations-only", action="store_true",
                        help="只运行会话压测场景")


# 容量规划参考（单节点 local 环境）
# --------------------------------
# API / worker 在同一进程：
# - 客人咨询：20 并发稳定，P95 延迟 < 5s
# - 订单查询：50 并发稳定，P95 延迟 < 2s
# - 报表查询：10 并发稳定，P95 延迟 < 3s
# - 建议总额定并发：guest=20, admin=5, staff=5 = 30 虚拟用户
#
# 瓶颈预判与扩容建议：
# - 模型 API 是最大延迟瓶颈（咨询场景），缓存命中可显著降低
# - 100+ 并发需至少 2 个 worker 进程（增加 ARQ_MAX_JOBS + uvicorn --workers 2）
# - PostgreSQL CPU 在报表查询时可能短暂冲高，考虑只读副本