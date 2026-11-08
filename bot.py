
import json
import os
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SYMBOL = "BTCUSDT"
INTERVAL = "1h"
EVAL_HOURS = 24
TRACK_DAYS = 30
SUMMARY_HOUR_UTC = 6
STATE_FILE = "data/state.json"
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "signal-bot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_closes():
    last_err = None
    for host in HOSTS:
        try:
            url = f"{host}/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=200"
            data = http_json(url)
            return [float(k[4]) for k in data]
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Fiyat alınamadı: {last_err}")


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


def analyze(closes):
    r = rsi(closes)
    s20, s50 = sma(closes, 20), sma(closes, 50)
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
    signal = "AL" if score >= 2 else "SAT" if score <= -2 else "BEKLE"

    rets = [b / a - 1 for a, b in zip(closes[-25:], closes[-24:])]
    vol = statistics.pstdev(rets) * 100
    risk = max(1, min(10, round(vol * 8)))
    risk_label = "Düşük" if risk <= 3 else "Orta" if risk <= 6 else "Yüksek"
    return {"signal": signal, "rsi": r, "sma20": s20, "sma50": s50,
            "risk": risk, "risk_label": risk_label}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"start": datetime.now(timezone.utc).isoformat(),
            "last_signal": None, "signals": [],
            "last_summary": None, "final_sent": False}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def send(text):
    print(text)
    if not TOKEN or not CHAT_ID:
        print("(Telegram bilgileri yok, mesaj gönderilmedi)")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=20)
    except Exception as e:
        print("Telegram hatası:", e)


def evaluate(state, price, now):
    for s in state["signals"]:
        if s.get("result") is not None:
            continue
        opened = datetime.fromisoformat(s["time"])
        if now - opened >= timedelta(hours=EVAL_HOURS):
            chg = (price / s["price"] - 1) * 100
            if s["signal"] == "SAT":
                chg = -chg
            s["exit_price"] = price
            s["pnl"] = round(chg, 2)
            s["result"] = "doğru" if chg > 0 else "yanlış"


def stats_text(state, title):
    done = [s for s in state["signals"] if s.get("result")]
    open_n = len(state["signals"]) - len(done)
    if not done:
        return f"{title}\nHenüz değerlendirilen sinyal yok. Açık sinyal: {open_n}"
    wins = [s for s in done if s["result"] == "doğru"]
    rate = len(wins) / len(done) * 100
    avg = sum(s["pnl"] for s in done) / len(done)
    total = sum(s["pnl"] for s in done)
    lines = [title,
             f"Değerlendirilen: {len(done)} | Açık: {open_n}",
             f"İsabet: %{rate:.0f} ({len(wins)}/{len(done)})",
             f"Ortalama getiri: %{avg:.2f} | Toplam: %{total:.2f}"]
    for kind in ("AL", "SAT"):
        k = [s for s in done if s["signal"] == kind]
        if k:
            kw = len([s for s in k if s["result"] == "doğru"])
            lines.append(f"{kind}: {kw}/{len(k)} doğru")
    return "\n".join(lines)


def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    if state.get("final_sent"):
        print("30 günlük takip bitti.")
        return

    closes = get_closes()
    price = closes[-1]
    evaluate(state, price, now)
    a = analyze(closes)

    start = datetime.fromisoformat(state["start"])
    elapsed = now - start
    tracking = elapsed < timedelta(days=TRACK_DAYS)

    if tracking and a["signal"] != state["last_signal"]:
        if a["signal"] in ("AL", "SAT"):
            state["signals"].append({"time": now.isoformat(), "price": price,
                                     "signal": a["signal"], "rsi": round(a["rsi"], 1),
                                     "risk": a["risk"], "result": None})
            icon = "🟢" if a["signal"] == "AL" else "🔴"
            send(f"{icon} {a['signal']} sinyali: BTC ${price:,.0f}\n"
                 f"RSI: {a['rsi']:.0f} | Risk: {a['risk']}/10 ({a['risk_label']})\n"
                 f"{EVAL_HOURS} saat sonra değerlendirilecek.")
        state["last_signal"] = a["signal"]

    today = now.strftime("%Y-%m-%d")
    if now.hour == SUMMARY_HOUR_UTC and state.get("last_summary") != today:
        day = min(elapsed.days + 1, TRACK_DAYS)
        send(stats_text(state, f"📊 Günlük özet (gün {day}/{TRACK_DAYS})"))
        state["last_summary"] = today

    if elapsed >= timedelta(days=TRACK_DAYS, hours=EVAL_HOURS):
        send(stats_text(state, "🏁 30 günlük FİNAL RAPOR"))
        state["final_sent"] = True

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send(f"✅ Bot çalışıyor. BTC ${price:,.0f} | Sinyal: {a['signal']} | "
             f"RSI {a['rsi']:.0f} | Risk {a['risk']}/10")

    save_state(state)


if __name__ == "__main__":
    main()
