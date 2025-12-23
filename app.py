# 聚宽API + 上期所期货回测（Streamlit界面）
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import datetime, timedelta
from jqdatasdk import auth, get_price, get_security_info  # 聚宽API核心库

# ---------------------- 1. 聚宽API初始化 + 数据获取（核心） ----------------------
def jq_auth(jq_user, jq_password):
    """聚宽账号登录"""
    try:
        auth(jq_user, jq_password)
        return True
    except Exception as e:
        st.error(f"❌ 聚宽账号登录失败：{str(e)}")
        return False

def get_jq_shfe_data(
    symbol="RB9999.XSGE",  # 聚宽上期所代码（正确的聚宽格式）
    start_date="2023-01-01",
    end_date="2024-01-01",
    freq="1m"  # 1m=分钟线，1d=日线
):
    """
    从聚宽API获取上期所历史期货数据
    :param symbol: 聚宽期货代码（必须包含.XSGE后缀）
    :param start_date/end_date: 数据时间范围
    :param freq: 周期（1m=分钟线，1d=日线）
    :return: 标准化DataFrame（适配回测引擎）
    """
    # 校验合约是否存在
    try:
        sec_info = get_security_info(symbol)
        if sec_info.exchange not in ["XSGE", "SHFE"]:  # 聚宽交易所代码是XSGE
            st.warning(f"⚠️ {symbol} 不是上期所品种！当前交易所：{sec_info.exchange}")
    except Exception as e:
        st.error(f"❌ 聚宽中未找到合约 {symbol}，请检查代码格式。错误：{str(e)}")
        return None
    
    # 调用聚宽API获取K线数据
    try:
        klines = get_price(
            security=symbol,
            start_date=start_date,
            end_date=end_date,
            frequency=freq,  # 聚宽周期格式：1m=分钟，1d=日线
            fields=["open", "high", "low", "close", "volume"],  # 需要的字段
            skip_paused=False,
            fq=None  # 期货无需复权
        )
    except Exception as e:
        st.error(f"❌ 聚宽数据获取失败：{str(e)}")
        return None
    
    # 数据格式标准化（适配回测引擎）
    df = klines.reset_index()  # 把时间索引转为列
    df.rename(columns={"index": "datetime"}, inplace=True)  # 列名对齐
    # 时间格式处理
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    # 过滤空数据
    df = df.dropna(subset=["open", "high", "low", "close"])
    
    st.success(f"✅ 聚宽数据获取成功！{symbol} | {start_date} 至 {end_date} | 共 {len(df)} 条记录")
    return df

