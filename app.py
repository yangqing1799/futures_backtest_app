# 聚宽API + 上期所期货回测（Streamlit界面）
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import datetime, timedelta
from jqdatasdk import auth, get_price, get_security_info, query, valuation  # 聚宽API核心库

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
    symbol="RB8888.XSGE",  # 聚宽上期所代码（正确的聚宽格式）
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
        if sec_info is None:
            st.error(f"❌ 聚宽中未找到合约 {symbol}，请检查代码格式。")
            return None
        
        # 检查是否期货品种
        if sec_info.type != 'futures':
            st.warning(f"⚠️ {symbol} 不是期货品种！")
    except Exception as e:
        st.error(f"❌ 获取合约信息失败：{str(e)}")
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
    
    if klines is None or len(klines) == 0:
        st.warning(f"⚠️ 未获取到 {symbol} 的数据，请检查时间范围或合约代码")
        return None
    
    # 数据格式标准化（适配回测引擎）
    df = klines.reset_index()  # 把时间索引转为列
    df.rename(columns={"index": "datetime"}, inplace=True)  # 列名对齐
    # 时间格式处理
    df["datetime"] = pd.to_datetime(df["datetime"])
    # 过滤空数据
    df = df.dropna(subset=["open", "high", "low", "close"])
    
    st.success(f"✅ 聚宽数据获取成功！{symbol} | {start_date} 至 {end_date} | 共 {len(df)} 条记录")
    return df

# ---------------------- 2. 上期所回测引擎（适配聚宽规则） ----------------------
class SHFEFuturesBacktest:
    def __init__(self, data, symbol="RB8888.XSGE", initial_capital=1000000):
        self.data = data.copy()
        self.symbol = symbol
        self.initial_capital = initial_capital
        
        # 从品种代码判断合约规则
        symbol_prefix = symbol[:2]  # 获取品种代码前2位
        
        # 常见期货品种的合约规格
        futures_specs = {
            "RB": {"contract_size": 10, "pricetick": 1, "name": "螺纹钢"},  # 10吨/手，最小变动1元
            "HC": {"contract_size": 10, "pricetick": 1, "name": "热轧卷板"},
            "CU": {"contract_size": 5, "pricetick": 10, "name": "铜"},  # 5吨/手，最小变动10元
            "AL": {"contract_size": 5, "pricetick": 5, "name": "铝"},  # 5吨/手，最小变动5元
            "ZN": {"contract_size": 5, "pricetick": 5, "name": "锌"},
            "PB": {"contract_size": 5, "pricetick": 5, "name": "铅"},
            "NI": {"contract_size": 1, "pricetick": 10, "name": "镍"},  # 1吨/手
            "SN": {"contract_size": 1, "pricetick": 10, "name": "锡"},
            "AU": {"contract_size": 1000, "pricetick": 0.02, "name": "黄金"},  # 1000克/手
            "AG": {"contract_size": 15, "pricetick": 1, "name": "白银"},  # 15千克/手
            "RU": {"contract_size": 10, "pricetick": 5, "name": "橡胶"},
            "BU": {"contract_size": 10, "pricetick": 2, "name": "沥青"},
            "FU": {"contract_size": 10, "pricetick": 1, "name": "燃料油"},
            "SP": {"contract_size": 10, "pricetick": 2, "name": "纸浆"},
        }
        
        # 设置合约规格
        if symbol_prefix in futures_specs:
            spec = futures_specs[symbol_prefix]
            self.contract_size = spec["contract_size"]
            self.pricetick = spec["pricetick"]
            self.futures_name = spec["name"]
        else:
            # 默认值
            self.contract_size = 10
            self.pricetick = 1
            self.futures_name = "未知品种"
        
        # 默认参数
        self.margin_ratio = 0.10
        self.commission_rate = 0.0001
        
        # 账户参数
        self.slippage = self.pricetick * 2  # 滑点=2个最小变动价位
        self.cash = initial_capital
        self.margin = 0
        self.holdings = 0  # 持仓手数（+多单，-空单）
        self.total_asset = [initial_capital]
        self.trade_records = []
        
        st.info(f"📊 合约信息：{self.futures_name} ({symbol})，"
                f"合约乘数：{self.contract_size}吨/手，"
                f"最小变动价位：{self.pricetick}元")
    
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
        margin_per_contract = price * self.contract_size * self.margin_ratio
        max_vol = int(self.cash * 0.9 / margin_per_contract)
        
        if max_vol <= 0:
            return
        
        # 滑点处理（对齐最小变动价位）
        if direction == "long":
            exec_price = price + self.slippage
        else:
            exec_price = price - self.slippage
        exec_price = round(exec_price / self.pricetick) * self.pricetick
        
        # 手续费（最低5元）
        commission = exec_price * max_vol * self.contract_size * self.commission_rate
        commission = max(commission, 5)
        
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
        if direction == "long":
            exec_price = price - self.slippage
        else:
            exec_price = price + self.slippage
        exec_price = round(exec_price / self.pricetick) * self.pricetick
        
        # 手续费（上期所平今仓更高，螺纹钢平今仓手续费×5）
        commission_rate = self.commission_rate * 5 if "RB" in self.symbol else self.commission_rate
        commission = exec_price * vol * self.contract_size * commission_rate
        commission = max(commission, 5)
        
        # 查找最近一次同方向的开仓记录
        open_price = 0
        for trade in reversed(self.trade_records):
            if (trade["action"] == "开仓" and 
                trade["symbol"] == self.symbol and
                ((direction == "long" and trade["direction"] == "long") or
                 (direction == "short" and trade["direction"] == "short"))):
                open_price = trade["price"]
                break
        
        if open_price == 0:
            open_price = price
            
        # 盈亏计算
        if direction == "long":
            profit = (exec_price - open_price) * vol * self.contract_size
        else:
            profit = (open_price - exec_price) * vol * self.contract_size
        
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
            open_price = 0
            for trade in reversed(self.trade_records):
                if (trade["action"] == "开仓" and 
                    trade["symbol"] == self.symbol and
                    ((direction == "long" and trade["direction"] == "long") or
                     (direction == "short" and trade["direction"] == "short"))):
                    open_price = trade["price"]
                    break
            
            if open_price > 0:
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
        
        # 计算日收益率序列
        if len(asset) > 1:
            returns = asset.pct_change().dropna()
            if len(returns) > 0:
                annual_return = returns.mean() * 252 * 100  # 年化收益率
                sharpe = returns.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else 0
            else:
                annual_return = 0
                sharpe = 0
        else:
            annual_return = 0
            sharpe = 0
            
        # 最大回撤
        if len(asset) > 0:
            cummax = asset.cummax()
            drawdown = (asset - cummax) / cummax * 100
            max_dd = drawdown.min()
        else:
            max_dd = 0
            
        # 总交易次数（开仓次数）
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

