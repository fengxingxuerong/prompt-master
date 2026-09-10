"""进程内滑动窗口速率限制（服务层成本护栏的第二道闸）。

为什么需要它（H3 的遗留半边）：
H3 修了 token 鉴权 + CORS 收敛 + 入参长度上限，但那只拦"陌生人"。
局域网里一个拿到 token（或未设 token 时的本机页面）仍可以脚本循环打
`/api/optimize`，每个任务最多 480 次 target + 960 次评估调用 —— 鉴权挡不住
合法身份的高频滥用。这里在**提交入口**加限流：窗口内请求数超预算直接 429。

设计要点：
- **滑动窗口**而不是固定窗口：固定窗口在边界处可突发 2× 配额（窗口尾 + 下一窗口头），
  对"烧 API 余额"这种成本型资源，滑动窗口的保证更诚实。
- **按调用方计费**：key = API token（设了就用它，天然区分身份）否则客户端 IP。
  `POST /api/race` 的成本 = 参赛任务数（一次提交最多 10 个任务），不是 1 次请求。
- **默认开启**：`PM_RATE_LIMIT_PER_MIN`（默认 10 次/分钟，0 = 关闭）。成本型服务
  "默认不限流、靠用户记得开"等于没限流 —— 与 CORS 默认关闭是同一个安全立场。
- 线程安全：调度器本身是线程池，限流器会被多线程同时 acquire。
- 内存有界：key 池超过上限时清掉已滑出窗口的空桶，防止被伪造 IP 撑爆字典。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# key 池的防御上限：正常部署（单 token / 局域网几台机器）远达不到；
# 超过就做一次清扫，防止 X-Forwarded-For 之类的伪造源把字典撑到无界。
_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    """按 key 的滑动窗口限流器。

    acquire(key, cost) 尝试消耗 cost 个额度：
    - 成功 → (True, 0.0)
    - 超额 → (False, retry_after_seconds)：距窗口内最早一条记录滑出还需多久。
    """

    def __init__(self, max_events: int, window_seconds: float = 60.0):
        self.max_events = max(1, int(max_events))
        self.window_seconds = max(0.001, float(window_seconds))
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._denied = 0  # 观测指标：被拒次数（诊断"是谁在打满配额"）

    def acquire(self, key: str, cost: int = 1) -> tuple[bool, float]:
        now = time.monotonic()
        cost = max(1, int(cost))
        with self._lock:
            dq = self._events[key]
            cutoff = now - self.window_seconds
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) + cost > self.max_events:
                self._denied += 1
                retry_after = (dq[0] + self.window_seconds - now) if dq else self.window_seconds
                return False, max(0.0, retry_after)
            dq.extend(now for _ in range(cost))
            if len(self._events) > _MAX_KEYS:
                # 只清空桶，不动还有余留的 key：正在活跃的调用方不受影响
                for k in [k for k, v in self._events.items() if not v]:
                    del self._events[k]
            return True, 0.0

    @property
    def denied_count(self) -> int:
        with self._lock:
            return self._denied


def parse_rate_limit_env(raw: str | None, default: int = 10) -> tuple[int, str | None]:
    """解析 PM_RATE_LIMIT_PER_MIN。返回 (每分钟次数, 告警信息)。

    非法值告警回退默认而不是炸启动（与 A2 的容错立场一致）；
    返回 0 表示显式关闭限流（本地单人开发可以关）。
    """
    text = (raw or "").strip() or str(default)
    try:
        val = int(text)
    except ValueError:
        return default, f"PM_RATE_LIMIT_PER_MIN={raw!r} 不是整数，回退默认 {default}"
    if val < 0:
        return default, f"PM_RATE_LIMIT_PER_MIN={val} 为负数，回退默认 {default}"
    return val, None
