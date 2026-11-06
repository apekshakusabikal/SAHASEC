"""Sahasec backend: sends SMS (normal text messages) to the caretaker.
 - Daily report at 12:00 PM (dashboard sends it; backend sends a backup at 12:01 if it didn't)
 - Emergency alerts from the dashboard

Install:  pip install flask flask-cors apscheduler requests
Run:      python server.py

Choose ONE SMS provider with env variables:

  Twilio (works worldwide):
    SMS_PROVIDER=twilio  TWILIO_SID=...  TWILIO_TOKEN=...  TWILIO_FROM=+1xxxxxxxxxx
  Fast2SMS (India):
    SMS_PROVIDER=fast2sms  FAST2SMS_KEY=...

Optional: CARETAKER_PHONE=+91XXXXXXXXXX (used by the backup job if the dashboard is closed)
"""
import os, re, datetime as dt
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__); CORS(app)
samples = []  # (datetime, dict)
PHONE_FILE = "caretaker_phone.txt"
def load_phone():
    try: return open(PHONE_FILE).read().strip()
    except OSError: return os.environ.get("CARETAKER_PHONE", "")
def save_phone(p):
    state["phone"] = p
    try: open(PHONE_FILE, "w").write(p)
    except OSError: pass
state = {"phone": "", "sent": None}
state["phone"] = load_phone()
R = {"hr": (50, 60, 100, 120), "spo2": (91, 95, 101, 101),
     "temp": (35, 36.1, 37.5, 38.5)}
N = {"hr": "Heart rate", "spo2": "SpO2", "temp": "Temp"}
U = {"hr": "bpm", "spo2": "%", "temp": "C"}
E = ["NORMAL", "MID RISK", "HIGH RISK"]
SP = "▁▂▃▄▅▆▇█"
TH = {"hr": 5, "spo2": 1, "temp": 0.3}
def t12(h): return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"

def cls(k, x):
    r = R[k]
    return 2 if (x < r[0] or x > r[3]) else 1 if (x < r[1] or x > r[2]) else 0

def bar(k, v):
    r = R[k]; n = round(max(0, min(1, (v - r[0]) / (r[3] - r[0]))) * 8)
    return "#" * n + "-" * (8 - n)

def build_report():
    now = dt.datetime.now(); today = now.date()
    d = [(t, s) for t, s in samples if t.date() == today]
    if not d: return "SAHASEC: no readings received today."
    hrs = {}
    for t, v in d: hrs.setdefault(t.hour, []).append(v)
    up = 23; fr = 0  # whole day
    avg = {k: sum(v[k] for _, v in d) / len(d) for k in R}
    red_hours = [t12(h) for h in sorted(hrs) if any(cls(k, v[k]) == 2 for v in hrs[h] for k in R)]
    red = sum(1 for _, v in d if any(cls(k, v[k]) == 2 for k in R))
    st = max(cls(k, avg[k]) for k in R)
    if red > 36: st = 2  # about 3 minutes of high-risk readings
    elif red: st = max(st, 1)
    msg = f"SAHASEC DAILY REPORT\n{today:%d %b}\nSTATUS: {E[st]}\n"
    for k in R:
        line, f, l = "", None, None
        for h in range(fr, up + 1):
            if h not in hrs: line += "·"; continue
            a = sum(v[k] for v in hrs[h]) / len(hrs[h]); f = a if f is None else f; l = a
            line += SP[round(max(0, min(1, (a - R[k][0]) / (R[k][3] - R[k][0]))) * 7)]
        tr = "" if f is None else "➡️" if abs(l - f) < TH[k] else "📈" if l > f else "📉"
        msg += f"\n{N[k]}: {avg[k]:.1f}{U[k]} {tr} {E[cls(k, avg[k])]}\n{line}\n"
    msg += f"({t12(fr)} to {t12(up)}, hourly)\n\nHigh-risk alerts: {red}" + (f" at {', '.join(red_hours)}" if red_hours else "")
    return msg + "\n" + ("Please check on the patient." if st else "No action needed.")

def clean(t):  # plain-ASCII so it goes as a normal (cheap) SMS
    for a, b in {"🟢": "[OK]", "🟡": "[WARN]", "🔴": "[CRIT]", "▓": "#", "░": "-", "₂": "2", "–": "-"}.items():
        t = t.replace(a, b)
    return re.sub(r"[^\x00-\x7F]", "", t)

def send_sms(phone, text):
    prov = os.environ.get("SMS_PROVIDER", "twilio").lower()
    nums = [n for n in re.split(r"[,;\s]+", phone) if n]  # one or many numbers
    if prov != "textbee": text = clean(text)  # TextBee sends emoji/graphs as-is
    if prov == "textbee":  # sends from your Android phone's SIM
        r = requests.post(f"https://api.textbee.dev/api/v1/gateway/devices/{os.environ['TEXTBEE_DEVICE_ID']}/send-sms",
                          headers={"x-api-key": os.environ["TEXTBEE_API_KEY"]},
                          json={"recipients": nums, "message": text}, timeout=30)
    elif prov == "fast2sms":
        r = requests.post("https://www.fast2sms.com/dev/bulkV2",
                          headers={"authorization": os.environ["FAST2SMS_KEY"]},
                          data={"route": "q", "message": text,
                                "numbers": ",".join(re.sub(r"\D", "", n)[-10:] for n in nums), "flash": 0}, timeout=30)
    else:
        sid = os.environ["TWILIO_SID"]
        r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                          auth=(sid, os.environ["TWILIO_TOKEN"]),
                          data={"From": os.environ["TWILIO_FROM"], "To": nums[0], "Body": text}, timeout=30)
    print("SMS", r.status_code, r.text[:200])
    return r.ok

def handle(mark_daily):
    j = request.get_json(silent=True) or {}
    phone = j.get("phone") or state["phone"]
    if not phone: return jsonify(ok=False, error="no phone"), 400
    state["phone"] = phone
    ok = send_sms(phone, j.get("message") or build_report())
    if ok and mark_daily: state["sent"] = dt.date.today()
    return jsonify(ok=ok), (200 if ok else 502)

@app.post("/vitals")
def vitals():
    j = request.get_json(force=True)
    if j.get("phone"): state["phone"] = j["phone"]
    samples.append((dt.datetime.now(), {k: float(j[k]) for k in R}))
    return jsonify(ok=True)

@app.post("/send_report")   # daily report
def send_report(): return handle(True)

@app.post("/send_sms")      # emergency alert
def send_alert(): return handle(False)

@app.post("/settings")      # save / edit caretaker number
def settings():
    p = (request.get_json(silent=True) or {}).get("phone", "").strip()
    if p: save_phone(p)
    return jsonify(ok=True, phone=state["phone"])

def backup_daily():  # only if the dashboard didn't already send today's report
    if state["sent"] != dt.date.today() and state["phone"]:
        if send_sms(state["phone"], build_report()): state["sent"] = dt.date.today()

sched = BackgroundScheduler()
sched.add_job(backup_daily, "cron", hour=12, minute=1)
sched.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)