# 初始化session状态
if "jq_login" not in st.session_state:
    st.session_state["jq_login"] = False
if "shfe_data" not in st.session_state:
    st.session_state["shfe_data"] = None
if "current_symbol" not in st.session_state:
    st.session_state["current_symbol"] = "RB8888.XSGE"

login_btn = st.sidebar.button("登录聚宽API", type="primary")

# 登录状态校验
if login_btn:
    if not jq_user or not jq_password:
        st.sidebar.error("❌ 账号/密码不能为空！")
    else:
        with st.spinner("正在登录聚宽API..."):
            login_success = jq_auth(jq_user, jq_password)
        if login_success:
            st.sidebar.success("✅ 聚宽API登录成功！")
            st.session_state["jq_login"] = True
        else:
            st.session_state["jq_login"] = False
elif not st.session_state["jq_login"]:
    st.sidebar.warning("⚠️ 请先登录聚宽API")
    st.info("请先在左侧输入聚宽账号密码并点击登录")
    st.stop()

# 主界面标题
st.title("📊 上海期货交易所（上期所）期货回测")
st.markdown("### 基于聚宽API | 无需外接数据源 | 一键获取历史数据")

# 第一步：选择品种和时间范围
st.divider()
st.subheader("📁 数据配置（聚宽API获取）")
col1, col2, col3 = st.columns(3)
with col1:
    # 聚宽上期所品种列表（使用主力连续合约）
    symbol_options = {
        "螺纹钢主力": "RB8888.XSGE",
        "螺纹钢指数": "RB9999.XSGE",
        "铜主力": "CU8888.XSGE",
        "铜指数": "CU9999.XSGE",
        "铝主力": "AL8888.XSGE",
        "铝指数": "AL9999.XSGE",
        "热轧卷板主力": "HC8888.XSGE",
        "不锈钢主力": "SS8888.XSGE",
        "白银主力": "AG8888.XSGE",
        "黄金主力": "AU8888.XSGE",
        "锌主力": "ZN8888.XSGE",
        "铅主力": "PB8888.XSGE",
        "镍主力": "NI8888.XSGE",
        "锡主力": "SN8888.XSGE",
        "橡胶主力": "RU8888.XSGE",
        "沥青主力": "BU8888.XSGE",
        "燃料油主力": "FU8888.XSGE",
        "纸浆主力": "SP8888.XSGE"
    }
    selected_name = st.selectbox("选择上期所品种", list(symbol_options.keys()), index=0)
    symbol = symbol_options[selected_name]
    st.caption(f"合约代码：{symbol}")
    
    # 自定义合约代码输入
    st.markdown("---")
    custom_symbol = st.text_input("或输入自定义合约代码", placeholder="如：RB8888.XSGE")
    if custom_symbol:
        symbol = custom_symbol
        st.caption(f"使用自定义合约：{symbol}")
    