# ---------------------- 2. 上期所回测引擎（适配聚宽规则） ----------------------
class SHFEFuturesBacktest:
    def __init__(self, data, symbol="RB9999.XSGE", initial_capital=1000000):
        self.data = data.copy()
        self.symbol = symbol
        self.initial_capital = initial_capital
        
        # 从聚宽获取合约规则（自动适配，无需手动配置）
        try:
            sec_info = get_security_info(symbol)
            # 聚宽API返回的字段
            self.contract_size = getattr(sec_info, 'contract_multiplier', 10)  # 合约乘数，默认10
            self.margin_ratio = 0.10  # 聚宽未直接返回保证金，用上期所默认值（可自定义）
            self.commission_rate = 0.0001  # 手续费率（万分之一，可自定义）
            self.pricetick = getattr(sec_info, 'price_tick', 1)  # 最小变动价位，默认1
            st.info(f"✅ 合约信息：{symbol}，合约乘数：{self.contract_size}，最小变动价位：{self.pricetick}")
        except Exception as e:
            st.warning(f"⚠️ 获取合约信息失败，使用默认参数：{str(e)}")
            # 备用规则（聚宽获取失败时）
            if "RB" in symbol:
                self.contract_size = 10  # 螺纹钢10吨/手
                self.pricetick = 1
            elif "CU" in symbol:
                self.contract_size = 5  # 铜5吨/手
                self.pricetick = 10
            elif "AL" in symbol:
                self.contract_size = 5  # 铝5吨/手
                self.pricetick = 5
            else:
                self.contract_size = 10
                self.pricetick = 1
            
            self.margin_ratio = 0.10
            self.commission_rate = 0.0001
        
        # 账户参数
        self.slippage = self.pricetick * 2  # 滑点=2个最小变动价位
        self.cash = initial_capital
        self.margin = 0
        self.holdings = 0  # 持仓手数（+多单，-空单）
        self.total_asset = [initial_capital]
        self.trade_records = []
    
    def set_params(self, margin_ratio=None, commission_rate=None, slippage=None):
        """自定义参数覆盖默认值"""
        if margin_ratio is not None:
            self.margin_ratio = margin_ratio
        if commission_rate is not None:
            self.commission_rate = commission_rate
        if slippage is not None:
            self.slippage = slippage
    
    def calculate_ma(self, fast_window, slow_window):
        """计算双均线"""
        self.data["ma_fast"] = self.data["close"].rolling(fast_window).mean()
        self.data["ma_slow"] = self.data["close"].rolling(slow_window).mean()
        self.data = self.data.dropna()
    
    def run_backtest(self, fast_window, slow_window):
        """执行双均线策略回测"""
        self.calculate_ma(fast_window, slow_window)
        
        for idx, row in self.data.iterrows():
            price = row["close"]
            ma_fast = row["ma_fast"]
            ma_slow = row["ma_slow"]
            
            # 双均线策略逻辑：金叉开多，死叉开空
            if ma_fast > ma_slow and self.holdings <= 0:
                if self.holdings < 0:
                    self._close(price, row)  # 先平空仓
                self._open("long", price, row)  # 开多仓
            elif ma_fast < ma_slow and self.holdings >= 0:
                if self.holdings > 0:
                    self._close(price, row)  # 先平多仓
                self._open("short", price, row)  # 开空仓
            
            # 更新总资产（含浮盈）
            self._update_asset(price)
    
    def _open(self, direction, price, row):
        """开仓逻辑"""
        # 计算可开仓手数（基于保证金）
        max_vol = int(self.cash * 0.9 / (price * self.contract_size * self.margin_ratio))
        if max_vol <= 0:
            return
        
        # 滑点处理（对齐最小变动价位）
        exec_price = price + self.slippage if direction == "long" else price - self.slippage
        exec_price = round(exec_price / self.pricetick) * self.pricetick
        
        # 手续费（最低5元）
        commission = max(exec_price * max_vol * self.contract_size * self.commission_rate, 5)
        # 保证金计算
        margin = exec_price * max_vol * self.contract_size * self.margin_ratio
        
        # 更新账户状态
        self.cash -= (margin + commission)
        self.margin += margin
        self.holdings = max_vol if direction == "long" else -max_vol
        
        # 记录交易
        self.trade_records.append({
            "datetime": row["datetime"],
            "symbol": self.symbol,
            "action": "开仓",
            "direction": direction,
            "price": exec_price,
            "volume": max_vol,
            "commission": round(commission, 2),
            "margin": round(margin, 2)
        })
    
    def _close(self, price, row):
        """平仓逻辑"""
        if self.holdings == 0:
            return
        
        vol = abs(self.holdings)
        direction = "long" if self.holdings > 0 else "short"
        
        # 滑点处理
        exec_price = price - self.slippage if direction == "long" else price + self.slippage
        exec_price = round(exec_price / self.pricetick) * self.pricetick
        
        # 手续费（上期所平今仓更高，螺纹钢平今仓手续费×5）
        commission_rate = self.commission_rate * 5 if "RB" in self.symbol else self.commission_rate
        commission = max(exec_price * vol * self.contract_size * commission_rate, 5)
        
        # 盈亏计算
        if self.trade_records:
            # 查找最近一次同方向的开仓记录
            recent_opening = next((t for t in reversed(self.trade_records) 
                                if t["action"] == "开仓" and 
                                t["symbol"] == self.symbol and
                                ((direction == "long" and t["direction"] == "long") or
                                 (direction == "short" and t["direction"] == "short"))), None)
            open_price = recent_opening["price"] if recent_opening else price
        else:
            open_price = price
            
        profit = (exec_price - open_price) * vol * self.contract_size if direction == "long" else (open_price - exec_price) * vol * self.contract_size
        
        # 更新账户状态
        self.cash += (self.margin + profit - commission)
        self.margin = 0
        self.holdings = 0
        
        # 记录交易
        self.trade_records.append({
            "datetime": row["datetime"],
            "symbol": self.symbol,
            "action": "平仓",
            "direction": direction,
            "price": exec_price,
            "volume": vol,
            "commission": round(commission, 2),
            "profit": round(profit, 2)
        })
    
    def _update_asset(self, price):
        """更新总资产（含浮盈）"""
        floating_profit = 0
        if self.holdings != 0 and self.trade_records:
            # 查找最近一次同方向的开仓记录
            direction = "long" if self.holdings > 0 else "short"
            recent_opening = next((t for t in reversed(self.trade_records) 
                                if t["action"] == "开仓" and 
                                t["symbol"] == self.symbol and
                                ((direction == "long" and t["direction"] == "long") or
                                 (direction == "short" and t["direction"] == "short"))), None)
            if recent_opening:
                open_price = recent_opening["price"]
                if direction == "long":
                    floating_profit = (price - open_price) * self.holdings * self.contract_size
                else:
                    floating_profit = (open_price - price) * abs(self.holdings) * self.contract_size
        
        total = self.cash + self.margin + floating_profit
        self.total_asset.append(total)
    
    def _get_metrics(self):
        """计算回测指标"""
        if len(self.total_asset) <= 1:
            return {
                "总收益率(%)": 0,
                "年化收益率(%)": 0,
                "夏普比率": 0,
                "最大回撤(%)": 0,
                "总交易次数": 0,
                "初始资金(元)": self.initial_capital,
                "最终总资产(元)": self.initial_capital
            }
            
        asset = pd.Series(self.total_asset)
        total_return = (asset.iloc[-1] - self.initial_capital) / self.initial_capital * 100
        daily_return = asset.pct_change().dropna()
        
        # 年化收益率（上期所交易时间：每年250个交易日，每天4小时）
        annual_return = daily_return.mean() * 250 * 4 if len(daily_return) > 0 else 0
        # 夏普比率（无风险利率按0计算）
        sharpe = (daily_return.mean() / daily_return.std()) * np.sqrt(250 * 4) if (len(daily_return) > 0 and daily_return.std() != 0) else 0
        # 最大回撤
        max_dd = (asset / asset.cummax() - 1).min() * 100
        # 总交易次数（开平仓算1次）
        trade_count = len([t for t in self.trade_records if t["action"] == "开仓"])
        
        return {
            "总收益率(%)": round(total_return, 2),
            "年化收益率(%)": round(annual_return, 2),
            "夏普比率": round(sharpe, 2),
            "最大回撤(%)": round(max_dd, 2),
            "总交易次数": trade_count,
            "初始资金(元)": self.initial_capital,
            "最终总资产(元)": round(asset.iloc[-1], 2)
        }

