# -*- coding: utf-8 -*-
"""
===================================
期货AI决策助手
===================================
借鉴: ZhuLinsen/daily_stock_analysis 决策仪表盘 + 多数据源架构

功能：
1. AI决策仪表盘：持仓/自选品种的评分、风险、机会、操作建议
2. 市场概览：板块强弱、资金流向、市场情绪
3. 多数据源fallback：主力Sina + 备用AkShare
4. 通知格式化：Telegram推送格式优化

用法：
    from ai_decision import generate_decision_dashboard, generate_market_overview

    dashboard = generate_decision_dashboard(positions, watchlist, scores_dict)
    overview = generate_market_overview(all_quote_data)
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# MiniMax VL 调用（复用车载scripts/minimax_vl.py）
# ─────────────────────────────────────────────
MINIMAX_VL_URL = "https://api.minimaxi.com/v1/coding_plan/vlm"
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")


def _call_minimax_vl(prompt: str, images: List[str] = None, timeout: int = 60) -> str:
    """调用 MiniMax VL 生成 AI 分析"""
    if not MINIMAX_API_KEY:
        return "[AI总结需要配置MINIMAX_API_KEY环境变量]"

    import requests
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "MiniMax-V01",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800
    }
    try:
        resp = requests.post(MINIMAX_VL_URL, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"MiniMax VL 调用失败: {e}")
        return f"[AI总结失败: {e}]"


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class FuturesSignal:
    """期货信号"""
    variety: str          # 合约代码，如 "TA2609"
    name: str             # 中文名称，如 "PTA"
    direction: str        # "long" 或 "short"
    score: float          # 综合评分 0-100
    risk_level: str       # 🟢安全 / 🟡注意 / 🟠高风险 / 🔴危险

    # 技术信号
    trend: str            # "强势多头" / "空头排列" 等
    entry_price: float    # 开仓价
    current_price: float  # 当前价
    pnl: float            # 浮动盈亏（元/手）

    # 支撑阻力
    support: float
    resistance: float

    # 风险要素
    risk_alerts: List[str] = field(default_factory=list)   # 🚨风险点
    opportunities: List[str] = field(default_factory=list)  # ✨机会点
    catalyst: List[str] = field(default_factory=list)        # 📢催化因素

    # 附加指标
    kdj: Optional[str] = None
    macd: Optional[str] = None
    bollinger: Optional[str] = None
    oi_status: Optional[str] = None
    div_type: Optional[str] = None   # "顶背离" / "底背离" / None


@dataclass
class MarketOverview:
    """市场概览"""
    timestamp: str
    market_sentiment: str   # "偏多" / "偏空" / "震荡"
    sentiment_score: float # 0-100，>50偏多

    # 板块
    hot_sectors: List[str]   # 强势板块
    cold_sectors: List[str]  # 弱势板块

    # 资金
    money_flow: str         # "净流入" / "净流出"

    # 关键事件
    key_events: List[str]   # 今日重要事件


# ─────────────────────────────────────────────
# 决策仪表盘生成（核心函数）
# ─────────────────────────────────────────────

def generate_decision_dashboard(
    positions: List[Dict],
    watchlist: List[Dict],
    quote_fn: Callable[[str], Optional[Dict]]
) -> str:
    """
    生成期货AI决策仪表盘

    借鉴 stock_analysis 的决策仪表盘格式：
    🎯 期货决策 | 评分 + 风险警报 + 操作检查清单

    Args:
        positions: 持仓列表 [{variety, direction, entry_price, quantity, ...}]
        watchlist: 自选列表 [{variety, ...}]
        quote_fn: 获取实时行情的函数，签名: (variety) -> {price, change, ...} or None
    """
    signals = []

    # 处理持仓
    for pos in positions:
        v = pos.get("variety", "")
        dir = pos.get("direction", "long")
        entry = pos.get("entry_price", 0)
        qty = pos.get("quantity", 0)
        quote = quote_fn(v)

        if not quote:
            continue

        price = quote.get("price", entry)
        change = quote.get("change_pct", 0)
        name = quote.get("name", v)

        # 计算盈亏
        pv = POINT_VALUE.get(v.replace("260", "").replace("270", "").replace("250", "")[:2], 10)
        if dir == "long":
            pnl = (price - entry) * pv * qty
        else:
            pnl = (entry - price) * pv * qty

        signals.append(FuturesSignal(
            variety=v, name=name, direction=dir,
            score=pos.get("score", 50),
            risk_level=_score_to_risk(pos.get("score", 50)),
            trend=_trend_from_price(price, quote.get("ma5", 0), quote.get("ma20", 0)),
            entry_price=entry, current_price=price, pnl=pnl,
            support=quote.get("support", 0),
            resistance=quote.get("resistance", 0),
            risk_alerts=_extract_risk_alerts(quote, pos),
            opportunities=_extract_opportunities(quote, pos),
            catalyst=_extract_catalyst(quote),
            kdj=quote.get("kdj_status"),
            macd=quote.get("macd_status"),
            bollinger=quote.get("bb_position"),
            oi_status=quote.get("oi_status"),
            div_type=quote.get("div_type")
        ))

    return _format_dashboard(signals, positions, watchlist)


def _score_to_risk(score: float) -> str:
    if score <= 30: return "🟢安全"
    elif score <= 60: return "🟡注意"
    elif score <= 80: return "🟠高风险"
    else: return "🔴危险"


def _trend_from_price(price: float, ma5: float, ma20: float) -> str:
    if ma5 <= 0 or ma20 <= 0:
        return "数据不足"
    if ma5 > ma20:
        if price > ma5: return "强势多头"
        elif price > ma20: return "多头排列"
        else: return "弱势多头"
    else:
        if price < ma5: return "强势空头"
        elif price < ma20: return "空头排列"
        else: return "弱势空头"


def _extract_risk_alerts(quote: Dict, pos: Dict) -> List[str]:
    alerts = []
    if quote.get("div_type") == "顶背离":
        alerts.append("⚠️ 顶背离形成，存在回调风险")
    if quote.get("kdj_status") == "超买":
        alerts.append("⚠️ RSI/RSI指标超买，注意回调")
    if quote.get("oi_change", 0) < 0 and quote.get("price_change", 0) > 0:
        alerts.append("⚠️ 价格上涨但OI下降，警惕多头平仓")
    if quote.get("vol_ratio", 1) > 1.5:
        alerts.append("⚠️ 成交量异常放大，趋势可能逆转")
    sl = pos.get("stop_loss")
    if sl and quote.get("price", 0) <= sl:
        alerts.append("🚨 已触及止损价")
    return alerts


def _extract_opportunities(quote: Dict, pos: Dict) -> List[str]:
    opts = []
    if quote.get("div_type") == "底背离":
        opts.append("✨ 底背离形成，反弹概率增加")
    if quote.get("kdj_status") == "超卖":
        opts.append("✨ RSI/RSI超卖，反弹预期")
    if quote.get("oi_change", 0) > 0 and quote.get("price_change", 0) > 0:
        opts.append("✨ 顺势增仓，多头信号确认")
    bb_pos = quote.get("bb_position", 0.5)
    if bb_pos < 0.2:
        opts.append("✨ 布林下轨支撑，低位买入机会")
    tp = pos.get("take_profit")
    if tp and quote.get("price", 0) >= tp:
        opts.append("✨ 已达目标价，注意止盈")
    return opts


def _extract_catalyst(quote: Dict) -> List[str]:
    cats = []
    chg = quote.get("change_pct", 0)
    if chg > 3:
        cats.append(f"📈 今日涨幅{chg:.1f}%，资金积极入场")
    elif chg < -3:
        cats.append(f"📉 今日跌幅{abs(chg):.1f}%，关注是否超跌")
    if quote.get("news"):
        cats.append(f"📰 {quote['news']}")
    return cats


def _format_dashboard(signals: List[FuturesSignal], positions: List, watchlist: List) -> str:
    if not signals:
        return "🎯 期货决策仪表盘\n暂无持仓数据"

    buy_count = sum(1 for s in signals if s.direction == "long" and s.score >= 60)
    sell_count = sum(1 for s in signals if s.direction == "short" and s.score >= 60)
    watch_count = len(watchlist)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"🎯 期货决策仪表盘 | {now}",
        f"共 {len(positions)} 持仓 | 🟢买入:{buy_count} 🟡观望:{len(signals)-buy_count-sell_count} 🔴卖出:{sell_count} | 自选:{watch_count}个",
        ""
    ]

    for s in signals:
        emoji = "⚪" if s.direction == "long" else "🔵"
        dir_cn = "做多" if s.direction == "long" else "做空"
        risk_emoji = "🟢" if s.score <= 30 else "🟡" if s.score <= 60 else "🟠" if s.score <= 80 else "🔴"

        lines.append(f"{emoji} {s.name}({s.variety}): {dir_cn} | 评分 {risk_emoji}{s.score:.0f} | {s.trend}")
        lines.append(f"   成本:{s.entry_price:.2f} 当前:{s.current_price:.2f} 浮盈:{'+' if s.pnl >= 0 else ''}{s.pnl:.0f}元/手")

        if s.risk_alerts:
            for a in s.risk_alerts[:2]:
                lines.append(f"   {a}")
        if s.opportunities:
            for o in s.opportunities[:2]:
                lines.append(f"   {o}")
        if s.catalyst:
            lines.append(f"   {s.catalyst[0]}")

        lines.append("")

    return "\n".join(lines)


def generate_market_overview(all_quotes: Dict[str, Dict]) -> str:
    """
    生成市场概览（板块强弱 + 资金流向 + 市场情绪）
    借鉴 stock_analysis 的大盘复盘格式
    """
    if not all_quotes:
        return "📊 市场概览\n暂无行情数据"

    # 计算市场情绪
    up_count = sum(1 for q in all_quotes.values() if (q.get("change_pct", 0) or 0) > 0)
    down_count = sum(1 for q in all_quotes.values() if (q.get("change_pct", 0) or 0) < 0)
    total = up_count + down_count
    sentiment = "偏多" if up_count > down_count else "偏空" if down_count > up_count else "震荡"
    sentiment_score = (up_count / max(total, 1)) * 100

    # 涨幅/跌幅排行
    sorted_quotes = sorted(all_quotes.items(), key=lambda x: x[1].get("change_pct", 0) or 0, reverse=True)
    hot = [v for v, q in sorted_quotes[:3] if (q.get("change_pct", 0) or 0) > 1]
    cold = [v for v, q in sorted_quotes[-3:] if (q.get("change_pct", 0) or 0) < -1]

    # 资金流向（简单版：看OI变化）
    oi_up = sum(1 for q in all_quotes.values() if (q.get("oi_change", 0) or 0) > 0)
    oi_down = sum(1 for q in all_quotes.values() if (q.get("oi_change", 0) or 0) < 0)
    money_flow = "净流入" if oi_up >= oi_down else "净流出"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📊 市场概览 | {now}",
        f"上涨:{up_count} 下跌:{down_count} | 情绪:{sentiment} ({sentiment_score:.0f}%)",
        f"资金流向:{money_flow}",
    ]

    if hot:
        lines.append(f"🔥 强势: {', '.join(hot)}")
    if cold:
        lines.append(f"❄️ 弱势: {', '.join(cold)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# AI增强版决策（调用MiniMax VL）
# ─────────────────────────────────────────────

def generate_ai_enhanced_decision(
    position_data: Dict[str, Any],
    quote_fn: Callable[[str], Optional[Dict]]
) -> str:
    """
    用 MiniMax VL 生成 AI 增强决策建议
    输入：持仓数据 + 实时行情
    输出：AI 分析结论（趋势判断、风险、机会、操作建议）
    """
    # 构建 prompt
    items = []
    for v, data in position_data.items():
        q = quote_fn(v)
        if not q:
            continue
        entry = data.get("entry_price", 0)
        dir = data.get("direction", "long")
        qty = data.get("quantity", 0)
        price = q.get("price", entry)
        chg = q.get("change_pct", 0)
        score = data.get("score", 50)

        pv = POINT_VALUE.get(v[:2], 10)
        pnl = (price - entry) * pv * qty if dir == "long" else (entry - price) * pv * qty

        pnl_str = f"+{pnl:.0f}" if pnl >= 0 else f"{pnl:.0f}"
        items.append(f"{v}({q.get('name',v)}): {dir} 多, 成本{entry}, 当前{price}({chg:+.1f}%), 浮亏{pnl_str}元, 评分{score}")

    if not items:
        return "📊 AI决策\n暂无持仓数据"

    prompt = f"""你是期货AI交易顾问。根据以下持仓数据，给出决策建议：

