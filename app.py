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
    symbol="RB9999   ．超高频   ．超高频",  # 聚宽上期所代码
    start_date="2023-01-01",
    end_date="2024-01-01",
    freq="1m"  # 1m=分钟线，1d=日线
):
    """
    从聚宽API获取上期所历史期货数据
    :param symbol: 聚宽期货代码（如RB9999   ．超高频   ．超高频   ．超高频   ．超高频   ．超高频）
    :param start_date/end_date: 数据时间范围
    :param freq: 周期（1m=分钟线，1d=日线）
    :return: 标准化DataFrame（适配回测引擎）
    """
    # 校验合约是否存在
    try:
        sec_info = get_security_info(symbol)
        if sec_info.exchange != "SHFE":
            st.error   错误(f"❌ {symbol} 不是上期所品种！")
            return None
    except:
        st.error(f"❌ 聚宽中未找到合约 {symbol}，请检查代码格式（如RB9999   ．超高频   ．超高频   ．超高频）")
        return None
    
    # 调用聚宽API获取K线数据
    try:   试一试:
        klines = get_price(
            security=symbol,
            start_date=start_date,
            end_date=end_date,
            frequency=freq,  # 聚宽周期格式：1m=分钟，1d=日线
            fields=["open", "high", "low", "close", "volume"],  # 需要的字段
            skip_paused=False,
            fq=None  # 期货无需复权
        )
    except Exception as e:   例外情况如下：
        st.error(f"❌ 聚宽数据获取失败：{str(e)}")
        return None   回来没有
    
    # 数据格式标准化（适配回测引擎）
    df = klines.reset_index()  # 把时间索引转为列
    df.rename(columns={"index": "datetime"}, inplace=True)  # 列名对齐
    # 时间格式处理
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    # 过滤空数据
    df = df.dropna(subset=["open", "high", "low", "close"])
    
    st.success(f"✅ 聚宽数据获取成功！{symbol} | {start_date} 至 {end_date} | 共 {len(df)} 条记录")
    return   返回 df

# ---------------------- 2. 上期所回测引擎（适配聚宽规则） ----------------------
class SHFEFuturesBacktest:
    def __init__(self, data, symbol="RB9999   ．超高频", initial_capital=1000000):
        self.data = data.copy()
        self.symbol = symbol
        self.initial_capital = initial_capital
        
        # 从聚宽获取合约规则（自动适配，无需手动配置）
        try:
            sec_info = get_security_info(symbol)
            self.contract_size = sec_info.contract_multiplier  # 合约乘数（螺纹钢10吨/手）
            self.margin_ratio = 0.10  # 聚宽未直接返回保证金，用上期所默认值（可自定义）
            self.commission_rate = 0.0001  # 手续费率（万分之一，可自定义）
            self.pricetick = sec_info.price_tick  # 最小变动价位（螺纹钢1元/吨）
        except:
            # 备用规则（聚宽获取失败时）
            self.contract_size = 10 if "RB" in symbol else 5  # 螺纹钢10吨，铜5吨
            self.margin_ratio = 0.10
            self.commission_rate = 0.0001
            self.pricetick = 1 if "RB" in symbol else 10
        
        # 账户参数
        self.slippage = self.pricetick * 2  # 滑点=2个最小变动价位
        self.cash = initial_capital
        self.margin = 0
        self.holdings = 0  # 持仓手数（+多单，-空单）
        self.total_asset = [initial_capital]
        self.trade_records = []
    
    def set_params(self, margin_ratio=None, commission_rate=None, slippage=None):
        """自定义参数覆盖默认值"""
        self.margin_ratio = margin_ratio if margin_ratio else self.margin_ratio
        self.commission_rate = commission_rate if commission_rate else self.commission_rate
        self.slippage = slippage if slippage else self.slippage
    
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
        open_price = self.trade_records[-1]["price"] if self.trade_records else price
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
        if self.holdings != 0:
            open_price = self.trade_records[-1]["price"] if self.trade_records else price
            if self.holdings > 0:
                floating_profit = (price - open_price) * self.holdings * self.contract_size
            else:
                floating_profit = (open_price - price) * abs(self.holdings) * self.contract_size
        total = self.cash + self.margin + floating_profit
        self.total_asset.append(total)
    
    def _get_metrics(self):
        """计算回测指标"""
        asset = pd.Series(self.total_asset)
        total_return = (asset.iloc[-1] - self.initial_capital) / self.initial_capital * 100
        daily_return = asset.pct_change().dropna()
        
        # 年化收益率（上期所交易时间：每年250个交易日，每天4小时）
        annual_return = daily_return.mean() * 250 * 4 if len(daily_return) > 0 else 0
        # 夏普比率（无风险利率按0计算）
        sharpe = (daily_return.mean() / daily_return.std()) * np.sqrt(250 * 4) if   如果 (len(daily_return) > 0 and daily_return.std() != 0) else 0
        # 最大回撤
        max_dd = (asset / asset.cummax() - 1).min() * 100
        # 总交易次数（开平仓算1次）
        trade_count = len(self.trade_records) // 2
        
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

# 主界面标题
st.title("📊 上海期货交易所（上期所）期货回测")
st.markdown("### 基于聚宽API | 无需外接数据源 | 一键获取历史数据")

# 第一步：选择品种和时间范围
st.divider()
st.subheader("📁 数据配置（聚宽API获取）")
col1, col2, col3 = st.columns(3)
with col1:
    # 聚宽上期所品种列表
    symbol_options = {
        "螺纹钢主力": "RB9999   ．超高频   ．超高频",
        "螺纹钢2410": "RB2410   ．超高频",
        "铜主力": "CU9999   ．超高频",
        "铜2410": "CU2410   ．超高频",
        "铝主力": "AL9999   ．超高频"
    }
    selected_name = st.selectbox("选择上期所品种", list(symbol_options.keys()))
    symbol = symbol_options[selected_name]
with col2:
    start_date = st.date_input("数据开始日期", datetime(2023, 1, 1))
    end_date = st.date_input("数据结束日期", datetime(2024, 1, 1))
with col3:
    freq_options = {"分钟线": "1m", "日线": "1d"}
    selected_freq_name = st.selectbox("数据周期", list(freq_options.keys()))
    freq = freq_options[selected_freq_name]

# 获取聚宽数据
get_data_btn = st.button("📥 一键获取聚宽历史数据", use_container_width=True)
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
else:
    # 校验数据是否存在
    if "shfe_data" not in st.session_state:
        st.info("ℹ️ 请先点击「一键获取聚宽历史数据」按钮")
        st.stop()
    df = st.session_state["shfe_data"]

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
        "时间": df.iloc[:len(backtest_engine.total_asset)-1]["datetime"],
        "总资产（元）": backtest_engine.total_asset[1:]
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
    st.dataframe(pd.DataFrame(metrics, index=[0]), use_container_width=True)
    
    # 交易记录
    if backtest_engine.trade_records:
        st.markdown("### 📝 交易记录")
        trade_df = pd.DataFrame(backtest_engine.trade_records)
        st.dataframe(trade_df, use_container_width=True)
    else:

        st.info("ℹ️ 本次回测无交易产生，可调整均线窗口重试。")
