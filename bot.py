import os
import time
import math
import requests
import pandas as pd
from datetime import datetime
from flask import Flask
from threading import Thread

# =========================
# ENV SETTINGS
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CMC_API_KEY = os.getenv("CMC_API_KEY", "")
USE_CMC_FILTER = os.getenv("USE_CMC_FILTER", "true").lower() == "true"
CMC_TOP_N = int(os.getenv("CMC_TOP_N", "1000"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "0"))
MAX_MARKET_CAP = float(os.getenv("MAX_MARKET_CAP", "1000000000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "900"))
TIMEFRAME = os.getenv("TIMEFRAME", "4h")
MAX_COINS = int(os.getenv("MAX_COINS", "300"))

MAX_RSI_BUY = float(os.getenv("MAX_RSI_BUY", "40"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "1.0"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "50000"))
MIN_CURRENT_CANDLE_VOLUME = float(os.getenv("MIN_CURRENT_CANDLE_VOLUME", "8000"))
VOLUME_LOOKBACK = int(os.getenv("VOLUME_LOOKBACK", "20"))
MAX_24H_CHANGE = float(os.getenv("MAX_24H_CHANGE", "25"))

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
STOCH_PERIOD = int(os.getenv("STOCH_PERIOD", "14"))
K_SMOOTH = int(os.getenv("K_SMOOTH", "3"))
D_SMOOTH = int(os.getenv("D_SMOOTH", "3"))

REQUIRE_MACD_RISING = os.getenv("REQUIRE_MACD_RISING", "true").lower() == "true"
REQUIRE_MACD_POSITIVE = os.getenv("REQUIRE_MACD_POSITIVE", "true").lower() == "true"

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "6"))

# Multi-exchange confirmation settings
MIN_EXCHANGE_CONFIRMATIONS = int(os.getenv("MIN_EXCHANGE_CONFIRMATIONS", "2"))
MULTI_EXCHANGE_WINDOW_MINUTES = int(os.getenv("MULTI_EXCHANGE_WINDOW_MINUTES", "60"))
MULTI_EXCHANGE_GLOBAL_COOLDOWN_HOURS = int(os.getenv("MULTI_EXCHANGE_GLOBAL_COOLDOWN_HOURS", "6"))

ENABLE_GATE = os.getenv("ENABLE_GATE", "true").lower() == "true"
ENABLE_MEXC = os.getenv("ENABLE_MEXC", "true").lower() == "true"
ENABLE_KUCOIN = os.getenv("ENABLE_KUCOIN", "true").lower() == "true"
ENABLE_OKX = os.getenv("ENABLE_OKX", "true").lower() == "true"
ENABLE_BYBIT = os.getenv("ENABLE_BYBIT", "true").lower() == "true"
ENABLE_BITGET = os.getenv("ENABLE_BITGET", "true").lower() == "true"

# =========================
# FLASK
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Early Reversal Bot 4H is running ✅"

# =========================
# FILTERS
# =========================
EXCLUDED_KEYWORDS = [
    "3L", "3S", "5L", "5S", "BULL", "BEAR",
    "UP", "DOWN", "LONG", "SHORT",
    "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP",
    "PEPE", "DOGE", "SHIB", "FLOKI", "BONK", "WIF",
    "MEME", "CAT", "DOG", "PUMP",
    "GAME", "GAMING", "CASINO", "BET", "PREDICT", "POLYMARKET",
    "BABAON", "NVDAX", "TSLA3S", "TSLA3L", "SBUXON"
]

sent_signals = {}
global_sent_signals = {}
pending_multi_signals = {}
cmc_allowed_symbols = {}
last_cmc_update = 0

# الصفقات المفتوحة لمتابعة الأهداف
active_trades = {}

# =========================
# TELEGRAM
# =========================
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print("Telegram Error:", e)

# =========================
# HELPERS
# =========================
def safe_float(x, default=0):
    try:
        return float(x)
    except Exception:
        return default

def base_symbol(symbol):
    s = symbol.upper()
    s = s.replace("_USDT", "")
    s = s.replace("-USDT", "")
    s = s.replace("USDT", "")
    return s

def normalize_symbol(symbol):
    return symbol.replace("_", "/").replace("-", "/")

def is_excluded(symbol):
    s = base_symbol(symbol)
    return any(x in s for x in EXCLUDED_KEYWORDS)

