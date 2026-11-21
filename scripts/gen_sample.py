"""生成演示用样例 CSV 到 samples/ 目录。

运行: python scripts/gen_sample.py
"""
from __future__ import annotations

import csv
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


def main():
    os.makedirs(OUT, exist_ok=True)

    # 检测批次 1: 设备 DEV-A / DEV-B 全段各测一遍(时刻 100/101)
    with open(os.path.join(OUT, "inspect_b1.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_m", "end_m", "measured_at", "device_id", "run_id"])
        for i in range(10):
            w.writerow([i * 100, (i + 1) * 100, 100, "DEV-A", "RUN-A1"])
        for i in range(10):
            w.writerow([i * 100, (i + 1) * 100, 101, "DEV-B", "RUN-B1"])

    # 重铺批次: [300,600) 在时刻 150 完工 -> 此前跑次在该段失效
    with open(os.path.join(OUT, "repave_r1.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_m", "end_m", "completed_at"])
        w.writerow([300, 600, 150])

    # 检测批次 2: 重铺后 DEV-A 复测 [300,600)(时刻 200), 仍缺一台
    with open(os.path.join(OUT, "inspect_b2.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_m", "end_m", "measured_at", "device_id", "run_id"])
        w.writerow([300, 600, 200, "DEV-A", "RUN-A2"])

    # 检测批次 3: DEV-C 补测 [300,600)(时刻 210) -> 该段合格
    with open(os.path.join(OUT, "inspect_b3.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_m", "end_m", "measured_at", "device_id", "run_id"])
        w.writerow([300, 600, 210, "DEV-C", "RUN-C1"])

    print(f"样例已生成到 {OUT}/")
    print("建议上传顺序(车道任意, 如 L1): "
          "inspect_b1.csv(批 JC-1) -> repave_r1.csv(批 RP-1) -> "
          "inspect_b2.csv(批 JC-2) -> inspect_b3.csv(批 JC-3), 然后查询 0~1000 米")


if __name__ == "__main__":
    main()