# ---------------------- 3. Streamlit可视化界面 ----------------------
st.set_page_config(page_title="聚宽API - 上期所期货回测", page_icon="📊", layout="wide")

# 侧边栏：聚宽账号登录
st.sidebar.title("🔑 聚宽账号配置")
jq_user = st.sidebar.text_input("聚宽账号（手机号/邮箱）", placeholder="请输入你的聚宽账号")
jq_password = st.sidebar.text_input("聚宽密码", type="password", placeholder="请输入你的聚宽密码")
login_btn = st.sidebar.button("登录聚宽API", type="primary")

# 登录状态校验
if login_btn:
    if not jq_user or not jq_password:
        st.sidebar.error("❌ 账号/密码不能为空！")
    else:
        login_success = jq_auth(jq_user, jq_password)
        if login_success:
            st.sidebar.success("✅ 聚宽API登录成功！")
            st.session_state["jq_login"] = True
else:
    # 保留登录状态
    if "jq_login" not in st.session_state:
        st.sidebar.warning("⚠️ 请先登录聚宽API")
        st.stop()
    elif st.session_state["jq_login"]:
        st.sidebar.success("✅ 聚宽API已登录")

# 主界面标题
st.title("📊 上海期货交易所（上期所）期货回测")
st.markdown("### 基于聚宽API | 无需外接数据源 | 一键获取历史数据")

