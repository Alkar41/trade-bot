"""Backtest: bot.py'deki kurallari gecmis Binance verisinde dener, dogruluk raporunu Telegram'a yollar.
Gecmiste tutan kuralin gelecekte de tutacagi garanti degildir."""
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import bot

DAYS = int(os.environ.get("BT_DAYS", 60))
TOP_N = int(os.environ.get("BT_TOP", 80))
WIN = 119      # canli kodda 119 kapali mum kullaniliyor
H = 3_600_000  # 1 saat (ms)


def fetch(sym):
    end = int(time.time() * 1000)
    start = end - DAYS * 24 * H
    t, c, q = [], [], []
    while start < end:
        d = bot.http_json(f"/api/v3/klines?symbol={sym}&interval=1h&limit=1000&startTime={start}")
        if not d:
            break
        for k in d:
            t.append(int(k[0]))
            c.append(float(k[4]))
            q.append(float(k[7]))
        start = int(d[-1][0]) + H
        if len(d) < 1000:
            break
    return sym, t[:-1], c[:-1], q[:-1]   # son (acik) mumu at


def pct(a, b):
    return (b / a - 1) * 100


def simulate(times, closes, qv, btc, res):
    n = len(closes)
    last_sig, last_open, radar_open = None, -999, -999
    for i in range(WIN - 1, n - 1):
        c = closes[i - WIN + 1:i + 1]
        q = qv[i - WIN + 1:i + 1]
        try:
            a = bot.analyze(c)
            rd = bot.radar(c, q)
        except Exception:
            continue

        def move(h):
            j = i + h
            if j >= n:
                return None
            b0, b1 = btc.get(times[i]), btc.get(times[j])
            return pct(closes[i], closes[j]), (pct(b0, b1) if b0 and b1 else None)

        m24, m72 = move(24), move(72)
        if m24:
            res["base"].append(m24[0])
        if m72:
            res["base72"].append(m72[0])

        sig = a["signal"]
        if sig != "BEKLE" and sig != last_sig and i - last_open >= bot.EVAL_HOURS and m24:
            chg, bchg = m24
            if sig == "SAT":
                chg = -chg
                bchg = -bchg if bchg is not None else None
            res["sig"].append({"g": sig, "sc": a["score"], "t": times[i], "r": chg,
                               "a": chg - bchg if bchg is not None else None})
            last_open = i
        last_sig = sig

        if rd and rd["hit"] and i - radar_open >= bot.RADAR_COOLDOWN_H:
            row = {"t": times[i]}
            for h in bot.RADAR_HORIZONS:
                m = move(h)
                if m:
                    row[f"r{h}"] = m[0]
                    row[f"a{h}"] = m[0] - m[1] if m[1] is not None else None
            res["radar"].append(row)
            radar_open = i


def summ(rows):
    n = len(rows)
    if not n:
        return None
    w = sum(1 for x in rows if x["r"] > 0)
    al = [x["a"] for x in rows if x["a"] is not None]
    return n, w / n * 100, sum(x["r"] for x in rows) / n, (sum(al) / len(al) if al else 0.0)


def line(name, rows, base=None):
    s = summ(rows)
    if not s:
        return f"{name}: veri yok"
    n, p, avg, alpha = s
    extra = f" | piyasa tabani %{base:.0f}" if base is not None else ""
    return f"{name}: {n} sinyal | isabet %{p:.0f}{extra} | ort. %{avg:+.2f} | BTC'ye gore %{alpha:+.2f}"


