# 生成模拟螺纹钢1分钟K线数据（供回测使用）
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 生成1000根1分钟K线（2024年1月）
start_time = datetime(2024, 1, 1, 9, 0)
time_series = [start_time + timedelta(minutes=i) for i in range(1000)]
np.random.seed(42)  # 固定随机种子，结果可复现

# 生成价格数据（基于3800上下波动）
close = 3800 + np.cumsum(np.random.randn(1000) * 2)
open = close + np.random.randn(1000) * 1
high = np.maximum(open, close) + np.abs(np.random.randn(1000) * 1)
low = np.minimum(open, close) - np.abs(np.random.randn(1000) * 1)
volume = np.random.randint(5000, 20000, size=1000)

# 保存为CSV
df = pd.DataFrame({
    "datetime": time_series,
    "open": open.round(1),
    "high": high.round(1),
    "low": low.round(1),
    "close": close.round(1),
    "volume": volume
})
df["datetime"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
df.to_csv("rb_test_data.csv", index=False)
print("✅ 模拟数据生成完成！文件：rb_test_data.csv")