def cooldown_ok(key):
    now = time.time()
    last = sent_signals.get(key)
    if not last:
        return True
    return now - last >= SIGNAL_COOLDOWN_HOURS * 3600

def global_cooldown_ok(symbol_key):
    now = time.time()
    last = global_sent_signals.get(symbol_key)
    if not last:
        return True
    return now - last >= MULTI_EXCHANGE_GLOBAL_COOLDOWN_HOURS * 3600

def format_money(value):
    try:
        value = float(value)
    except Exception:
        value = 0
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:,.0f}"

def convert_timeframe(exchange):
    tf = TIMEFRAME
    mapping = {
        "Gate": tf,
        "MEXC": tf,
        "KuCoin": {
            "1m": "1min", "5m": "5min", "15m": "15min",
            "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day"
        }.get(tf, "4hour"),
        "OKX": {
            "1m": "1m", "5m": "5m", "15m": "15m",
            "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"
        }.get(tf, "4H"),
        "Bybit": {
            "1m": "1", "5m": "5", "15m": "15",
            "30m": "30", "1h": "60", "4h": "240", "1d": "D"
        }.get(tf, "240"),
        "Bitget": {
            "1m": "1min", "5m": "5min", "15m": "15min",
            "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"
        }.get(tf, "4h")
    }
    return mapping.get(exchange, tf)

# =========================
# CMC FILTER
# =========================
def update_cmc_filter():
    global cmc_allowed_symbols, last_cmc_update

    if not USE_CMC_FILTER:
        cmc_allowed_symbols = {}
        return

    if not CMC_API_KEY:
        print("CMC_API_KEY missing. CMC filter disabled temporarily.")
        cmc_allowed_symbols = {}
        return

    now = time.time()
    if now - last_cmc_update < 3600 and cmc_allowed_symbols:
        return

    print("Updating CoinMarketCap filter...")

    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {
        "start": "1",
        "limit": str(CMC_TOP_N),
        "convert": "USD",
        "sort": "market_cap",
        "sort_dir": "desc"
    }

    try:
        data = requests.get(url, headers=headers, params=params, timeout=30).json()

        if "data" not in data:
            print("CMC error:", data)
            return

        allowed = {}

        for coin in data["data"]:
            symbol = str(coin.get("symbol", "")).upper()
            name = str(coin.get("name", "")).upper()
            quote = coin.get("quote", {}).get("USD", {})

            market_cap = safe_float(quote.get("market_cap"))
            volume_24h = safe_float(quote.get("volume_24h"))
            change_24h = safe_float(quote.get("percent_change_24h"))

            if not symbol:
                continue
            if market_cap < MIN_MARKET_CAP:
                continue
            if market_cap > MAX_MARKET_CAP:
                continue
            if volume_24h < MIN_VOLUME_USDT:
                continue
            if abs(change_24h) > MAX_24H_CHANGE:
                continue

            combined = symbol + " " + name
            if any(x in combined for x in EXCLUDED_KEYWORDS):
                continue

            allowed[symbol] = {
                "name": coin.get("name", symbol),
                "market_cap": market_cap,
                "volume_24h": volume_24h,
                "change_24h": change_24h,
                "rank": coin.get("cmc_rank")
            }

        cmc_allowed_symbols = allowed
        last_cmc_update = now
        print(f"CMC allowed symbols: {len(cmc_allowed_symbols)}")

    except Exception as e:
        print("CMC update error:", e)

def cmc_is_allowed(symbol):
    if not USE_CMC_FILTER:
        return True
    if not CMC_API_KEY:
        return True
    if not cmc_allowed_symbols:
        return True
    return base_symbol(symbol) in cmc_allowed_symbols

def get_cmc_info(symbol):
    return cmc_allowed_symbols.get(base_symbol(symbol), {})

# =========================
# INDICATORS
# =========================
def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, math.nan)
    return 100 - (100 / (1 + rs))

def stoch_rsi(close):
    r = rsi(close, RSI_PERIOD)
    min_rsi = r.rolling(STOCH_PERIOD).min()
    max_rsi = r.rolling(STOCH_PERIOD).max()
    stoch = 100 * (r - min_rsi) / (max_rsi - min_rsi)
    k = stoch.rolling(K_SMOOTH).mean()
    d = k.rolling(D_SMOOTH).mean()
    return k, d

def macd_hist(close):
    macd_line = ema(close, 12) - ema(close, 26)
    signal = ema(macd_line, 9)
    return macd_line - signal

