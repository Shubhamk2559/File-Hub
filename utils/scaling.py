"""
utils/scaling.py
Scaling advisor (gateway only): looks at the live cluster snapshot and tells the owner
"load is rising -> add N more server(s) and everything stays normal".

Pure functions + one small stateful class. No I/O and no Telegram/Mongo imports, so it is easy to test.

What it looks at (every signal gives a "servers needed" number, the biggest one wins):
  users     active users now / recent peak / where the trend goes in the next 10 min  (+ spare room)
  load      combined load score of all servers (users, tasks, streams, CPU, RAM)
  streams   live Stream/Download viewers vs what the servers can carry
  tasks     requests being processed at the same moment
  hot       servers at CPU >= 90% / RAM >= 92% / all task slots busy
  overflow  users who arrived while EVERY server was full (the clearest "add a server NOW" sign)
Down servers are reported separately: "restart these first", then "add N new".

Levels:  ok  ->  watch (rising, nothing to do yet)  ->  warn (add server(s))  ->  crit (add NOW, users are suffering)
"""
import html
import math
from collections import deque

RETIRED_AFTER = 6 * 3600     # a server silent for this long (or stopped on purpose) is ignored, its id can be reused
LOOKAHEAD_MIN = 10           # minutes we look ahead with the growth trend
HIST_SEC = 1800              # how long we remember samples
RANK = {"ok": 0, "watch": 1, "warn": 2, "crit": 3}


def _ceil(x: float) -> int:
    return int(math.ceil(x - 1e-9))


def next_node_ids(rows: list, count: int) -> list:
    """Free NODE_IDs for new servers: n1, n2, ... the first ones nobody is using right now."""
    used = {r["id"] for r in rows if r.get("age", 0) < RETIRED_AFTER}
    out, k = [], 1
    while len(out) < count:
        if f"n{k}" not in used:
            out.append(f"n{k}")
        k += 1
    return out


