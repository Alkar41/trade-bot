"""Binance USDT coin tarayici: AL / SAT / BEKLE sinyalleri + 30 gun isabet istatistigi.
Emir vermez, sadece bildirim yollar. Yatirim tavsiyesi degildir."""
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

INTERVAL = "1h"
EVAL_HOURS = 24          # sinyal kac saat sonra degerlendirilsin
TRACK_DAYS = 30          # kac gun sinyal uretilsin
SUMMARY_HOUR_UTC = 6     # gunluk ozet saati (UTC 06:00 = TR 09:00)
MIN_VOLUME = 2_000_000   # 24 saatlik minimum hacim (USDT), olu coinleri eler
MAX_NOTIFY = 10          # tek mesajda en fazla kac sinyal gosterilsin
SIGNAL_SCORE = 3         # AL/SAT icin gereken minimum skor
STRONG_SCORE = 4         # "guclu sinyal" siniri
STATE_FILE = "data/state.json"
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
           "EURI", "EUR", "AEUR", "XUSD", "UST", "USDS"}

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------- veri ----------
def http_json(path):
    last = None
    for host in HOSTS:
        for _ in range(2):
            try:
                req = urllib.request.Request(host + path, headers={"User-Agent": "signal-bot"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (418, 429):
                    time.sleep(3)
                else:
                    break
            except Exception as e:
                last = e
                time.sleep(1)
    raise RuntimeError(f"{path}: {last}")


def get_tickers():
    data = http_json("/api/v3/ticker/24hr")
    universe, prices_all = {}, {}
    for t in data:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        price = float(t["lastPrice"])
        if price <= 0:
            continue
        prices_all[sym] = price
        if sym[:-4] in STABLES:
            continue
        vol = float(t["quoteVolume"])
        if vol >= MIN_VOLUME:
            universe[sym] = {"price": price, "vol": vol}
    return universe, prices_all


def get_closes(sym):
    d = http_json(f"/api/v3/klines?symbol={sym}&interval={INTERVAL}&limit=120")
    return [float(k[4]) for k in d]


# ---------- gostergeler ----------
def rsi(closes, n=14):
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def sma(closes, n):
    return sum(closes[-n:]) / n


def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd_hist(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    sig = ema(macd, 9)
    hist = [m - s for m, s in zip(macd, sig)]
    return hist[-1], hist[-2]


def boll_pb(closes, n=20):
    w = closes[-n:]
    mean = sum(w) / n
    sd = statistics.pstdev(w)
    if sd == 0:
        return 0.5
    return (closes[-1] - (mean - 2 * sd)) / (4 * sd)


def analyze(c):
    r = rsi(c)
    s20, s50 = sma(c, 20), sma(c, 50)
    hist, prev = macd_hist(c)
    pb = boll_pb(c)
    score = 0
    if r < 30:
        score += 2
    elif r < 40:
        score += 1
    elif r > 70:
        score -= 2
    elif r > 60:
        score -= 1
    score += 1 if s20 > s50 else -1
    score += 1 if hist > prev else -1
    if pb < 0.1:
        score += 1
    elif pb > 0.9:
        score -= 1
    signal = "AL" if score >= SIGNAL_SCORE else "SAT" if score <= -SIGNAL_SCORE else "BEKLE"
    rets = [b / a - 1 for a, b in zip(c[-25:], c[-24:])]
    vol = statistics.pstdev(rets) * 100
    risk = max(1, min(10, round(vol * 4)))
    return {"signal": signal, "score": score, "rsi": r, "risk": risk}


def safe_analyze(sym):
    try:
        closes = get_closes(sym)
        if len(closes) < 60:
            return None
        return analyze(closes[:-1])  # son (acik) mumu katma
    except Exception as e:
        print("hata", sym, e)
        return None


# ---------- durum ----------
def load_state():
    st = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    st.setdefault("start", datetime.now(timezone.utc).isoformat())
    st.setdefault("last_signals", {})
    st.setdefault("last_open", {})
    st.setdefault("last_summary", None)
    st.setdefault("final_sent", False)
    st["signals"] = [s for s in st.get("signals", []) if "sym" in s]
    return st


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))


def send(text):
    print(text)
    if not TOKEN or not CHAT_ID:
        print("(Telegram bilgileri yok, mesaj gonderilmedi)")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=20)
    except Exception as e:
        print("Telegram hatasi:", e)


def fmt(p):
    if p >= 1:
        return f"{p:,.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.8f}"


# ---------- degerlendirme ----------
def evaluate(state, prices_all, btc, now):
    for s in state["signals"]:
        if s.get("r") is not None:
            continue
        if now - datetime.fromisoformat(s["t"]) < timedelta(hours=EVAL_HOURS):
            continue
        price = prices_all.get(s["sym"])
        if not price:
            continue
        chg = (price / s["p"] - 1) * 100
        bchg = (btc / s["b"] - 1) * 100
        if s["g"] == "SAT":      # SAT'ta fiyat dusunce dogru
            chg, bchg = -chg, -bchg
        s["r"] = round(chg, 2)
        s["a"] = round(chg - bchg, 2)


