"""一次性探针 v5（零调用）：可估性门槛 N 由 κ 的标准误反解，再用蒙特卡洛核对。

要定的判据：一张考卷至少要有几条"人工达标"的正例，报出来的 κ 才有意义。
用 Fleiss 的 κ 标准误公式反解总条数 n，再拿模拟验证公式没骗人。
"""

import math
import random
import sys


def kappa_se(theta: float, theta0: float, n: int) -> float:
    """Fleiss (1971) 大样本近似：SE(kappa) = sqrt[ theta(1-theta) / (n (1-theta0)^2) ]。"""
    if n <= 0 or theta0 >= 1:
        return float("inf")
    return math.sqrt(theta * (1 - theta) / (n * (1 - theta0) ** 2))


def theta_from_kappa(kappa: float, theta0: float) -> float:
    """由 kappa = (theta-theta0)/(1-theta0) 反解观测一致率 theta。"""
    return kappa * (1 - theta0) + theta0


def required_n(pi: float, kappa_true: float, half_width: float) -> int:
    """给定正例占比 pi、真 kappa、想要的 95% CI 半宽，反解最小 n。

    theta0（偶然一致率）用"两侧都按 pi 独立分布"下的值近似：pi^2+(1-pi)^2。
    """
    theta0 = pi * pi + (1 - pi) ** 2
    theta = theta_from_kappa(kappa_true, theta0)
    for n in range(4, 4001):
        if 1.96 * kappa_se(theta, theta0, n) <= half_width:
            return n
    return -1


def sim(pi: float, kappa_true: float, n: int, reps: int, seed: int) -> tuple[float, float, float]:
    """蒙特卡洛核对：按 pi/kappa_true 生成 2x2 表，量实测 kappa 的 90% 分位宽度。"""
    theta0 = pi * pi + (1 - pi) ** 2
    theta = theta_from_kappa(kappa_true, theta0)
    # 对称构造：人工与评委的正例边际都取 pi
    d = (theta - theta0) / 2.0
    p11 = pi * pi + d
    p10 = p01 = pi * (1 - pi) - d
    p00 = (1 - pi) ** 2 + d
    probs = [p11, p00, p10, p01]
    if min(probs) < -1e-9:  # 该 (pi, kappa) 组合不合法，clip 后归一
        probs = [max(0.0, p) for p in probs]
        s = sum(probs)
        probs = [p / s for p in probs]
    rnd = random.Random(seed)
    ks = []
    for _ in range(reps):
        u = rnd.random()
        c = 0.0
        cell = 3
        for i, p in enumerate(probs):
            c += p
            if u <= c:
                cell = i
                break
        tp = tn = fp = fn = 0
        for _ in range(n):
            u = rnd.random()
            c = 0.0
            for i, p in enumerate(probs):
                c += p
                if u <= c:
                    cell = i
                    break
            if cell == 0:
                tp += 1
            elif cell == 1:
                tn += 1
            elif cell == 2:
                fn += 1
            else:
                fp += 1
        po = (tp + tn) / n
        pe = ((tp + fp) * (tp + fn) + (tn + fn) * (fp + tn)) / (n * n)
        if pe < 1:
            ks.append((po - pe) / (1 - pe))
    ks.sort()
    return ks[int(0.05 * len(ks))], ks[len(ks) // 2], ks[int(0.95 * len(ks))]


def main() -> int:
    print("目标：95% CI 半宽 <= 0.20（两个读数差在这个宽度内不构成「量具变了」）")
    for kappa_true in (0.4, 0.6, 0.8):
        parts = []
        for pi in (0.10, 0.20, 0.30):
            n = required_n(pi, kappa_true, 0.20)
            lo, med, hi = sim(pi, kappa_true, max(n, 6), 400, 11)
            parts.append(f"正例占比{pi:.0%}: n={n:4d}（正例 {pi*n:4.0f} 条）模拟 kappa={med:+.2f} 90%区间 {lo:+.2f}~{hi:+.2f}")
        print(f"kappa_true={kappa_true:+.1f}  " + "　".join(parts))
    print()
    print("当前实配：calib48 的 owner 子集 n=22、正例 1 条（占比 4.5%）；cal_swap n=10、正例 4 条（40%）")
    for n, pos in ((22, 1), (10, 4), (45, 3)):
        pi = pos / n
        theta0 = pi * pi + (1 - pi) ** 2
        se = kappa_se(theta_from_kappa(0.6, theta0), theta0, n)
        print(f"  n={n} 正例={pos}（{pi:.1%}）：即便真实 kappa=0.6，半宽也有 {1.96*se:.3f} —— 反解需要的 n={required_n(pi,0.6,0.20)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