# 第一步：选择品种和时间范围
st.divider()
st.subheader("📁 数据配置（聚宽API获取）")
col1, col2, col3 = st.columns(3)
with col1:
    # 聚宽上期所品种列表（正确的聚宽代码格式）
    symbol_options = {
        "螺纹钢指数": "RB9999.XSGE",
        "螺纹钢主力": "RB8888.XSGE",
        "螺纹钢2410": "RB2410.XSGE",
        "铜指数": "CU9999.XSGE",
        "铜主力": "CU8888.XSGE",
        "铜2410": "CU2410.XSGE",
        "铝指数": "AL9999.XSGE",
        "铝主力": "AL8888.XSGE",
        "热轧卷板指数": "HC9999.XSGE",
        "不锈钢指数": "SS9999.XSGE",
        "白银指数": "AG9999.XSGE",
        "黄金指数": "AU9999.XSGE"
    }
    selected_name = st.selectbox("选择上期所品种", list(symbol_options.keys()))
    symbol = symbol_options[selected_name]
    
    # 显示合约信息
    st.caption(f"合约代码：{symbol}")
with col2:
    start_date = st.date_input("数据开始日期", datetime(2023, 1, 1))
    end_date = st.date_input("数据结束日期", datetime(2024, 1, 1))
with col3:
    freq_options = {"分钟线": "1m", "日线": "1d"}
    selected_freq_name = st.selectbox("数据周期", list(freq_options.keys()))
    freq = freq_options[selected_freq_name]

# 获取聚宽数据
get_data_btn = st.button("📥 一键获取聚宽历史数据", use_container_width=True, type="primary")
if get_data_btn:
    with st.spinner("🔄 正在从聚宽API获取数据..."):
        df = get_jq_shfe_data(
            symbol=symbol,
            start_date=str(start_date),
            end_date=str(end_date),
            freq=freq
        )
    if df is not None and len(df) > 0:
        st.dataframe(df.head(5), use_container_width=True)
        st.session_state["shfe_data"] = df  # 保存数据到会话
        st.session_state["current_symbol"] = symbol
else:
    # 校验数据是否存在
    if "shfe_data" not in st.session_state:
        st.info("ℹ️ 请先点击「一键获取聚宽历史数据」按钮")
        st.stop()
    df = st.session_state["shfe_data"]
    symbol = st.session_state.get("current_symbol", symbol)

# 第二步：回测参数配置
st.divider()
st.subheader("⚙️ 回测参数配置")
col1, col2 = st.columns(2)
with col1:
    fast_window = st.slider("📈 短期均线窗口", min_value=3, max_value=30, value=5, step=1)
    slow_window = st.slider("📉 长期均线窗口", min_value=10, max_value=60, value=10, step=1)
with col2:
    initial_capital = st.number_input("💰 初始资金（元）", min_value=100000, max_value=10000000, value=1000000, step=100000)
    margin_ratio = st.slider("📌 保证金比例", min_value=0.05, max_value=0.2, value=0.10, step=0.01)
    commission_rate = st.slider("💴 手续费率（万分之）", min_value=0.1, max_value=2.0, value=1.0, step=0.1)
    slippage = st.slider("🛶 滑点（点）", min_value=0.0, max_value=20.0, value=2.0, step=0.1)