# =========================
# EXCHANGE FUNCTIONS
# =========================
def gate_symbols():
    try:
        data = requests.get("https://api.gateio.ws/api/v4/spot/currency_pairs", timeout=20).json()
        symbols = []
        for x in data:
            pair = x.get("id", "")
            if pair.endswith("_USDT") and x.get("trade_status") == "tradable" and cmc_is_allowed(pair):
                symbols.append(pair)
        return symbols[:MAX_COINS]
    except Exception as e:
        print("Gate symbols error:", e)
        return []

def gate_ticker(symbol):
    try:
        data = requests.get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={symbol}", timeout=15).json()
        if not data:
            return None
        x = data[0]
        return {
            "price": safe_float(x.get("last")),
            "quote_volume": safe_float(x.get("quote_volume")),
            "change_24h": safe_float(x.get("change_percentage"))
        }
    except Exception:
        return None

def gate_candles(symbol):
    try:
        params = {"currency_pair": symbol, "interval": convert_timeframe("Gate"), "limit": 120}
        data = requests.get("https://api.gateio.ws/api/v4/spot/candlesticks", params=params, timeout=20).json()
        rows = []
        for c in data:
            rows.append({
                "time": int(c[0]),
                "volume_quote": safe_float(c[1]),
                "close": safe_float(c[2]),
                "high": safe_float(c[3]),
                "low": safe_float(c[4]),
                "open": safe_float(c[5])
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception:
        return None

def mexc_symbols():
    try:
        data = requests.get("https://api.mexc.com/api/v3/exchangeInfo", timeout=20).json()
        symbols = []
        for x in data.get("symbols", []):
            s = x.get("symbol", "")
            if s.endswith("USDT") and x.get("status") == "ENABLED" and cmc_is_allowed(s):
                symbols.append(s)
        return symbols[:MAX_COINS]
    except Exception as e:
        print("MEXC symbols error:", e)
        return []

def mexc_ticker(symbol):
    try:
        x = requests.get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={symbol}", timeout=15).json()
        return {
            "price": safe_float(x.get("lastPrice")),
            "quote_volume": safe_float(x.get("quoteVolume")),
            "change_24h": safe_float(x.get("priceChangePercent"))
        }
    except Exception:
        return None

def mexc_candles(symbol):
    try:
        params = {"symbol": symbol, "interval": convert_timeframe("MEXC"), "limit": 120}
        data = requests.get("https://api.mexc.com/api/v3/klines", params=params, timeout=20).json()
        rows = []
        for c in data:
            rows.append({
                "time": int(c[0]),
                "open": safe_float(c[1]),
                "high": safe_float(c[2]),
                "low": safe_float(c[3]),
                "close": safe_float(c[4]),
                "volume_quote": safe_float(c[7])
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception:
        return None

def kucoin_symbols():
    try:
        data = requests.get("https://api.kucoin.com/api/v1/symbols", timeout=20).json()
        symbols = []
        for x in data.get("data", []):
            s = x.get("symbol", "")
            if s.endswith("-USDT") and x.get("enableTrading") and cmc_is_allowed(s):
                symbols.append(s)
        return symbols[:MAX_COINS]
    except Exception as e:
        print("KuCoin symbols error:", e)
        return []

def kucoin_ticker(symbol):
    try:
        x = requests.get(f"https://api.kucoin.com/api/v1/market/stats?symbol={symbol}", timeout=15).json().get("data", {})
        return {
            "price": safe_float(x.get("last")),
            "quote_volume": safe_float(x.get("volValue")),
            "change_24h": safe_float(x.get("changeRate")) * 100
        }
    except Exception:
        return None

def kucoin_candles(symbol):
    try:
        params = {"symbol": symbol, "type": convert_timeframe("KuCoin")}
        data = requests.get("https://api.kucoin.com/api/v1/market/candles", params=params, timeout=20).json().get("data", [])
        rows = []
        for c in data[:120]:
            rows.append({
                "time": int(c[0]),
                "open": safe_float(c[1]),
                "close": safe_float(c[2]),
                "high": safe_float(c[3]),
                "low": safe_float(c[4]),
                "volume_quote": safe_float(c[6])
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception:
        return None

def okx_symbols():
    try:
        data = requests.get("https://www.okx.com/api/v5/public/instruments?instType=SPOT", timeout=20).json()
        symbols = []
        for x in data.get("data", []):
            s = x.get("instId", "")
            if s.endswith("-USDT") and x.get("state") == "live" and cmc_is_allowed(s):
                symbols.append(s)
        return symbols[:MAX_COINS]
    except Exception as e:
        print("OKX symbols error:", e)
        return []

def okx_ticker(symbol):
    try:
        x = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={symbol}", timeout=15).json().get("data", [])[0]
        open24h = safe_float(x.get("open24h"))
        last = safe_float(x.get("last"))
        change = ((last - open24h) / open24h * 100) if open24h > 0 else 0
        return {
            "price": last,
            "quote_volume": safe_float(x.get("volCcy24h")),
            "change_24h": change
        }
    except Exception:
        return None

def okx_candles(symbol):
    try:
        params = {"instId": symbol, "bar": convert_timeframe("OKX"), "limit": 120}
        data = requests.get("https://www.okx.com/api/v5/market/candles", params=params, timeout=20).json().get("data", [])
        rows = []
        for c in data:
            rows.append({
                "time": int(c[0]),
                "open": safe_float(c[1]),
                "high": safe_float(c[2]),
                "low": safe_float(c[3]),
                "close": safe_float(c[4]),
                "volume_quote": safe_float(c[7])
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception:
        return None

def bybit_symbols():
    try:
        params = {"category": "spot"}
        data = requests.get("https://api.bybit.com/v5/market/instruments-info", params=params, timeout=20).json()
        symbols = []
        for x in data.get("result", {}).get("list", []):
            s = x.get("symbol", "")
            if s.endswith("USDT") and x.get("status") == "Trading" and cmc_is_allowed(s):
                symbols.append(s)
        return symbols[:MAX_COINS]
    except Exception as e:
        print("Bybit symbols error:", e)
        return []

def bybit_ticker(symbol):
    try:
        params = {"category": "spot", "symbol": symbol}
        x = requests.get("https://api.bybit.com/v5/market/tickers", params=params, timeout=15).json().get("result", {}).get("list", [])[0]
        return {
            "price": safe_float(x.get("lastPrice")),
            "quote_volume": safe_float(x.get("turnover24h")),
            "change_24h": safe_float(x.get("price24hPcnt")) * 100
        }
    except Exception:
        return None

def bybit_candles(symbol):
    try:
        params = {"category": "spot", "symbol": symbol, "interval": convert_timeframe("Bybit"), "limit": 120}
        data = requests.get("https://api.bybit.com/v5/market/kline", params=params, timeout=20).json().get("result", {}).get("list", [])
        rows = []
        for c in data:
            rows.append({
                "time": int(c[0]),
                "open": safe_float(c[1]),
                "high": safe_float(c[2]),
                "low": safe_float(c[3]),
                "close": safe_float(c[4]),
                "volume_quote": safe_float(c[6])
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception:
        return None

def bitget_symbols():
    try:
        data = requests.get("https://api.bitget.com/api/v2/spot/public/symbols", timeout=20).json()
        symbols = []
        for x in data.get("data", []):
            s = x.get("symbol", "")
            if s.endswith("USDT") and x.get("status") == "online" and cmc_is_allowed(s):
                symbols.append(s)
        return symbols[:MAX_COINS]
    except Exception as e:
        print("Bitget symbols error:", e)
        return []

def bitget_ticker(symbol):
    try:
        params = {"symbol": symbol}
        x = requests.get("https://api.bitget.com/api/v2/spot/market/tickers", params=params, timeout=15).json().get("data", [])[0]
        return {
            "price": safe_float(x.get("lastPr")),
            "quote_volume": safe_float(x.get("quoteVolume")),
            "change_24h": safe_float(x.get("change24h")) * 100
        }
    except Exception:
        return None

def bitget_candles(symbol):
    try:
        params = {"symbol": symbol, "granularity": convert_timeframe("Bitget"), "limit": 120}
        data = requests.get("https://api.bitget.com/api/v2/spot/market/candles", params=params, timeout=20).json().get("data", [])
        rows = []
        for c in data:
            rows.append({
                "time": int(c[0]),
                "open": safe_float(c[1]),
                "high": safe_float(c[2]),
                "low": safe_float(c[3]),
                "close": safe_float(c[4]),
                "volume_quote": safe_float(c[6])
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception:
        return None

# =========================
# TARGET ALERTS
# =========================
def add_active_trade(signal):
    key = f"{signal['exchange']}:{signal['raw_symbol']}"

    active_trades[key] = {
        "exchange": signal["exchange"],
        "raw_symbol": signal["raw_symbol"],
        "symbol": signal["symbol"],
        "entry": signal["price"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "tp3": signal["tp3"],
        "sl": signal["sl"],
        "hit_tp1": False,
        "hit_tp2": False,
        "hit_tp3": False,
        "hit_sl": False,
        "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }

def format_target_alert(trade, target_name, target_price, current_price, pct):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""
🎯 <b>تم تحقيق الهدف {target_name}</b>
━━━━━━━━━━━━━━
⏰ الوقت: {now}
🏦 المنصة: <b>{trade['exchange']}</b>
🪙 العملة: <b>{trade['symbol']}</b>

💰 سعر الدخول: <b>{trade['entry']:.8f}</b>
🎯 سعر الهدف: <b>{target_price:.8f}</b>
📍 السعر الحالي: <b>{current_price:.8f}</b>

📈 الربح التقريبي: <b>+{pct}%</b>

✅ تم إرسال التنبيه تلقائيًا عند وصول السعر للهدف.
"""

def format_stop_loss_alert(trade, current_price):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""
🛑 <b>تم ضرب وقف الخسارة</b>
━━━━━━━━━━━━━━
⏰ الوقت: {now}
🏦 المنصة: <b>{trade['exchange']}</b>
🪙 العملة: <b>{trade['symbol']}</b>

💰 سعر الدخول: <b>{trade['entry']:.8f}</b>
🛑 وقف الخسارة: <b>{trade['sl']:.8f}</b>
📍 السعر الحالي: <b>{current_price:.8f}</b>

⚠️ تم إغلاق متابعة هذه الإشارة.
"""

def get_ticker_func(exchange):
    if exchange == "Gate":
        return gate_ticker
    if exchange == "MEXC":
        return mexc_ticker
    if exchange == "KuCoin":
        return kucoin_ticker
    if exchange == "OKX":
        return okx_ticker
    if exchange == "Bybit":
        return bybit_ticker
    if exchange == "Bitget":
        return bitget_ticker
    return None

def monitor_active_trades():
    if not active_trades:
        return

    closed_trades = []

    for key, trade in list(active_trades.items()):
        ticker_func = get_ticker_func(trade["exchange"])
        if not ticker_func:
            continue

        ticker = ticker_func(trade["raw_symbol"])
        if not ticker:
            continue

        current_price = ticker["price"]
        if current_price <= 0:
            continue

        if not trade["hit_tp1"] and current_price >= trade["tp1"]:
            trade["hit_tp1"] = True
            send_telegram(format_target_alert(trade, "TP1", trade["tp1"], current_price, 3))

        if not trade["hit_tp2"] and current_price >= trade["tp2"]:
            trade["hit_tp2"] = True
            send_telegram(format_target_alert(trade, "TP2", trade["tp2"], current_price, 6))

        if not trade["hit_tp3"] and current_price >= trade["tp3"]:
            trade["hit_tp3"] = True
            send_telegram(format_target_alert(trade, "TP3", trade["tp3"], current_price, 10))
            closed_trades.append(key)

        if not trade["hit_sl"] and current_price <= trade["sl"]:
            trade["hit_sl"] = True
            send_telegram(format_stop_loss_alert(trade, current_price))
            closed_trades.append(key)

        time.sleep(0.15)

    for key in closed_trades:
        active_trades.pop(key, None)

# =========================
# ANALYSIS
# =========================
def analyze_symbol(exchange, symbol, ticker_func, candle_func):
    if is_excluded(symbol):
        return None

    ticker = ticker_func(symbol)
    if not ticker:
        return None

    price = ticker["price"]
    quote_volume = ticker["quote_volume"]
    change_24h = ticker["change_24h"]

    if price <= 0 or quote_volume < MIN_VOLUME_USDT or abs(change_24h) > MAX_24H_CHANGE:
        return None

    df = candle_func(symbol)
    if df is None or len(df) < 60:
        return None

    close = df["close"]
    volume = df["volume_quote"]

    k, d = stoch_rsi(close)
    hist = macd_hist(close)
    ema20 = ema(close, 20)

    k_now = k.iloc[-1]
    d_now = d.iloc[-1]
    k_prev = k.iloc[-2]
    d_prev = d.iloc[-2]

    hist_now = hist.iloc[-1]
    hist_prev = hist.iloc[-2]

    current_price = close.iloc[-1]
    current_volume = volume.iloc[-1]

    if current_volume < MIN_CURRENT_CANDLE_VOLUME:
        return None

    avg_volume = volume.iloc[-(VOLUME_LOOKBACK + 1):-1].mean()

    if pd.isna(k_now) or pd.isna(d_now) or pd.isna(hist_now) or avg_volume <= 0:
        return None

    volume_ratio = current_volume / avg_volume

    stoch_cross = k_prev <= d_prev and k_now > d_now
    stoch_low = k_now < MAX_RSI_BUY
    macd_rising = (hist_now > hist_prev) if REQUIRE_MACD_RISING else True
    macd_positive = (hist_now > 0) if REQUIRE_MACD_POSITIVE else True
    volume_ok = volume_ratio >= MIN_VOLUME_RATIO
    price_above_ema20 = current_price > ema20.iloc[-1]

    if not (stoch_cross and stoch_low and macd_rising and macd_positive and volume_ok):
        return None

    key = f"{exchange}:{symbol}"
    if not cooldown_ok(key):
        return None

    score = 0
    reasons = []

    if stoch_cross:
        score += 25
        reasons.append("✅ Stoch RSI K اخترق D")
    if stoch_low:
        score += 20
        reasons.append(f"✅ Stoch RSI تحت {MAX_RSI_BUY}")
    if macd_rising:
        score += 20
        reasons.append("✅ MACD Histogram يتحسن")
    if REQUIRE_MACD_POSITIVE and hist_now > 0:
        score += 15
        reasons.append("✅ MACD Histogram موجب")
    if volume_ok:
        score += 15
        reasons.append(f"✅ Volume Ratio أعلى من {MIN_VOLUME_RATIO}x")
    if price_above_ema20:
        score += 5
        reasons.append("✅ السعر فوق EMA20")

    cmc = get_cmc_info(symbol)

    return {
        "exchange": exchange,
        "raw_symbol": symbol,
        "symbol": normalize_symbol(symbol),
        "price": current_price,
        "k": k_now,
        "d": d_now,
        "macd": hist_now,
        "macd_prev": hist_prev,
        "volume": current_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "quote_volume": quote_volume,
        "change_24h": change_24h,
        "score": score,
        "reasons": reasons,
        "tp1": current_price * 1.03,
        "tp2": current_price * 1.06,
        "tp3": current_price * 1.10,
        "sl": current_price * 0.94,
        "cmc_name": cmc.get("name", ""),
        "cmc_rank": cmc.get("rank", ""),
        "market_cap": cmc.get("market_cap", 0),
        "cmc_volume_24h": cmc.get("volume_24h", 0)
    }

def register_multi_exchange_signal(signal):
    symbol_key = base_symbol(signal["raw_symbol"])
    now_ts = time.time()
    window_seconds = MULTI_EXCHANGE_WINDOW_MINUTES * 60

    if symbol_key not in pending_multi_signals:
        pending_multi_signals[symbol_key] = []

    # Keep only recent signals inside the confirmation window
    pending_multi_signals[symbol_key] = [
        x for x in pending_multi_signals[symbol_key]
        if now_ts - x.get("detected_at", now_ts) <= window_seconds
    ]

    # Avoid duplicate exchange entries for the same coin inside the same window
    pending_multi_signals[symbol_key] = [
        x for x in pending_multi_signals[symbol_key]
        if x.get("exchange") != signal.get("exchange")
    ]

    signal["detected_at"] = now_ts
    pending_multi_signals[symbol_key].append(signal)

    return symbol_key, pending_multi_signals[symbol_key]

def calculate_multi_score(signals):
    if not signals:
        return 0

    exchanges_count = len({s["exchange"] for s in signals})
    best_volume_ratio = max(s["volume_ratio"] for s in signals)
    avg_k = sum(s["k"] for s in signals) / len(signals)
    macd_positive_count = sum(1 for s in signals if s["macd"] > 0 and s["macd"] > s["macd_prev"])

    score = 0

    if exchanges_count >= 2:
        score += 25
    if exchanges_count >= 3:
        score += 10

    if best_volume_ratio >= 2.5:
        score += 25
    elif best_volume_ratio >= 1.8:
        score += 18
    else:
        score += 10

    if avg_k < 20:
        score += 20
    elif avg_k < MAX_RSI_BUY:
        score += 12

    if macd_positive_count >= exchanges_count:
        score += 20
    elif macd_positive_count >= 1:
        score += 12

    score += min(10, exchanges_count * 3)

    return min(score, 100)

def format_multi_exchange_signal(signals):
    signals = sorted(signals, key=lambda x: x["volume_ratio"], reverse=True)
    best = signals[0]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    exchanges = []
    for s in signals:
        exchanges.append(
            f"• {s['exchange']} | السعر: {s['price']:.8f} | Volume Ratio: {s['volume_ratio']:.2f}x"
        )
    exchange_text = "\n".join(exchanges)

    avg_price = sum(s["price"] for s in signals) / len(signals)
    avg_k = sum(s["k"] for s in signals) / len(signals)
    avg_d = sum(s["d"] for s in signals) / len(signals)
    avg_volume_ratio = sum(s["volume_ratio"] for s in signals) / len(signals)
    best_volume_ratio = max(s["volume_ratio"] for s in signals)

    tp1 = avg_price * 1.03
    tp2 = avg_price * 1.06
    tp3 = avg_price * 1.10
    sl = avg_price * 0.94

    score = calculate_multi_score(signals)

    cmc_text = ""
    if best["market_cap"]:
        cmc_text = f"""
🌐 <b>CoinMarketCap</b>
الاسم: {best['cmc_name']}
الترتيب: {best['cmc_rank']}
Market Cap: {format_money(best['market_cap'])}
CMC 24H Volume: {format_money(best['cmc_volume_24h'])}
"""

    return f"""
🔥 <b>MULTI-EXCHANGE EARLY REVERSAL</b>
━━━━━━━━━━━━━━
🪙 العملة: <b>{base_symbol(best['raw_symbol'])}/USDT</b>
⏰ الوقت: {now}

🏦 <b>تم رصد التحرك على {len(signals)} منصات:</b>
{exchange_text}

📊 <b>متوسط البيانات بين المنصات</b>
💰 متوسط سعر الدخول: <b>{avg_price:.8f}</b>

📈 <b>Stoch RSI</b>
K: {avg_k:.2f}
D: {avg_d:.2f}
الحالة: Oversold Reversal ✅

📈 <b>MACD Histogram</b>
الحالة: تحول من سالب إلى موجب ✅
الزخم: يتحسن بقوة ✅

💧 <b>Volume Analysis</b>
أعلى Volume Ratio: <b>{best_volume_ratio:.2f}x</b>
متوسط Volume Ratio: <b>{avg_volume_ratio:.2f}x</b>
الحالة: Smart Money Activity ✅
{cmc_text}
🎯 <b>الأهداف</b>
TP1: {tp1:.8f} (+3%)
TP2: {tp2:.8f} (+6%)
TP3: {tp3:.8f} (+10%)

🛑 <b>Stop Loss</b>
SL: {sl:.8f} (-6%)

⭐ قوة الإشارة: <b>{score}%</b>

🔥 <b>أسباب قوة الإشارة</b>
✅ نفس الحركة ظهرت على عدة منصات
✅ MACD تحول موجب مبكر
✅ Volume قوي
✅ Stoch RSI منخفض جدًا
✅ السعر قريب من EMA20
✅ بداية انعكاس مبكرة

🚨 توافق عدة منصات غالبًا يعني أن السيولة بدأت تدخل فعليًا للعملة.

⚠️ تحليل آلي فقط وليس نصيحة مالية.
"""

def process_multi_exchange_signals(new_signals):
    sent_count = 0

    for signal in new_signals:
        symbol_key, grouped_signals = register_multi_exchange_signal(signal)

        exchanges_count = len({s["exchange"] for s in grouped_signals})

        if exchanges_count < MIN_EXCHANGE_CONFIRMATIONS:
            continue

        if not global_cooldown_ok(symbol_key):
            continue

        grouped_signals = sorted(grouped_signals, key=lambda x: x["volume_ratio"], reverse=True)

        send_telegram(format_multi_exchange_signal(grouped_signals))
        global_sent_signals[symbol_key] = time.time()

        # Mark each exchange pair cooldown only after the confirmed multi-exchange alert is sent
        for s in grouped_signals:
            sent_signals[f"{s['exchange']}:{s['raw_symbol']}"] = time.time()

        # Follow targets using the strongest exchange signal
        add_active_trade(grouped_signals[0])

        sent_count += 1

    return sent_count

def startup_message():
    exchanges = []
    if ENABLE_GATE:
        exchanges.append("Gate")
    if ENABLE_MEXC:
        exchanges.append("MEXC")
    if ENABLE_KUCOIN:
        exchanges.append("KuCoin")
    if ENABLE_OKX:
        exchanges.append("OKX")
    if ENABLE_BYBIT:
        exchanges.append("Bybit")
    if ENABLE_BITGET:
        exchanges.append("Bitget")

    exchange_text = "\n".join([f"• {x}" for x in exchanges])

    msg = f"""
🤖 <b>بوت Early Reversal 4H اشتغل بنجاح ✅</b>

━━━━━━━━━━━━━━
📊 الفريم: <b>{TIMEFRAME}</b>
⏱️ الفحص كل: <b>{CHECK_INTERVAL} ثانية</b>

🏦 <b>المنصات المفعلة:</b>
{exchange_text}

🌐 <b>CoinMarketCap Filter:</b>
الحالة: {'مفعل ✅' if USE_CMC_FILTER else 'غير مفعل ❌'}
Top N: {CMC_TOP_N}
Min Market Cap: ${MIN_MARKET_CAP:,.0f}
Max Market Cap: ${MAX_MARKET_CAP:,.0f}

🎯 <b>شروط الدخول الحالية:</b>
• Stoch RSI K يخترق D
• Stoch RSI أقل من {MAX_RSI_BUY}
• MACD Histogram يتحسن: {'مطلوب ✅' if REQUIRE_MACD_RISING else 'غير مطلوب ❌'}
• MACD Histogram موجب: {'مطلوب ✅' if REQUIRE_MACD_POSITIVE else 'غير مطلوب ❌'}
• Volume Ratio أعلى من {MIN_VOLUME_RATIO}x
• حجم الشمعة الحالية أعلى من ${MIN_CURRENT_CANDLE_VOLUME:,.0f}
• 24H Change أقل من {MAX_24H_CHANGE}%
• تأكيد الإشارة من عدد منصات: {MIN_EXCHANGE_CONFIRMATIONS}
• نافذة توافق المنصات: {MULTI_EXCHANGE_WINDOW_MINUTES} دقيقة

🎯 <b>متابعة الأهداف:</b>
• TP1 +3%
• TP2 +6%
• TP3 +10%
• SL -6%

✅ سيتم إرسال تنبيه عند تحقق كل هدف.
"""
    send_telegram(msg)

def scan_exchange(name, symbols_func, ticker_func, candle_func):
    signals = []

    try:
        symbols = symbols_func()
        print(f"Scanning {name}: {len(symbols)} symbols")

        for symbol in symbols:
            signal = analyze_symbol(name, symbol, ticker_func, candle_func)
            if signal:
                signals.append(signal)
                print(f"Candidate Found: {name} {symbol}")
            time.sleep(0.15)

        print(f"{name} scan finished. Candidates: {len(signals)}")

    except Exception as e:
        print(f"{name} scan error:", e)

    return signals

def scanner_loop():
    startup_message()

    while True:
        try:
            update_cmc_filter()

            monitor_active_trades()

            all_signals = []

            if ENABLE_GATE:
                all_signals.extend(scan_exchange("Gate", gate_symbols, gate_ticker, gate_candles))
            if ENABLE_MEXC:
                all_signals.extend(scan_exchange("MEXC", mexc_symbols, mexc_ticker, mexc_candles))
            if ENABLE_KUCOIN:
                all_signals.extend(scan_exchange("KuCoin", kucoin_symbols, kucoin_ticker, kucoin_candles))
            if ENABLE_OKX:
                all_signals.extend(scan_exchange("OKX", okx_symbols, okx_ticker, okx_candles))
            if ENABLE_BYBIT:
                all_signals.extend(scan_exchange("Bybit", bybit_symbols, bybit_ticker, bybit_candles))
            if ENABLE_BITGET:
                all_signals.extend(scan_exchange("Bitget", bitget_symbols, bitget_ticker, bitget_candles))

            multi_sent = process_multi_exchange_signals(all_signals)

            monitor_active_trades()

            print(f"Full scan finished. Candidates: {len(all_signals)} | Multi alerts sent: {multi_sent}")

        except Exception as e:
            print("Main scanner error:", e)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    Thread(target=scanner_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
