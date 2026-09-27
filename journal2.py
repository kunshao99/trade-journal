# -*- coding: utf-8 -*-
"""
交易双轨记录工具
轨道1: 系统信号(自动记录每次分析建议)
轨道2: 实际交易(一键从信号填充, 或手动)
+ 自动取价: 实时显示盈亏, 判断止盈止损
用法: python journal2.py  打开 http://localhost:8630
"""
import json, os, sqlite3, urllib.request, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

PORT = int(os.environ.get("PORT", 8630))  # Render 会注入 PORT 环境变量
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "trades_v2.db")

# ---------- 数据库 ----------
def conn():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side TEXT, entry REAL, stop REAL, target REAL,
        score REAL, trend TEXT, reason TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side TEXT, entry REAL, stop REAL, target REAL,
        exit_price REAL, exit_reason TEXT, leverage REAL,
        note TEXT, source TEXT DEFAULT 'manual', signal_id INTEGER,
        opened_at TEXT, closed_at TEXT)""")
    c.commit(); c.close()

# ---------- 行情 ----------
_result_cache = {}
def get_price(symbol="UNIUSDT"):
    now = time.time()
    if symbol in _result_cache and now - _result_cache[symbol][0] < 5:
        return _result_cache[symbol][1]
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            p = float(json.loads(r.read().decode())["price"])
            _result_cache[symbol] = (now, p)
            return p
    except Exception:
        return None

# ---------- 信号 ----------
def add_signal(d):
    c = conn()
    cur = c.execute("""INSERT INTO signals(symbol,side,entry,stop,target,score,trend,reason,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (d.get("symbol","UNIUSDT"), d.get("side",""), d.get("entry"), d.get("stop"),
         d.get("target"), d.get("score"), d.get("trend"), d.get("reason",""),
         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")))
    c.commit(); sid = cur.lastrowid; c.close()
    return sid

def list_signals(limit=50):
    c = conn()
    rows = c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    cols = [x[1] for x in c.execute("PRAGMA table_info(signals)").fetchall()]
    c.close()
    out = []
    for r in rows:
        t = dict(zip(cols, r))
        # 用当前价格算信号"至今"表现
        p = get_price(t["symbol"])
        if p and t["entry"]:
            chg = (p - t["entry"]) / t["entry"] * 100
            if t["side"] == "空": chg = -chg
            t["cur_price"] = p
            t["since_pct"] = round(chg, 2)
            t["status"] = "✅盈利" if chg > 0 else ("❌亏损" if chg < 0 else "持平")
        out.append(t)
    return out

# ---------- 交易 ----------
def add_trade(d):
    c = conn()
    cur = c.execute("""INSERT INTO trades(symbol,side,entry,stop,target,exit_price,exit_reason,
        leverage,note,source,signal_id,opened_at,closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get("symbol","UNIUSDT"), d.get("side",""), d.get("entry"), d.get("stop"),
         d.get("target"), d.get("exit_price"), d.get("exit_reason",""),
         d.get("leverage"), d.get("note",""), d.get("source","manual"),
         d.get("signal_id"), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), ""))
    c.commit(); tid = cur.lastrowid; c.close()
    return tid

def close_trade(tid, exit_price, reason):
    c = conn()
    c.execute("UPDATE trades SET exit_price=?, exit_reason=?, closed_at=? WHERE id=?",
              (exit_price, reason, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), tid))
    c.commit(); c.close()

def del_trade(tid):
    c = conn()
    c.execute("DELETE FROM trades WHERE id=?", (tid,))
    c.commit(); c.close()

def list_trades():
    c = conn()
    rows = c.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    cols = [x[1] for x in c.execute("PRAGMA table_info(trades)").fetchall()]
    c.close()
    out = []
    for r in rows:
        t = dict(zip(cols, r))
        entry = t.get("entry")
        if entry:
            ex = t.get("exit_price")
            p = ex if ex is not None else get_price(t.get("symbol","UNIUSDT"))
            if p:
                raw = (p - entry)/entry*100
                if t.get("side") == "空": raw = -raw
                lev = t.get("leverage") or 1
                t["cur_price"] = p
                t["raw_pct"] = round(raw, 2)
                t["pnl_pct"] = round(raw * lev, 2)
                t["closed"] = ex is not None
                # 判断是否触及止损/目标
                if not t["closed"]:
                    stop = t.get("stop"); tgt = t.get("target")
                    if stop and ((t["side"]=="多" and p<=stop) or (t["side"]=="空" and p>=stop)):
                        t["alert"] = "⚠️ 已触及止损价"
                    elif tgt and ((t["side"]=="多" and p>=tgt) or (t["side"]=="空" and p<=tgt)):
                        t["alert"] = "🎯 已触及目标价"
        out.append(t)
    return out

def stats():
    trades = list_trades()
    closed = [t for t in trades if t.get("closed")]
    wins = [t for t in closed if t.get("pnl_pct",0) > 0]
    losses = [t for t in closed if t.get("pnl_pct",0) < 0]
    n = len(closed)
    avg_w = sum(t["pnl_pct"] for t in wins)/len(wins) if wins else 0
    avg_l = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0
    total = sum(t.get("pnl_pct",0) for t in closed)
    return {
        "count": n, "win": len(wins), "loss": len(losses),
        "win_rate": round(len(wins)/n*100,1) if n else 0,
        "avg_win": round(avg_w,2), "avg_loss": round(avg_l,2),
        "rr": round(abs(avg_w/avg_l),2) if avg_l else 0,
        "total": round(total,2),
        "open_count": len([t for t in trades if not t.get("closed")]),
    }

def review():
    st = stats()
    tips = []
    sigs = list_signals(100)
    if st["count"] == 0:
        tips.append("还没有已平仓交易, 先记录几笔")
    else:
        tips.append(f"{'✅' if st['win_rate']>=50 else '⚠️'} 胜率 {st['win_rate']}%")
        if st["rr"] >= 2:
            tips.append(f"✅ 盈亏比 {st['rr']}（很好，即使胜率40%也赚）")
        elif st["rr"] > 0:
            tips.append(f"⚠️ 盈亏比 {st['rr']}（建议止损1.5%、目标3%+）")
        tips.append(f"{'📈' if st['total']>=0 else '📉'} 总收益 {st['total']}%")
    # 信号表现
    if sigs:
        with_pct = [s for s in sigs if s.get("since_pct") is not None]
        if with_pct:
            good = [s for s in with_pct if s["since_pct"] > 0]
            tips.append(f"📡 系统信号: {len(with_pct)}个, 至今{len(good)}个正确 ({len(good)/len(with_pct)*100:.0f}%)")
    return {"tips": tips, "stats": st, "signals": sigs, "trades": list_trades()}

# ---------- 实时信号分析(内置, 不依赖外部) ----------
def _fetch(url, retry=2, timeout=8):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(0.3)
    return None

def _sma(v, n):
    return sum(v[-n:])/n if len(v) >= n else None

def _rsi(c, p=14):
    if len(c) < p+1: return None
    g = l = 0.0
    for i in range(-p, 0):
        ch = c[i]-c[i-1]
        if ch >= 0: g += ch
        else: l += -ch
    if l == 0: return 100.0
    return 100-100/(1+(g/p)/(l/p))

def analyze_signal(symbol="UNIUSDT"):
    """内置信号分析: 返回 方向/入场/止损/目标/评分"""
    cur = _fetch(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
    if not cur: return {"error":"价格获取失败"}
    price = float(cur["price"])

    def kl(iv, lim=100):
        return _fetch(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={iv}&limit={lim}") or []

    c15 = [float(x[4]) for x in kl("15m")]
    c1h = [float(x[4]) for x in kl("1h")]
    c4h = [float(x[4]) for x in kl("4h")]
    c1d = [float(x[4]) for x in kl("1d")]

    score = 0.0; details = []
    # 趋势
    ma1 = _sma(c1h,20); ma4 = _sma(c4h,20); mad = _sma(c1d,20)
    trend = "range"; tdesc = "震荡"
    if ma1 and ma4 and mad:
        up = sum([price>ma1, price>ma4, price>mad, ma1>ma4])
        dn = sum([price<ma1, price<ma4, price<mad, ma1<ma4])
        if up >= 3: trend="up"; tdesc="上升"
        elif dn >= 3: trend="down"; tdesc="下降"

    for tf, c, wt in [("15m",c15,1.0),("1h",c1h,1.5),("4h",c4h,2.0),("1d",c1d,2.0)]:
        if not c: continue
        r = _rsi(c); ma = _sma(c,20)
        if r is not None:
            w = 0.5 if (r>=70 and trend=="up") else (0.5 if (r<=30 and trend=="down") else 1.0)
            if r>=80: score-=2*wt*w
            elif r>=70: score-=1*wt*w
            elif r<=20: score+=2*wt*w
            elif r<=30: score+=1*wt*w
        if ma:
            if price>ma: score+=1*wt
            else: score-=1*wt

    # 订单簿
    ob = _fetch(f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=500")
    wa = wb = None
    if ob:
        bids=[(float(p),float(q)) for p,q in ob["bids"]]
        asks=[(float(p),float(q)) for p,q in ob["asks"]]
        tb=sum(q for _,q in bids); ta=sum(q for _,q in asks)
        if ta>0:
            ratio=tb/ta
            if ratio>1.2: score+=2
            elif ratio<0.8: score-=2
        avg=(tb+ta)/max(1,len(bids)+len(asks)); wth=max(avg*4,1000)
        aa=[(p,q) for p,q in asks if 0.2<=(p-price)/price*100<=8 and q>=wth]
        bb=[(p,q) for p,q in bids if 0.2<=(price-p)/price*100<=8 and q>=wth]
        if aa:
            p,q=sorted(aa,key=lambda x:-x[1])[0]; wa={"price":round(p,3),"qty":round(q)}
        if bb:
            p,q=sorted(bb,key=lambda x:-x[1])[0]; wb={"price":round(p,3),"qty":round(q)}

    # 结论
    if score>=4: side, verdict = "多", "强烈偏多"
    elif score>=1.5: side, verdict = "多", "偏多"
    elif score<=-4: side, verdict = "空", "强烈偏空"
    elif score<=-1.5: side, verdict = "空", "偏空"
    else: side, verdict = "", "中性观望"

    # 操作位
    atr = price*0.02
    if len(c1h)>=15:
        trs=[abs(c1h[i]-c1h[i-1]) for i in range(-14,0)]
        atr=sum(trs)/len(trs) if trs else atr
    stop = target = None
    if side=="多":
        stop = round(price-max(atr*1.2, price*0.012), 3)
        target = round(wa["price"] if (wa and wa["price"]>price+atr*0.8) else price+atr*2, 3)
    elif side=="空":
        stop = round(price+max(atr*1.2, price*0.012), 3)
        target = round(wb["price"] if (wb and wb["price"]<price-atr*0.8) else price-atr*2, 3)

    return {"symbol":symbol,"price":price,"side":side,"verdict":verdict,
            "score":round(score,1),"trend":tdesc,"entry":round(price,3),
            "stop":stop,"target":target,"wall_ask":wa,"wall_bid":wb,
            "atr":round(atr,4)}


# ---------- 自动抓取信号(定时任务) ----------
import threading

THRESHOLD = 3.0    # 评分阈值(≥此值才自动记录)
CHECK_INTERVAL = 30*60   # 检查间隔(秒), 30分钟
_last_auto = {"time": None, "score": None}

def auto_grab_once():
    """执行一次自动抓取, 返回结果"""
    try:
        r = analyze_signal("UNIUSDT")
        if r.get("error"):
            return {"ok":False, "msg":r["error"]}
        score = abs(r.get("score",0))
        side = r.get("side","")
        if not side:
            return {"ok":True, "grabbed":False, "msg":f"中性({r['score']}), 不记录"}
        if score < THRESHOLD:
            return {"ok":True, "grabbed":False, "msg":f"评分{r['score']} < {THRESHOLD}, 不记录"}
        # 避免重复记录(同一信号2小时内不重复)
        c = conn()
        row = c.execute("""SELECT id FROM signals WHERE symbol=? AND side=? AND score=?
            AND created_at > datetime('now','-2 hours')""",
            (r["symbol"], side, r["score"])).fetchone()
        c.close()
        if row:
            return {"ok":True, "grabbed":False, "msg":"2小时内已有相同信号, 跳过"}
        # 记录
        sid = add_signal({"symbol":r["symbol"],"side":side,"entry":r["entry"],
            "stop":r["stop"],"target":r["target"],"score":r["score"],
            "trend":r["trend"],"reason":"自动抓取: "+r["verdict"]})
        _last_auto["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        _last_auto["score"] = score
        return {"ok":True, "grabbed":True, "id":sid,
                "msg":f"已记录#{sid}: {side} 入场{r['entry']} 止损{r['stop']} 目标{r['target']}"}
    except Exception as e:
        return {"ok":False, "msg":str(e)}

def auto_worker():
    """后台线程: 定时执行"""
    while True:
        try:
            res = auto_grab_once()
            print(f"[自动抓取] {datetime.now(timezone.utc).strftime('%H:%M')} {res.get('msg')}", flush=True)
        except Exception as e:
            print(f"[自动抓取] 异常: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

def start_auto_worker():
    t = threading.Thread(target=auto_worker, daemon=True)
    t.start()
    return t


init_db()

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>交易双轨记录</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#0f1220;color:#e8ebf5;padding:16px}
.w{max-width:860px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}
.sub{color:#8b93ad;font-size:12px;margin-bottom:16px}
.card{background:linear-gradient(180deg,#1a1f35,#232a45);border:1px solid #2e3555;
border-radius:14px;padding:18px;margin-bottom:14px}
.card h2{font-size:15px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
input,select{flex:1;min-width:85px;padding:9px;border-radius:8px;border:1px solid #2e3555;
background:#12162b;color:#e8ebf5;font-size:14px}
button{padding:9px 14px;border:none;border-radius:8px;background:#5b7cfa;color:#fff;
font-weight:600;cursor:pointer;font-size:13px}
button.green{background:#2a9d5c}button.danger{background:#ff5f7a}
button.small{padding:4px 9px;font-size:11px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:9px}
.st{background:#12162b;border:1px solid #2e3555;border-radius:9px;padding:11px}
.st .l{color:#8b93ad;font-size:11px}
.st .v{font-size:18px;font-weight:800;margin-top:3px}
.g{color:#3ddc84}.r{color:#ff5f7a}.y{color:#ffb84d}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
th,td{padding:7px 5px;text-align:left;border-bottom:1px solid #232a45}
th{color:#8b93ad;font-size:11px}
.tips li{padding:6px 0;font-size:13px;color:#c3c9dc;border-bottom:1px solid #232a45;list-style:none}
.tips li:last-child{border:none}
.note{color:#8b93ad;font-size:11px;margin-top:10px;line-height:1.7}
.badge{padding:2px 7px;border-radius:5px;font-size:10px}
.b-sig{background:#2a3a7a;color:#8faeff}
.b-man{background:#3a3a3a;color:#aaa}
</style></head><body><div class="w">
<h1>📊 交易双轨记录</h1>
<div class="sub">轨道1: 系统信号自动记录 ｜ 轨道2: 你的实际交易</div>

<div class="card"><h2>📈 数据统计</h2><div class="stats" id="stats"></div></div>
<div class="card"><h2>💡 复盘建议</h2><ul class="tips" id="tips"></ul></div>

<div class="card">
  <h2>📡 系统信号（自动记录）<button class="small" onclick="grabSignal()">📥 抓取当前信号</button></h2>
  <table><thead><tr><th>时间</th><th>币</th><th>方向</th><th>入场</th><th>止损</th><th>目标</th><th>现价</th><th>表现</th><th>用</th></tr></thead>
  <tbody id="siglist"></tbody></table>
  <div class="note">点"用"→ 自动填入下方交易记录</div>
</div>

<div class="card">
  <h2>➕ 记录实际交易</h2>
  <div class="row">
    <select id="symbol"><option>UNIUSDT</option><option>BTCUSDT</option><option>ETHUSDT</option></select>
    <select id="side"><option value="多">做多</option><option value="空">做空</option></select>
    <input id="leverage" type="number" placeholder="杠杆" value="5">
  </div>
  <div class="row">
    <input id="entry" type="number" step="any" placeholder="入场价">
    <input id="stop" type="number" step="any" placeholder="止损价">
    <input id="target" type="number" step="any" placeholder="目标价">
  </div>
  <div class="row">
    <input id="note" placeholder="备注" style="min-width:160px">
    <button onclick="addTrade()">记录</button>
    <button class="green" onclick="fillCurrent()">用现价填入</button>
  </div>
</div>

<div class="card">
  <h2>📋 实际交易记录</h2>
  <table><thead><tr><th>ID</th><th>来源</th><th>币/向</th><th>杠杆</th><th>入场</th><th>现价</th><th>盈亏</th><th>状态</th><th>操作</th></tr></thead>
  <tbody id="tradelist"></tbody></table>
</div>
<div class="note">⚠️ 记录工具，不构成投资建议。数据存本地，环境重置会丢，请截图备份。</div>
</div>
<script>
let curSignals=[];
async function load(){
  const r = await fetch('/api/all').then(x=>x.json());
  const s = r.stats;
  document.getElementById('stats').innerHTML =
    `<div class="st"><div class="l">已平仓</div><div class="v">${s.count}</div></div>
     <div class="st"><div class="l">胜率</div><div class="v ${s.win_rate>=50?'g':'r'}">${s.win_rate}%</div></div>
     <div class="st"><div class="l">盈亏比</div><div class="v ${s.rr>=2?'g':'r'}">${s.rr}</div></div>
     <div class="st"><div class="l">总收益</div><div class="v ${s.total>=0?'g':'r'}">${s.total}%</div></div>
     <div class="st"><div class="l">持仓中</div><div class="v">${s.open_count}</div></div>`;
  document.getElementById('tips').innerHTML = r.tips.map(t=>`<li>${t}</li>`).join('');
  curSignals = r.signals||[];
  document.getElementById('siglist').innerHTML = curSignals.length ? curSignals.map((g,i)=>{
    const pc = g.since_pct;
    const cls = pc===undefined?'':(pc>0?'g':'r');
    return `<tr><td>${(g.created_at||'').slice(5)}</td><td>${g.symbol}</td><td>${g.side}</td>
      <td>${g.entry||'-'}</td><td>${g.stop||'-'}</td><td>${g.target||'-'}</td>
      <td>${g.cur_price||'-'}</td><td class="${cls}">${pc!==undefined?(pc>0?'+':'')+pc+'%':'-'}</td>
      <td><button class="small" onclick="useSignal(${i})">用</button></td></tr>`;
  }).join('') : '<tr><td colspan="9" style="color:#8b93ad;text-align:center">暂无信号，点上方抓取</td></tr>';
  document.getElementById('tradelist').innerHTML = (r.trades||[]).length ? r.trades.map(t=>{
    const p = t.pnl_pct;
    const cls = p===undefined?'':(p>=0?'g':'r');
    const src = t.source==='signal'?'<span class="badge b-sig">信号</span>':'<span class="badge b-man">手动</span>';
    const status = t.closed ? '<span class="g">已平</span>' : (t.alert||'<span class="y">持仓</span>');
    return `<tr><td>${t.id}</td><td>${src}</td><td>${t.symbol.replace('USDT','')}/${t.side}</td>
      <td>${t.leverage||'-'}x</td><td>${t.entry}</td><td>${t.cur_price||'-'}</td>
      <td class="${cls}">${p!==undefined?(p>0?'+':'')+p+'%':'-'}</td>
      <td>${status}</td>
      <td>${t.closed?'':`<button class="small danger" onclick="delTrade(${t.id})">平/删</button>`}</td></tr>`;
  }).join('') : '<tr><td colspan="9" style="color:#8b93ad;text-align:center">暂无交易</td></tr>';
}
function useSignal(i){
  const g=curSignals[i]; if(!g) return;
  document.getElementById('symbol').value=g.symbol||'UNIUSDT';
  document.getElementById('side').value=g.side||'多';
  document.getElementById('entry').value=g.entry||'';
  document.getElementById('stop').value=g.stop||'';
  document.getElementById('target').value=g.target||'';
  document.getElementById('note').value='来自信号#'+g.id;
  window._signalId = g.id;
  alert('已填入, 检查后点"记录"');
}
async function grabSignal(){
  const btn = event.target; btn.textContent='分析中...';
  try{
    const sym = document.getElementById('symbol').value;
    const r = await fetch('/api/signal/analyze?symbol='+sym).then(x=>x.json());
    if(r.error){alert(r.error);btn.textContent='📥 抓取当前信号';return;}
    if(!r.side){alert('当前信号: '+r.verdict+'（中性，无明确方向）');btn.textContent='📥 抓取当前信号';return;}
    await fetch('/api/signal/add',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbol:r.symbol,side:r.side,entry:r.entry,stop:r.stop,target:r.target,
        score:r.score,trend:r.trend,reason:'自动分析: '+r.verdict})});
    alert('✅ 已抓取信号\n方向:'+r.side+'\n入场:'+r.entry+'\n止损:'+r.stop+'\n目标:'+r.target+'\n评分:'+r.score);
  }catch(e){alert('失败:'+e.message);}
  btn.textContent='📥 抓取当前信号';
  load();
}
async function addTrade(){
  const body={symbol:document.getElementById('symbol').value,
    side:document.getElementById('side').value,
    entry:parseFloat(document.getElementById('entry').value),
    stop:parseFloat(document.getElementById('stop').value)||null,
    target:parseFloat(document.getElementById('target').value)||null,
    leverage:parseFloat(document.getElementById('leverage').value)||5,
    note:document.getElementById('note').value,
    source: window._signalId?'signal':'manual',
    signal_id: window._signalId||null};
  if(!body.entry){alert('请填入场价');return;}
  await fetch('/api/trade/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  window._signalId=null;
  ['entry','stop','target','note'].forEach(id=>document.getElementById(id).value='');
  load();
}
async function fillCurrent(){
  const r = await fetch('/api/price?symbol='+document.getElementById('symbol').value).then(x=>x.json());
  if(r.price){document.getElementById('entry').value=r.price;}
}
async function delTrade(id){
  const p = prompt('平仓价(留空=删除记录):');
  if(p===null) return;
  if(p===''){ if(confirm('删除这条记录?')){await fetch('/api/trade/del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}); load();} return; }
  await fetch('/api/trade/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,exit_price:parseFloat(p),reason:'手动平仓'})});
  load();
}
load(); setInterval(load, 15000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def _s(self, body, ct="text/html; charset=utf-8", code=200):
        try:
            b = body.encode() if isinstance(body,str) else body
            self.send_response(code); self.send_header("Content-Type",ct)
            self.send_header("Content-Length",str(len(b))); self.send_header("Cache-Control","no-store")
            self.end_headers(); self.wfile.write(b)
        except Exception: pass
    def do_GET(self):
        try:
            p = self.path
            if p.startswith("/api/all"):
                self._s(json.dumps(review(), ensure_ascii=False), "application/json; charset=utf-8")
            elif p.startswith("/api/auto/run"):
                res = auto_grab_once()
                self._s(json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            elif p.startswith("/api/signal/analyze"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(p).query)
                sym = q.get("symbol",["UNIUSDT"])[0]
                self._s(json.dumps(analyze_signal(sym), ensure_ascii=False), "application/json; charset=utf-8")
            elif p.startswith("/api/price"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(p).query)
                sym = q.get("symbol",["UNIUSDT"])[0]
                self._s(json.dumps({"price": get_price(sym)}), "application/json")
            else:
                self._s(PAGE)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._s(json.dumps({"error":str(e)}), "application/json; charset=utf-8", 500)
    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length",0))
            body = json.loads(self.rfile.read(ln).decode()) if ln else {}
            if self.path.startswith("/api/signal/add"):
                add_signal(body); self._s(json.dumps({"ok":True}),"application/json")
            elif self.path.startswith("/api/trade/add"):
                add_trade(body); self._s(json.dumps({"ok":True}),"application/json")
            elif self.path.startswith("/api/trade/close"):
                close_trade(body["id"], body["exit_price"], body.get("reason","")); self._s(json.dumps({"ok":True}),"application/json")
            elif self.path.startswith("/api/trade/del"):
                del_trade(body["id"]); self._s(json.dumps({"ok":True}),"application/json")
            else:
                self._s(json.dumps({"ok":False}),"application/json",404)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._s(json.dumps({"error":str(e)}),"application/json; charset=utf-8",500)
    def log_message(self,*a): pass

if __name__=="__main__":
    print("="*48)
    print("  交易双轨记录工具")
    print("="*48)
    print(f"  浏览器打开: http://localhost:{PORT}")
    print("  Ctrl+C 停止")
    print("="*48)
    print(f"  自动抓取: 每{CHECK_INTERVAL//60}分钟, 阈值{THRESHOLD}分")
    start_auto_worker()
    ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