def _trend(hist: list, now: float) -> tuple:
    """(users per minute, peak of the last 2 min). Uses the average of the first/last 3 samples of the last 10 min."""
    recent = [(t, a) for t, a in hist if now - t <= 600]
    peak = max((a for t, a in hist if now - t <= 120), default=0)
    if len(recent) < 4:
        return 0.0, peak
    k = min(3, len(recent) // 2)
    t0 = sum(t for t, _ in recent[:k]) / k
    a0 = sum(a for _, a in recent[:k]) / k
    t1 = sum(t for t, _ in recent[-k:]) / k
    a1 = sum(a for _, a in recent[-k:]) / k
    if t1 - t0 < 180:
        return 0.0, peak
    return (a1 - a0) / ((t1 - t0) / 60.0), peak


def advise(snap: dict, hist: list = (), overflow_recent: int = 0, now: float = 0.0,
           headroom: float = 1.25, target: float = 0.70, min_servers: int = 2, max_servers: int = 0) -> dict:
    rows = snap.get("nodes", [])
    live = [r for r in rows if r["state"] != "DOWN"]
    down = [r for r in rows if r["state"] == "DOWN" and r.get("age", 0) < RETIRED_AFTER]
    n = len(live)
    adv = {"level": "ok", "n_live": n, "n_down": len(down), "down_ids": [r["id"] for r in down],
           "active": snap.get("active", 0), "cap_total": 0, "util": 0.0, "slope": 0.0, "projected": 0,
           "need_total": max(1, n), "add": 0, "restart": 0, "new": 0, "new_ids": [], "reasons": [],
           "surplus": 0, "eta_min": None, "left": 0, "capped": False, "gateway_hot": False, "hot_ids": [],
           "next_id": next_node_ids(rows, 1)[0]}
    if not n:
        adv.update(level="crit", need_total=1, add=1, new=1, new_ids=next_node_ids(rows, 1),
                   reasons=["❌ Koi server online nahi dikh raha"])
        return adv

    cap_total = sum(r["cap"] for r in live)
    avg_cap = cap_total / n
    active = adv["active"]
    slope, peak5 = _trend(list(hist), now)
    projected = active + max(0.0, slope) * LOOKAHEAD_MIN
    projected = min(projected, active * 2 + avg_cap)                 # a short spike must not give crazy numbers
    demand = max(active, peak5, projected)
    util = active / cap_total if cap_total else 0.0

    # ---- one "servers needed" number per signal
    need_users = _ceil(demand * headroom / avg_cap)
    need_load = _ceil(sum(r["load"] for r in live) / 100.0 / target)
    avg_ms = sum(max(1, r.get("max_streams", 8)) for r in live) / n
    avg_mt = sum(max(1, r.get("max_tasks", 20)) for r in live) / n
    need_streams = _ceil(sum(r["streams"] for r in live) / (target * avg_ms))
    need_tasks = _ceil(sum(r["tasks"] for r in live) / (target * avg_mt))
    hot = [r for r in live if r["state"] == "HOT" or r["cpu"] >= 90 or r["ram"] >= 92]
    need_hot = n + _ceil(len(hot) / 2) if hot and len(hot) * 2 >= n else 0
    need_ovf = n + max(1, _ceil(overflow_recent / avg_cap)) if overflow_recent > 0 else 0

    reasons = []
    if need_users > n:
        reasons.append(f"👥 Users: {active} active ({LOOKAHEAD_MIN} min me ~{round(projected)} tak) → {need_users} server chahiye")
    if need_load > n:
        reasons.append(f"📊 Total load {round(sum(r['load'] for r in live))}% ({n} server) → {need_load} server chahiye")
    if need_streams > n:
        reasons.append(f"🎬 Stream/Download viewers: {sum(r['streams'] for r in live)} → {need_streams} server chahiye")
    if need_tasks > n:
        reasons.append(f"⚙️ Requests ek saath: {sum(r['tasks'] for r in live)} → {need_tasks} server chahiye")
    if need_hot:
        reasons.append("🌡 Hot servers (CPU/RAM full): " + ", ".join(r["id"] for r in hot))
    if need_ovf:
        reasons.append(f"🔁 {overflow_recent} user aaye jab saare server full the")

    total = max(need_users, need_load, need_streams, need_tasks, need_hot, need_ovf, 1)
    capped = False
    if max_servers and total > max_servers:
        total, capped = max_servers, True
    add = max(0, total - n)
    restart = min(len(down), add)

    if overflow_recent > 0 or util >= 1.0 or (hot and len(hot) == n):
        level = "crit"
    elif add > 0:
        level = "warn"
    elif util >= 0.70 or projected >= 0.85 * cap_total:
        level = "watch"
    else:
        level = "ok"

    eta = None
    if slope > 0.3 and cap_total > active:
        eta = max(1, round((cap_total - active) / slope))
    gw = next((r for r in live if r.get("gateway")), None)

    adv.update(level=level, cap_total=cap_total, util=util, slope=slope, projected=round(projected),
               need_total=total, add=add, restart=restart, new=add - restart, reasons=reasons,
               surplus=max(0, n - max(total + 1, min_servers)) if level == "ok" else 0,
               eta_min=eta, left=max(0, cap_total - active), capped=capped,
               gateway_hot=bool(gw and (gw["cpu"] >= 90 or gw["ram"] >= 92)), hot_ids=[r["id"] for r in hot])
    adv["new_ids"] = next_node_ids(rows, adv["new"]) if adv["new"] else []
    return adv


# ----------------------------------------------------------------------------- texts (HTML, Hinglish)
def _ids(ids: list) -> str:
    return ", ".join(ids[:6]) + (f" +{len(ids) - 6}" if len(ids) > 6 else "")


def _how_to(adv: dict) -> str:
    ids = adv["new_ids"]
    first = ids[0] if ids else adv["next_id"]
    return ("<b>Naya server kaise add karein:</b>\n"
            "1. Naye account me wahi repo deploy karo (Dockerfile).\n"
            "2. Env bilkul n1 jaisa (same <code>BOT_TOKEN</code>, <code>MONGO_URI</code>, <code>DB_NAME</code>, "
            "<code>CLUSTER_SECRET</code>, <code>PUBLIC_URL</code>), sirf ye alag:\n"
            f"   <code>NODE_ID={first}</code>" + (f" (agle: {html.escape(_ids(ids[1:]))})" if len(ids) > 1 else "") + " aur <code>NODE_ROLE=worker</code>\n"
            "3. Agar Cloudflare Worker use kar rahe ho to uske <code>ORIGINS</code> me naye server ka URL comma se jodo.\n"
            "4. 10 sec me ye Panel → Servers me dikh jayega, kuch restart nahi karna.")


def format_alert(adv: dict) -> str:
    lvl = adv["level"]
    head = {"crit": "🚨 <b>Bot pe load bahut zyada hai!</b>",
            "warn": "⚠️ <b>Load badh gaya, server add karne ka time aa gaya</b>",
            "watch": "👀 <b>Load badh raha hai</b>"}[lvl]
    pct = round(100 * adv["util"])
    lines = [head, "",
             f"👥 Active users: <b>{adv['active']}</b> / {adv['cap_total']} ({pct}%)",
             f"🖥 Servers chal rahe: <b>{adv['n_live']}</b>" + (f" · 🔴 down: {html.escape(_ids(adv['down_ids']))}" if adv["n_down"] else "")]
    if lvl == "watch":
        lines.append(f"📈 Abhi koi action zaroori nahi, bas ready raho. ~{adv['left']} users aur tak sab normal chalega"
                     + (f"; is speed se ~{adv['eta_min']} min me servers full ho sakte hain." if adv["eta_min"] else "."))
        lines.append(f"Agla server: <code>NODE_ID={adv['next_id']}</code>")
        return "\n".join(lines)
    lines.append("")
    if adv["restart"]:
        lines.append(f"🔧 <b>Pehle down server wapas chalao:</b> {html.escape(_ids(adv['down_ids'][:adv['restart']]))} (restart / redeploy)")
    if adv["new"]:
        lines.append(f"➕ <b>{adv['new']} naya server add karo</b> (total {adv['need_total']}) → tab sab normal chalega.")
    elif adv["restart"]:
        lines.append(f"✅ Wo wapas aa gaye to total {adv['need_total']} server ho jayenge, kaafi hai.")
    if adv["capped"]:
        lines.append("ℹ️ <code>SCALE_MAX_SERVERS</code> ki limit lagi hai, usse zyada nahi bataya. "
                     "Limit badhao ya <code>NODE_CAPACITY</code> badhao.")
    if adv["gateway_hot"]:
        lines.append("🚪 Gateway khud hot hai, naye server aane se uska load bhi kam hoga.")
    if adv["reasons"]:
        lines += ["", "<b>Wajah:</b>"] + [f"• {r}" for r in adv["reasons"]]
    if adv["new"]:
        lines += ["", _how_to(adv)]
    return "\n".join(lines)


def format_clear(adv: dict) -> str:
    return (f"✅ <b>Load normal ho gaya.</b> {adv['active']} active users, {adv['n_live']} server kaafi hain."
            + (f"\n💡 Chaho to {adv['surplus']} extra server band kar sakte ho (zaroori nahi)." if adv["surplus"] else ""))


def format_line(adv: dict) -> str:
    """One line for the Servers summary screen."""
    icon = {"ok": "🟢", "watch": "🟡", "warn": "🟠", "crit": "🔴"}[adv["level"]]
    word = {"ok": "Sab normal", "watch": "Load badh raha hai", "warn": "Server add karo", "crit": "Abhi server add karo"}[adv["level"]]
    return f"{icon} <b>{word}</b>" + (f" · +{adv['new']} server chahiye" if adv["new"] else "") + (
        f" · 🔧 restart: {html.escape(_ids(adv['down_ids'][:adv['restart']]))}" if adv["restart"] else "")


def format_panel(adv: dict) -> str:
    """Short live block for Panel -> Servers -> Scaling."""
    icon = {"ok": "🟢", "watch": "🟡", "warn": "🟠", "crit": "🔴"}[adv["level"]]
    word = {"ok": "Sab normal", "watch": "Load badh raha hai", "warn": "Server add karo", "crit": "Abhi server add karo"}[adv["level"]]
    lines = [f"{icon} <b>Live status: {word}</b>",
             f"👥 {adv['active']} / {adv['cap_total']} users ({round(100 * adv['util'])}%) · 🖥 {adv['n_live']} server"
             + (f" · 🔴 down: {html.escape(_ids(adv['down_ids']))}" if adv["n_down"] else "")]
    if adv["slope"] > 0.3:
        lines.append(f"📈 Trend: +{adv['slope']:.1f} users/min"
                     + (f" · ~{adv['eta_min']} min me full" if adv["eta_min"] else ""))
    if adv["restart"]:
        lines.append(f"🔧 Pehle wapas chalao: {html.escape(_ids(adv['down_ids'][:adv['restart']]))}")
    if adv["new"]:
        lines.append(f"➕ <b>{adv['new']} naya server add karo</b> (total {adv['need_total']})")
    elif adv["level"] == "ok":
        lines.append(f"✅ {adv['n_live']} server abhi ke load ke liye kaafi hain.")
        if adv["surplus"]:
            lines.append(f"💡 {adv['surplus']} server extra hain, chaho to band kar sakte ho.")
    lines += [f"• {r}" for r in adv["reasons"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- state + owner message throttling
class Advisor:
    """Remembers recent samples and decides WHEN to message the owner (no spam, but never miss a worse situation)."""
    WORSE_GAP = 300        # a worse situation is sent after at least this many seconds since the last message
    REMIND = 1800          # still bad -> reminder every 30 min (warn / crit only)
    CALM_FOR = 600         # load must stay ok this long before "all clear"

    def __init__(self):
        self.hist = deque()
        self.ovf = deque()
        self.last = None
        self.sent_level, self.sent_add, self.sent_at, self.calm_since = "ok", 0, 0.0, 0.0

    def update(self, snap: dict, now: float, overflow_total: int, **cfg):
        """Returns (advice dict, message text or None)."""
        self.hist.append((now, snap.get("active", 0)))
        self.ovf.append((now, overflow_total))
        while self.hist and now - self.hist[0][0] > HIST_SEC:
            self.hist.popleft()
        while self.ovf and now - self.ovf[0][0] > HIST_SEC:
            self.ovf.popleft()
        base = next((v for t, v in self.ovf if now - t <= 300), overflow_total)
        adv = advise(snap, list(self.hist), max(0, overflow_total - base), now, **cfg)
        self.last = adv
        lvl, msg = adv["level"], None
        if lvl != "ok":
            self.calm_since = 0.0
            worse = RANK[lvl] > RANK[self.sent_level] or adv["add"] > self.sent_add
            if worse and now - self.sent_at >= self.WORSE_GAP:
                msg = format_alert(adv)
            elif not worse and lvl in ("warn", "crit") and now - self.sent_at >= self.REMIND:
                msg = format_alert(adv)
            if msg:
                self.sent_at = now
            if msg or not worse:                    # follow the current situation (so a later rise counts as "worse")
                self.sent_level, self.sent_add = lvl, adv["add"]
        elif self.sent_level != "ok":
            self.calm_since = self.calm_since or now
            if now - self.calm_since >= self.CALM_FOR:
                msg = format_clear(adv)
                self.sent_level, self.sent_add, self.sent_at, self.calm_since = "ok", 0, now, 0.0
        return adv, msg