with col2:
    start_date = st.date_input("数据开始日期", datetime(2023, 1, 1))
    end_date = st.date_input("数据结束日期", datetime(2024, 1, 1))
    
    # 时间范围验证
    if start_date >= end_date:
        st.error("❌ 结束日期必须晚于开始日期")
        st.stop()
        
with col3:
    freq_options = {"日线": "1d", "分钟线": "1m"}
    selected_freq_name = st.selectbox("数据周期", list(freq_options.keys()), index=0)
    freq = freq_options[selected_freq_name]
    
    st.info(f"💡 提示：\n- 主力合约：8888结尾\n- 指数合约：9999结尾\n- 具体合约：如RB2410.XSGE")

# 获取聚宽数据
get_data_btn = st.button("📥 一键获取聚宽历史数据", use_container_width=True, type="primary")
if get_data_btn:
    with st.spinner(f"🔄 正在从聚宽API获取 {symbol} 数据..."):
        df = get_jq_shfe_data(
            symbol=symbol,
            start_date=str(start_date),
            end_date=str(end_date),
            freq=freq
        )
    if df is not None and len(df) > 0:
        st.dataframe(df.head(5), use_container_width=True)
        st.session_state["shfe_data"] = df
        st.session_state["current_symbol"] = symbol
        
        # 显示数据统计
        st.subheader("📈 数据统计")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("数据条数", len(df))
        col2.metric("起始时间", str(df["datetime"].iloc[0])[:10])
        col3.metric("结束时间", str(df["datetime"].iloc[-1])[:10])
        col4.metric("平均价格", f"{df['close'].mean():.2f}")
        
        # 价格走势预览
        st.subheader("📊 价格走势预览")
        fig = px.line(df.tail(100), x="datetime", y=["open", "high", "low", "close"], 
                      title=f"{symbol} 价格走势（最近100条）", template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("⚠️ 未获取到有效数据，请检查合约代码和时间范围")
        st.session_state["shfe_data"] = None
else:
    # 检查是否已有数据
    if st.session_state["shfe_data"] is not None:
        df = st.session_state["shfe_data"]
        st.success(f"✅ 已加载 {st.session_state['current_symbol']} 的历史数据，共 {len(df)} 条")
    else:
        st.info("ℹ️ 请先点击「一键获取聚宽历史数据」按钮获取数据")
        st.stop()

# 第二步：回测参数配置
st.divider()
st.subheader("⚙️ 回测参数配置")
col1, col2 = st.columns(2)
with col1:
    fast_window = st.slider("📈 短期均线窗口", min_value=3, max_value=30, value=5, step=1)
    slow_window = st.slider("📉 长期均线窗口", min_value=10, max_value=60, value=10, step=1)
    
    # 显示均线预览
    if df is not None and len(df) > 0:
        df_preview = df.copy()
        df_preview["ma_fast"] = df_preview["close"].rolling(fast_window).mean()
        df_preview["ma_slow"] = df_preview["close"].rolling(slow_window).mean()
        df_preview = df_preview.tail(50)
        
        fig_ma = px.line(df_preview, x="datetime", y=["close", "ma_fast", "ma_slow"], 
                         title="均线策略预览（最近50条）", 
                         labels={"value": "价格", "variable": "线型"},
                         template="plotly_white")
        fig_ma.update_traces(line=dict(width=2))
        st.plotly_chart(fig_ma, use_container_width=True)
    
with col2:
    initial_capital = st.number_input("💰 初始资金（元）", min_value=10000, max_value=10000000, value=1000000, step=100000)
    margin_ratio = st.slider("📌 保证金比例", min_value=0.05, max_value=0.2, value=0.10, step=0.01)
    commission_rate = st.slider("💴 手续费率（万分之）", min_value=0.1, max_value=2.0, value=1.0, step=0.1)
    slippage = st.slider("🛶 滑点（点）", min_value=0.0, max_value=20.0, value=2.0, step=0.1)
    
    st.info(f"📊 当前合约规格：\n- 合约乘数：{st.session_state.get('contract_size', 10)}吨/手\n- 最小变动价位：{st.session_state.get('pricetick', 1)}元")

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
        symbol=st.session_state["current_symbol"],
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
    
    # 保存回测结果
    st.session_state["backtest_results"] = metrics
    st.session_state["backtest_engine"] = backtest_engine
    
    # 展示回测结果
    st.success("✅ 回测完成！")
    st.divider()
    st.subheader("📈 回测结果汇总")
    
    # 核心指标卡片
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("总收益率", f"{metrics['总收益率(%)']} %", 
                 delta=f"{metrics['总收益率(%)']:.2f}%" if metrics['总收益率(%)'] > 0 else f"{metrics['总收益率(%)']:.2f}%")
    col_b.metric("年化收益率", f"{metrics['年化收益率(%)']} %", 
                 delta=f"{metrics['年化收益率(%)']:.2f}%" if metrics['年化收益率(%)'] > 0 else f"{metrics['年化收益率(%)']:.2f}%")
    col_c.metric("夏普比率", f"{metrics['夏普比率']:.2f}", 
                 delta="↑" if metrics['夏普比率'] > 1 else "↓")
    col_d.metric("最大回撤", f"{metrics['最大回撤(%)']} %", 
                 delta=f"{metrics['最大回撤(%)']:.2f}%" if metrics['最大回撤(%)'] < 0 else "0%")
    
    col_e, col_f, col_g, col_h = st.columns(4)
    col_e.metric("总交易次数", metrics['总交易次数'])
    col_f.metric("初始资金", f"{metrics['初始资金(元)']:,.0f} 元")
    col_g.metric("最终资产", f"{metrics['最终总资产(元)']:,.0f} 元")
    col_h.metric("总盈亏", f"{metrics['最终总资产(元)'] - metrics['初始资金(元)']:,.0f} 元")
    
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
        title=f"{selected_name}（{st.session_state['current_symbol']}）双均线策略总资产变化",
        template="plotly_white"
    )
    fig.add_hline(y=initial_capital, line_dash="dash", line_color="red", annotation_text="初始资金")
    st.plotly_chart(fig, use_container_width=True)
    
    # 详细指标
    st.markdown("### 📋 详细回测指标")
    metrics_df = pd.DataFrame([metrics])
    st.dataframe(metrics_df, use_container_width=True)
    
    # 交易记录
    if backtest_engine.trade_records:
        st.markdown("### 📝 交易记录")
        trade_df = pd.DataFrame(backtest_engine.trade_records)
        trade_df["累计盈亏"] = trade_df[trade_df["action"] == "平仓"]["profit"].cumsum()
        st.dataframe(trade_df, use_container_width=True)
        
        # 交易统计
        st.markdown("#### 📊 交易统计")
        if len(trade_df) > 0:
            # 分离开仓和平仓记录
            opening_trades = trade_df[trade_df["action"] == "开仓"]
            closing_trades = trade_df[trade_df["action"] == "平仓"]
            
            if len(closing_trades) > 0:
                winning_trades = closing_trades[closing_trades["profit"] > 0]
                losing_trades = closing_trades[closing_trades["profit"] <= 0]
                
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("开仓次数", len(opening_trades))
                col2.metric("平仓次数", len(closing_trades))
                col3.metric("盈利交易数", len(winning_trades))
                col4.metric("亏损交易数", len(losing_trades))
                
                col5, col6, col7, col8 = st.columns(4)
                win_rate = len(winning_trades) / len(closing_trades) * 100 if len(closing_trades) > 0 else 0
                col5.metric("胜率", f"{win_rate:.1f}%")
                avg_profit = winning_trades["profit"].mean() if len(winning_trades) > 0 else 0
                col6.metric("平均盈利", f"{avg_profit:.2f} 元")
                avg_loss = losing_trades["profit"].mean() if len(losing_trades) > 0 else 0
                col7.metric("平均亏损", f"{avg_loss:.2f} 元")
                total_profit = closing_trades["profit"].sum()
                col8.metric("总盈亏", f"{total_profit:.2f} 元")
    else:
        st.info("ℹ️ 本次回测无交易产生，可调整均线窗口重试。")

# 第四步：注意事项
st.divider()
st.subheader("📋 使用说明")
with st.expander("点击查看使用说明", expanded=False):
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
       
    6. **数据获取**：
       - 主力连续合约（8888）数据最全
       - 指数合约（9999）适合长线回测
       - 具体合约（如RB2410）在到期前才有数据
    """)