持仓列表：
{chr(10).join(items)}

请给出：
1. 当前总体风险评估（1-100分，越高越危险）
2. 哪些品种需要重点关注（止损/止盈）
3. 今日操作建议（加仓/减仓/观望）
4. 最大风险点

请用简洁的列表格式回复，适合手机查看。"""

    ai_result = _call_minimax_vl(prompt)
    if not ai_result or "[AI总结" in ai_result:
        return f"📊 AI决策\n暂无法生成AI建议，请检查MINIMAX_API_KEY"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"📊 AI决策 | {now}\n\n{ai_result}"


# ─────────────────────────────────────────────
# POINT_VALUE（从主程序复制，保持同步）
# ─────────────────────────────────────────────

POINT_VALUE = {
    # SHFE
    "AU": 1000, "AG": 15, "CU": 5, "AL": 5, "ZN": 5, "PB": 5,
    "NI": 1, "SN": 1, "RB": 10, "HC": 10, "WR": 10, "SS": 5,
    "RU": 10, "FU": 10, "BU": 10, "SP": 10, "AO": 20, "AD": 10, "OP": 40, "BR": 5, "SC": 100,
    # DCE
    "I": 100, "JM": 60, "J": 100, "M": 10, "Y": 10, "P": 10,
    "L": 5, "V": 5, "PP": 5, "PE": 5, "PG": 20, "EB": 5, "EG": 10, "LH": 16,
    "A": 10, "B": 10, "C": 10, "CS": 10, "JD": 10, "RR": 10, "FB": 10, "BB": 500, "LG": 90, "BZ": 30,
    # CZCE
    "SR": 10, "CF": 5, "TA": 5, "MA": 10, "RM": 10, "OI": 10,
    "PK": 5, "FG": 20, "WH": 20, "PM": 50, "ZC": 100,
    "SA": 20, "RI": 20, "JR": 20, "LR": 20, "RS": 10,
    "SF": 5, "SM": 5, "AP": 10, "CJ": 5, "CY": 5, "PF": 5, "UR": 20,
    "SH": 30, "PX": 5, "PR": 15, "PL": 20,
    # INE
    "BC": 5, "NR": 10, "EC": 10, "LU": 10,
    # GFEX
    "SI": 5, "LC": 1, "PS": 3, "PT": 1000, "PD": 1000,
    # CFFEX
    "IF": 300, "IH": 300, "IC": 200, "IM": 200, "TL": 10000, "TF": 10000, "T": 10000, "TS": 20000,
}


# ─────────────────────────────────────────────
# 多数据源 Fallback（借鉴 DataFetcherManager 思路）
# ─────────────────────────────────────────────

class DataFetcherFallback:
    """
    多数据源fallback：Sina为主，AkShare备用
    解决：CZCE/DCE有时候超时/被拦的问题
    """

    def __init__(self):
        self._primary_ok = True
        self._fallback_count = 0

    def fetch_with_fallback(self, variety: str, fetch_fn, fallback_fn=None) -> Optional[Dict]:
        """
        优先用 fetch_fn（Sina），失败后用 fallback_fn（AkShare）
        """
        try:
            result = fetch_fn(variety)
            if result:
                self._primary_ok = True
                return result
        except Exception as e:
            logger.warning(f"Primary fetch failed for {variety}: {e}")
            self._primary_ok = False

        if fallback_fn:
            try:
                result = fallback_fn(variety)
                if result:
                    self._fallback_count += 1
                    logger.info(f"Fallback used for {variety} (fallback_count={self._fallback_count})")
                    return result
            except Exception as e:
                logger.warning(f"Fallback also failed for {variety}: {e}")

        return None

    @property
    def is_primary_healthy(self) -> bool:
        return self._primary_ok


# 全局单例
_data_fetcher = DataFetcherFallback()