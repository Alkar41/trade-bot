"""Binance USDT coin tarayici.
1) AL / SAT / BEKLE sinyalleri (+ 30 gun isabet istatistigi)
2) RADAR: hacmi patlayan ama fiyati henuz cok yukselmemis coinleri erken yakalar
3) Yeni listelenen USDT paritelerini haber verir
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
EVAL_HOURS = 24          # AL/SAT sinyali kac saat sonra degerlendirilsin
TRACK_DAYS = 30          # kac gun sinyal uretilsin
SUMMARY_HOUR_UTC = 6     # gunluk ozet saati (UTC 06:00 = TR 09:00)
MIN_VOLUME = 2_000_000   # AL/SAT icin 24s minimum hacim (USDT)
MAX_NOTIFY = 10          # tek mesajda en fazla kac AL/SAT sinyali
SIGNAL_SCORE = 3         # AL/SAT icin gereken minimum skor
STRONG_SCORE = 4         # "guclu sinyal" siniri

RADAR_MIN_VOLUME = 500_000   # radar icin 24s minimum hacim (USDT)
RADAR_VOL_RATIO = 3.0        # son 6 saat hacmi / onceki 4 gun ortalamasi
RADAR_MIN_CH = -3.0          # 24s degisim alt siniri (%)
RADAR_MAX_CH = 15.0          # 24s degisim ust siniri (%): zaten ucmus coini alma
RADAR_NEAR_HIGH = 0.97       # fiyat 4 gunluk zirvenin en az %97'sinde olsun
RADAR_COOLDOWN_H = 72        # ayni coin icin tekrar radar sinyali bekleme suresi
RADAR_HORIZONS = (24, 72, 168)
RADAR_HIT_PCT = 10.0         # "buyuk hareket" sayilacak yukselis (%)
MAX_RADAR_NOTIFY = 8

STATE_FILE = "data/state.json"
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
           "EURI", "EUR", "AEUR", "XUSD", "UST", "USDS",
           # kripto gibi davranmayan varliklar (altin, hisse tokenlari): listeye ekleyebilirsin
           "PAXG", "XAUT", "NVDAB", "SNDKB"}

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def parse(ts):
    return datetime.fromisoformat(ts)


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
        if vol >= RADAR_MIN_VOLUME:
            universe[sym] = {"price": price, "vol": vol}
    return universe, prices_all


def get_candles(sym):
    d = http_json(f"/api/v3/klines?symbol={sym}&interval={INTERVAL}&limit=120")
    return [float(k[4]) for k in d], [float(k[7]) for k in d]


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


def radar(c, q):
    """Hacim patlamasi var, fiyat zirveye yakin ama henuz cok yukselmemis mi?"""
    if len(c) < 100:
        return None
    base_list = q[-102:-6]
    base = sum(base_list) / len(base_list)
    if base <= 0:
        return None
    ratio = (sum(q[-6:]) / 6) / base
    ch24 = (c[-1] / c[-25] - 1) * 100
    high = max(c[-97:-1])
    hit = (ratio >= RADAR_VOL_RATIO and RADAR_MIN_CH <= ch24 <= RADAR_MAX_CH
           and c[-1] >= RADAR_NEAR_HIGH * high)
    return {"hit": hit, "ratio": ratio, "ch24": ch24}


def safe_scan(sym):
    try:
        closes, qv = get_candles(sym)
        if len(closes) < 61:
            return None
        c, q = closes[:-1], qv[:-1]   # son (acik) mumu katma
        return {"a": analyze(c), "radar": radar(c, q)}
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
    st.setdefault("radar", [])
    st.setdefault("radar_open", {})
    st.setdefault("seen", [])
    st["signals"] = [s for s in st.get("signals", [])
                     if "sym" in s and s["sym"][:-4] not in STABLES]
    st["radar"] = [s for s in st["radar"] if s["sym"][:-4] not in STABLES]
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
        if now - parse(s["t"]) < timedelta(hours=EVAL_HOURS):
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


def evaluate_radar(state, prices_all, btc, now):
    for s in state["radar"]:
        price = prices_all.get(s["sym"])
        if not price:
            continue
        age = now - parse(s["t"])
        for h in RADAR_HORIZONS:
            if f"r{h}" in s:
                continue
            if age < timedelta(hours=h) or age > timedelta(hours=h + 6):
                continue
            chg = (price / s["p"] - 1) * 100
            bchg = (btc / s["b"] - 1) * 100
            s[f"r{h}"] = round(chg, 2)
            s[f"a{h}"] = round(chg - bchg, 2)


def stats_text(state, title):
    done = [s for s in state["signals"] if s.get("r") is not None]
    open_n = len(state["signals"]) - len(done)
    if not done:
        return f"{title}\nHenuz degerlendirilen AL/SAT sinyali yok. Acik sinyal: {open_n}"

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


def radar_text(state):
    rs = state["radar"]
    if not rs:
        return "🔭 Radar: henuz sinyal yok"
    lines = [f"🔭 Radar: {len(rs)} sinyal (sadece alim yonlu, fiyat degisimi)"]
    for h in RADAR_HORIZONS:
        vals = [s[f"r{h}"] for s in rs if f"r{h}" in s]
        if not vals:
            continue
        al = [s[f"a{h}"] for s in rs if f"a{h}" in s]
        pos = sum(1 for v in vals if v > 0)
        big = sum(1 for v in vals if v >= RADAR_HIT_PCT)
        lines.append(f"{h}s ({len(vals)} sinyal): pozitif %{pos / len(vals) * 100:.0f} | "
                     f"+%{RADAR_HIT_PCT:.0f} ustu %{big / len(vals) * 100:.0f} | "
                     f"medyan %{statistics.median(vals):.1f} | "
                     f"BTC'ye gore ort. %{sum(al) / len(al):+.1f}")
    if len(lines) == 1:
        lines.append("Henuz degerlendirilen sinyal yok (ilk sonuc 24 saat sonra).")
    return "\n".join(lines)


# ---------- ana akis ----------
def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    if state.get("final_sent"):
        print("Takip bitti.")
        return

    universe, prices_all = get_tickers()
    btc = prices_all.get("BTCUSDT")
    if not btc:
        raise RuntimeError("BTC fiyati alinamadi")
    evaluate(state, prices_all, btc, now)
    evaluate_radar(state, prices_all, btc, now)

    # yeni listelenen parite uyarisi (ilk calismada sadece kaydeder)
    seen = set(state["seen"])
    new_listings = sorted(s for s in prices_all if s not in seen) if seen else []
    state["seen"] = sorted(seen | set(prices_all))
    if new_listings:
        names = ", ".join(s[:-4] for s in new_listings[:15])
        send(f"🆕 Yeni USDT paritesi: {names}\n"
             "Yeni listelemelerde fiyat cok oynak olur, hem sert yukselir hem sert duser.")

    elapsed = now - parse(state["start"])
    tracking = elapsed < timedelta(days=TRACK_DAYS)
    counts = None
    new, radar_new = [], []

    if tracking:
        syms = sorted(universe, key=lambda s: -universe[s]["vol"])
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(safe_scan, syms))
        counts = {"AL": 0, "SAT": 0, "BEKLE": 0, "n": 0}
        for sym, res in zip(syms, results):
            if res is None:
                continue
            vol24 = universe[sym]["vol"]
            price = universe[sym]["price"]

            a = res["a"]
            if vol24 >= MIN_VOLUME:
                sig = a["signal"]
                counts[sig] += 1
                counts["n"] += 1
                prev = state["last_signals"].get(sym)
                state["last_signals"][sym] = sig
                lo = state["last_open"].get(sym)
                cooling = lo and now - parse(lo) < timedelta(hours=EVAL_HOURS)
                if sig != "BEKLE" and sig != prev and not cooling:
                    state["signals"].append({"sym": sym, "t": now.isoformat(), "p": price,
                                             "b": btc, "g": sig, "sc": a["score"], "r": None})
                    state["last_open"][sym] = now.isoformat()
                    new.append((sym, a, price))

            r = res["radar"]
            if r and r["hit"]:
                ro = state["radar_open"].get(sym)
                if not (ro and now - parse(ro) < timedelta(hours=RADAR_COOLDOWN_H)):
                    state["radar"].append({"sym": sym, "t": now.isoformat(), "p": price,
                                           "b": btc, "x": round(r["ratio"], 1),
                                           "c": round(r["ch24"], 1), "v": round(vol24)})
                    state["radar_open"][sym] = now.isoformat()
                    radar_new.append((sym, r, price, vol24))

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

    if radar_new:
        radar_new.sort(key=lambda x: -x[1]["ratio"])
        lines = [f"🔭 Radar: {len(radar_new)} coinde hacim patlamasi, fiyat henuz cok yukselmedi"]
        for sym, r, price, vol24 in radar_new[:MAX_RADAR_NOTIFY]:
            lines.append(f"{sym[:-4]} ${fmt(price)} | hacim {r['ratio']:.1f}x | "
                         f"24s %{r['ch24']:+.1f} | gunluk hacim ${vol24 / 1e6:.1f}M")
        lines.append("Erken uyari, tavsiye degil. Dusuk hacimli coinlerde risk cok yuksek.")
        send("\n".join(lines))

    today = now.strftime("%Y-%m-%d")
    if now.hour == SUMMARY_HOUR_UTC and state.get("last_summary") != today:
        day = min(elapsed.days + 1, TRACK_DAYS)
        text = stats_text(state, f"📊 Gunluk ozet (gun {day}/{TRACK_DAYS})")
        text += "\n" + radar_text(state)
        if counts:
            text += (f"\nTarama: {counts['n']} coin | "
                     f"AL {counts['AL']} / SAT {counts['SAT']} / BEKLE {counts['BEKLE']}")
        send(text)
        state["last_summary"] = today

    if elapsed >= timedelta(days=TRACK_DAYS + 7):
        send(stats_text(state, "🏁 FINAL RAPOR") + "\n" + radar_text(state))
        state["final_sent"] = True

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        if counts:
            send(f"✅ Bot calisiyor. {len(universe)} coin tarandi | "
                 f"AL {counts['AL']} / SAT {counts['SAT']} / BEKLE {counts['BEKLE']} | "
                 f"radar: {len(radar_new)} yeni")
        else:
            send("✅ Bot calisiyor (sinyal uretme suresi bitti, sonuclar degerlendiriliyor).")

    save_state(state)


if __name__ == "__main__":
    main()