# 第三步：执行回测
st.divider()
st.subheader("🚀 运行回测")
run_backtest_btn = st.button("开始回测", type="primary", use_container_width=True)
if run_backtest_btn:
    if df is None or len(df) == 0:
        st.error("❌ 没有有效数据，请先获取数据！")
        st.stop()
        
    # 初始化回测引擎
    backtest_engine = SHFEFuturesBacktest(
        data=df,
        symbol=symbol,
        initial_capital=initial_capital
    )
    # 设置自定义参数
    backtest_engine.set_params(
        margin_ratio=margin_ratio,
        commission_rate=commission_rate/10000,  # 万分之转小数
        slippage=slippage
    )
    
    # 执行回测
    with st.spinner("🔄 正在执行上期所期货回测，请稍候..."):
        backtest_engine.run_backtest(fast_window, slow_window)
        metrics = backtest_engine._get_metrics()
    
    # 展示回测结果
    st.success("✅ 回测完成！")
    st.divider()
    st.subheader("📈 回测结果汇总")
    
    # 核心指标卡片
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("总收益率", f"{metrics['总收益率(%)']} %")
    col_b.metric("年化收益率", f"{metrics['年化收益率(%)']} %")
    col_c.metric("夏普比率", metrics['夏普比率'])
    col_d.metric("最大回撤", f"{metrics['最大回撤(%)']} %")
    
    # 总资产变化曲线
    st.markdown("### 📊 总资产变化曲线")
    asset_df = pd.DataFrame({
        "时间": df.iloc[:len(backtest_engine.total_asset)]["datetime"].tolist() if len(df) >= len(backtest_engine.total_asset) else list(range(len(backtest_engine.total_asset))),
        "总资产（元）": backtest_engine.total_asset
    })
    fig = px.line(
        asset_df,
        x="时间",
        y="总资产（元）",
        title=f"{selected_name}（{symbol}）双均线策略总资产变化",
        template="plotly_white"
    )
    st.plotly_chart(fig, use_container_width=True)
    
    # 详细指标
    st.markdown("### 📋 详细回测指标")
    metrics_df = pd.DataFrame(metrics, index=[0])
    st.dataframe(metrics_df, use_container_width=True)
    
    # 交易记录
    if backtest_engine.trade_records:
        st.markdown("### 📝 交易记录")
        trade_df = pd.DataFrame(backtest_engine.trade_records)
        st.dataframe(trade_df, use_container_width=True)
        
        # 交易统计
        st.markdown("#### 📊 交易统计")
        if len(trade_df) > 0:
            winning_trades = trade_df[trade_df["action"] == "平仓"][trade_df["profit"] > 0]
            losing_trades = trade_df[trade_df["action"] == "平仓"][trade_df["profit"] <= 0]
            
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("总交易次数", len(trade_df[trade_df["action"] == "开仓"]))
            col2.metric("盈利交易数", len(winning_trades))
            col3.metric("亏损交易数", len(losing_trades))
            if len(winning_trades) + len(losing_trades) > 0:
                win_rate = len(winning_trades) / (len(winning_trades) + len(losing_trades)) * 100
                col4.metric("胜率", f"{win_rate:.1f}%")
    else:
        st.info("ℹ️ 本次回测无交易产生，可调整均线窗口重试。")

# 第四步：注意事项
st.divider()
st.subheader("📋 使用说明")
with st.expander("点击查看使用说明"):
    st.markdown("""
    ### 🎯 使用指南
    
    1. **聚宽账号**：需要聚宽（JoinQuant）账号才能获取数据
    2. **期货代码格式**：
       - 指数合约：`RB9999.XSGE`（螺纹钢指数）
       - 主力合约：`RB8888.XSGE`（螺纹钢主力）
       - 具体合约：`RB2410.XSGE`（螺纹钢2410合约）
    
    3. **交易所后缀**：
       - 上期所：`.XSGE`
       - 大商所：`.XDCE`
       - 郑商所：`.XZCE`
       - 中金所：`.CCFX`
    
    4. **回测参数说明**：
       - 保证金比例：默认10%（螺纹钢标准）
       - 手续费率：默认万分之一
       - 滑点：默认2个最小变动价位
    
    5. **注意事项**：
       - 聚宽API有调用频率限制
       - 期货数据需要聚宽VIP权限获取完整历史数据
       - 回测结果仅供参考，不构成投资建议
    """)

# 第五步：数据统计
st.divider()
st.subheader("📈 数据统计")
if df is not None and len(df) > 0:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("数据条数", len(df))
    col2.metric("起始时间", str(df["datetime"].iloc[0]))
    col3.metric("结束时间", str(df["datetime"].iloc[-1]))
    col4.metric("平均成交量", f"{df['volume'].mean():.0f}")
    
    # 价格走势图
    st.markdown("### 📊 价格走势图")
    fig = px.line(df.tail(100), x="datetime", y=["open", "high", "low", "close"], 
                  title=f"{symbol} 价格走势（最近100条）", template="plotly_white")
    st.plotly_chart(fig, use_container_width=True)