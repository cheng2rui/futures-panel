# 版本: v0.2.3（2026-04-20）
# 修复: (1) VaR计算abs()修正direction问题 (2) 保证金预警阈值逻辑反转Bug
#       (3) VaR冗余if/else删除 (4) 评分波动率/成交量加方向修正 (5) VaR ret_std改ddof=0
# ─────────────────────────────────────────────
import json
import os
import time
import subprocess
import signal
import math
import threading
import datetime
import requests
import traceback
import queue
import akshare as ak
import pandas as pd
import numpy as np

# 文件读写锁（防止并发写入竞争）
_positions_lock = threading.Lock()
_watchlist_lock = threading.Lock()
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

# ─────────────────────────────────────────────
# 熔断器缓存（BreakerCache）— 兼容 Redis 升级
# 功能：
# 1. 多级 TTL 缓存（内存 → Redis 将来无缝切换）
# 2. 熔断器：失败 N 次后 open-circuit，冷却期内直接返回 stale 数据
# 3. 异步刷新：后台线程定期更新，不阻塞读请求
# 4. 线程安全
# ─────────────────────────────────────────────

class CircuitBreakerCache:
    """
    带熔断器的多级缓存，支持未来无缝升级 Redis。
    现状：纯内存（threading.Lock）
    升级路径：swap _redis_get/_redis_set → 直接替换底层
    """

    def __init__(self, ttl_price=2, ttl_market=5, ttl_kline=300):
        self._mem = {}          # {key: (value, timestamp)}
        self._stale = {}        # {key: (value, timestamp)} 熔断期备用
        self._ttl_price = ttl_price
        self._ttl_market = ttl_market
        self._ttl_kline = ttl_kline
        self._lock = threading.RLock()

        # 熔断器状态 per-key 前缀
        self._breaker = {}      # {key_prefix: {"failures": int, "last_failure": float, "state": "closed|open|half"}}
        self._BREAKER_THRESHOLD = 3   # 连续失败 N 次 → open
        self._BREAKER_COOLDOWN = 30   # open 后 30 秒尝试 half-open
        self._BREAKER_MAX_STALE = 120 # stale 数据最多保留 120 秒
        # k线原始数据缓存（用于预热和调试）
        self._kline_cache = {}

    # ── 底层存储（将来替换为 Redis）───
    def _mem_get(self, key):
        with self._lock:
            if key in self._mem:
                val, ts = self._mem[key]
                return val, ts
        return None, None

    def _mem_set(self, key, value, ttl):
        with self._lock:
            self._mem[key] = (value, time.time(), ttl)

    def _mem_get_stale(self, key):
        with self._lock:
            if key in self._stale:
                val, ts = self._stale[key]
                if time.time() - ts < self._BREAKER_MAX_STALE:
                    return val
            return None

    def _mem_set_stale(self, key, value):
        with self._lock:
            self._stale[key] = (value, time.time())

    # ── 熔断器逻辑 ──────────────────────
    def _get_breaker(self, key_prefix):
        if key_prefix not in self._breaker:
            self._breaker[key_prefix] = {"failures": 0, "last_failure": 0, "state": "closed"}
        return self._breaker[key_prefix]

    def _record_failure(self, key_prefix):
        b = self._get_breaker(key_prefix)
        b["failures"] += 1
        b["last_failure"] = time.time()
        if b["failures"] >= self._BREAKER_THRESHOLD:
            b["state"] = "open"
            print(f"[CircuitBreaker] 🔴 OPEN → {key_prefix} (failures={b['failures']})")

    def _record_success(self, key_prefix):
        b = self._get_breaker(key_prefix)
        b["failures"] = 0
        b["state"] = "closed"

    def _is_open(self, key_prefix):
        b = self._get_breaker(key_prefix)
        if b["state"] == "closed":
            return False
        if b["state"] == "open":
            elapsed = time.time() - b["last_failure"]
            if elapsed >= self._BREAKER_COOLDOWN:
                b["state"] = "half"
                b["failures"] = 0  # 重置计数，给新的 half-open 测试一个干净的开始
                print(f"[CircuitBreaker] 🟡 HALF-OPEN → {key_prefix}")
                return False
            return True  # still cooling
        # half-open: allow ONE attempt through
        return False

    # ── 高层 API ────────────────────────
    def get_price(self, variety):
        """返回 (price, name, timestamp) 或 (None, None, None)"""
        key = f"price:{variety}"
        ttl = self._ttl_price

        with self._lock:
            if key in self._mem:
                val, ts, _ = self._mem[key]
                if time.time() - ts < ttl:
                    # 顺便检查是否该异步刷新
                    return val[0], val[1], val[2]
                # TTL 到期：标记即将刷新（do nothing, async thread handles）

        # 检查熔断
        prefix = variety[:2].lower()
        if self._is_open(prefix):
            stale = self._mem_get_stale(key)
            if stale:
                print(f"[CircuitBreaker] ↩️ 使用 STALE 价格 {variety}")
                return stale[0], stale[1], stale[2]
            return None, None, None

        return None, None, None  # cache miss → caller fetches

    def set_price(self, variety, price, unit, name=None):
        """存储行情数据：price=float, unit=str, name=str"""
        key = f"price:{variety}"
        with self._lock:
            self._mem[key] = ((price, unit, name), time.time(), self._ttl_price)
        prefix = variety[:2].lower()
        self._record_success(prefix)

    def get_market(self, sym):
        key = f"market:{sym}"
        with self._lock:
            if key in self._mem:
                val, ts, _ = self._mem[key]
                if time.time() - ts < self._ttl_market:
                    return val
        return None

    def set_market(self, sym, data):
        key = f"market:{sym}"
        with self._lock:
            self._mem[key] = (data, time.time(), self._ttl_market)
        prefix = sym[:2].lower()
        self._record_success(prefix)

    def get_kline(self, sym):
        key = f"kline:{sym}"
        with self._lock:
            if key in self._mem:
                val, ts, _ = self._mem[key]
                if time.time() - ts < self._ttl_kline:
                    return val
        return None

    def set_kline(self, sym, data):
        key = f"kline:{sym}"
        with self._lock:
            self._mem[key] = (data, time.time(), self._ttl_kline)

    def invalidate(self, variety):
        """清除指定品种的行情缓存，下次访问会重新抓取"""
        with self._lock:
            # 清除 price 缓存
            price_key = f"price:{variety}"
            if price_key in self._mem:
                del self._mem[price_key]
            if price_key in self._stale:
                del self._stale[price_key]
            # 清除 kline 缓存
            if hasattr(self, '_kline_cache') and variety in self._kline_cache:
                del self._kline_cache[variety]

    def record_error(self, variety):
        prefix = variety[:2].lower()
        # 保存当前缓存为 stale
        key = f"price:{variety}"
        with self._lock:
            if key in self._mem:
                val, ts, _ = self._mem[key]
                self._stale[key] = (val, ts)
        self._record_failure(prefix)

    def flush_stale(self):
        """清理过期 stale 数据 + 过期_mem entries，防止内存无限增长"""
        with self._lock:
            now = time.time()
            # 1. 清理过期 stale
            stale_keys = [k for k, (_, ts) in self._stale.items() if now - ts >= self._BREAKER_MAX_STALE]
            for k in stale_keys:
                del self._stale[k]
            # 2. 清理已过期的_mem entries（price/market/kline）
            expired_mem_keys = []
            for key in list(self._mem.keys()):
                _, ts, ttl = self._mem[key]
                if now - ts >= ttl:
                    expired_mem_keys.append(key)
            for k in expired_mem_keys:
                del self._mem[k]
            if expired_mem_keys:
                print(f"[Cache] 清理 {len(expired_mem_keys)} 个过期_mem entries")

# 全局缓存实例（TTL统一60秒，akshare慢，减少穿透）

# ─────────────────────────────────────────────
# 启动预热：后台线程预热当前持仓的日线数据
# ─────────────────────────────────────────────
def _warm_klines_for_positions():
    """启动时后台加载所有持仓的日线（避免首次访问 akshare 卡顿）"""
    try:
        positions = load_positions()
        for pos in positions:
            sym = pos.get('variety', '')
            if not sym:
                continue
            # 后台线程预热，非阻塞
            t = threading.Thread(target=_warm_kline_async, args=(sym,), daemon=True)
            t.start()
        print(f"[Warmup] 启动预热 {len(positions)} 个持仓行情")
    except Exception as e:
        print(f"[Warmup] 预热失败: {e}")

def _warm_kline_async(variety):
    """异步预热单个品种日线（存储 DataFrame，供 calc_sr_multi_period 和 _tf_signal 使用）"""
    try:
        prefix = "".join(filter(str.isalpha, variety)).upper()
        suffix = "".join(filter(str.isdigit, variety))
        key = _resolve_variety_key(prefix)
        if not key:
            return
        sina_sym = key.upper() + suffix  # e.g. V2609

        df_d = ak.futures_zh_daily_sina(symbol=sina_sym)
        if df_d is None or len(df_d) == 0:
            return
        # 周线从日线派生（与 calc_sr_multi_period 保持一致）
        df_w_raw = df_d.copy()
        df_w_raw['date'] = pd.to_datetime(df_w_raw['date'], errors='coerce')
        df_w_raw = df_w_raw.set_index('date').sort_index()
        df_w = df_w_raw.resample('W').agg({'high': 'max', 'low': 'min', 'close': 'last'})
        df_w = df_w.dropna().tail(12)

        # 统一通过 set_kline 写入_mem（TTL 300s），与 calc_sr_multi_period 的 get_kline 读取路径一致
        _cache.set_kline(sina_sym, (df_d, df_w))
        print(f"[Warmup] {variety}({sina_sym}) 日线+周线预热完成")
    except Exception as e:
        print(f"[Warmup] {variety} 预热失败: {e}")

_cache = CircuitBreakerCache(ttl_price=60, ttl_market=60, ttl_kline=300)

# ─────────────────────────────────────────────
# 异步预热线程：定期刷新即将过期的缓存
# ─────────────────────────────────────────────
_cache_warm_lock = threading.Lock()
_cache_warming = set()   # 正在预热的 variety，避免重复

def _warm_cache_async(variety, fetch_fn):
    """异步刷新单个品种行情，防止击穿"""
    with _cache_warm_lock:
        if variety in _cache_warming:
            return
        _cache_warming.add(variety)
    try:
        result = fetch_fn()
        if result:
            price_data, unit, name = result
            _cache.set_price(variety, price_data, unit, name)
    except Exception as e:
        _cache.record_error(variety)
        print(f"[CacheWarm] 刷新 {variety} 失败: {e}")
    finally:
        with _cache_warm_lock:
            _cache_warming.discard(variety)

def _ensure_cache_warm(variety, fetch_fn):
    """检查缓存，未命中则异步预热（非阻塞）"""
    price, name, _ = _cache.get_price(variety)
    if price is None:
        threading.Thread(target=_warm_cache_async, args=(variety, fetch_fn), daemon=True).start()

def _fetch_price_from_sina(variety: str) -> tuple | None:
    """内部函数：从 Sina API 抓取指定合约的实时价格，支持重试 + 备用数据源"""
    prefix = "".join(filter(str.isalpha, variety))
    if prefix.upper() in INACTIVE_CONTRACTS:
        return None
    suffix = "".join(filter(str.isdigit, variety))
    key = _resolve_variety_key(prefix)
    meta = VARIETY_META_LOWER.get(key) if key else None
    if not meta:
        return None
    sina_sym = meta[1]

    # 策略1：akshare（带重试）
    for attempt in range(3):
        try:
            df = ak.futures_zh_realtime(symbol=sina_sym)
            if df is not None and not df.empty:
                target_sym = sina_sym.upper() + suffix
                for _, row in df.iterrows():
                    sym = str(row.get('symbol', ''))
                    if sym == variety or sym == variety.upper() or sym == target_sym:
                        return (float(row['trade']), meta[2], meta[0])
                for _, row in df.iterrows():
                    sym = str(row.get('symbol', ''))
                    name2 = str(row.get('name', ''))
                    if prefix.upper() in sym.upper() or prefix in name2:
                        return (float(row['trade']), meta[2], meta[0])
                return None
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5)
                continue
            print(f"Sina行情抓取失败({attempt+1}次) {variety}: {e}")
            _cache.record_error(variety)
            # 重试耗尽，继续走备用数据源（不要 return None）

    # 策略2：直接解析 Sina hq 接口（akshare 失败时的备用）
    return _fetch_price_from_sina_direct(variety, prefix, suffix, meta)

def _fetch_preclose_from_sina(variety: str) -> float | None:
    """从 Sina hq 接口抓取前一交易日收盘价（parts[2]）"""
    try:
        prefix = "".join(filter(str.isalpha, variety))
        suffix = "".join(filter(str.isdigit, variety))
        key = _resolve_variety_key(prefix)
        meta = VARIETY_META_LOWER.get(key) if key else None
        if not meta:
            return None
        direct_sym = f"nf_{prefix.upper()}{suffix}"
        import requests
        r = requests.get(
            f"https://hq.sinajs.cn/list={direct_sym}",
            headers={"Referer": "https://finance.sina.com.cn",
                     "User-Agent": "Mozilla/5.0"},
            timeout=5
        )
        r.encoding = "gb2312"
        val = r.text.split('=')[1].strip('";\n ')
        parts = val.split(',')
        if len(parts) > 2:
            return float(parts[2])
        return None
    except:
        return None