def stats_text(state, title):
    done = [s for s in state["signals"] if s.get("r") is not None]
    open_n = len(state["signals"]) - len(done)
    if not done:
        return f"{title}\nHenuz degerlendirilen sinyal yok. Acik sinyal: {open_n}"

    def line(name, items):
        if not items:
            return None
        w = sum(1 for s in items if s["r"] > 0)
        avg = sum(s["r"] for s in items) / len(items)
        alpha = sum(s["a"] for s in items) / len(items)
        return (f"{name}: {w}/{len(items)} dogru (%{w / len(items) * 100:.0f}) | "
                f"ort. %{avg:.2f} | BTC'ye gore %{alpha:+.2f}")

    lines = [title, f"Degerlendirilen: {len(done)} | Acik: {open_n}"]
    groups = [("Tumu", done),
              ("AL", [s for s in done if s["g"] == "AL"]),
              ("SAT", [s for s in done if s["g"] == "SAT"]),
              (f"Guclu(>={STRONG_SCORE})", [s for s in done if abs(s["sc"]) >= STRONG_SCORE])]
    for name, items in groups:
        l = line(name, items)
        if l:
            lines.append(l)
    return "\n".join(lines)


# ---------- ana akis ----------
def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    if state.get("final_sent"):
        print("30 gunluk takip bitti.")
        return

    universe, prices_all = get_tickers()
    btc = prices_all.get("BTCUSDT")
    if not btc:
        raise RuntimeError("BTC fiyati alinamadi")
    evaluate(state, prices_all, btc, now)

    elapsed = now - datetime.fromisoformat(state["start"])
    tracking = elapsed < timedelta(days=TRACK_DAYS)
    counts = None
    new = []

    if tracking:
        syms = sorted(universe, key=lambda s: -universe[s]["vol"])
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(safe_analyze, syms))
        counts = {"AL": 0, "SAT": 0, "BEKLE": 0, "n": 0}
        for sym, a in zip(syms, results):
            if a is None:
                continue
            sig = a["signal"]
            counts[sig] += 1
            counts["n"] += 1
            prev = state["last_signals"].get(sym)
            state["last_signals"][sym] = sig
            if sig == "BEKLE" or sig == prev:
                continue
            lo = state["last_open"].get(sym)
            if lo and now - datetime.fromisoformat(lo) < timedelta(hours=EVAL_HOURS):
                continue
            price = universe[sym]["price"]
            state["signals"].append({"sym": sym, "t": now.isoformat(), "p": price,
                                     "b": btc, "g": sig, "sc": a["score"], "r": None})
            state["last_open"][sym] = now.isoformat()
            new.append((sym, a, price))

    if new:
        new.sort(key=lambda x: (-abs(x[1]["score"]), -universe[x[0]]["vol"]))
        lines = [f"📣 {len(new)} yeni sinyal ({EVAL_HOURS} saat sonra degerlendirilecek)"]
        for sym, a, price in new[:MAX_NOTIFY]:
            icon = "🟢" if a["signal"] == "AL" else "🔴"
            lines.append(f"{icon} {a['signal']} {sym[:-4]} ${fmt(price)} | skor {a['score']:+d} | "
                         f"RSI {a['rsi']:.0f} | risk {a['risk']}/10")
        if len(new) > MAX_NOTIFY:
            lines.append(f"... ve {len(new) - MAX_NOTIFY} sinyal daha (hepsi istatistige kaydedildi)")
        send("\n".join(lines))

    today = now.strftime("%Y-%m-%d")
    if now.hour == SUMMARY_HOUR_UTC and state.get("last_summary") != today:
        day = min(elapsed.days + 1, TRACK_DAYS)
        text = stats_text(state, f"📊 Gunluk ozet (gun {day}/{TRACK_DAYS})")
        if counts:
            text += f"\nTarama: {counts['n']} coin | AL {counts['AL']} / SAT {counts['SAT']} / BEKLE {counts['BEKLE']}"
        send(text)
        state["last_summary"] = today

    if elapsed >= timedelta(days=TRACK_DAYS, hours=EVAL_HOURS):
        send(stats_text(state, "🏁 30 gunluk FINAL RAPOR"))
        state["final_sent"] = True

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        if counts:
            send(f"✅ Bot calisiyor. {counts['n']} coin tarandi | "
                 f"AL {counts['AL']} / SAT {counts['SAT']} / BEKLE {counts['BEKLE']}")
        else:
            send("✅ Bot calisiyor (takip suresi bitti).")

    save_state(state)


if __name__ == "__main__":
    main()