def main():
    universe, _ = bot.get_tickers()
    syms = sorted(universe, key=lambda s: -universe[s]["vol"])[:TOP_N]
    if "BTCUSDT" not in syms:
        syms.append("BTCUSDT")
    with ThreadPoolExecutor(max_workers=6) as ex:
        data = [d for d in ex.map(fetch, syms) if len(d[2]) > WIN + 30]
    btc_d = [d for d in data if d[0] == "BTCUSDT"]
    if not btc_d:
        raise RuntimeError("BTC verisi alinamadi")
    btc = dict(zip(btc_d[0][1], btc_d[0][2]))

    res = {"sig": [], "radar": [], "base": [], "base72": []}
    for _, t, c, q in data:
        simulate(t, c, q, btc, res)

    sigs = res["sig"]
    up = sum(1 for x in res["base"] if x > 0) / max(1, len(res["base"])) * 100
    L = [f"📈 BACKTEST ({DAYS} gun, {len(data)} coin, 1 saatlik mum)",
         "Not: bugunun hacimli coinleri kullanildi, gecmiste dusenler yok (sonuclar iyimser olabilir).",
         "", f"AL/SAT sinyalleri (skor 3 ve uzeri):"]
    L.append(line("Tumu", sigs))
    L.append(line("AL", [x for x in sigs if x["g"] == "AL"], up))
    L.append(line("SAT", [x for x in sigs if x["g"] == "SAT"], 100 - up))
    L.append("Skora gore isabet:")
    for k in (3, 4, 5):
        rows = [x for x in sigs if abs(x["sc"]) == k]
        s = summ(rows)
        if s:
            L.append(f"  skor ±{k}: {s[0]} sinyal | isabet %{s[1]:.0f} | ort. %{s[2]:+.2f}")
    L.append("Esik taramasi (en az skor):")
    for k in (3, 4, 5):
        s = summ([x for x in sigs if abs(x["sc"]) >= k])
        if s:
            L.append(f"  >= {k}: {s[0]} sinyal | isabet %{s[1]:.0f} | BTC'ye gore %{s[3]:+.2f}")
    if sigs:
        ts = sorted(x["t"] for x in sigs)
        cut = ts[0] + (ts[-1] - ts[0]) * 2 // 3
        a1 = summ([x for x in sigs if x["t"] <= cut])
        a2 = summ([x for x in sigs if x["t"] > cut])
        if a1 and a2:
            L.append(f"Tutarlilik: ilk 2/3 -> %{a1[1]:.0f} ({a1[0]} sinyal) | son 1/3 -> %{a2[1]:.0f} ({a2[0]} sinyal)")
    s = summ(sigs)
    if s:
        n_al = sum(1 for x in sigs if x["g"] == "AL")
        base_mix = (n_al * up + (len(sigs) - n_al) * (100 - up)) / len(sigs)
        L.append(f"Avantaj: tum sinyallerde isabet %{s[1]:.0f}, rastgele giris tabani yaklasik %{base_mix:.0f}")

    rr = res["radar"]
    L += ["", f"🔭 Radar: {len(rr)} sinyal"]
    b72 = res["base72"]
    for h in bot.RADAR_HORIZONS:
        v = [x for x in rr if f"r{h}" in x]
        if not v:
            continue
        r = [x[f"r{h}"] for x in v]
        al = [x[f"a{h}"] for x in v if x.get(f"a{h}") is not None]
        L.append(f"  {h}s ({len(v)}): pozitif %{sum(1 for x in r if x > 0) / len(r) * 100:.0f} | "
                 f"+%{bot.RADAR_HIT_PCT:.0f} ustu %{sum(1 for x in r if x >= bot.RADAR_HIT_PCT) / len(r) * 100:.0f} | "
                 f"medyan %{statistics.median(r):+.1f} | BTC'ye gore %{(sum(al) / len(al) if al else 0):+.1f}")
    if b72:
        L.append(f"  Rastgele giris tabani (72s): pozitif %{sum(1 for x in b72 if x > 0) / len(b72) * 100:.0f} | "
                 f"+%{bot.RADAR_HIT_PCT:.0f} ustu %{sum(1 for x in b72 if x >= bot.RADAR_HIT_PCT) / len(b72) * 100:.0f}")
    L += ["", "Gecmiste tutan kural gelecekte de tutar diye garanti yok. Canli sonuclarla birlikte degerlendir."]
    bot.send("\n".join(L))


if __name__ == "__main__":
    main()