def _fetch_price_from_sina_direct(variety: str, prefix: str, suffix: str, meta) -> tuple | None:
    """备用：直接请求 Sina hq.sinajs.cn，绕过 akshare 的 demjson 解析器"""
    try:
        # Sina 的 nf_ 前缀合约代码（如 V2609 -> nf_V2609）
        direct_sym = f"nf_{prefix.upper()}{suffix}"
        import requests
        r = requests.get(
            f"https://hq.sinajs.cn/list={direct_sym}",
            headers={"Referer": "https://finance.sina.com.cn",
                     "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=5
        )
        r.encoding = "gb2312"
        text = r.text
        # 格式: var hq_str_nf_V2609="PVC..."
        if 'hq_str_' not in text or '=' not in text:
            return None
        val = text.split('=')[1].strip('";\n ')
        parts = val.split(',')
        if len(parts) < 4:
            return None
        price = float(parts[3])
        preclose = float(parts[2]) if len(parts) > 2 else 0
        # field[3] 与前结算几乎相等时（如 PX 合约），说明 field[3] 是昨日结算；用 field[6] 当今日现价
        if preclose > 0 and abs(price - preclose) < 50 and len(parts) > 6:
            alt = float(parts[6])
            if alt > 0 and abs(alt - preclose) > 50:
                price = alt
        return (price, meta[2], meta[0])
    except Exception as e:
        # print(f"Sina direct fallback 失败 {variety}: {e}")  # 太吵，注释掉
        return None

def _refresh_price_now(variety):
    """同步刷新指定品种行情，立即抓取并写入缓存（用于添加持仓/自选时）"""
    result = _fetch_price_from_sina(variety)
    if result:
        price, unit, name = result
        _cache.set_price(variety, price, unit, name)
        # 追踪止损计算（找对应持仓）
        trail_payload = None
        positions = load_positions()
        for pos in positions:
            v = pos.get('variety', '')
            if v != variety or not price:
                continue
            entry = float(pos.get('entry_price', 0))
            direction = pos.get('direction', 'long')
            sr = calc_sr_multi_period(variety)
            support, resistance, k_val, d_val, j_val, atr_val = sr[0], sr[1], sr[2], sr[3], sr[4], sr[5]
            if not atr_val or not support or not resistance:
                continue
            trail_active = False
            trail_price = None
            tp_hit = False
            if direction == 'long':
                tp_hit = (price >= resistance)
                if tp_hit:
                    trail_active = True
                    # 动态追踪：随价格上涨不断提升止损位
                    prev_trail = pos.get('trail_price')
                    new_trail = round(price - 0.5 * atr_val, 2)
                    trail_price = max(prev_trail or 0, new_trail) if prev_trail else new_trail
            else:
                tp_hit = (price <= support)
                if tp_hit:
                    trail_active = True
                    prev_trail = pos.get('trail_price')
                    new_trail = round(price + 0.5 * atr_val, 2)
                    trail_price = min(prev_trail or 999999, new_trail) if prev_trail else new_trail
            trail_payload = {
                'variety': variety,
                'price': price,
                'atr': round(atr_val, 2),
                'support': support,
                'resistance': resistance,
                'entry': entry,
                'direction': direction,
                'trail_active': trail_active,
                'trail_price': trail_price,
                'tp_hit': tp_hit,
            }
            break
        if trail_payload:
            _broadcast_event('price_update', trail_payload)
        else:
            _broadcast_event('price_update', {"variety": variety, "price": price})
        print(f"[Cache] ✅ {variety} 行情已抓取: {price}")
    else:
        print(f"[Cache] ⚠️ {variety} 未找到匹配合约")


# 定期清理 stale（每 5 分钟）
def _stale_cleaner():
    while True:
        time.sleep(300)
        _cache.flush_stale()

_cleaner_thread = threading.Thread(target=_stale_cleaner, daemon=True)
_cleaner_thread.start()

# ── 自选合约实时行情广播（每5秒）──
def _watchlist_broadcaster():
    """每5秒强制抓取自选合约价格并推送到所有SSE客户端"""
    while True:
        time.sleep(5)
        try:
            wl = load_watchlist()
            for item in wl:
                v = item if isinstance(item, str) else item.get('variety', '')
                if not v:
                    continue
                # 强制走网络抓取，直接广播给所有SSE客户端（带完整字段供前端badge判断）
                result = _fetch_price_from_sina(v)
                if result:
                    price, unit, name = result
                    # change_pct：从前一天结算价计算
                    preclose = _fetch_preclose_from_sina(v)
                    change_pct = round((price - preclose) / preclose * 100, 2) if preclose and preclose != 0 else 0
                    # entry_prompt：从支撑/阻力推算（需先算支撑阻力）
                    sr = calc_sr_multi_period(v)
                    support, resistance = sr[0], sr[1]
                    atr = sr[5] or 0
                    entry_prompt_long = round(support + 0.3 * atr, 2) if support else None
                    entry_prompt_short = round(resistance - 0.3 * atr, 2) if resistance else None
                    payload = {
                        'variety': v,
                        'price': price,
                        'is_watchlist': True,
                        'name': name,
                        'change_pct': change_pct,
                        'entry_prompt_long': entry_prompt_long,
                        'entry_prompt_short': entry_prompt_short,
                        'current_price': price,
                    }
                    _broadcast_event('price_update', payload)
        except Exception as e:
            print(f"[_watchlist_broadcaster] 异常: {e}")

_watchlist_thread = threading.Thread(target=_watchlist_broadcaster, daemon=True)
_watchlist_thread.start()

app = Flask(__name__,
            template_folder='/Users/rey/.openclaw/workspace/futures-panel/templates',
            static_folder='/Users/rey/.openclaw/workspace/futures-panel/static')
CORS(app)

# Flask 500 错误统一处理（打印堆栈方便调试）
@app.errorhandler(500)
def internal_error(error):
    traceback.print_exc()
    return jsonify({"error": "Internal Server Error", "message": str(error)}), 500

# 实时行情缓存（已统一60秒TTL，akshare慢，减少穿透）
INACTIVE_CONTRACTS = {'PS', 'TL', 'BR'}
_market_cache = CircuitBreakerCache(ttl_price=60, ttl_market=60, ttl_kline=300)

# ── 账户资金配置 ─────────────────────────────────────────
ACCOUNT_FILE = '/Users/rey/.openclaw/workspace/futures-panel/account.json'
_ACCOUNT_LOCK = threading.Lock()

def _load_account():
    defaults = {"total_balance": 1_000_000, "margin_alert_threshold": 80, "webhook_url": ""}
    try:
        if os.path.exists(ACCOUNT_FILE):
            with open(ACCOUNT_FILE, 'r') as f:
                loaded = json.load(f)
    except Exception:
        loaded = {}
    for k, v in defaults.items():
        loaded.setdefault(k, v)
    # 确保 alerts 子对象也有所有字段
    if "alerts" not in loaded:
        loaded["alerts"] = {"sound": True, "sl_hit": True, "tp_hit": True, "score_reversal": 30, "leverage_alert": 80, "expiry_warning": True, "leverage_warning": True}
    else:
        for k, v in {"sound": True, "sl_hit": True, "tp_hit": True, "score_reversal": 30, "leverage_alert": 80, "expiry_warning": True, "leverage_warning": True}.items():
            loaded["alerts"].setdefault(k, v)
    return loaded

def _save_account(acct):
    with _ACCOUNT_LOCK:
        with open(ACCOUNT_FILE, 'w') as f:
            json.dump(acct, f, indent=4)

def _send_webhook(url, alerts):
    """推送预警到钉钉/飞书等 Webhook"""
    if not url or url.strip() == "":
        return
    try:
        import requests
        text = "⚠️ 期货风控预警\n" + "\n".join(
            f"• [{a.get('type','?')}] {a.get('variety','')} {a.get('message','') or a.get('margin_util_pct','')}"
            for a in alerts
        )
        payload = {"msgtype": "text", "text": {"content": text}}
        requests.post(url, json=payload, timeout=5)
        print(f"Webhook 推送成功: {len(alerts)} 条预警")
    except Exception as e:
        print(f"Webhook 推送失败: {e}")

# ── SSE 推送（P0-2：改进版 — 连接状态追踪）──
_SSE_MSG_QUEUE = queue.Queue()
_SSE_CLIENTS = []           # [(queue, metadata_dict), ...]
_SSE_LOCK = threading.Lock()
_SSE_CLIENT_ID = 0
_SSE_CONNECTIONS = {}       # client_id → {"connected_at": float, "last_msg": float, "alive": bool}

# SSE 全局连接状态（供前端查询）
_sse_global_lock = threading.Lock()
_sse_connection_count = 0
_sse_last_heartbeat = time.time()

def _broadcast_event(event_type, data):
    """线程安全地将事件放入队列"""
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    try:
        _SSE_MSG_QUEUE.put_nowait(payload)
    except queue.Full:
        pass
    with _sse_global_lock:
        global _sse_last_heartbeat
        _sse_last_heartbeat = time.time()

def _sse_broadcast_safe():
    """每秒检查队列并将消息发送给所有 SSE 客户端"""
    while True:
        time.sleep(1)
        try:
            while True:
                msg = _SSE_MSG_QUEUE.get_nowait()
                dead = []
                with _SSE_LOCK:
                    for client_id, client in _SSE_CLIENTS:
                        try:
                            client['queue'].put_nowait(msg)
                            client['last_msg'] = time.time()
                        except Exception:
                            dead.append(client_id)
                    # 清理死连接（从后往前pop，避免索引偏移）
                    for cid in dead:
                        for i in range(len(_SSE_CLIENTS) - 1, -1, -1):
                            c_id, c = _SSE_CLIENTS[i]
                            if c_id == cid:
                                c['alive'] = False
                                _SSE_CLIENTS.pop(i)
                                if cid in _SSE_CONNECTIONS:
                                    del _SSE_CONNECTIONS[cid]
                                break
        except queue.Empty:
            pass

_broadcaster_thread = None
def _ensure_broadcaster():
    global _broadcaster_thread
    if _broadcaster_thread is None or not _broadcaster_thread.is_alive():
        _broadcaster_thread = threading.Thread(target=_sse_broadcast_safe, daemon=True)
        _broadcaster_thread.start()

def get_realtime_price(variety):
    # 先查缓存
    price, name, cached_at = _cache.get_price(variety)
    if price is not None:
        return price, name, cached_at
    result = _fetch_price_from_sina(variety)
    if result:
        price, unit, name = result
        _cache.set_price(variety, price, unit, name)
        _broadcast_event('price_update', {"variety": variety, "price": price})
        return price, unit, name
    return None, None, None

POSITIONS_FILE = '/Users/rey/.openclaw/workspace/futures-panel/positions.json'
WATCHLIST_FILE = '/Users/rey/.openclaw/workspace/futures-panel/watchlist.json'
TRADES_FILE   = '/Users/rey/.openclaw/workspace/futures-panel/trades.json'

if not os.path.exists(POSITIONS_FILE):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump([], f)
if not os.path.exists(WATCHLIST_FILE):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump([], f)

def load_positions():
    with _positions_lock:
        if not os.path.exists(POSITIONS_FILE):
            return []
        with open(POSITIONS_FILE, 'r') as f:
            positions = json.load(f)
        # 兼容旧数据：缺失 _idx 则按数组下标+1 补上
        for i, p in enumerate(positions):
            if p.get('_idx') is None:
                p['_idx'] = i + 1
        return positions

def save_positions(positions):
    with _positions_lock:
        with open(POSITIONS_FILE, 'w') as f:
            json.dump(positions, f, indent=4)

def load_watchlist():
    with _watchlist_lock:
        if not os.path.exists(WATCHLIST_FILE):
            return []
        with open(WATCHLIST_FILE, 'r') as f:
            return json.load(f)

def save_watchlist(watchlist):
    with _watchlist_lock:
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(watchlist, f, indent=4)

# ── 平仓日志 ──────────────────────────────────────────────
def _safe_json_load(path, fallback):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return fallback

def load_trades():
    return _safe_json_load(TRADES_FILE, [])

def save_trades(trades):
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(trades, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"写入trades.json失败: {e}")

# ─────────────────────────────────────────────
# 品种元数据
# ─────────────────────────────────────────────
VARIETY_META = {
    # 上海期货交易所（SHFE）
    "au":  ("黄金",     "黄金",     "上海期货交易所"), "ag":  ("白银",     "白银",     "上海期货交易所"),
    "cu":  ("铜",       "沪铜",     "上海期货交易所"), "al":  ("铝",       "沪铝",     "上海期货交易所"),
    "zn":  ("锌",       "沪锌",     "上海期货交易所"), "pb":  ("铅",       "沪铅",     "上海期货交易所"),
    "ni":  ("镍",       "沪镍",     "上海期货交易所"), "sn":  ("锡",       "沪锡",     "上海期货交易所"),
    "rb":  ("螺纹钢",   "螺纹钢",   "上海期货交易所"), "hc":  ("热轧卷板", "热轧卷板", "上海期货交易所"),
    "wr":  ("线材",     "线材",     "上海期货交易所"), "ss":  ("不锈钢",   "不锈钢",   "上海期货交易所"),
    "ru":  ("天然橡胶", "橡胶",     "上海期货交易所"), "fu":  ("燃料油",   "燃料油",   "上海期货交易所"),
    "bu":  ("沥青",     "沥青",     "上海期货交易所"), "sc":  ("原油",     "原油",     "上海期货交易所"),
    "sp":  ("纸浆",     "纸浆",     "上海期货交易所"), "ao":  ("氧化铝",   "氧化铝",   "上海期货交易所"),
    "ad":  ("铸造铝合金","铝",      "上海期货交易所"), "br":  ("丁二烯橡胶","丁二烯橡胶","上海期货交易所"),
    "op":  ("胶版印刷纸","纸",       "上海期货交易所"),
    # 大连商品交易所（DCE）
    "i":   ("铁矿石",   "铁矿石",   "大连商品交易所"), "jm":  ("焦煤",     "焦煤",     "大连商品交易所"),
    "j":   ("焦炭",     "焦炭",     "大连商品交易所"), "m":   ("豆粕",     "豆粕",     "大连商品交易所"),
    "y":   ("豆油",     "豆油",     "大连商品交易所"), "p":   ("棕榈油",   "棕榈",     "大连商品交易所"),
    "a":   ("大豆一号", "豆一",     "大连商品交易所"), "b":   ("大豆二号", "豆二",     "大连商品交易所"),
    "c":   ("黄玉米",   "玉米",     "大连商品交易所"), "cs":  ("玉米淀粉", "玉米淀粉", "大连商品交易所"),
    "l":   ("聚乙烯",   "塑料",     "大连商品交易所"), "v":   ("聚氯乙烯", "PVC",      "大连商品交易所"),
    "pp":  ("聚丙烯",   "PP",       "大连商品交易所"), "pe":  ("聚乙烯",   "塑料",     "大连商品交易所"),
    "eg":  ("乙二醇",   "乙二醇",   "大连商品交易所"), "eb":  ("苯乙烯",   "苯乙烯",   "大连商品交易所"),
    "pg":  ("液化石油气","LPG",     "大连商品交易所"), "lh":  ("生猪",     "生猪",     "大连商品交易所"),
    "jd":  ("鸡蛋",     "鸡蛋",     "大连商品交易所"), "rr":  ("粳米",     "粳米",     "大连商品交易所"),
    "fb":  ("细木工板", "纤维板",   "大连商品交易所"), "bb":  ("胶合板",   "胶合板",   "大连商品交易所"),
    "lg":  ("原木",     "原木",     "大连商品交易所"), "bz":  ("纯苯",     "纯苯",     "大连商品交易所"),
    # 郑州商品交易所（CZCE）
    "CF":  ("棉花",     "棉花",     "郑州商品交易所"), "SR":  ("白糖",     "白糖",     "郑州商品交易所"),
    "TA":  ("PTA",      "PTA",      "郑州商品交易所"), "MA":  ("甲醇",     "甲醇",     "郑州商品交易所"),
    "RM":  ("菜粕",     "菜粕",     "郑州商品交易所"), "OI":  ("菜油",     "菜油",     "郑州商品交易所"),
    "FG":  ("玻璃",     "玻璃",     "郑州商品交易所"), "WH":  ("强麦",     "强麦",     "郑州商品交易所"),
    "PM":  ("普麦",     "普麦",     "郑州商品交易所"), "RI":  ("早籼稻",   "早籼稻",   "郑州商品交易所"),
    "LR":  ("晚籼稻",   "晚籼稻",   "郑州商品交易所"), "JR":  ("粳稻",     "粳稻",     "郑州商品交易所"),
    "RS":  ("油菜籽",   "菜籽",     "郑州商品交易所"), "AP":  ("苹果",     "鲜苹果",   "郑州商品交易所"),
    "CJ":  ("红枣",     "红枣",     "郑州商品交易所"), "CY":  ("棉纱",     "棉纱",     "郑州商品交易所"),
    "PF":  ("短纤",     "短纤",     "郑州商品交易所"), "PK":  ("花生",     "花生",     "郑州商品交易所"),
    "SF":  ("硅铁",     "硅铁",     "郑州商品交易所"), "SM":  ("锰硅",     "锰硅",     "郑州商品交易所"),
    "UR":  ("尿素",     "尿素",     "郑州商品交易所"), "SH":  ("烧碱",     "烧碱",     "郑州商品交易所"),
    "ZC":  ("动力煤",   "动力煤",   "郑州商品交易所"), "SA":  ("纯碱",     "纯碱",     "郑州商品交易所"),
    "PX":  ("对二甲苯", "PX",       "郑州商品交易所"), "PR":  ("瓶片",     "瓶片",     "郑州商品交易所"),
    "PL":  ("丙烯",     "丙烯",     "郑州商品交易所"),
    # 国际能源/广州期货交易所
    "BC":  ("阴极铜",   "铜",       "广州期货交易所"), "NR":  ("20号胶",   "NR",       "上海国际能源"),
    "LU":  ("低硫燃料油","LU",      "上海国际能源"), "EC":  ("集运指数", "EC",       "上海国际能源"),
    "SI":  ("工业硅",   "工业硅",   "广州期货交易所"), "LC":  ("碳酸锂",   "碳酸锂",   "广州期货交易所"),
    "PS":  ("聚苯乙烯", "PS",       "广州期货交易所"), "PT":  ("铂",       "铂",       "广州期货交易所"),
    "PD":  ("钯",       "钯",       "广州期货交易所"),
    # 金融期货（中金所）
    "IF":  ("沪深300",  "IF",       "中金所"),       "IH":  ("上证50",   "IH",       "中金所"),
    "IC":  ("中证500",  "IC",       "中金所"),       "IM":  ("中证1000","IM",       "中金所"),
    "TL":  ("30年期国债","TL",      "中金所"),       "TF":  ("10年期国债","TF",      "中金所"),
    "T":   ("5年期国债", "T",        "中金所"),       "TS":  ("2年期国债","TS",       "中金所"),
}
VARIETY_META_LOWER = {k.lower(): v for k, v in VARIETY_META.items()}

# ─────────────────────────────────────────────
# 反向映射：Sina品种符号(全大写) → VARIETY_META key
# 用于处理用户输入如 "PVC2609" → 查到 "PVC" → 映射到 "v" key
# ─────────────────────────────────────────────
_SINA_TO_KEY: dict[str, str] = {}
for _k, _v in VARIETY_META.items():
    _sina = _v[1].upper()  # e.g. "V", "PVC", "豆一"
    if _sina not in _SINA_TO_KEY:
        _SINA_TO_KEY[_sina] = _k.lower()

def _resolve_variety_key(prefix: str):
    """从用户输入的品种前缀解析出 VARIETY_META key（如 "PVC" → "v", "V" → "v", "TA" → "TA"）"""
    p = prefix.lower()
    if p in VARIETY_META_LOWER:
        return p
    # 尝试用 Sina 反向映射（如 "PVC" → "v"）
    if prefix.upper() in _SINA_TO_KEY:
        return _SINA_TO_KEY[prefix.upper()]
    return None

FEE_PER_LOT = 0.0

# ─────────────────────────────────────────────
# 启动自检：异步校验 VARIETY_META（后台执行，不阻塞启动）
# ─────────────────────────────────────────────
def _validate_variety_meta_async():
    """后台线程校验 Sina 符号，只打印警告不阻止启动"""
    failures = []
    for k, v in VARIETY_META.items():
        sina_sym = v[1]
        if sina_sym in INACTIVE_CONTRACTS:
            continue
        try:
            df = ak.futures_zh_realtime(symbol=sina_sym)
            if df is None or df.empty:
                failures.append((k, sina_sym))
        except Exception:
            failures.append((k, sina_sym))
        time.sleep(0.5)  # 礼貌延迟，避免高频请求
    if failures:
        print(f"⚠️ VARIETY_META 警告：{len(failures)} 个品种Sina符号可能有问题:")
        for k, sina in failures:
            print(f"   [{k}] 符号 \"{sina}\"")
    else:
        print(f"  ✅ VARIETY_META 自检通过（{len(VARIETY_META)} 个品种）")

threading.Thread(target=_validate_variety_meta_async, daemon=True).start()

# ─────────────────────────────────────────────
# POINT_VALUE：每跳动1个点的人民币金额（元/点/手）
# = 最小变动价位 × 一手合约对应的吨数
# 有疑问请调用 GET /api/validate 检查
# ─────────────────────────────────────────────
POINT_VALUE = {
    # 上海期货交易所（SHFE）
    "AU": 1000, "AG": 15,   "CU": 5,   "AL": 5,    "ZN": 5,    "PB": 5,
    "NI": 1,    "SN": 1,    "RB": 10,  "HC": 10,  "WR": 10,  "SS": 5,
    "RU": 10,   "FU": 10,  "BU": 10,  "SP": 10,  "AO": 20,  "AD": 10,
    "OP": 40,   "BR": 5,   "SC": 100,
    # 大连商品交易所（DCE）
    "I": 100,   "JM": 60,   "J": 100,  "M": 10,   "Y": 10,   "P": 10,
    "A": 10,    "B": 10,   "C": 10,   "CS": 10,  "L": 5,    "V": 5,
    "PP": 5,    "PE": 5,   "PG": 20,  "EB": 5,   "EG": 10,  "LH": 16,
    "JD": 10,   "RR": 10,  "FB": 10,  "BB": 500, "LG": 90,   "BZ": 30,
    # 郑州商品交易所（CZCE）
    "SR": 10,   "CF": 5,    "TA": 5,   "MA": 10,  "RM": 10,  "OI": 10,
    "PK": 5,    "FG": 20,  "WH": 20,  "PM": 50,  "ZC": 100, "SA": 20,
    "RI": 20,   "JR": 20,  "LR": 20,  "RS": 10,  "SF": 5,   "SM": 5,
    "AP": 10,   "CJ": 5,   "CY": 5,   "PF": 5,   "UR": 20,  "SH": 30,
    "PX": 5,    "PR": 15,  "PL": 20,
    # 国际能源/广州期货交易所
    "BC": 5,    "NR": 10,  "EC": 10,  "LU": 10,
    "SI": 5,    "LC": 1,   "PS": 3,   "PT": 1000, "PD": 1000,
    # 金融期货（中金所）
    "IF": 300,  "IH": 300,  "IC": 200,  "IM": 200,
    "TL": 10000,"TF": 10000,"T": 10000, "TS": 20000,
}



def calc_kdj(df, n=9, m1=3, m2=3):
    try:
        low_n  = df['low'].astype(float).rolling(n).min()
        high_n = df['high'].astype(float).rolling(n).max()
        rsv = (df['close'].astype(float) - low_n) / (high_n - low_n + 1e-9) * 100
        k = rsv.ewm(alpha=1/m1, adjust=False).mean()
        d = k.ewm(alpha=1/m2, adjust=False).mean()
        j = 3*k - 2*d
        return float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    except:
        return None

# ─────────────────────────────────────────────
# MACD（12,26,9）
# ─────────────────────────────────────────────
def calc_macd(df, fast=12, slow=26, signal=9):
    try:
        c = df['close'].astype(float)
        ema_f = c.ewm(span=fast, adjust=False).mean()
        ema_s = c.ewm(span=slow, adjust=False).mean()
        macd_l = ema_f - ema_s
        signal_l = macd_l.ewm(span=signal, adjust=False).mean()
        hist = macd_l - signal_l
        return float(macd_l.iloc[-1]), float(signal_l.iloc[-1]), float(hist.iloc[-1])
    except:
        return None, None, None

# ─────────────────────────────────────────────
# 布林带（20日±2σ）
# ─────────────────────────────────────────────
def calc_bollinger(df, period=20, k=2):
    try:
        mid  = df['close'].astype(float).rolling(period).mean()
        std  = df['close'].astype(float).rolling(period).std()
        upper = mid + k * std
        lower = mid - k * std
        price = float(df['close'].iloc[-1])
        u = float(upper.iloc[-1]); l = float(lower.iloc[-1])
        band = u - l
        position = (price - l) / band if band > 0 else 0.5
        return u, float(mid.iloc[-1]), l, position
    except:
        return None, None, None, None

# ─────────────────────────────────────────────
# 持仓量变化率
# ─────────────────────────────────────────────
def calc_oi_change(df):
    try:
        oi = df['hold'].astype(float)
        if oi.iloc[-1] <= 0 or oi.iloc[-5] <= 0:
            return None
        current = oi.iloc[-1]
        avg_5d = oi.tail(6).iloc[:-1].mean()
        return (current - avg_5d) / avg_5d if avg_5d > 0 else 0
    except:
        return None

# ─────────────────────────────────────────────
# 背离检测（O(n) 预计算版）
# 返回: (div_type: -1顶背离 / 1底背离 / 0无背离, strength: 0-10)
# ─────────────────────────────────────────────
def calc_divergence(df, lookback=20):
    """
    底背离(1)：价格创前半段新低，但 MACD 柱未跟随创新低 → 潜在反弹
    顶背离(-1)：价格创前半段新高，但 MACD 柱未跟随创新高 → 潜在回调
    性能：预计算全量 MACD histogram，O(n) 无嵌套 apply
    """
    try:
        n = min(lookback, len(df))
        if n < 10:
            return 0, 0.0

        df_s = df.tail(n).reset_index(drop=True)
        c = df_s['close'].astype(float)

        # 预计算 MACD 柱状图（全量一次，无嵌套）
        ema_f = c.ewm(span=12, adjust=False).mean()
        ema_s = c.ewm(span=26, adjust=False).mean()
        macd_l = ema_f - ema_s
        signal_l = macd_l.ewm(span=9, adjust=False).mean()
        macd_h = macd_l - signal_l  # MACD 柱

        mid = len(df_s) // 2
        recent = df_s.iloc[mid:]
        prev   = df_s.iloc[:mid]

        price_low_r  = float(recent['low'].min())
        price_low_p  = float(prev['low'].min())
        price_high_r = float(recent['high'].max())
        price_high_p = float(prev['high'].max())

        macd_low_r  = float(macd_h.iloc[mid:].min())
        macd_low_p  = float(macd_h.iloc[:mid].min())
        macd_high_r = float(macd_h.iloc[mid:].max())
        macd_high_p = float(macd_h.iloc[:mid].max())

        if price_low_r < price_low_p and macd_low_r > macd_low_p:
            drift    = (price_low_p - price_low_r) / price_low_p
            recover  = (macd_low_r - macd_low_p) / max(abs(macd_low_p), 1e-9)
            strength = max(-10, min(10, 5 + drift * 500 + recover * 5))
            return 1, round(strength, 1)

        if price_high_r > price_high_p and macd_high_r < macd_high_p:
            drift   = (price_high_r - price_high_p) / price_high_p
            drop    = (macd_high_p - macd_high_r) / max(abs(macd_high_p), 1e-9)
            strength = max(-10, min(10, 5 + drift * 500 + drop * 5))
            return -1, round(strength, 1)

        return 0, 0.0
    except Exception as e:
        print(f"背离检测失败: {e}")
        return 0, 0.0


# ─────────────────────────────────────────────
# 智能取整：根据价格大小决定小数位数
# ─────────────────────────────────────────────
def _ri(val):
    """取整到合适位数：≥100→整数，<100→1位（避免显示 36.25 这样的精度）"""
    if val is None: return None
    v = float(val)
    if abs(v) >= 100: return int(round(v))
    return round(v, 1)

# ─────────────────────────────────────────────
# 综合评分（0-100 风险等级）
# 因子：趋势15% + SR距离15% + KDJ10% + 波动率20% + OI20% + 布林10% + 成交量10%
# ─────────────────────────────────────────────
def calc_sr_score(price, support, resistance, entry_price, direction,
                  k_val=None, d_val=None, j_val=None, atr_val=None,
                  oi_change=None, bb_position=None, macd_hist=None,
                  vol_ratio=None, ma5_above_ma20=None,
                  div_type=None, div_strength=None):
    if not all([price, support, resistance]):
        return None, {}
    range_sr = resistance - support
    if range_sr <= 0:
        return None, {}

    # 1. 趋势（15%）
    pir = (price - support) / range_sr
    trend_s = (1 - pir) * 10 if direction == 'long' else pir * 10
    if ma5_above_ma20 is not None:
        trend_s *= 1.2 if ma5_above_ma20 else 0.85
    trend_active = True

    # 2. SR距离（15%）— 均值回归：价格越靠近入场S/R边际越好
    if direction == 'long':
        sr_s = (price - support) / range_sr * 10
    else:
        sr_s = (resistance - price) / range_sr * 10
    sr_active = True

    # 3. KDJ（10%）
    kdj_active = k_val is not None and d_val is not None and j_val is not None
    if kdj_active:
        if direction == 'long':
            kdj_s = 10.0 if j_val < 20 else (1.0 if j_val > 80 else 5.0 + (50 - abs(j_val - 50)) / 50 * 4)
        else:
            kdj_s = 10.0 if j_val > 80 else (1.0 if j_val < 20 else 5.0 + (50 - abs(j_val - 50)) / 50 * 4)
        if macd_hist is not None:
            if (direction == 'long' and macd_hist > 0) or (direction == 'short' and macd_hist < 0):
                kdj_s = min(10, kdj_s + 1.0)
            else:
                kdj_s = max(1, kdj_s - 0.5)
    else:
        kdj_s = None

    # 4. 波动率（20%）— 高波动对所有方向都危险，做空尤甚（顺势持仓不怕回调，逆势持仓怕剧烈波动）
    vol_active = atr_val is not None and price > 0
    if vol_active:
        vol_penalty = atr_val / price * 200
        vol_s = max(1, min(10, 10 - vol_penalty))
        if direction == 'short':
            vol_s = max(1, min(10, vol_s - vol_penalty * 0.5))  # 做空：高波动双重惩罚
    else:
        vol_s = None

    # 5. OI（20%）
    oi_active = oi_change is not None
    if oi_active:
        oi_raw = oi_change * 40
        if direction == 'long':
            oi_s = max(1, min(10, 5.0 - oi_raw))
        else:
            oi_s = max(1, min(10, 5.0 + oi_raw))
    else:
        oi_s = None

    # 6. 布林带（10%）— 均值回归：价格越靠近下轨做多/越靠近上轨做空，得分越高
    bb_active = bb_position is not None
    if bb_active:
        bb_pct = min(max(bb_position, 0), 1.0)
        bb_s = (1.0 - bb_pct) * 10 if direction == 'long' else bb_pct * 10
    else:
        bb_s = None

    # 7. 成交量（10%）— 缩量反弹/回调是更好的顺势信号，放量则趋势可能逆转
    # 做多：缩量(看多信号) > 温和 > 放量 > 爆量（可能反转）
    # 做空：缩量下跌 > 温和 > 放量上涨 > 爆量（做空者应警惕）
    vol2_active = vol_ratio is not None and vol_ratio > 0
    if vol2_active:
        if direction == 'long':
            vol_s2 = 8.0 if vol_ratio < 0.7 else (6.0 if vol_ratio < 1.0 else (4.0 if vol_ratio < 1.5 else 2.5))
        else:
            vol_s2 = 8.0 if vol_ratio < 0.7 else (6.0 if vol_ratio < 1.0 else (4.0 if vol_ratio < 1.5 else 2.5))
        # 无论方向，爆量都对持仓不利（趋势可能即将反转）
        if vol_ratio >= 1.5:
            vol_s2 = max(1.5, vol_s2 - 1.0)
    else:
        vol_s2 = None

    WEIGHTS = {'trend': 0.15, 'sr': 0.15, 'kdj': 0.10, 'vol': 0.20, 'oi': 0.20, 'bb': 0.10, 'vol2': 0.10}
    active_weights = 0.0
    raw_score = 0.0
    if trend_active: raw_score += trend_s * WEIGHTS['trend']; active_weights += WEIGHTS['trend']
    if sr_active:    raw_score += sr_s * WEIGHTS['sr'];    active_weights += WEIGHTS['sr']
    if kdj_active:   raw_score += kdj_s * WEIGHTS['kdj'];  active_weights += WEIGHTS['kdj']
    if vol_active:   raw_score += vol_s * WEIGHTS['vol'];   active_weights += WEIGHTS['vol']
    if oi_active:    raw_score += oi_s * WEIGHTS['oi'];    active_weights += WEIGHTS['oi']
    if bb_active:    raw_score += bb_s * WEIGHTS['bb'];    active_weights += WEIGHTS['bb']
    if vol2_active:  raw_score += vol_s2 * WEIGHTS['vol2']; active_weights += WEIGHTS['vol2']

    total = raw_score * 10

    div_score = 0.0
    if div_type == 1:
        div_score = min(10, (div_strength or 0)) if direction == 'long' else -3.0
    elif div_type == -1:
        div_score = min(10, (div_strength or 0)) if direction == 'short' else -3.0

    score = round(max(1, min(100, total + div_score)), 1)
    detail = {
        "趋势":   round(trend_s * 10, 1),
        "SR距离": round(sr_s * 10, 1),
        "KDJ":   round(kdj_s * 10, 1) if kdj_s is not None else None,
        "波动率": round(vol_s * 10, 1) if vol_s is not None else None,
        "OI":    round(oi_s * 10, 1) if oi_s is not None else None,
        "布林带": round(bb_s * 10, 1) if bb_s is not None else None,
        "成交量": round(vol_s2 * 10, 1) if vol_s2 is not None else None,
        "背离":   round(div_score * 10, 1) if div_type != 0 else 0,
        "_active_weights_pct": round(active_weights * 100),
        "_weights": {"趋势":15,"SR距离":15,"KDJ":10,"波动率":20,"OI":20,"布林":10,"成交":10}
    }
    return score, detail

# ─────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────
def calc_atr(df, period=14):
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    c = df['close'].astype(float)
    prev_c = c.shift(1).fillna(c)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ─────────────────────────────────────────────
# 多周期支撑/阻力
# ─────────────────────────────────────────────
def calc_sr_multi_period(variety, daily_lookback=20, weekly_lookback=12):
    prefix = "".join(filter(str.isalpha, variety))
    suffix = "".join(filter(str.isdigit, variety))
    # Sina K线用品种key（如 PVC2609 -> V2609），因为 akshare 期货日线用交易所实际代码
    key = _resolve_variety_key(prefix)
    sina_symbol = key.upper() if key else prefix.upper()  # 交易所合约代码
    sym = f"{sina_symbol}{suffix}"

    # 检查缓存（K线数据缓存 60 秒）
    cached = _cache.get_kline(sym)
    if cached is not None:
        df_d, df_w = cached
    else:
        try:
            df_d = ak.futures_zh_daily_sina(symbol=sym)
            if df_d is None or len(df_d) == 0:
                _cache._kline_cache.pop(sym, None)
                return (None,) * 13
            df_w_raw = ak.futures_zh_daily_sina(symbol=sym)
            df_w_raw['date'] = pd.to_datetime(df_w_raw['date'], errors='coerce')
            df_w_raw = df_w_raw.set_index('date').sort_index()
            df_w = df_w_raw.resample('W').agg({'high': 'max', 'low': 'min', 'close': 'last'})
            df_w = df_w.dropna().tail(weekly_lookback)
            _cache.set_kline(sym, (df_d, df_w))
        except (ValueError, Exception) as e:
            # ValueError: akshare 返回空数据时列名不匹配（如 Length mismatch）
            # 清除缓存中可能损坏的数据，下次重试
            _cache._kline_cache.pop(sym, None)
            print(f"SR计算失败 {variety}: {e}")
            return (None,) * 13

    df_d = df_d.tail(daily_lookback).copy()
    if len(df_d) < 14:
        return (None,) * 13

    # 防御性检查：确保必要的列存在且有数据（防止缓存数据损坏导致 Length mismatch）
    for col in ['high', 'low', 'close']:
        if col not in df_d.columns or len(df_d[col].dropna()) < 14:
            _cache._kline_cache.pop(sym, None)  # 清除损坏的缓存
            return (None,) * 13

    atr_val = calc_atr(df_d, 14).iloc[-1]
    if pd.isna(atr_val) or atr_val <= 0:
        atr_val = float(df_d['close'].std())

    d_high = float(df_d['high'].max())
    d_low  = float(df_d['low'].min())
    w_high = float(df_w['high'].max()) if len(df_w) > 0 else d_high
    w_low  = float(df_w['low'].min())  if len(df_w) > 0 else d_low

    ma5 = float(df_d['close'].tail(5).mean())
    ma5_nb = abs(ma5 - d_low) / d_low < 0.01
    ma5_nt = abs(ma5 - d_high) / d_high < 0.01

    resist_b = d_high - 0.5 * atr_val
    if ma5_nt: resist_b = min(resist_b, ma5)
    resistance = _ri(min(resist_b, w_high))

    support_b = d_low + 0.5 * atr_val
    if ma5_nb: support_b = max(support_b, ma5)
    support = _ri(max(support_b, w_low))

    kdj = calc_kdj(df_d)
    k_val = d_val = j_val = None
    if kdj:
        k_val, d_val, j_val = kdj
    if k_val is not None:
        if j_val > 80:
            support = _ri(support + 0.3 * atr_val)
        elif j_val < 20:
            resistance = _ri(resistance - 0.3 * atr_val)

    oi_change = calc_oi_change(df_d)
    _, _, _, bb_pos = calc_bollinger(df_d)
    _, _, macd_h = calc_macd(df_d)
    try:
        vol5avg = df_d['volume'].astype(float).tail(6).iloc[:-1].mean()
        vol_ratio = float(df_d['volume'].iloc[-1]) / vol5avg if vol5avg > 0 else None
    except:
        vol_ratio = None
    try:
        ma5v = float(df_d['close'].tail(5).mean())
        ma20 = float(df_d['close'].tail(20).mean())
        ma5_above_ma20 = ma5v > ma20
    except:
        ma5_above_ma20 = None

    div_type, div_strength = calc_divergence(df_d)

    return (support, resistance, k_val, d_val, j_val, float(atr_val),
            oi_change, bb_pos, macd_h, vol_ratio, ma5_above_ma20,
            div_type, div_strength)

# ─────────────────────────────────────────────
# 追踪止损 ATR 获取
# ─────────────────────────────────────────────
def get_atr_now(variety, period=14):
    """获取品种当前 ATR（日线14周期）"""
    prefix = ''.join(filter(str.isalpha, variety)).upper()
    suffix = ''.join(filter(str.isdigit, variety))
    sym = f"{prefix}{suffix}"
    try:
        df = ak.futures_zh_daily_sina(symbol=sym)
        if df is None or len(df) < period:
            return None
        return round(float(calc_atr(df, period).iloc[-1]), 2)
    except Exception:
        return None

# ─────────────────────────────────────────────
# 多周期共振分析
# ─────────────────────────────────────────────
def _get_minute_df(sym, period='60', bars=100):
    """获取分钟K线，period='15'或'60'"""
    try:
        df = ak.futures_zh_minute_sina(symbol=sym, period=period)
        if df is None or len(df) == 0:
            return None
        return df.tail(bars).copy()
    except Exception:
        return None

def _tf_signal(df_daily, df_hour=None, df_15min=None):
    """
    给定三个时间周期的 dataframe，计算该周期的交易信号。
    返回 dict: {trend, kdj_j, support, resistance, atr, signal_score, direction}
    direction: 1=做多信号强, -1=做空信号强, 0=中性
    """
    result = {}
    df = df_daily
    if df is None or len(df) == 0:
        return None
    # 日线基础信号
    atr_s = calc_atr(df, 14).iloc[-1] if len(df) >= 14 else None
    kdj_s = calc_kdj(df)
    k_v, d_v, j_v = (kdj_s or (None, None, None))
    ma5 = float(df['close'].tail(5).mean())
    ma20v = float(df['close'].tail(20).mean()) if len(df) >= 20 else ma5
    ma5_above = ma5 > ma20v
    
    high_n = float(df['high'].tail(20).max())
    low_n = float(df['low'].tail(20).min())
    close_last = float(df['close'].iloc[-1])
    sr_range = high_n - low_n
    
    # 趋势打分（10分制）
    price_pos = (close_last - low_n) / sr_range if sr_range > 0 else 0.5
    trend_score = (1 - price_pos) * 10 if ma5_above else price_pos * 10
    
    # KDJ 信号
    kdj_score = 5.0
    if j_v is not None:
        if j_v < 20:
            kdj_score = 10.0  # 超卖 → 做多有利
        elif j_v > 80:
            kdj_score = 10.0  # 超买 → 做空有利
        else:
            kdj_score = 5.0 + (50 - abs(j_v - 50)) / 50 * 4
    
    # 支撑阻力
    atr_val = float(atr_s) if atr_s is not None else close_last * 0.02
    resistance = _ri(high_n - 0.5 * atr_val)
    support = _ri(low_n + 0.5 * atr_val)
    
    # 综合信号分（10分制）
    signal_score = trend_score * 0.5 + kdj_score * 0.5
    
    # 方向判断
    # 做多信号强：价格在中下轨 + KDJ超卖 + 均线多头
    long_signal = (price_pos < 0.4) and (j_v < 40 if j_v else False) and ma5_above
    short_signal = (price_pos > 0.6) and (j_v > 60 if j_v else False) and not ma5_above
    
    if long_signal:
        direction = 1
    elif short_signal:
        direction = -1
    else:
        direction = 0
    
    return {
        'trend_score': round(trend_score, 1),
        'kdj_score': round(kdj_score, 1),
        'kdj_j': round(j_v, 1) if j_v is not None else None,
        'support': support,
        'resistance': resistance,
        'atr': _ri(atr_val),
        'signal_score': round(signal_score, 1),
        'direction': direction,
        'ma5_above': ma5_above,
        'price_position': round(price_pos, 3),
        'close': close_last,
    }

def calc_multi_resonance(variety):
    """
    多周期共振分析：日线 + 60分钟 + 15分钟
    每个周期给出独立信号，多周期共振时得分加倍。
    返回共振评分和建议方向。
    加缓存（60秒TTL）
    """
    cache_key = f"resonance:{variety}"
    cached = _cache.get_market(cache_key) # 复用市场缓存（TTL 60s）
    if cached:
        return cached

    prefix = "".join(filter(str.isalpha, variety)).upper()
    suffix = "".join(filter(str.isdigit, variety))
    sym = f"{prefix}{suffix}"
    
    # 日线
    cached_k = _cache.get_kline(sym)
    if cached_k is not None:
        df_d, _ = cached_k
    else:
        try:
            df_d = ak.futures_zh_daily_sina(symbol=sym)
        except Exception as e:
            print(f"共振分析失败 {variety}: {e}")
            return None
    
    if df_d is None or len(df_d) < 20:
        return None
    df_d = df_d.tail(60).copy()
    
    # 60分钟（取最近200根）
    df_h = _get_minute_df(sym, '60', 200)
    # 15分钟（取最近200根）
    df_15 = _get_minute_df(sym, '15', 200)
    
    daily_sig = _tf_signal(df_d)
    hour_sig = _tf_signal(df_h) if df_h is not None and len(df_h) >= 20 else None
    min15_sig = _tf_signal(df_15) if df_15 is not None and len(df_15) >= 20 else None
    
    # 共振计算
    scores = {
        'daily': daily_sig,
        'hour': hour_sig,
        'min15': min15_sig,
    }
    
    # 多周期同向加成
    def resonance_score():
        long_count = sum(1 for s in [daily_sig, hour_sig, min15_sig]
                         if s is not None and s['direction'] == 1)
        short_count = sum(1 for s in [daily_sig, hour_sig, min15_sig]
                          if s is not None and s['direction'] == -1)
        # 基线分 = 平均信号分
        valid = [s for s in [daily_sig, hour_sig, min15_sig] if s is not None]
        if not valid:
            return 0, {}
        base = sum(s['signal_score'] for s in valid) / len(valid)
        # 共振加成：2周期同向 +2分，3周期同向 +5分
        resonance_bonus = 0
        resonance_direction = 0
        if long_count >= 2:
            resonance_bonus = 5 if long_count == 3 else 2
            resonance_direction = 1
        elif short_count >= 2:
            resonance_bonus = 5 if short_count == 3 else 2
            resonance_direction = -1
        total = min(10, base + resonance_bonus)
        return round(total, 1), {
            'long_count': long_count,
            'short_count': short_count,
            'resonance_bonus': resonance_bonus,
            'resonance_direction': resonance_direction,
            'base_score': round(base, 1),
        }
    
    total_score, meta = resonance_score()
    
    # 汇聚各周期关键数据
    periods_data = {}
    for label, sig in [('日线', daily_sig), ('60分钟', hour_sig), ('15分钟', min15_sig)]:
        if sig:
            periods_data[label] = {
                'direction': sig['direction'],
                'signal': sig['signal_score'],
                'kdj_j': sig['kdj_j'],
                'ma5_above': sig['ma5_above'],
                'atr': sig['atr'],
                'support': sig['support'],
                'resistance': sig['resistance'],
            }
    
    # 推荐方向
    if meta.get('resonance_direction') == 1:
        recommendation = '做多共振' if meta.get('long_count') == 3 else '偏多'
    elif meta.get('resonance_direction') == -1:
        recommendation = '做空共振' if meta.get('short_count') == 3 else '偏空'
    else:
        recommendation = '中性'
    
    res = {
        'variety': variety.upper(),
        'resonance_score': total_score,
        'recommendation': recommendation,
        'periods': periods_data,
        'meta': meta,
    }
    _cache.set_market(cache_key, res)
    return res

# ─────────────────────────────────────────────
# 动态仓位计算（海龟法则 ATR仓位管理）
# ─────────────────────────────────────────────
def calc_position_sizing(equity, risk_pct, atr, pv):
    """
    海龟式 ATR 仓位管理。
    
    参数:
      equity: 账户权益（元）
      risk_pct: 单笔风险比例（默认 2%）
      atr: 真实波动幅度（价格单位）
      pv: 每点价值（元/点/手）
    
    返回:
      unit: 1个ATR波动对应的合约数（1手起）
      max_units: 最大持仓单元数（海龟法则上限4个）
      risk_amount: 单笔最大亏损金额
      recommended_size: 推荐开仓手数（默认1个单元）
      atr_risk_points: ATR风险点数（止损距离）
    """
    if not all([equity, risk_pct, atr, pv]) or atr <= 0 or pv <= 0:
        return None
    
    risk_amount = equity * (risk_pct / 100)  # 单笔最大亏损金额
    # 单位 = 账户1%风险 / (1个ATR × 每点价值)
    unit = risk_amount / (atr * pv)
    unit = max(1, int(unit))  # 至少1手，向下取整（交易所不支持零碎手）

    max_units = 4  # 海龟法则上限
    atr_risk_points = atr  # 止损距离 = 1 ATR
    recommended_size = unit  # 默认开1个单元
    max_size = unit * max_units  # 最大4个单元

    return {
        'unit': unit,
        'max_units': max_units,
        'max_size': max_size,
        'risk_amount': round(risk_amount, 0),
        'recommended_size': recommended_size,  # 整数手
        'atr_risk_points': _ri(atr_risk_points),
        'risk_pct': risk_pct,
        'atr': _ri(atr),
        'pv': pv,
        # 总仓位风险（所有单元都用完时）
        'total_risk_amount': round(risk_amount * max_units, 0),
        'total_risk_pct': round(risk_pct * max_units, 1),
    }

# ─────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


# ── SSE 实时推送（P0-2 改进） ──────────────────────────
@app.route('/api/stream')
def sse_stream():
    from flask import Response
    _ensure_broadcaster()

    global _SSE_CLIENT_ID
    with _SSE_LOCK:
        _SSE_CLIENT_ID += 1
        client_id = _SSE_CLIENT_ID
        client_queue = queue.Queue()
        client_meta = {"queue": client_queue, "connected_at": time.time(),
                       "last_msg": time.time(), "alive": True}
        _SSE_CLIENTS.append((client_id, client_meta))
        _SSE_CONNECTIONS[client_id] = client_meta

    with _sse_global_lock:
        global _sse_connection_count, _sse_last_heartbeat
        _sse_connection_count += 1
        _sse_last_heartbeat = time.time()

    def enqueue(msg):
        try:
            client_queue.put_nowait(msg)
        except queue.Full:
            pass

    try:
        while True:
            try:
                msg = client_queue.get(timeout=55)
                yield msg
            except queue.Empty:
                # 发心跳保活
                yield f"event: heartbeat\ndata: {json.dumps({'ts': time.time()})}\n\n"
    except GeneratorExit:
        pass  # 客户端主动断开，正常
    except Exception as e:
        print(f"SSE stream error: {e}")
    finally:
        with _SSE_LOCK:
            for i, (c_id, c) in enumerate(_SSE_CLIENTS):
                if c_id == client_id:
                    c['alive'] = False
                    _SSE_CLIENTS.pop(i)
                    break
        with _sse_global_lock:
            _sse_connection_count = max(0, _sse_connection_count - 1)
        if client_id in _SSE_CONNECTIONS:
            del _SSE_CONNECTIONS[client_id]

@app.after_request
def _ensure_sse_content_type(response):
    if request.path == '/api/stream':
        response.headers['Content-Type'] = 'text/event-stream; charset=utf-8'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Accel-Buffering'] = 'no'
    return response

# SSE 连接状态查询（P0-3 前端状态灯）
@app.route('/api/sse_status', methods=['GET'])
def sse_status():
    with _sse_global_lock:
        count = _sse_connection_count
        last_hb = _sse_last_heartbeat
    now = time.time()
    # 判断连接状态：最后心跳 > 20s → 断开
    if count == 0:
        status = "disconnected"
    elif now - last_hb > 20:
        status = "stale"
    else:
        status = "connected"
    return jsonify({
        "connections": count,
        "last_heartbeat": last_hb,
        "status": status,
        "server_time": now
    })

# ── 账户资金配置 ─────────────────────────────────────────
@app.route('/api/account', methods=['GET', 'PUT'])
def handle_account():
    if request.method == 'GET':
        acct = _load_account()
        return jsonify(acct)
    data = request.json or {}
    acct = _load_account()
    if 'total_balance' in data:
        acct['total_balance'] = float(data['total_balance'])
    if 'margin_alert_threshold' in data:
        val = float(data['margin_alert_threshold'])
        acct['margin_alert_threshold'] = val
        if 'alerts' not in acct:
            acct['alerts'] = {}
        acct['alerts']['leverage_alert'] = val
    if 'webhook_url' in data:
        acct['webhook_url'] = data['webhook_url'].strip()
    _save_account(acct)
    return jsonify({"message": "已保存", "account": acct})

# ── 风控预警 ─────────────────────────────────────────────
@app.route('/api/alerts', methods=['GET', 'POST'])
def handle_alerts():
    acct = _load_account()
    alerts_cfg = acct.get('alerts', {
        "sound": True,
        "sl_hit": True,
        "tp_hit": True,
        "score_reversal": 30,
        "leverage_alert": 80,
        "expiry_warning": True
    })
    if request.method == 'GET':
        return jsonify(alerts_cfg)
    data = request.json or {}
    acct['alerts'] = {**alerts_cfg, **data}
    _save_account(acct)
    return jsonify({"message": "预警配置已保存", "alerts": acct['alerts']})

_alert_rate_limit = {'last_time': 0.0}  # check_alerts 防重放

@app.route('/api/alerts/check', methods=['GET'])
def check_alerts():
    # Rate limit: 5秒内只处理一次，防止重复预警
    now = time.time()
    if now - _alert_rate_limit['last_time'] < 5:
        return jsonify({"alerts": [], "has_alerts": False, "_rate_limited": True})
    _alert_rate_limit['last_time'] = now
    positions = load_positions()
    if not positions:
        return jsonify({"alerts": [], "has_alerts": False})
    acct = _load_account()
    alerts = acct.get('alerts', {})
    fired = []

    total_bal = acct.get('total_balance', 1_000_000)
    threshold = acct.get('margin_alert_threshold', 80)
    # ── 一次性抓取所有持仓价格（避免重复串行调用）──
    _price_cache = {}
    for p in positions:
        variety = p.get('variety', '')
        if variety not in _price_cache:
            _price_cache[variety] = get_realtime_price(variety)

    def _exposed_one(p):
        cur = (_price_cache.get(p.get('variety', ''), (0, None, None))[0] or 0)
        prefix_key = "".join(filter(str.isalpha, p.get('variety', ''))).upper()
        pv = POINT_VALUE.get(prefix_key, 1)
        return cur * pv * p.get('quantity', 1)

    total_exposed = sum(_exposed_one(p) for p in positions)
    total_pnl_for_equity = 0
    for p in positions:
        cur = (_price_cache.get(p.get('variety', ''), (0, None, None))[0] or 0)
        if cur and p.get('entry_price'):
            pv = POINT_VALUE.get("".join(filter(str.isalpha, p.get('variety', ''))).upper(), 1)
            qty = p.get('quantity', 1)
            entry = float(p.get('entry_price'))
            if p.get('direction') == 'long':
                total_pnl_for_equity += (cur - entry) * pv * qty
            else:
                total_pnl_for_equity += (entry - cur) * pv * qty
    current_equity = total_bal + total_pnl_for_equity
    margin_util = (total_exposed / current_equity) * 100 if current_equity > 0 else 0
    if margin_util > 1000:
        # 权益接近零时杠杆率会暴表（如浮亏导致权益≈0），
        # 此时用名义本金×10%保证金率估算，避免显示99999%的荒谬数字
        margin_util = (total_exposed * 0.10 / current_equity * 100) if current_equity > 0 else 0
    if alerts.get('leverage_warning', True) and margin_util > threshold:
        fired.append({"type": "LEVERAGE_ALERT", "margin_util_pct": round(margin_util, 1), "threshold": threshold, "level": "warning"})

    for pos in positions:
        prefix = "".join(filter(str.isalpha, pos.get('variety', ''))).upper()
        pv = POINT_VALUE.get(prefix, 1)
        qty = pos.get('quantity', 1)
        direction = pos.get('direction', 'long')
        entry = float(pos.get('entry_price', 0))
        cur_price, _, _ = _price_cache.get(pos.get('variety', ''), (None, None, None))
        if cur_price is None:
            continue
        sr = calc_sr_multi_period(pos.get('variety', ''))
        support, resistance = sr[0], sr[1]
        if support and alerts.get('sl_hit'):
            if direction == 'long' and cur_price <= support:
                fired.append({"type": "SL_HIT", "variety": pos['variety'], "price": cur_price, "level": "danger"})
            elif direction == 'short' and cur_price >= resistance:
                fired.append({"type": "SL_HIT", "variety": pos['variety'], "price": cur_price, "level": "danger"})
        suffix = "".join(filter(str.isdigit, pos.get('variety', '')))
        if len(suffix) == 4 and alerts.get('expiry_warning'):
            prefix_sym = "".join(filter(str.isalpha, pos.get('variety', ''))).upper()
            meta = VARIETY_META_LOWER.get(prefix_sym.lower())
            if meta:
                now = datetime.datetime.now()
                try:
                    df_realtime = ak.futures_zh_realtime(symbol=meta[1])
                    contracts_oi = []
                    for _, row in df_realtime.iterrows():
                        sym = str(row.get('symbol', ''))
                        if len(sym) < 6 or not sym[:2].isalpha():
                            continue
                        oi = float(row.get('position') or 0)
                        if oi > 0:
                            contracts_oi.append((sym, oi))
                    if contracts_oi:
                        contracts_oi.sort(key=lambda x: x[1], reverse=True)
                        dominant = contracts_oi[0][0]
                        if dominant and dominant.upper() != pos['variety'].upper():
                            dom_suffix = "".join(filter(str.isdigit, dominant))
                            if dom_suffix and dom_suffix != suffix:
                                fired.append({
                                    "type": "EXPIRY_WARNING",
                                    "variety": pos['variety'],
                                    "message": f"{dominant} 已成主力（持仓量更高），当前持仓 {pos['variety']} 需关注展期",
                                    "level": "warning"
                                })
                except Exception as e:
                    print(f"换月预警检测失败: {e}")
                suffix_month = int(suffix[2:4])
                if suffix_month in (1, 5, 9) and now.day >= 20:
                    fired.append({
                        "type": "EXPIRY_WARNING",
                        "variety": pos['variety'],
                        "message": f"{pos['variety']} 为主力切换月（{suffix_month}月），请关注是否需要展期",
                        "level": "warning"
                    })
    has_alerts = len(fired) > 0
    if has_alerts:
        webhook_url = _load_account().get('webhook_url', '')
        if webhook_url:
            threading.Thread(target=_send_webhook, args=(webhook_url, fired), daemon=True).start()
    return jsonify({"alerts": fired, "has_alerts": has_alerts})

@app.route('/api/positions', methods=['GET', 'POST'])
def handle_positions():
    if request.method == 'GET':
        positions = load_positions()
        result = []
        for pos in positions:
            prefix = "".join(filter(str.isalpha, pos.get('variety', '')))
            pv = POINT_VALUE.get(prefix, 1)
            entry = float(pos.get('entry_price', 0))
            qty = float(pos.get('quantity', 1))
            direction = pos.get('direction', 'long')
            cur_price, _, name = get_realtime_price(pos.get('variety', ''))
            pnl = None
            if cur_price:
                pnl = (((cur_price - entry) * pv * qty) - (FEE_PER_LOT * qty) if direction == 'long'
                       else ((entry - cur_price) * pv * qty) - (FEE_PER_LOT * qty))

            sr = calc_sr_multi_period(pos.get('variety', ''))
            (support, resistance, k_val, d_val, j_val, atr_val,
             oi_change, bb_pos, macd_h, vol_ratio, ma5_above_ma20,
             div_type, div_strength) = sr

            score = None
            rr_ratio = None
            stop_loss = None
            take_profit = None
            entry_prompt_long = None
            entry_prompt_short = None
            if support and resistance and cur_price:
                score, score_detail = calc_sr_score(
                    cur_price, support, resistance, entry, direction,
                    k_val=k_val, d_val=d_val, j_val=j_val, atr_val=atr_val,
                    oi_change=oi_change, bb_position=bb_pos, macd_hist=macd_h,
                    vol_ratio=vol_ratio, ma5_above_ma20=ma5_above_ma20,
                    div_type=div_type, div_strength=div_strength)
                atr = atr_val or 0
                entry_prompt_long = _ri(support + 0.3 * atr) if support else None
                entry_prompt_short = _ri(resistance - 0.3 * atr) if resistance else None
                if cur_price != support:
                    # 计算止损（先算出来再用于 R/R）
                    if direction == 'long':
                        stop_loss = _ri(support - 0.5 * atr)
                        # R/R = 剩余盈利 / 当前风险（从当前价到止损）
                        risk = cur_price - stop_loss
                        reward = resistance - cur_price
                        rr_ratio = round(reward / risk, 2) if risk > 0 else None
                        take_profit = resistance
                    else:
                        stop_loss = _ri(resistance + 0.5 * atr)
                        # R/R = 剩余盈利 / 当前风险（从当前价到止损）
                        risk = stop_loss - cur_price
                        reward = cur_price - support
                        rr_ratio = round(reward / risk, 2) if risk > 0 else None
                        take_profit = support

            # ── 追踪止损计算 ──
            atr_now = atr_val
            tp_hit = False
            trail_active = False
            trail_price = None
            if cur_price and atr_now and support and resistance:
                if direction == 'long':
                    tp_hit = (cur_price >= resistance)
                    if tp_hit:
                        trail_active = True
                        # 动态追踪：随价格上涨不断提升止损位
                        new_trail = round(cur_price - 0.5 * atr_now, 2)
                        prev_trail = pos.get('trail_price')
                        trail_price = max(prev_trail or 0, new_trail) if prev_trail else new_trail
                else:  # short
                    tp_hit = (cur_price <= support)
                    if tp_hit:
                        trail_active = True
                        new_trail = round(cur_price + 0.5 * atr_now, 2)
                        prev_trail = pos.get('trail_price')
                        trail_price = min(prev_trail or 999999, new_trail) if prev_trail else new_trail

            out = dict(pos)
            out.update({
                'pnl': round(pnl, 0) if pnl is not None else None,
                'current_price': cur_price,
                'point_value': pv,
                'variety_name': name,
                'support': support,
                'resistance': resistance,
                'score': score,
                'rr_ratio': rr_ratio,
                'stop_loss': stop_loss,
                'take_profit': _ri(take_profit) if take_profit else None,
                'atr': round(atr_now, 2) if atr_now else None,
                'trail_active': trail_active,
                'trail_price': trail_price,
                'tp_hit': tp_hit,
                'kdj': {"K": round(k_val,1), "D": round(d_val,1), "J": round(j_val,1)} if k_val else None,
                'oi_change': round(oi_change, 3) if oi_change is not None else None,
                'bb_position': round(bb_pos, 2) if bb_pos is not None else None,
                'vol_ratio': round(vol_ratio, 2) if vol_ratio is not None else None,
                'score_detail': score_detail,
                'divergence': {"type": div_type, "strength": div_strength} if div_type != 0 else None,
                'entry_prompt_long': entry_prompt_long,
                'entry_prompt_short': entry_prompt_short,
            })
            # 动态仓位建议（海龟法则 ATR）
            if atr_val and atr_val > 0 and pv:
                equity = _load_account().get('total_balance', 1_000_000)
                try:
                    sizing = calc_position_sizing(equity, 2.0, float(atr_val), pv)
                    out['position_sizing'] = sizing
                except Exception:
                    pass
            result.append(out)
        total_pnl = sum(r.get('pnl', 0) or 0 for r in result)
        acct = _load_account()
        total_balance = acct.get('total_balance', 1_000_000)
        # 计算名义值（用于保证金估算）
        total_exposed = 0
        for pos in positions:
            prefix = "".join(filter(str.isalpha, pos.get('variety', '')))
            pv = POINT_VALUE.get(prefix, 1)
            cur_price, _, _ = get_realtime_price(pos.get('variety', ''))
            if cur_price:
                total_exposed += cur_price * pv * float(pos.get('quantity', 1))
        # 当前权益 = 余额 + 浮动盈亏
        current_equity = total_balance + total_pnl
        # 估算保证金占用（名义值 × 10%，各品种大约需要 5%~15% 的名义值作为保证金）
        MARGIN_RATIO = 0.10
        margin_used = total_exposed * MARGIN_RATIO
        available_balance = max(0, current_equity - margin_used)
        margin_util_pct = (margin_used / current_equity * 100) if current_equity > 0 else 0
        leverage = (total_exposed / current_equity) if current_equity > 0 else 0
        account_obj = {
            "total_balance": total_balance,
            "available_balance": round(available_balance, 0),
            "margin_util_pct": round(margin_util_pct, 1),
            "margin_alert_threshold": acct.get('margin_alert_threshold', 80),
            "leverage": round(leverage, 2)
        }
        return jsonify({"positions": result, "total_pnl": round(total_pnl, 0), "account": account_obj})
    data = request.json or {}
    variety = data.get('variety', '').strip()
    direction = data.get('direction', 'long')
    entry_price = float(data.get('entry_price', 0))
    quantity = int(data.get('quantity', 1))
    if not variety or entry_price <= 0:
        return jsonify({"error": "无效参数"}), 400
    positions = load_positions()
    # 读取当前最大 index
    max_idx = max([0] + [p.get('_idx', 0) for p in positions]) if positions else 0
    new_pos = {"_idx": max_idx + 1, "variety": variety.upper(),
               "direction": direction, "entry_price": entry_price, "quantity": quantity}
    positions.append(new_pos)
    save_positions(positions)
    # 添加时立即抓取该品种行情（后台线程，不阻塞响应）
    threading.Thread(target=_refresh_price_now, args=(variety.upper(),), daemon=True).start()
    return jsonify({"message": "持仓已添加", "position": new_pos})

@app.route('/api/positions/<int:index>', methods=['DELETE'])
def delete_position(index):
    with _positions_lock:
        positions = load_positions()
        # 通过 _idx 匹配
        target = next((p for p in positions if p.get('_idx') == index), None)
        new_positions = [p for p in positions if p.get('_idx') != index]
        if len(new_positions) == len(positions):
            return jsonify({"error": "持仓不存在"}), 404
        save_positions(new_positions)
        # 删除时清除该品种缓存
        if target:
            _cache.invalidate(target.get('variety', ''))
    return jsonify({"message": "已删除"})

# ── 多周期共振 ─────────────────────────────────────────
@app.route('/api/resonance/<variety>', methods=['GET'])
def api_resonance(variety):
    """多周期共振分析：日线×60分钟×15分钟共振信号"""
    result = calc_multi_resonance(variety)
    if result is None:
        return jsonify({"error": f"无法获取 {variety} 共振数据"}), 500
    return jsonify(result)

# ── 动态仓位建议 ─────────────────────────────────────────
@app.route('/api/position_sizing', methods=['GET'])
def api_position_sizing():
    """
    ATR海龟式仓位计算。
    参数: variety (optional) — 品种代码，有则用其ATR
          equity — 账户权益（默认从 account.json）
          risk_pct — 风险比例%（默认2）
    """
    variety = request.args.get('variety', '').strip()
    equity = float(request.args.get('equity') or _load_account().get('total_balance', 1_000_000))
    risk_pct = float(request.args.get('risk_pct') or 2.0)
    
    atr = None
    pv = None
    if variety:
        prefix = "".join(filter(str.isalpha, variety)).upper()
        pv = POINT_VALUE.get(prefix, 1)
        sr = calc_sr_multi_period(variety)
        if sr and sr[5] is not None:
            atr = float(sr[5])
    
    if atr is None:
        # 无品种时用通用ATR估算（价格×2%）
        if variety:
            cur, _, _ = get_realtime_price(variety)
            if cur:
                atr = cur * 0.02
                pv = POINT_VALUE.get("".join(filter(str.isalpha, variety)).upper(), 1)
        if atr is None:
            return jsonify({"error": "请提供有效品种以获取ATR"}), 400
    
    sizing = calc_position_sizing(equity, risk_pct, atr, pv)
    sizing['variety'] = variety.upper() if variety else None
    sizing['equity'] = equity
    return jsonify(sizing)

@app.route('/api/quote/<variety>', methods=['GET'])
def quote_variety(variety):
    prefix = "".join(filter(str.isalpha, variety))
    suffix = "".join(filter(str.isdigit, variety))
    sym = f"{prefix.upper()}{suffix}"
    cur_price, _, name = get_realtime_price(variety)
    if cur_price is None:
        return jsonify({"error": f"无法获取 {variety} 行情"}), 404
    sr = calc_sr_multi_period(variety)
    (support, resistance, k_val, d_val, j_val, atr_val,
     oi_change, bb_pos, macd_h, vol_ratio, ma5_above_ma20,
     div_type, div_strength) = sr
    if support is None:
        return jsonify({"error": f"无法计算 {variety} 技术指标"}), 500
    result = {
        "variety": variety.upper(), "name": name, "current_price": cur_price,
        "support": support, "resistance": resistance,
        "atr": round(atr_val, 2) if atr_val else None,
        "kdj": {"K": round(k_val,1), "D": round(d_val,1), "J": round(j_val,1)} if k_val else None,
        "oi_change": round(oi_change, 3) if oi_change is not None else None,
        "bb_position": round(bb_pos, 2) if bb_pos is not None else None,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "divergence": {"type": div_type, "strength": div_strength} if div_type != 0 else None,
    }

    # 多周期共振（附加到 quote 响应，默认关闭避免拖慢）
    if request.args.get('resonance') == '1':
        try:
            resonance = calc_multi_resonance(variety)
            if resonance:
                result['resonance'] = resonance
        except Exception as e:
            print(f"共振数据获取失败: {e}")

    # 动态仓位建议
    try:
        equity = _load_account().get('total_balance', 1_000_000)
        pv = POINT_VALUE.get(prefix.upper(), 1)
        if atr_val and atr_val > 0:
            sizing = calc_position_sizing(equity, 2.0, float(atr_val), pv)
            result['position_sizing'] = sizing
    except Exception as e:
        print(f"仓位计算失败: {e}")

    for direction in ['long', 'short']:
        score, detail = calc_sr_score(
            cur_price, support, resistance, cur_price, direction,
            k_val=k_val, d_val=d_val, j_val=j_val, atr_val=atr_val,
            oi_change=oi_change, bb_position=bb_pos, macd_hist=macd_h,
            vol_ratio=vol_ratio, ma5_above_ma20=ma5_above_ma20,
            div_type=div_type, div_strength=div_strength)
        atr = atr_val or 0
        result[f'score_{direction}'] = score
        result[f'entry_prompt_{direction}'] = (
            round(support - 0.5 * atr, 2) if direction == 'long' else round(resistance + 0.5 * atr, 2)
        )
        if cur_price != support:
            rr = (resistance - cur_price) / (cur_price - support) if direction == 'long' \
                 else (cur_price - support) / (resistance - cur_price)
            result[f'rr_{direction}'] = round(rr, 2)
    return jsonify(result)

@app.route('/api/lookup', methods=['GET'])
def lookup_variety():
    variety = request.args.get('variety', '').strip()
    if not variety:
        return jsonify({"error": "缺少 variety 参数"}), 400
    prefix = "".join(filter(str.isalpha, variety))
    suffix = "".join(filter(str.isdigit, variety))
    sym = f"{prefix.upper()}{suffix}"
    cur_price, name, _ = get_realtime_price(variety)
    if cur_price is None:
        return jsonify({"error": f"找不到 {variety}"}), 404
    return jsonify({"variety": variety.upper(), "price": cur_price, "name": name})

# ── 自选合约 ─────────────────────────────────────────────
@app.route('/api/watchlist', methods=['GET', 'POST', 'DELETE'])
def handle_watchlist():
    if request.method == 'GET':
        # GET 返回统一格式：[{"variety": "TA2605"}, ...]
        wl = load_watchlist()
        return jsonify([{"variety": v} if isinstance(v, str) else v for v in wl])
    if request.method == 'POST':
        data = request.json or {}
        variety = data.get('variety', '').strip().upper()
        if not variety:
            return jsonify({"error": "无效合约"}), 400
        wl = load_watchlist()
        # 去重（支持新旧两种格式）
        wl_strs = [v if isinstance(v, str) else v.get('variety', '') for v in wl]
        if variety not in wl_strs:
            wl.append(variety)  # 存字符串，统一格式
            save_watchlist(wl)
            # 添加时立即抓取该品种行情
            threading.Thread(target=_refresh_price_now, args=(variety,), daemon=True).start()
        # 返回统一格式
        return jsonify({"message": "已添加", "watchlist": [{"variety": v} if isinstance(v, str) else v for v in wl]})
    if request.method == 'DELETE':
        # 支持 URL 路径参数（如 /api/watchlist/TA2605）或 JSON body
        variety = None
        if request.args:
            variety = request.args.get('variety', '').strip().upper()
        if not variety and request.json:
            variety = request.json.get('variety', '').strip().upper()
        if not variety:
            return jsonify({"error": "缺少 variety 参数"}), 400
        wl = load_watchlist()
        # 兼容处理：可能是字符串也可能是对象
        wl = [v for v in wl if (v if isinstance(v, str) else v.get('variety', '')) != variety]
        save_watchlist(wl)
        # 删除时清除该品种缓存
        _cache.invalidate(variety)
        return jsonify({"message": "已删除", "watchlist": [{"variety": v} if isinstance(v, str) else v for v in wl]})

# ── VaR 计算 ──────────────────────────────────────────────
@app.route('/api/var', methods=['GET'])
def var_report():
    positions = load_positions()
    if not positions:
        return jsonify({"var_95": 0, "var_99": 0, "positions": []})
    try:
        prices = {}
        rets = {}
        for pos in positions:
            v = pos.get('variety', '')
            cur, _, _ = get_realtime_price(v)
            if cur:
                prices[v] = cur
            prefix = "".join(filter(str.isalpha, v)).upper()
            suffix = "".join(filter(str.isdigit, v))
            sym = f"{prefix}{suffix}"
            cached = _cache.get_kline(sym)
            if cached:
                df_d, _ = cached
            else:
                try:
                    df_d = ak.futures_zh_daily_sina(symbol=sym)
                    if df_d is not None and len(df_d) > 20:
                        df_w_raw = ak.futures_zh_daily_sina(symbol=sym)
                        df_w_raw['date'] = pd.to_datetime(df_w_raw['date'], errors='coerce')
                        df_w_raw = df_w_raw.set_index('date').sort_index()
                        df_w = df_w_raw.resample('W').agg({'high': 'max', 'low': 'min', 'close': 'last'})
                        df_w = df_w.dropna().tail(12)
                        _cache.set_kline(sym, (df_d, df_w))
                except:
                    continue
            if df_d is not None and len(df_d) > 20:
                close_s = df_d['close'].astype(float).tail(60)
                log_ret = close_s.pct_change().dropna()
                ret_mean = log_ret.mean()
                ret_std = log_ret.std(ddof=0)  # 总体标准差（ddof=0），VaR/波动率计算标准用法
                rets[v] = (ret_mean, ret_std)
        acct = _load_account()
        total_bal = acct.get('total_balance', 1_000_000)
        var_95 = 0; var_99 = 0; details = []
        for pos in positions:
            v = pos.get('variety', '')
            if v not in rets or v not in prices:
                continue
            ret_mean, ret_std = rets[v]
            pv = POINT_VALUE.get("".join(filter(str.isalpha, v)).upper(), 1)
            qty = pos.get('quantity', 1)
            notional = prices[v] * pv * qty
            exposure = notional / total_bal
            direction = pos.get('direction', 'long')
            z95 = 1.645; z99 = 2.326
            # VaR = 名义本金 × 日收益率标准差 × z值
            # ret_std 用 ddof=0（总体标准差），取 abs() 消除方向影响（做空同样关注上涨风险）
            daily_var_95 = abs(notional * ret_std * z95)
            daily_var_99 = abs(notional * ret_std * z99)
            var_95 += daily_var_95; var_99 += daily_var_99
            details.append({"variety": v, "var_95": round(daily_var_95), "var_99": round(daily_var_99)})
        return jsonify({"var_95": round(var_95), "var_99": round(var_99), "positions": details})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 趋势突破回测 ──────────────────────────────────────────
def _backtest_one(variety, qty, pv, lookback, atr_mult, direction=None):
    """
    趋势突破回测 — 完全基于支撑/阻力
    - 做多：收盘突破阻力 → 开多；收盘跌破支撑 → 止损
    - 做空：收盘跌破支撑 → 开空；收盘突破阻力 → 止损
    - 移动止损：持仓≥10天后，收盘反向突破1*ATR则提前退出
    - 滑点：入场和出场各扣0.1%
    """
    prefix = "".join(filter(str.isalpha, variety)).upper()
    suffix = "".join(filter(str.isdigit, variety))
    sym = f"{prefix}{suffix}"
    slippage = 0.002
    try:
        df = ak.futures_zh_daily_sina(symbol=sym)
        if df is None or len(df) < 30:
            return None
        df = df.tail(lookback + 20).copy().reset_index(drop=True)
        atr = calc_atr(df, 14)
        df['atr'] = atr
        df['support'] = None; df['resistance'] = None
        # 计算每日 S/R（前20日极值 ± 0.5*ATR）
        for i in range(20, len(df)):
            if pd.isna(df['atr'].iloc[i]): continue
            lo_n = float(df['low'].astype(float).iloc[max(0, i-20):i].min())
            hi_n = float(df['high'].astype(float).iloc[max(0, i-20):i].max())
            df.at[df.index[i], 'support'] = lo_n + 0.5 * float(df['atr'].iloc[i])
            df.at[df.index[i], 'resistance'] = hi_n - 0.5 * float(df['atr'].iloc[i])
        pos = 0; entry_price = 0; entry_idx = 0; trades = []
        # 追踪止损状态（持仓中随价格更新）
        trail_long = False; trail_short = False
        trail_price = 0.0
        for i in range(20, len(df)):
            if pd.isna(df['atr'].iloc[i]) or pd.isna(df['support'].iloc[i]):
                continue
            c = float(df['close'].iloc[i])
            atr_v = float(df['atr'].iloc[i])
            sup = float(df['support'].iloc[i])
            res = float(df['resistance'].iloc[i])
            ts_active = (i - entry_idx >= 10)  # 持仓≥10日后激活移动止损
            ts_level_long = float(df['close'].iloc[i-1]) - 1 * atr_v if ts_active else None
            ts_level_short = float(df['close'].iloc[i-1]) + 1 * atr_v if ts_active else None
            # ── 入场 ──
            if pos == 0 and c > res:  # 收盘突破阻力 → 做多
                if direction and direction != 'long': pass
                else:
                    pos = 1; entry_price = c * (1 - slippage / 2); entry_idx = i
                    trail_long = False; trail_short = False
            elif pos == 0 and c < sup:  # 收盘跌破支撑 → 做空
                if direction and direction != 'short': pass
                else:
                    pos = -1; entry_price = c * (1 + slippage / 2); entry_idx = i
                    trail_long = False; trail_short = False
            # ── 持仓 ──
            elif pos == 1:
                # 触发止盈位 → 激活追踪止损（TPHit后持续有效）
                if c >= res and not trail_long:
                    trail_long = True
                    trail_price = entry_price + 0.5 * atr_v  # 初始追踪线
                # 追踪止损只上移（跟随最高收盘价）
                if trail_long:
                    new_trail = c - 0.5 * atr_v
                    trail_price = max(trail_price, new_trail)
                    # 价格跌破追踪止损线 → 追踪止损退出
                    if c < trail_price:
                        exit_px = c * (1 - slippage / 2)
                        pnl = (exit_px - entry_price) * pv * qty
                        trades.append({"entry": round(entry_price, 2), "exit": round(exit_px, 2), "pnl": round(pnl), "type": "long", "entry_idx": int(entry_idx), "exit_idx": int(i), "entry_date": str(df["date"].iloc[entry_idx]), "exit_date": str(df["date"].iloc[i]), "exit_reason": "trailing_stop"})
                        pos = 0; trail_long = False
                        continue
                # 止损：跌破支撑（或移动止损触发）
                if c < sup or (ts_active and ts_level_long is not None and c < ts_level_long):
                    exit_px = c * (1 - slippage / 2)
                    pnl = (exit_px - entry_price) * pv * qty
                    trades.append({"entry": round(entry_price, 2), "exit": round(exit_px, 2), "pnl": round(pnl), "type": "long", "entry_idx": int(entry_idx), "exit_idx": int(i), "entry_date": str(df["date"].iloc[entry_idx]), "exit_date": str(df["date"].iloc[i]), "exit_reason": "stop_loss"})
                    pos = 0; trail_long = False
            elif pos == -1:
                # 触发止盈位 → 激活追踪止损
                if c <= sup and not trail_short:
                    trail_short = True
                    trail_price = entry_price - 0.5 * atr_v
                # 追踪止损只下移（跟随最低收盘价）
                if trail_short:
                    new_trail = c + 0.5 * atr_v
                    trail_price = min(trail_price, new_trail)
                    if c > trail_price:
                        exit_px = c * (1 + slippage / 2)
                        pnl = (entry_price - exit_px) * pv * qty
                        trades.append({"entry": round(entry_price, 2), "exit": round(exit_px, 2), "pnl": round(pnl), "type": "short", "entry_idx": int(entry_idx), "exit_idx": int(i), "entry_date": str(df["date"].iloc[entry_idx]), "exit_date": str(df["date"].iloc[i]), "exit_reason": "trailing_stop"})
                        pos = 0; trail_short = False
                        continue
                # 止损：突破阻力（或移动止损触发）
                if c > res or (ts_active and ts_level_short is not None and c > ts_level_short):
                    exit_px = c * (1 + slippage / 2)
                    pnl = (entry_price - exit_px) * pv * qty
                    trades.append({"entry": round(entry_price, 2), "exit": round(exit_px, 2), "pnl": round(pnl), "type": "short", "entry_idx": int(entry_idx), "exit_idx": int(i), "entry_date": str(df["date"].iloc[entry_idx]), "exit_date": str(df["date"].iloc[i]), "exit_reason": "stop_loss"})
                    pos = 0; trail_short = False
        wins = [t['pnl'] for t in trades if t['pnl'] > 0]; losses = [t['pnl'] for t in trades if t['pnl'] <= 0]
        total_pnl = sum(t['pnl'] for t in trades)
        n_wins = len(wins); n_losses = len(losses); n_trades = len(trades)
        avg_win_v = sum(wins) / max(1, n_wins) if wins else 0
        avg_loss_v = sum(losses) / max(1, n_losses) if losses else 0
        pnl_ratio = round(avg_win_v / abs(avg_loss_v), 2) if avg_loss_v != 0 else None
        # OHLC（从有数据的部分开始，仅在有交易时返回）
        ohlc = None
        if trades:
            start = 20
            _dates   = [str(d) for d in df['date'].iloc[start:].tolist()]
            _open    = [float(x) for x in df['open'].iloc[start:].tolist()]
            _high    = [float(x) for x in df['high'].iloc[start:].tolist()]
            _low     = [float(x) for x in df['low'].iloc[start:].tolist()]
            _close   = [float(x) for x in df['close'].iloc[start:].tolist()]
            _sup_arr = df['support'].iloc[start:]
            _res_arr = df['resistance'].iloc[start:]
            _sup_l   = [None if pd.isna(_sup_arr.iloc[j]) else round(float(_sup_arr.iloc[j]), 2) for j in range(len(_sup_arr))]
            _res_l   = [None if pd.isna(_res_arr.iloc[j]) else round(float(_res_arr.iloc[j]), 2) for j in range(len(_res_arr))]
            ohlc = {
                "dates":     _dates,
                "open":      _open,
                "high":      _high,
                "low":       _low,
                "close":     _close,
                "support":   _sup_l,
                "resistance":_res_l,
            }
        return {
            "variety": variety, "trades": trades, "total_pnl": round(total_pnl),
            "total_trades": n_trades, "win": n_wins, "loss": n_losses,
            "win_rate": round(n_wins / max(1, n_trades) * 100, 1),
            "avg_win": round(avg_win_v, 0), "avg_loss": round(avg_loss_v, 0),
            "max_win": round(max(wins), 0) if wins else 0,
            "max_loss": round(min(losses), 0) if losses else 0,
            "pnl_ratio": pnl_ratio,
            "ohlc": ohlc,
        }
    except Exception as e:
        print(f"回测失败 {variety}: {e}")
        return None

def _backtest_mean_reversion(variety, qty, pv, lookback, direction=None):
    """
    均值回归回测 — 完全基于支撑/阻力
    - 做多：收盘碰到支撑 → 开多；收盘碰到阻力 → 止损
    - 做空：收盘碰到阻力 → 开空；收盘碰到支撑 → 止损
    - 止盈：回归到区间中轴（(支撑+阻力)/2）时主动退出
    - 滑点：入场和出场各扣0.1%
    """
    prefix = "".join(filter(str.isalpha, variety)).upper()
    suffix = "".join(filter(str.isdigit, variety))
    sym = f"{prefix}{suffix}"
    slippage = 0.002
    try:
        df = ak.futures_zh_daily_sina(symbol=sym)
        if df is None or len(df) < 30:
            return None
        df = df.tail(lookback + 10).copy().reset_index(drop=True)
        atr = calc_atr(df, 14)
        df['atr'] = atr
        df['support'] = None; df['resistance'] = None
        for i in range(20, len(df)):
            if pd.isna(df['atr'].iloc[i]): continue
            lo_n = float(df['low'].astype(float).iloc[max(0, i-20):i].min())
            hi_n = float(df['high'].astype(float).iloc[max(0, i-20):i].max())
            df.at[df.index[i], 'support'] = lo_n + 0.5 * float(df['atr'].iloc[i])
            df.at[df.index[i], 'resistance'] = hi_n - 0.5 * float(df['atr'].iloc[i])
        pos = 0; entry_price = 0; entry_idx = 0; trades = []
        for i in range(20, len(df)):
            if pd.isna(df['atr'].iloc[i]) or pd.isna(df['support'].iloc[i]):
                continue
            c = float(df['close'].iloc[i])
            atr_v = float(df['atr'].iloc[i])
            sup = float(df['support'].iloc[i])
            res = float(df['resistance'].iloc[i])
            mid = (sup + res) / 2.0  # 区间中轴止盈目标
            # ── 入场 ──
            if pos == 0 and c <= sup:  # 收盘碰到/跌破支撑 → 做多
                if direction and direction != 'long': pass
                else:
                    pos = 1; entry_price = c * (1 - slippage / 2); entry_idx = i
            elif pos == 0 and c >= res:  # 收盘碰到/突破阻力 → 做空
                if direction and direction != 'short': pass
                else:
                    pos = -1; entry_price = c * (1 + slippage / 2); entry_idx = i
            # ── 持仓 ──
            elif pos == 1:
                # 止损：收盘碰到阻力（回归失败）
                # 止盈：收盘回归到中轴（mid）
                exited = c >= res or c >= mid
                if exited:
                    exit_px = c * (1 - slippage / 2)
                    pnl = (exit_px - entry_price) * pv * qty
                    trades.append({"entry": round(entry_price, 2), "exit": round(exit_px, 2), "pnl": round(pnl), "type": "long", "entry_idx": int(entry_idx), "exit_idx": int(i), "entry_date": str(df["date"].iloc[entry_idx]), "exit_date": str(df["date"].iloc[i])})
                    pos = 0
            elif pos == -1:
                # 止损：收盘碰到支撑（回归失败）
                # 止盈：收盘回归到中轴（mid）
                exited = c <= sup or c <= mid
                if exited:
                    exit_px = c * (1 + slippage / 2)
                    pnl = (entry_price - exit_px) * pv * qty
                    trades.append({"entry": round(entry_price, 2), "exit": round(exit_px, 2), "pnl": round(pnl), "type": "short", "entry_idx": int(entry_idx), "exit_idx": int(i), "entry_date": str(df["date"].iloc[entry_idx]), "exit_date": str(df["date"].iloc[i])})
                    pos = 0
        wins = [t['pnl'] for t in trades if t['pnl'] > 0]; losses = [t['pnl'] for t in trades if t['pnl'] <= 0]
        total_pnl = sum(t['pnl'] for t in trades)
        n_wins = len(wins); n_losses = len(losses); n_trades = len(trades)
        avg_win_v = sum(wins) / max(1, n_wins) if wins else 0
        avg_loss_v = sum(losses) / max(1, n_losses) if losses else 0
        pnl_ratio = round(avg_win_v / abs(avg_loss_v), 2) if avg_loss_v != 0 else None
        # OHLC（从有数据的部分开始，仅在有交易时返回）
        ohlc = None
        if trades:
            start = 20
            _dates   = [str(d) for d in df['date'].iloc[start:].tolist()]
            _open    = [float(x) for x in df['open'].iloc[start:].tolist()]
            _high    = [float(x) for x in df['high'].iloc[start:].tolist()]
            _low     = [float(x) for x in df['low'].iloc[start:].tolist()]
            _close   = [float(x) for x in df['close'].iloc[start:].tolist()]
            _sup_arr = df['support'].iloc[start:]
            _res_arr = df['resistance'].iloc[start:]
            _sup_l   = [None if pd.isna(_sup_arr.iloc[j]) else round(float(_sup_arr.iloc[j]), 2) for j in range(len(_sup_arr))]
            _res_l   = [None if pd.isna(_res_arr.iloc[j]) else round(float(_res_arr.iloc[j]), 2) for j in range(len(_res_arr))]
            ohlc = {
                "dates":     _dates,
                "open":      _open,
                "high":      _high,
                "low":       _low,
                "close":     _close,
                "support":   _sup_l,
                "resistance":_res_l,
            }
        return {
            "variety": variety, "trades": trades, "total_pnl": round(total_pnl),
            "total_trades": n_trades, "win": n_wins, "loss": n_losses,
            "win_rate": round(n_wins / max(1, n_trades) * 100, 1),
            "avg_win": round(avg_win_v, 0), "avg_loss": round(avg_loss_v, 0),
            "max_win": round(max(wins), 0) if wins else 0,
            "max_loss": round(min(losses), 0) if losses else 0,
            "pnl_ratio": pnl_ratio,
            "ohlc": ohlc,
        }
    except Exception as e:
        print(f"均值回归回测失败 {variety}: {e}")
        return None

@app.route('/api/backtest_mr', methods=['GET'])
def api_backtest_mr():
    lookback = int(request.args.get('lookback', 30))
    target_variety = request.args.get('variety', '').strip().upper()
    direction = request.args.get('direction', '').strip() or None
    qty = int(request.args.get('qty', 1))
    results = []
    total_pnl = 0
    if target_variety:
        prefix = "".join(filter(str.isalpha, target_variety)).upper()
        pv = POINT_VALUE.get(prefix, 1)
        r = _backtest_mean_reversion(target_variety, qty, pv, lookback, direction=direction)
        if r:
            results.append(r)
            total_pnl = r["total_pnl"]
    else:
        positions = load_positions()
        if not positions:
            return jsonify({"error": "无持仓"}), 400
        for p in positions:
            prefix = ''.join(filter(str.isalpha, p.get('variety', ''))).upper()
            pv = p.get('point_value') or POINT_VALUE.get(prefix, 1)
            pqty = p.get('quantity', 1)
            r = _backtest_mean_reversion(p['variety'], pqty, pv, lookback)
            if r:
                results.append(r)
                total_pnl += r["total_pnl"]
    strategy = "均值回归（评分SR：触及支撑→买/触及阻力→卖/回归中轴出）"
    if direction: strategy += f" · {direction}"
    return jsonify({
        "lookback": lookback,
        "results": results,
        "total_pnl": round(total_pnl, 0),
        "strategy": strategy
    })

@app.route('/api/backtest', methods=['GET'])
def api_backtest():
    lookback = int(request.args.get('lookback', 30))
    atr_mult = float(request.args.get('atr_mult', 1.5))
    atr_mult = max(0.5, min(3.0, atr_mult))
    target_variety = request.args.get('variety', '').strip().upper()
    direction = request.args.get('direction', '').strip() or None
    qty = int(request.args.get('qty', 1))
    results = []
    total_pnl = 0
    if target_variety:
        prefix = "".join(filter(str.isalpha, target_variety)).upper()
        pv = POINT_VALUE.get(prefix, 1)
        r = _backtest_one(target_variety, qty, pv, lookback, atr_mult, direction=direction)
        if r:
            results.append(r)
            total_pnl = r["total_pnl"]
    else:
        positions = load_positions()
        if not positions:
            return jsonify({"error": "无持仓"}), 400
        for p in positions:
            prefix = "".join(filter(str.isalpha, p.get('variety', ''))).upper()
            pv = p.get('point_value') or POINT_VALUE.get(prefix, 1)
            pqty = p.get('quantity', 1)
            r = _backtest_one(p['variety'], pqty, pv, lookback, atr_mult)
            if r:
                results.append(r)
                total_pnl += r["total_pnl"]
    strategy = f"趋势突破（ATR{atr_mult}×/MA60过滤/MA10移动止损/量能确认）"
    if direction: strategy += f" · {direction}"
    return jsonify({
        "lookback": lookback,
        "atr_mult": atr_mult,
        "results": results,
        "total_pnl": round(total_pnl, 0),
        "strategy": strategy
    })

@app.route('/api/chart/<variety>', methods=['GET'])
def chart_data(variety):
    prefix = "".join(filter(str.isalpha, variety))
    suffix = "".join(filter(str.isdigit, variety))
    sym = f"{prefix.upper()}{suffix}"
    cached = _cache.get_kline(sym)
    if cached:
        df_d, df_w = cached
    else:
        try:
            df_d = ak.futures_zh_daily_sina(symbol=sym)
            df_w_raw = ak.futures_zh_daily_sina(symbol=sym)
            df_w_raw['date'] = pd.to_datetime(df_w_raw['date'], errors='coerce')
            df_w_raw = df_w_raw.set_index('date').sort_index()
            df_w = df_w_raw.resample('W').agg({'high': 'max', 'low': 'min', 'close': 'last'})
            df_w = df_w.dropna().tail(12)
            _cache.set_kline(sym, (df_d, df_w))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    if df_d is None or len(df_d) == 0:
        return jsonify({"error": f"无历史数据 {variety}"}), 404
    df_d = df_d.tail(60).copy()
    df_d['date'] = pd.to_datetime(df_d['date'], errors='coerce')
    df_d = df_d.sort_values('date')
    return jsonify({
        "dates": df_d['date'].dt.strftime('%Y-%m-%d').tolist(),
        "open":   [float(x) for x in df_d['open'].tolist()],
        "high":   [float(x) for x in df_d['high'].tolist()],
        "low":    [float(x) for x in df_d['low'].tolist()],
        "close":  [float(x) for x in df_d['close'].tolist()],
        "volume": [float(x) for x in df_d.get('volume', [0]*len(df_d))],
    })

@app.route('/api/restart', methods=['POST'])
def restart_server():
    pid = os.getpid()
    def do_restart():
        time.sleep(1)
        os.kill(pid, signal.SIGTERM)
    t = threading.Thread(target=do_restart, daemon=True)
    t.start()
    return jsonify({'message': 'Restarting...'})

if __name__ == '__main__':
    # 启动时后台预热持仓日线（避免首次访问卡顿）
    _warm_klines_for_positions()
    app.run(host='0.0.0.0', port=8318)
