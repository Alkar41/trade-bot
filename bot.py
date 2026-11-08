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
